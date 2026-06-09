import os
import os.path
import time
import json
import hashlib
import urllib.parse
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List

from visidata import vd, VisiData, Path, modtime, asyncthread, Progress, BaseSheet


vd.option('cache_default_days', 1, 'default cache expiry in days', replay=True)
vd.option('cache_enabled', True, 'enable remote data caching', replay=True)
vd.option('cache_offline', False, 'offline mode: use cache only, do not make network requests', replay=True)


@dataclass
class CacheEntry:
    '''Metadata for a single cached remote resource.'''
    url: str
    local_path: str
    source_type: str = 'http'
    size: int = 0
    mtime: float = 0
    etag: str = ''
    last_modified: str = ''
    content_type: str = ''
    cache_policy: str = 'days:1'
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'CacheEntry':
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def is_expired(self) -> bool:
        if self.cache_policy == 'never':
            return False
        if self.cache_policy.startswith('days:'):
            days = float(self.cache_policy.split(':', 1)[1])
            return (time.time() - self.mtime) > days * 24 * 60 * 60
        if self.cache_policy == 'etag' or self.cache_policy == 'last-modified':
            return False
        return False

    def touch(self) -> None:
        self.last_accessed = time.time()


class CacheManager:
    '''Unified manager for remote resource caching.

    Maintains a persistent index of all cached resources with full metadata.
    '''

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.index_path = self.cache_dir / '_cache_index.json'
        self._entries: Dict[str, CacheEntry] = {}
        self._loaded = False

    def _ensure_dir(self) -> None:
        os.makedirs(self.cache_dir, exist_ok=True)

    def _load(self) -> None:
        if self._loaded:
            return
        self._ensure_dir()
        if self.index_path.exists():
            try:
                with open(self.index_path, 'r', encoding='utf-8') as fp:
                    data = json.load(fp)
                self._entries = {url: CacheEntry.from_dict(d) for url, d in data.items()}
            except Exception as e:
                vd.warning(f'corrupt cache index, resetting: {e}')
                self._entries = {}
        self._loaded = True

    def _save(self) -> None:
        self._ensure_dir()
        data = {url: entry.to_dict() for url, entry in self._entries.items()}
        tmp = self.index_path.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as fp:
            json.dump(data, fp, indent=2, default=str)
        os.replace(tmp, self.index_path)

    def _cache_path_for(self, url: str) -> Path:
        parsed = urllib.parse.urlparse(url)
        safe_name = urllib.parse.quote(url, safe='')
        if len(safe_name) > 180:
            safe_name = hashlib.sha256(url.encode('utf-8')).hexdigest()
            ext = os.path.splitext(parsed.path)[1]
            if ext:
                safe_name += ext
        return self.cache_dir / safe_name

    def _detect_source_type(self, url: str) -> str:
        scheme = urllib.parse.urlparse(url).scheme.lower()
        if scheme in ('http', 'https'):
            return 'http'
        if scheme in ('s3',):
            return 's3'
        if scheme:
            return scheme
        return 'other'

    def get(self, url: str) -> Optional[CacheEntry]:
        self._load()
        entry = self._entries.get(url)
        if entry:
            entry.touch()
            self._save()
            local = Path(entry.local_path)
            if not local.exists():
                del self._entries[url]
                self._save()
                return None
            entry.size = local.stat().st_size
            entry.mtime = modtime(local)
        return entry

    def put(self, url: str, local_path: Path, *,
            source_type: str = '',
            etag: str = '',
            last_modified: str = '',
            content_type: str = '',
            cache_policy: str = '',
            extra: Optional[Dict[str, Any]] = None) -> CacheEntry:
        self._load()
        local = Path(local_path)
        st = local.stat() if local.exists() else None
        entry = CacheEntry(
            url=url,
            local_path=str(local),
            source_type=source_type or self._detect_source_type(url),
            size=st.st_size if st else 0,
            mtime=st.st_mtime if st else time.time(),
            etag=etag,
            last_modified=last_modified,
            content_type=content_type,
            cache_policy=cache_policy or f'days:{vd.options.cache_default_days}',
            extra=extra or {},
        )
        self._entries[url] = entry
        self._save()
        return entry

    def remove(self, url: str) -> bool:
        self._load()
        entry = self._entries.pop(url, None)
        if entry:
            local = Path(entry.local_path)
            if local.exists():
                try:
                    local.unlink()
                except Exception as e:
                    vd.warning(f'cannot remove cache file {local}: {e}')
            self._save()
            return True
        return False

    def clear_all(self) -> int:
        self._load()
        count = len(self._entries)
        for entry in list(self._entries.values()):
            local = Path(entry.local_path)
            if local.exists():
                try:
                    local.unlink()
                except Exception:
                    pass
        self._entries.clear()
        self._save()
        return count

    def list(self) -> List[CacheEntry]:
        self._load()
        return list(self._entries.values())

    def is_valid(self, url: str) -> bool:
        entry = self.get(url)
        if not entry:
            return False
        return not entry.is_expired()

    def refresh_policy(self, url: str, policy: str) -> None:
        self._load()
        entry = self._entries.get(url)
        if entry:
            entry.cache_policy = policy
            self._save()


