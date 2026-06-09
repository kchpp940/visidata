import os
import os.path
import time
import json
import hashlib
import urllib.parse

from visidata import vd, VisiData, Path, modtime


vd.option('remote_cache_days', 1, 'default number of days to keep remote data cached', replay=True)
vd.option('remote_offline_fallback', True, 'fall back to stale cache when remote source is unavailable', replay=True)


class RemoteSourceSpec(dict):
    '''Declarative specification of a remote data source.

    Required keys:
        source_type   -- short label for the data source type ('http', 's3', 'zulip', etc.)
        source_params -- value uniquely identifying the resource (used for cache key)
        fetch_fn      -- callable returning raw data (bytes or str), OR a 2-tuple
                         (data_bytes_or_str, meta_dict) where meta_dict is JSON-serializable

    Optional keys:
        days                -- cache TTL in days (default: options.remote_cache_days)
        text                -- True for UTF-8 text, False for binary (default: True)
        force_refresh       -- skip cache check, always fetch (default: False)
        parse_fn            -- callable(raw_data) -> structured data (default: None)
        with_meta           -- if True, remote_open returns (result, meta) tuple;
                               meta is a dict (or None if not available) (default: False)
        required_meta_keys  -- list of meta keys that MUST be present alongside the body
                               for the cache entry to be considered valid.  If body exists
                               but meta is missing any of these keys, the body is treated
                               as stale and a refresh is attempted before falling back
                               with a clear degradation signal.  (default: [])
        status_online       -- status message when fetching live
        status_offline      -- status message when falling back to stale cache
        error_msg           -- failure message prefix when fetch fails and no cache
    '''
    __slots__ = ()

    _REQUIRED = ('source_type', 'source_params', 'fetch_fn')
    _DEFAULTS = {
        'days': None,
        'text': True,
        'force_refresh': False,
        'parse_fn': None,
        'with_meta': False,
        'required_meta_keys': [],
        'status_online': None,
        'status_offline': None,
        'error_msg': None,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for k in self._REQUIRED:
            if k not in self:
                raise TypeError(f'RemoteSourceSpec missing required key: {k}')
        for k, v in self._DEFAULTS.items():
            self.setdefault(k, v)


def _cache_key_from_spec(source_type, source_params):
    '''Generate a deterministic, filesystem-safe cache key from source spec.'''
    if isinstance(source_params, (dict, list)):
        param_str = json.dumps(source_params, sort_keys=True, ensure_ascii=True)
    else:
        param_str = str(source_params)
    raw = f'{source_type}:{param_str}'
    h = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]
    safe = urllib.parse.quote(raw, safe='')[:80]
    return f'{safe}_{h}'


def _cache_path_for(key):
    '''Return Path to cache file for the given key.'''
    os.makedirs(vd.cache_dir, exist_ok=True)
    return Path(vd.cache_dir / key)


def _meta_path_for(key):
    '''Return Path to the .meta sidecar JSON file for the given key.'''
    return _cache_path_for(key + '.meta')


def _cache_is_fresh(p, days):
    '''Return True if cache file exists and is within days TTL.'''
    if not p.exists():
        return False
    secs = time.time() - modtime(p)
    return secs < days * 24 * 60 * 60


def _cache_read_text(p):
    with p.open(encoding='utf-8') as fp:
        return fp.read()


def _cache_read_bytes(p):
    with p.open_bytes() as fp:
        return fp.read()


def _cache_write_text(p, data):
    with p.open(mode='w', encoding='utf-8') as fp:
        fp.write(data)


def _cache_write_bytes(p, data):
    with p.open_bytes(mode='w') as fp:
        fp.write(data)


def _meta_read(key):
    '''Read meta dict from sidecar file, or return None.'''
    mp = _meta_path_for(key)
    if not mp.exists():
        return None
    try:
        with mp.open(encoding='utf-8') as fp:
            return json.load(fp)
    except Exception:
        return None


def _meta_write(key, meta):
    '''Write meta dict to sidecar JSON file.'''
    if meta is None:
        return
    mp = _meta_path_for(key)
    with mp.open(mode='w', encoding='utf-8') as fp:
        json.dump(meta, fp, ensure_ascii=False, default=str)


def _meta_remove(key):
    '''Remove meta sidecar file if present.'''
    mp = _meta_path_for(key)
    if mp.exists():
        try:
            os.remove(str(mp))
        except OSError:
            pass


def _resolve_spec(spec):
    '''Normalize spec into a RemoteSourceSpec.'''
    if isinstance(spec, RemoteSourceSpec):
        return spec
    return RemoteSourceSpec(spec)


