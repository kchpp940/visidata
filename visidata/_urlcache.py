import os
import os.path
import time
import json
import hashlib
import urllib.parse
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List, Callable

from visidata import vd, VisiData, Path, modtime, asyncthread, Progress, BaseSheet


vd.option('cache_default_days', 1, 'default cache expiry in days', replay=True)
vd.option('cache_enabled', True, 'enable remote data caching', replay=True)
vd.option('cache_offline', False, 'offline mode: use cache only, do not make network requests', replay=True)


_CACHE_STATUS_OK = 'ok'
_CACHE_STATUS_MISSING = 'missing'
_CACHE_STATUS_STALE = 'stale'
_CACHE_STATUS_ERROR = 'error'


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
    status: str = _CACHE_STATUS_OK
    status_msg: str = ''
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'CacheEntry':
        valid_fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in valid_fields})

    def is_expired(self) -> bool:
        if self.status == _CACHE_STATUS_MISSING:
            return True
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
    All cache operations (refresh, remove, open, verify) MUST go through this
    manager -- individual loaders MUST NOT maintain their own cache state.
    '''

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.index_path = self.cache_dir / '_cache_index.json'
        self._entries: Dict[str, CacheEntry] = {}
        self._loaded = False
        self._source_handlers: Dict[str, Callable] = {}

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

    def register_source_handler(self, source_type: str, refresher: Callable) -> None:
        '''Register a callable refresher(url: str) -> Path for a given source_type.

        The refresher must download (or re-download) the resource and return
        the Path to the local cached file.  The manager will handle all metadata
        bookkeeping.
        '''
        self._source_handlers[source_type] = refresher

    def sync_entry(self, entry: CacheEntry) -> bool:
        '''Re-sync entry metadata from actual file on disk.

        Returns True if the local file is present and intact.
        Updates entry.size, entry.mtime, entry.status, entry.status_msg in-place.
        '''
        local = Path(entry.local_path)
        if not local.exists():
            entry.status = _CACHE_STATUS_MISSING
            entry.status_msg = 'local file missing'
            entry.size = 0
            entry.mtime = 0
            return False
        try:
            st = local.stat()
            entry.size = st.st_size
            entry.mtime = modtime(local)
            if entry.is_expired():
                entry.status = _CACHE_STATUS_STALE
                entry.status_msg = f'policy {entry.cache_policy}'
            else:
                entry.status = _CACHE_STATUS_OK
                entry.status_msg = ''
            return True
        except Exception as e:
            entry.status = _CACHE_STATUS_ERROR
            entry.status_msg = str(e)
            return False

    def get(self, url: str) -> Optional[CacheEntry]:
        '''Look up an entry by URL.  Syncs metadata from disk and touches access time.'''
        self._load()
        entry = self._entries.get(url)
        if not entry:
            return None
        self.sync_entry(entry)
        if entry.status == _CACHE_STATUS_MISSING:
            del self._entries[url]
            self._save()
            return None
        entry.touch()
        self._save()
        return entry

    def put(self, url: str, local_path: Path, *,
            source_type: str = '',
            etag: str = '',
            last_modified: str = '',
            content_type: str = '',
            cache_policy: str = '',
            status: str = _CACHE_STATUS_OK,
            status_msg: str = '',
            extra: Optional[Dict[str, Any]] = None) -> CacheEntry:
        '''Store (or update) a cache entry.  Syncs size/mtime from the actual file.'''
        self._load()
        local = Path(local_path)
        entry = CacheEntry(
            url=url,
            local_path=str(local),
            source_type=source_type or self._detect_source_type(url),
            etag=etag,
            last_modified=last_modified,
            content_type=content_type,
            cache_policy=cache_policy or f'days:{vd.options.cache_default_days}',
            status=status,
            status_msg=status_msg,
            extra=extra or {},
        )
        self.sync_entry(entry)
        self._entries[url] = entry
        self._save()
        return entry

    def remove(self, url: str) -> bool:
        '''Remove a cache entry and its local file.  Returns True if something was removed.'''
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
        '''Remove ALL cache entries and files.'''
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

    def list(self, verify: bool = True) -> List[CacheEntry]:
        '''Return all cache entries.  If verify=True, sync each entry from disk first.'''
        self._load()
        if verify:
            removed = []
            for url, entry in self._entries.items():
                self.sync_entry(entry)
                if entry.status == _CACHE_STATUS_MISSING:
                    removed.append(url)
            for url in removed:
                del self._entries[url]
            if removed:
                self._save()
        return list(self._entries.values())

    def verify_all(self) -> Dict[str, int]:
        '''Verify every entry against the real filesystem.

        Drops entries with missing files.  Returns counts per status.
        '''
        self._load()
        counts: Dict[str, int] = {_CACHE_STATUS_OK: 0, _CACHE_STATUS_MISSING: 0,
                                  _CACHE_STATUS_STALE: 0, _CACHE_STATUS_ERROR: 0}
        removed = []
        for url, entry in self._entries.items():
            self.sync_entry(entry)
            counts[entry.status] = counts.get(entry.status, 0) + 1
            if entry.status == _CACHE_STATUS_MISSING:
                removed.append(url)
        for url in removed:
            del self._entries[url]
        if removed:
            self._save()
        return counts

    def is_valid(self, url: str) -> bool:
        entry = self.get(url)
        if not entry:
            return False
        return entry.status == _CACHE_STATUS_OK

    def refresh_policy(self, url: str, policy: str) -> Optional[CacheEntry]:
        self._load()
        entry = self._entries.get(url)
        if entry:
            entry.cache_policy = policy
            self.sync_entry(entry)
            self._save()
        return entry

    def open_local(self, url: str) -> Path:
        '''Return a validated Path to the locally cached file.

        Fails if the file is missing or the cache entry cannot be found.
        This is the ONLY supported way to do "offline open".
        '''
        entry = self.get(url)
        if not entry:
            vd.fail(f'no cache entry for {url}')
        local = Path(entry.local_path)
        if not local.exists():
            self.remove(url)
            vd.fail(f'cache file missing: {entry.local_path}')
        entry.touch()
        self._save()
        return local

    def refresh(self, url: str, force: bool = False) -> Optional[CacheEntry]:
        '''Refresh (re-download) a single cache entry.

        Dispatches to the registered source handler based on entry.source_type.
        If force=True the entry is removed first; otherwise the handler decides.
        Returns the updated CacheEntry or None on failure.
        '''
        self._load()
        entry = self._entries.get(url)
        source_type = entry.source_type if entry else self._detect_source_type(url)

        if force:
            self.remove(url)

        handler = self._source_handlers.get(source_type)
        if not handler:
            vd.warning(f'no cache refresh handler registered for source type: {source_type}')
            return None

        try:
            handler(url)
        except Exception as e:
            vd.exceptionCaught(e)
            if entry:
                entry.status = _CACHE_STATUS_ERROR
                entry.status_msg = str(e)
                self._save()
            return None

        return self.get(url)


@VisiData.cached_property
def cache_manager(vd):
    return CacheManager(vd.cache_dir)


@VisiData.global_api
def urlcache(vd, url, days=1, text=True, headers=None):
    '''Return Path object to local cache of url contents.

    Legacy API preserved for backward compatibility.  New code should use
    `vd.cache_manager` for more control.
    '''
    policy = f'days:{days}'
    entry = vd.cache_manager.get(url)
    if entry and not entry.is_expired():
        return Path(entry.local_path)

    from urllib.request import Request, urlopen
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


def _do_cache_open(vd, url, source_type, *, fetcher, cache_policy='', etag='', last_modified='', content_type='', extra=None):
    '''Core helper for all cache_open_* entry points.

    fetcher() -> bytes  -- performs the actual download and returns raw bytes.
    This helper handles: cache lookup, offline mode, cache_enabled gate,
    writing to disk, storing metadata via CacheManager.put().
    Returns a Path to the cached local file.
    '''
    entry = vd.cache_manager.get(url)

    if vd.options.cache_offline:
        local = vd.cache_manager.open_local(url)
        vd.status(f'offline: using cached {url}')
        local._cache_hit = True
        local._from_cache = True
        if entry and (entry.etag or entry.last_modified):
            local._http_headers = {'ETag': entry.etag, 'Last-Modified': entry.last_modified}
        return local

    if not vd.options.cache_enabled:
        p = vd.cache_manager._cache_path_for(url)
        data = fetcher(entry=None, conditional=False)
        with p.open_bytes(mode='w') as fpout:
            fpout.write(data)
        return p

    p = Path(entry.local_path) if entry else vd.cache_manager._cache_path_for(url)

    try:
        data, meta = fetcher(entry=entry, conditional=bool(entry))
    except Exception as e:
        if entry:
            vd.status(f'using cached {url} ({e})')
            p._cache_hit = True
            p._from_cache = True
            return p
        raise

    if meta.get('not_modified', False) and entry:
        vd.debug(f'cache hit (304) for {url}')
        entry.touch()
        vd.cache_manager._save()
        p._cache_hit = True
        p._from_cache = True
        return p

    with p.open_bytes(mode='w') as fpout:
        fpout.write(data)

    policy = cache_policy or meta.get('cache_policy') or (
        'etag' if meta.get('etag') else ('last-modified' if meta.get('last_modified') else f'days:{vd.options.cache_default_days}')
    )

    vd.cache_manager.put(
        url, p,
        source_type=source_type,
        etag=meta.get('etag', etag) or '',
        last_modified=meta.get('last_modified', last_modified) or '',
        content_type=meta.get('content_type', content_type) or '',
        cache_policy=policy,
        extra=extra or meta.get('extra') or {},
    )

    p._cache_hit = False
    p._from_cache = False
    if 'response_headers' in meta:
        p._http_headers = meta['response_headers']
    return p


@VisiData.api
def cache_open_http(vd, url: str, *, headers=None, cache_policy: str = '') -> Path:
    '''Fetch URL via HTTP, cache locally, return Path to cached file.

    Uses conditional requests via ETag/Last-Modified when available.
    ALL HTTP caching goes through this function -- the HTTP loader must not
    maintain any separate cache state.
    '''
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError

    def _http_fetcher(entry=None, conditional=False):
        req = Request(url)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if conditional and entry:
            if entry.etag:
                req.add_header('If-None-Match', entry.etag)
            if entry.last_modified:
                req.add_header('If-Modified-Since', entry.last_modified)

        resp = urlopen(req)
        meta = {}
        if resp.status == 304:
            meta['not_modified'] = True
            return b'', meta
        data = resp.read()
        meta['etag'] = resp.headers.get('ETag', '') or ''
        meta['last_modified'] = resp.headers.get('Last-Modified', '') or ''
        meta['content_type'] = resp.headers.get('Content-Type', '') or ''
        meta['response_headers'] = {h: v for h, v in resp.headers.items()}
        return data, meta

    return _do_cache_open(vd, url, 'http', fetcher=_http_fetcher, cache_policy=cache_policy)


@VisiData.api
def cache_open_s3(vd, url: str, *, version_id=None, cache_policy: str = '') -> Path:
    '''Fetch S3 object, cache locally, return Path to cached file.'''
    s3fs_core = vd.importExternal('s3fs.core', 's3fs')

    def _s3_fetcher(entry=None, conditional=False):
        s3fs = s3fs_core.S3FileSystem(
            client_kwargs={'endpoint_url': vd.options.s3_endpoint or None},
            anon=vd.options.s3_anon,
        )
        info = s3fs.info(url, version_id=version_id)
        remote_mtime = info.get('LastModified', 0)
        if hasattr(remote_mtime, 'timestamp'):
            remote_mtime = remote_mtime.timestamp()
        meta = {
            'last_modified': str(remote_mtime),
            'content_type': info.get('ContentType', ''),
            'etag': info.get('ETag', ''),
            'extra': {'version_id': version_id, 'etag': info.get('ETag', '')},
            'cache_policy': cache_policy or 'last-modified',
        }
        if conditional and entry and entry.mtime and entry.mtime >= remote_mtime:
            meta['not_modified'] = True
            return b'', meta
        with s3fs.open(url, mode='rb', version_id=version_id) as src:
            data = src.read()
        return data, meta

    return _do_cache_open(vd, url, 's3', fetcher=_s3_fetcher, cache_policy=cache_policy)


@VisiData.api
def cache_open_api(vd, url: str, *, source_type: str, fetcher,
                   cache_policy: str = '', content_type: str = 'application/json',
                   extra: Optional[Dict[str, Any]] = None) -> Path:
    '''Generic API response caching helper.

    Parameters
    ----------
    url : str
        Logical identifier for this API call (may include query params for uniqueness).
    source_type : str
        Source identifier, e.g. 'airtable', 'reddit', 'zulip', 'matrix'.
    fetcher : callable
        Signature ``fetcher() -> bytes``.  Performs the actual API call and
        returns the raw response bytes (typically JSON).
    cache_policy : str
        One of the standard cache policies.  Defaults to ``days:{cache_default_days}``.
    content_type : str
        Stored in the cache entry for later inspection.
    extra : dict
        Any additional metadata to store alongside the entry.
    '''
    entry = vd.cache_manager.get(url)

    if vd.options.cache_offline:
        local = vd.cache_manager.open_local(url)
        vd.status(f'offline: using cached {url}')
        local._cache_hit = True
        local._from_cache = True
        return local

    if not vd.options.cache_enabled:
        p = vd.cache_manager._cache_path_for(url)
        data = fetcher()
        with p.open_bytes(mode='w') as fpout:
            fpout.write(data)
        return p

    use_cached = entry and not entry.is_expired()
    if use_cached:
        p = Path(entry.local_path)
        vd.debug(f'cache hit for {url}')
        p._cache_hit = True
        p._from_cache = True
        return p

    p = vd.cache_manager._cache_path_for(url)
    data = fetcher()
    with p.open_bytes(mode='w') as fpout:
        fpout.write(data)

    return vd.cache_manager.put(
        url, p,
        source_type=source_type,
        content_type=content_type,
        cache_policy=cache_policy or f'days:{vd.options.cache_default_days}',
        extra=extra or {},
    ).local_path and p or p


vd.cache_manager.register_source_handler('http', lambda url: vd.cache_open_http(url))
vd.cache_manager.register_source_handler('s3', lambda url: vd.cache_open_s3(url))


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