@VisiData.cached_property
def cache_manager(vd):
    return CacheManager(vd.cache_dir)


@VisiData.global_api
def urlcache(vd, url, days=1, text=True, headers=None):
    '''Return Path object to local cache of url contents.

    Legacy API preserved for backward compatibility.  New code should use
    `vd.cache_manager` for more control.
    '''
    from urllib.request import Request, urlopen

    policy = f'days:{days}'
    entry = vd.cache_manager.get(url)
    if entry and not entry.is_expired():
        return Path(entry.local_path)

    os.makedirs(vd.cache_dir, exist_ok=True)

    p = vd.cache_manager._cache_path_for(url)

    req = Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    etag = ''
    last_modified = ''
    content_type = ''

    with urlopen(req) as fp:
        ret = fp.read()
        etag = fp.headers.get('ETag', '') or ''
        last_modified = fp.headers.get('Last-Modified', '') or ''
        content_type = fp.headers.get('Content-Type', '') or ''
        if text:
            ret = ret.decode('utf-8').strip()
            with p.open(mode='w', encoding='utf-8') as fpout:
                fpout.write(ret)
        else:
            with p.open_bytes(mode='w') as fpout:
                fpout.write(ret)

    vd.cache_manager.put(
        url, p,
        source_type='http',
        etag=etag,
        last_modified=last_modified,
        content_type=content_type,
        cache_policy=policy,
    )

    return p


@VisiData.api
def enable_requests_cache(vd):
    try:
        import requests
        import requests_cache

        requests_cache.install_cache(str(Path(os.path.join(vd.options.visidata_dir, 'httpcache'))), backend='sqlite', expire_after=24*60*60)
    except ModuleNotFoundError:
        vd.warning('install requests_cache for less intrusive scraping')


@VisiData.api
def cache_open_http(vd, url: str, *, headers=None, cache_policy: str = '') -> Path:
    '''Fetch URL via HTTP, cache locally, return Path to cached file.

    Uses conditional requests via ETag/Last-Modified when available.
    Respects cache_offline and cache_enabled options.
    '''
    from urllib.request import Request, urlopen

    entry = vd.cache_manager.get(url)

    if vd.options.cache_offline:
        if entry:
            vd.status(f'offline: using cached {url}')
            p = Path(entry.local_path)
            p._cache_hit = True
            p._from_cache = True
            if entry.etag or entry.last_modified:
                p._http_headers = {'ETag': entry.etag, 'Last-Modified': entry.last_modified}
            return p
        vd.fail(f'offline: no cache available for {url}')

    if not vd.options.cache_enabled:
        p = vd.cache_manager._cache_path_for(url)
        req = Request(url)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urlopen(req) as fp:
            data = fp.read()
        with p.open_bytes(mode='w') as fpout:
            fpout.write(data)
        p._http_headers = {h: v for h, v in fp.headers.items()} if hasattr(fp, 'headers') else {}
        return p

    p = Path(entry.local_path) if entry else vd.cache_manager._cache_path_for(url)

    req = Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    if entry and entry.etag:
        req.add_header('If-None-Match', entry.etag)
    if entry and entry.last_modified:
        req.add_header('If-Modified-Since', entry.last_modified)

    try:
        resp = urlopen(req)
    except Exception as e:
        if entry:
            vd.status(f'using cached {url} ({e})')
            p._cache_hit = True
            p._from_cache = True
            return p
        raise

    if resp.status == 304 and entry:
        vd.debug(f'cache hit (304) for {url}')
        entry.touch()
        vd.cache_manager._save()
        p._cache_hit = True
        p._from_cache = True
        return p

    data = resp.read()
    with p.open_bytes(mode='w') as fpout:
        fpout.write(data)

    etag = resp.headers.get('ETag', '') or ''
    last_modified = resp.headers.get('Last-Modified', '') or ''
    content_type = resp.headers.get('Content-Type', '') or ''

    policy = cache_policy or (
        'etag' if etag else ('last-modified' if last_modified else f'days:{vd.options.cache_default_days}')
    )

    vd.cache_manager.put(
        url, p,
        source_type='http',
        etag=etag,
        last_modified=last_modified,
        content_type=content_type,
        cache_policy=policy,
    )

    p._cache_hit = False
    p._from_cache = False
    p._http_headers = {h: v for h, v in resp.headers.items()}
    return p