def _unpack_fetch_result(result):
    '''Unpack fetch_fn return value.  Returns (primary_data, meta_or_None).'''
    if isinstance(result, tuple) and len(result) == 2:
        return result[0], result[1]
    return result, None


def _apply_result(data, text, parse_fn, cp):
    '''Apply parse_fn / text mode to raw cache data and return the caller-facing value.'''
    if parse_fn:
        raw = _cache_read_text(cp) if text else _cache_read_bytes(cp)
        return parse_fn(raw)
    if text:
        return _cache_read_text(cp)
    return cp


def _meta_validate(meta, required_keys):
    '''Return True if meta is present and contains every key in required_keys.

    An empty required_keys list always validates as True, even when meta is None.
    '''
    if not required_keys:
        return True
    if not isinstance(meta, dict):
        return False
    return all(k in meta for k in required_keys)


def _meta_missing_keys(meta, required_keys):
    '''Return a list of required keys that are missing from meta (informational).'''
    if not required_keys:
        return []
    if not isinstance(meta, dict):
        return list(required_keys)
    return [k for k in required_keys if k not in meta]


@VisiData.global_api
def make_remote_spec(vd, source_type, source_params, fetch_fn, **kwargs):
    '''Build a RemoteSourceSpec.  Convenience constructor equivalent to RemoteSourceSpec(...).'''
    return RemoteSourceSpec(
        source_type=source_type,
        source_params=source_params,
        fetch_fn=fetch_fn,
        **kwargs,
    )


@VisiData.global_api
def remote_key(vd, source_type, source_params):
    '''Generate cache key for remote source.  *source_type* is a short string label (e.g. 'http', 's3', 'zulip').  *source_params* is a dict/str/obj that uniquely identifies the resource.'''
    return _cache_key_from_spec(source_type, source_params)


@VisiData.global_api
def remote_cached(vd, source_type_or_spec, source_params=None, days=None):
    '''Return Path to cached content if fresh, else None.

    Accepts either (source_type, source_params) pair or a single RemoteSourceSpec.
    '''
    if isinstance(source_type_or_spec, (dict, RemoteSourceSpec)):
        spec = _resolve_spec(source_type_or_spec)
        source_type = spec['source_type']
        source_params = spec['source_params']
        days = days if days is not None else spec['days']
    else:
        source_type = source_type_or_spec

    days = days if days is not None else vd.options.remote_cache_days
    key = vd.remote_key(source_type, source_params)
    cp = _cache_path_for(key)
    if _cache_is_fresh(cp, days):
        return cp
    return None


@VisiData.global_api
def remote_has_stale(vd, source_type_or_spec, source_params=None):
    '''Return True if any cached version (fresh or stale) exists.

    Accepts either (source_type, source_params) pair or a single RemoteSourceSpec.
    '''
    if isinstance(source_type_or_spec, (dict, RemoteSourceSpec)):
        spec = _resolve_spec(source_type_or_spec)
        source_type = spec['source_type']
        source_params = spec['source_params']
    else:
        source_type = source_type_or_spec

    key = vd.remote_key(source_type, source_params)
    cp = _cache_path_for(key)
    return cp.exists()


@VisiData.global_api
def remote_invalidate(vd, source_type_or_spec, source_params=None):
    '''Remove cached data and meta for the given source.

    Accepts either (source_type, source_params) pair or a single RemoteSourceSpec.
    '''
    if isinstance(source_type_or_spec, (dict, RemoteSourceSpec)):
        spec = _resolve_spec(source_type_or_spec)
        source_type = spec['source_type']
        source_params = spec['source_params']
    else:
        source_type = source_type_or_spec

    key = vd.remote_key(source_type, source_params)
    cp = _cache_path_for(key)
    if cp.exists():
        try:
            os.remove(str(cp))
        except OSError:
            pass
    _meta_remove(key)


