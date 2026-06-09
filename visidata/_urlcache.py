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

_SECRET_KEY_PATTERNS = (
    'authorization', 'cookie', 'x-api-key', 'x-auth-token',
    'token', 'secret', 'password', 'passwd', 'api_key', 'apikey',
    'auth', 'client_secret', 'private_key',
)


def _is_secret_key(k: str) -> bool:
    kl = k.lower().replace('-', '_').replace(' ', '_')
    return any(p in kl for p in _SECRET_KEY_PATTERNS)


def sanitize_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    '''Return a copy of headers with sensitive values replaced by [REDACTED].'''
    if not headers:
        return {}
    out = {}
    for k, v in headers.items():
        if _is_secret_key(k):
            out[k] = '[REDACTED]'
        else:
            out[k] = v
    return out


def mask_secrets(obj: Any) -> Any:
    '''Recursively mask values whose keys look like credentials.'''
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_secret_key(str(k)):
                out[k] = '[REDACTED]'
            else:
                out[k] = mask_secrets(v)
        return out
    if isinstance(obj, list):
        return [mask_secrets(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(mask_secrets(x) for x in obj)
    return obj


@dataclass
class CacheEntry:
    '''Metadata for a single cached remote resource.

    Fields are designed so that a refresh can be performed purely from the
    stored entry -- without relying on any runtime loader state.
    '''
    cache_key: str
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

    source_config: Dict[str, Any] = field(default_factory=dict)
    request_params: Dict[str, Any] = field(default_factory=dict)
    response_format: Dict[str, Any] = field(default_factory=dict)
    auth_hint: str = ''
    descr: str = ''

    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def url(self) -> str:
        '''Backward-compatible alias for cache_key.'''
        return self.cache_key

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'CacheEntry':
        valid_fields = cls.__dataclass_fields__
        kwargs = {k: v for k, v in d.items() if k in valid_fields}
        if 'cache_key' not in kwargs and 'url' in d:
            kwargs['cache_key'] = d['url']
        return cls(**kwargs)

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

    def summary(self) -> str:
        '''Return a short human-readable summary.'''
        if self.descr:
            return self.descr
        return f'{self.source_type}:{self.cache_key[:60]}'


class CacheManager:
    '''Unified manager for remote resource caching.

    Maintains a persistent index of all cached resources with full metadata.
    All cache operations (refresh, remove, open, verify) MUST go through this
    manager -- individual loaders MUST NOT maintain their own cache state.

    Refresh handlers receive the full CacheEntry, so they can reconstruct the
    original request using only stored metadata (source_config, request_params,
    auth_hint).  Secrets are never stored -- instead auth_hint tells the
    handler which option / env var to read at refresh time.
    '''

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.index_path = self.cache_dir / '_cache_index.json'
        self._entries: Dict[str, CacheEntry] = {}
        self._loaded = False
        self._source_handlers: Dict[str, Callable[[CacheEntry], Path]] = {}

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
                self._entries = {k: CacheEntry.from_dict(d) for k, d in data.items()}
            except Exception as e:
                vd.warning(f'corrupt cache index, resetting: {e}')
                self._entries = {}
        self._loaded = True

    def _save(self) -> None:
        self._ensure_dir()
        data = {entry.cache_key: entry.to_dict() for entry in self._entries.values()}
        tmp = self.index_path.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as fp:
            json.dump(data, fp, indent=2, default=str)
        os.replace(tmp, self.index_path)

    def _cache_path_for(self, cache_key: str) -> Path:
        parsed = urllib.parse.urlparse(cache_key)
        safe_name = urllib.parse.quote(cache_key, safe='')
        if len(safe_name) > 180:
            safe_name = hashlib.sha256(cache_key.encode('utf-8')).hexdigest()
            ext = os.path.splitext(parsed.path)[1]
            if ext:
                safe_name += ext
        return self.cache_dir / safe_name

    def _detect_source_type(self, cache_key: str) -> str:
        scheme = urllib.parse.urlparse(cache_key).scheme.lower()
        if scheme in ('http', 'https'):
            return 'http'
        if scheme in ('s3',):
            return 's3'
        if scheme:
            return scheme
        return 'other'

    def register_source_handler(self, source_type: str, refresher: Callable[[CacheEntry], Path]) -> None:
        '''Register a callable refresher(entry: CacheEntry) -> Path.

        The refresher receives the *full* stored CacheEntry and must be able to
        rebuild the original request from ``entry.source_config``,
        ``entry.request_params``, and the credential locations given in
        ``entry.auth_hint``.  It must NOT rely on any runtime loader state.
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

    def get(self, cache_key: str) -> Optional[CacheEntry]:
        '''Look up an entry by cache_key.  Syncs metadata from disk and touches access time.'''
        self._load()
        entry = self._entries.get(cache_key)
        if not entry:
            return None
        self.sync_entry(entry)
        if entry.status == _CACHE_STATUS_MISSING:
            del self._entries[cache_key]
            self._save()
            return None
        entry.touch()
        self._save()
        return entry

    def put(self, cache_key: str, local_path: Path, *,
            source_type: str = '',
            etag: str = '',
            last_modified: str = '',
            content_type: str = '',
            cache_policy: str = '',
            status: str = _CACHE_STATUS_OK,
            status_msg: str = '',
            source_config: Optional[Dict[str, Any]] = None,
            request_params: Optional[Dict[str, Any]] = None,
            response_format: Optional[Dict[str, Any]] = None,
            auth_hint: str = '',
            descr: str = '',
            extra: Optional[Dict[str, Any]] = None) -> CacheEntry:
        '''Store (or update) a cache entry.

        ``source_config``, ``request_params``, and ``extra`` are automatically
        run through :func:`mask_secrets` so credentials are never persisted.
        '''
        self._load()
        local = Path(local_path)
        entry = CacheEntry(
            cache_key=cache_key,
            local_path=str(local),
            source_type=source_type or self._detect_source_type(cache_key),
            etag=etag,
            last_modified=last_modified,
            content_type=content_type,
            cache_policy=cache_policy or f'days:{vd.options.cache_default_days}',
            status=status,
            status_msg=status_msg,
            source_config=mask_secrets(source_config or {}),
            request_params=mask_secrets(request_params or {}),
            response_format=response_format or {},
            auth_hint=auth_hint,
            descr=descr,
            extra=mask_secrets(extra or {}),
        )
        self.sync_entry(entry)
        self._entries[cache_key] = entry
        self._save()
        return entry

    def remove(self, cache_key: str) -> bool:
        '''Remove a cache entry and its local file.  Returns True if something was removed.'''
        self._load()
        entry = self._entries.pop(cache_key, None)
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
            for key, entry in self._entries.items():
                self.sync_entry(entry)
                if entry.status == _CACHE_STATUS_MISSING:
                    removed.append(key)
            for key in removed:
                del self._entries[key]
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
        for key, entry in self._entries.items():
            self.sync_entry(entry)
            counts[entry.status] = counts.get(entry.status, 0) + 1
            if entry.status == _CACHE_STATUS_MISSING:
                removed.append(key)
        for key in removed:
            del self._entries[key]
        if removed:
            self._save()
        return counts

    def is_valid(self, cache_key: str) -> bool:
        entry = self.get(cache_key)
        if not entry:
            return False
        return entry.status == _CACHE_STATUS_OK

    def refresh_policy(self, cache_key: str, policy: str) -> Optional[CacheEntry]:
        self._load()
        entry = self._entries.get(cache_key)
        if entry:
            entry.cache_policy = policy
            self.sync_entry(entry)
            self._save()
        return entry

    def open_local(self, cache_key: str) -> Path:
        '''Return a validated Path to the locally cached file.

        Fails if the file is missing or the cache entry cannot be found.
        This is the ONLY supported way to do "offline open" -- it works purely
        from the persisted index and does not require any loader to be loaded.
        '''
        entry = self.get(cache_key)
        if not entry:
            vd.fail(f'no cache entry for {cache_key}')
        local = Path(entry.local_path)
        if not local.exists():
            self.remove(cache_key)
            vd.fail(f'cache file missing: {entry.local_path}')
        entry.touch()
        self._save()
        return local

    def refresh(self, cache_key: str, force: bool = False) -> Optional[CacheEntry]:
        '''Refresh (re-download) a single cache entry.

        Dispatches to the registered source handler, passing the *full*
        CacheEntry so the request can be rebuilt from stored metadata alone.
        If force=True the entry is removed first; otherwise the handler decides.
        Returns the updated CacheEntry or None on failure.
        '''
        self._load()
        entry = self._entries.get(cache_key)
        if not entry:
            vd.warning(f'no cache entry found for {cache_key}')
            return None
        source_type = entry.source_type

        if force:
            self.remove(cache_key)

        handler = self._source_handlers.get(source_type)
        if not handler:
            vd.warning(f'no cache refresh handler registered for source type: {source_type}')
            return None

        try:
            handler(entry)
        except Exception as e:
            vd.exceptionCaught(e)
            reloaded = self._entries.get(cache_key)
            if reloaded:
                reloaded.status = _CACHE_STATUS_ERROR
                reloaded.status_msg = str(e)
                self._save()
            return None

        return self.get(cache_key)


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


def _do_cache_open(vd, cache_key, source_type, *, fetcher, cache_policy='',
                   etag='', last_modified='', content_type='',
                   source_config=None, request_params=None,
                   response_format=None, auth_hint='', descr='', extra=None):
    '''Core helper for all cache_open_* entry points.

    fetcher(entry, conditional) -> (bytes, meta_dict)  -- performs the actual
    download and returns raw bytes plus a metadata dict recognised by this
    helper: not_modified, etag, last_modified, content_type, cache_policy,
    response_headers, extra.
    '''
    entry = vd.cache_manager.get(cache_key)

    if vd.options.cache_offline:
        local = vd.cache_manager.open_local(cache_key)
        vd.status(f'offline: using cached {cache_key}')
        local._cache_hit = True
        local._from_cache = True
        if entry and (entry.etag or entry.last_modified):
            local._http_headers = {'ETag': entry.etag, 'Last-Modified': entry.last_modified}
        return local

    if not vd.options.cache_enabled:
        p = vd.cache_manager._cache_path_for(cache_key)
        data, _ = fetcher(entry=None, conditional=False)
        with p.open_bytes(mode='w') as fpout:
            fpout.write(data)
        return p

    p = Path(entry.local_path) if entry else vd.cache_manager._cache_path_for(cache_key)

    try:
        data, meta = fetcher(entry=entry, conditional=bool(entry))
    except Exception as e:
        if entry:
            vd.status(f'using cached {cache_key} ({e})')
            p._cache_hit = True
            p._from_cache = True
            return p
        raise

    if meta.get('not_modified', False) and entry:
        vd.debug(f'cache hit (304) for {cache_key}')
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
        cache_key, p,
        source_type=source_type,
        etag=meta.get('etag', etag) or '',
        last_modified=meta.get('last_modified', last_modified) or '',
        content_type=meta.get('content_type', content_type) or '',
        cache_policy=policy,
        source_config=source_config or meta.get('source_config'),
        request_params=request_params or meta.get('request_params'),
        response_format=response_format or meta.get('response_format'),
        auth_hint=auth_hint or meta.get('auth_hint', ''),
        descr=descr or meta.get('descr', ''),
        extra=extra or meta.get('extra') or {},
    )

    p._cache_hit = False
    p._from_cache = False
    if 'response_headers' in meta:
        p._http_headers = meta['response_headers']
    return p


@VisiData.api
def cache_open_http(vd, cache_key: str, *, headers=None, cache_policy: str = '',
                    ssl_verify: Optional[bool] = None) -> Path:
    '''Fetch URL via HTTP, cache locally, return Path to cached file.

    All HTTP caching goes through this function.  The stored entry preserves
    sanitized headers and SSL config so a later refresh can reconstruct the
    request without loader state.
    '''
    from urllib.request import Request, urlopen

    if ssl_verify is None:
        ssl_verify = vd.options.http_ssl_verify

    def _http_fetcher(entry=None, conditional=False):
        req = Request(cache_key)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if conditional and entry:
            if entry.etag:
                req.add_header('If-None-Match', entry.etag)
            if entry.last_modified:
                req.add_header('If-Modified-Since', entry.last_modified)

        ctx = None
        if not ssl_verify:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        resp = urlopen(req, context=ctx)
        meta: Dict[str, Any] = {}
        if resp.status == 304:
            meta['not_modified'] = True
            return b'', meta
        data = resp.read()
        meta['etag'] = resp.headers.get('ETag', '') or ''
        meta['last_modified'] = resp.headers.get('Last-Modified', '') or ''
        meta['content_type'] = resp.headers.get('Content-Type', '') or ''
        meta['response_headers'] = {h: v for h, v in resp.headers.items()}
        meta['source_config'] = {'ssl_verify': bool(ssl_verify)}
        meta['request_params'] = {'headers': sanitize_headers(headers or {})}
        if any(_is_secret_key(k) for k in (headers or {})):
            meta['auth_hint'] = 'headers: see http_req_headers / custom Authorization'
        meta['descr'] = f'HTTP {cache_key[:80]}'
        return data, meta

    return _do_cache_open(vd, cache_key, 'http', fetcher=_http_fetcher, cache_policy=cache_policy)


def _refresh_http_entry(entry: CacheEntry) -> Path:
    '''Refresh an HTTP cache entry using only stored metadata.'''
    headers = entry.request_params.get('headers', {})
    ssl_verify = entry.source_config.get('ssl_verify', True)
    return vd.cache_open_http(entry.cache_key, headers=headers,
                              cache_policy=entry.cache_policy,
                              ssl_verify=ssl_verify)


@VisiData.api
def cache_open_s3(vd, cache_key: str, *, version_id=None, cache_policy: str = '') -> Path:
    '''Fetch S3 object, cache locally, return Path to cached file.

    Stored entry includes endpoint_url, anon flag and version_id so refresh
    works independently of loader state.
    '''
    s3fs_core = vd.importExternal('s3fs.core', 's3fs')

    endpoint = vd.options.s3_endpoint or None
    anon = vd.options.s3_anon

    def _s3_fetcher(entry=None, conditional=False):
        s3fs = s3fs_core.S3FileSystem(
            client_kwargs={'endpoint_url': endpoint},
            anon=anon,
        )
        ver = version_id or (entry.extra.get('version_id') if entry else None)
        info = s3fs.info(cache_key, version_id=ver)
        remote_mtime = info.get('LastModified', 0)
        if hasattr(remote_mtime, 'timestamp'):
            remote_mtime = remote_mtime.timestamp()
        meta: Dict[str, Any] = {
            'last_modified': str(remote_mtime),
            'content_type': info.get('ContentType', ''),
            'etag': info.get('ETag', ''),
            'source_config': {'endpoint_url': endpoint, 'anon': bool(anon),
                              'version_aware': vd.options.s3_version_aware},
            'request_params': {'version_id': ver},
            'extra': {'version_id': ver, 'etag': info.get('ETag', '')},
            'cache_policy': cache_policy or 'last-modified',
            'descr': f'S3 {cache_key}',
        }
        if conditional and entry and entry.mtime and entry.mtime >= remote_mtime:
            meta['not_modified'] = True
            return b'', meta
        with s3fs.open(cache_key, mode='rb', version_id=ver) as src:
            data = src.read()
        return data, meta

    return _do_cache_open(vd, cache_key, 's3', fetcher=_s3_fetcher, cache_policy=cache_policy)


def _refresh_s3_entry(entry: CacheEntry) -> Path:
    '''Refresh an S3 cache entry using only stored metadata.'''
    cfg = entry.source_config or {}
    params = entry.request_params or {}
    if 'endpoint_url' in cfg:
        saved_endpoint = cfg.get('endpoint_url')
        orig = vd.options.s3_endpoint
        vd.options.s3_endpoint = saved_endpoint or ''
        orig_anon = vd.options.s3_anon
        vd.options.s3_anon = cfg.get('anon', True)
        try:
            return vd.cache_open_s3(entry.cache_key,
                                    version_id=params.get('version_id'),
                                    cache_policy=entry.cache_policy)
        finally:
            vd.options.s3_endpoint = orig
            vd.options.s3_anon = orig_anon
    return vd.cache_open_s3(entry.cache_key,
                            version_id=params.get('version_id'),
                            cache_policy=entry.cache_policy)


@VisiData.api
def cache_open_api(vd, cache_key: str, *, source_type: str, fetcher,
                   cache_policy: str = '', content_type: str = 'application/json',
                   source_config: Optional[Dict[str, Any]] = None,
                   request_params: Optional[Dict[str, Any]] = None,
                   response_format: Optional[Dict[str, Any]] = None,
                   auth_hint: str = '', descr: str = '',
                   extra: Optional[Dict[str, Any]] = None) -> Path:
    '''Generic API response caching helper.

    Parameters
    ----------
    cache_key : str
        Stable unique identifier for this API call.
    source_type : str
        Source identifier used to route refresh calls (e.g. 'airtable').
    fetcher : callable
        Signature ``fetcher() -> bytes``.  Performs the actual API call and
        returns the raw response bytes.
    source_config : dict
        Endpoint / account-level config needed to rebuild the request
        (base id, server url, etc).  Values are masked for secrets.
    request_params : dict
        Per-request parameters (view, filters, pagination range, etc).
        Values are masked for secrets.
    response_format : dict
        Hints about the stored response (filetype, encoding, compression).
    auth_hint : str
        Where to find credentials at refresh time, e.g.
        ``"env:AIRTABLE_AUTH_TOKEN / option:airtable_auth_token"``.
        The actual token is NEVER stored in the cache index.
    descr : str
        Short human-readable label shown in the Cache Sheet.
    '''
    entry = vd.cache_manager.get(cache_key)

    if vd.options.cache_offline:
        local = vd.cache_manager.open_local(cache_key)
        vd.status(f'offline: using cached {cache_key}')
        local._cache_hit = True
        local._from_cache = True
        return local

    if not vd.options.cache_enabled:
        p = vd.cache_manager._cache_path_for(cache_key)
        data = fetcher()
        with p.open_bytes(mode='w') as fpout:
            fpout.write(data)
        return p

    use_cached = entry and not entry.is_expired()
    if use_cached:
        p = Path(entry.local_path)
        vd.debug(f'cache hit for {cache_key}')
        p._cache_hit = True
        p._from_cache = True
        return p

    p = vd.cache_manager._cache_path_for(cache_key)
    data = fetcher()
    with p.open_bytes(mode='w') as fpout:
        fpout.write(data)

    vd.cache_manager.put(
        cache_key, p,
        source_type=source_type,
        content_type=content_type,
        cache_policy=cache_policy or f'days:{vd.options.cache_default_days}',
        source_config=source_config,
        request_params=request_params,
        response_format=response_format,
        auth_hint=auth_hint,
        descr=descr,
        extra=extra,
    )
    return p


vd.cache_manager.register_source_handler('http', _refresh_http_entry)
vd.cache_manager.register_source_handler('s3', _refresh_s3_entry)


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


vd.addGlobals({'urlcache': urlcache, 'CacheEntry': CacheEntry, 'CacheManager': CacheManager,
               'sanitize_headers': sanitize_headers, 'mask_secrets': mask_secrets})