@VisiData.api
def cache_open_s3(vd, url: str, *, version_id=None, cache_policy: str = '') -> Path:
    '''Fetch S3 object, cache locally, return Path to cached file.'''
    s3fs_core = vd.importExternal('s3fs.core', 's3fs')

    entry = vd.cache_manager.get(url)

    if vd.options.cache_offline:
        if entry:
            vd.status(f'offline: using cached {url}')
            p = Path(entry.local_path)
            p._cache_hit = True
            p._from_cache = True
            return p
        vd.fail(f'offline: no cache available for {url}')

    s3fs = s3fs_core.S3FileSystem(
        client_kwargs={'endpoint_url': vd.options.s3_endpoint or None},
        anon=vd.options.s3_anon,
    )

    if not vd.options.cache_enabled:
        p = vd.cache_manager._cache_path_for(url)
        with s3fs.open(url, mode='rb', version_id=version_id) as src:
            data = src.read()
        with p.open_bytes(mode='w') as fpout:
            fpout.write(data)
        return p

    p = Path(entry.local_path) if entry else vd.cache_manager._cache_path_for(url)

    info = s3fs.info(url, version_id=version_id)
    remote_mtime = info.get('LastModified', 0)
    if hasattr(remote_mtime, 'timestamp'):
        remote_mtime = remote_mtime.timestamp()

    need_download = True
    if entry and entry.mtime and remote_mtime:
        if entry.mtime >= remote_mtime:
            need_download = False
            entry.touch()
            vd.cache_manager._save()
            vd.debug(f'cache hit for {url}')
            p._cache_hit = True
            p._from_cache = True

    if need_download:
        vd.status(f'downloading {url}')
        with s3fs.open(url, mode='rb', version_id=version_id) as src:
            data = src.read()
        with p.open_bytes(mode='w') as fpout:
            fpout.write(data)

        policy = cache_policy or 'last-modified'
        vd.cache_manager.put(
            url, p,
            source_type='s3',
            last_modified=str(remote_mtime),
            content_type=info.get('ContentType', ''),
            cache_policy=policy,
            extra={'version_id': version_id, 'etag': info.get('ETag', '')},
        )
        p._cache_hit = False
        p._from_cache = False

    return p


BaseSheet.addCommand(
    '',
    'cache-toggle',
    "options.cache_enabled = not options.cache_enabled; status('caching ' + ('enabled' if options.cache_enabled else 'disabled'))",
    'toggle remote data caching on/off',
)

BaseSheet.addCommand(
    '',
    'cache-toggle-offline',
    "options.cache_offline = not options.cache_offline; status('offline mode ' + ('enabled' if options.cache_offline else 'disabled'))",
    'toggle offline mode (use cache only / allow network)',
)

BaseSheet.addCommand(
    '',
    'cache-clear-current',
    "src = sheet.source\nif hasattr(src, 'is_url') and src.is_url():\n    if vd.cache_manager.remove(src.given):\n        vd.status('cache cleared for ' + src.given)\n    else:\n        vd.status('no cache entry for ' + src.given)\nelse:\n    vd.fail('current sheet source is not a remote URL')",
    'clear cache for the current sheet source',
)

vd.addMenuItems('''
    File > Cache > Open cache sheet > open-cache
    File > Cache > Toggle caching > cache-toggle
    File > Cache > Toggle offline mode > cache-toggle-offline
    File > Cache > Clear current source > cache-clear-current
    File > Cache > Refresh current source > cache-refresh-source
''')


vd.addGlobals({'urlcache': urlcache, 'CacheEntry': CacheEntry, 'CacheManager': CacheManager})