@VisiData.global_api
def remote_open(vd, spec):
    '''Unified entry point for fetching remote data via a RemoteSourceSpec.

    All cache logic (key generation, freshness check, stale fallback, write-through,
    error/status messages, meta sidecar handling) is internal.  The loader only builds the spec.

    Returns:
        If spec['with_meta'] is True      -> (result, meta_dict_or_None) tuple
        If spec['parse_fn'] is set        -> parse_fn(raw_data)
        Else if spec['text'] is True      -> str (raw text content)
        Else (binary)                     -> Path to the cached binary file (for streaming)
    '''
    spec = _resolve_spec(spec)
    source_type = spec['source_type']
    source_params = spec['source_params']
    fetch_fn = spec['fetch_fn']
    days = spec['days'] if spec['days'] is not None else vd.options.remote_cache_days
    text = spec['text']
    force_refresh = spec['force_refresh']
    parse_fn = spec['parse_fn']
    with_meta = spec['with_meta']
    required_meta_keys = spec['required_meta_keys'] or []
    status_online = spec['status_online']
    status_offline = spec['status_offline']
    error_msg = spec['error_msg']

    key = vd.remote_key(source_type, source_params)
    cp = _cache_path_for(key)

    def _return_from_cache(cached_meta, degraded=False):
        '''Build the return value from cache data and meta.

        When degraded is True (meta missing required keys even after fallback), the
        meta dict will contain a special `_meta_degraded: True` marker so callers
        can detect the incomplete state.
        '''
        if degraded and isinstance(cached_meta, dict):
            cached_meta = dict(cached_meta)
            cached_meta['_meta_degraded'] = True
        elif degraded and cached_meta is None:
            cached_meta = {'_meta_degraded': True}
        result = _apply_result(None, text, parse_fn, cp)
        return (result, cached_meta) if with_meta else result

    # Fast path: fresh body and valid meta together
    if not force_refresh and _cache_is_fresh(cp, days):
        meta = _meta_read(key)
        if _meta_validate(meta, required_meta_keys):
            vd.debug(f'remote cache hit for {source_type}')
            return _return_from_cache(meta)
        missing = _meta_missing_keys(meta, required_meta_keys)
        vd.debug(f'remote cache body fresh but meta incomplete for {source_type} (missing {missing}); attempting refresh')

    # Try a live fetch (either because body was stale, or meta was incomplete)
    try:
        if status_online:
            vd.status(status_online)
        raw_result = fetch_fn()
        primary_data, meta = _unpack_fetch_result(raw_result)

        if text and isinstance(primary_data, bytes):
            primary_data = primary_data.decode('utf-8')
        elif not text and isinstance(primary_data, str):
            primary_data = primary_data.encode('utf-8')

        if text:
            _cache_write_text(cp, primary_data)
        else:
            _cache_write_bytes(cp, primary_data)
        _meta_write(key, meta)

        if parse_fn:
            result = parse_fn(primary_data)
        elif text:
            result = primary_data
        else:
            result = cp

        return (result, meta) if with_meta else result

    except Exception as e:
        # Live fetch failed; attempt stale-body fallback
        if cp.exists() and vd.options.remote_offline_fallback:
            meta = _meta_read(key)
            if _meta_validate(meta, required_meta_keys):
                offline_msg = status_offline or f'offline: using cached data for `{source_type}`'
                vd.warning(f'{offline_msg}; {e}')
                return _return_from_cache(meta)
            # Body exists but meta is incomplete — explicit degraded signal
            missing = _meta_missing_keys(meta, required_meta_keys)
            offline_msg = status_offline or f'offline: using cached data for `{source_type}`'
            vd.warning(f'{offline_msg}; missing meta keys {missing}; {e}')
            return _return_from_cache(meta, degraded=True)

        if error_msg:
            vd.fail(f'{error_msg}; {e}')
        raise


@VisiData.global_api
def remote_fetch(vd, source_type, source_params, fetch_fn, *,
                 days=None, text=True, force_refresh=False,
                 status_online=None, status_offline=None, error_msg=None):
    '''Legacy wrapper around remote_open.  Prefer building a RemoteSourceSpec and calling remote_open().'''
    spec = RemoteSourceSpec(
        source_type=source_type,
        source_params=source_params,
        fetch_fn=fetch_fn,
        days=days,
        text=text,
        force_refresh=force_refresh,
        status_online=status_online,
        status_offline=status_offline,
        error_msg=error_msg,
    )
    return vd.remote_open(spec)


@VisiData.global_api
def urlcache(vd, url, days=1, text=True, headers={}):
    'Return Path object to local cache of url contents.'
    from urllib.request import Request, urlopen

    def _fetch():
        req = Request(url)
        for k, v in headers.items():
            req.add_header(k, v)
        with urlopen(req) as fp:
            ret = fp.read()
            if text:
                return ret.decode('utf-8').strip()
            return ret

    spec = RemoteSourceSpec(
        source_type='urlcache',
        source_params=url,
        fetch_fn=_fetch,
        days=days,
        text=text,
        error_msg=f'cannot fetch url `{url}`',
    )
    return vd.remote_open(spec)


@VisiData.api
def enable_requests_cache(vd):
    try:
        import requests
        import requests_cache

        requests_cache.install_cache(str(Path(os.path.join(vd.options.visidata_dir, 'httpcache'))), backend='sqlite', expire_after=24*60*60)
    except ModuleNotFoundError:
        vd.warning('install requests_cache for less intrusive scraping')


vd.addGlobals({'urlcache': urlcache, 'RemoteSourceSpec': RemoteSourceSpec})
