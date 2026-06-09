import os
import os.path
import time
import json
import hashlib
import urllib.parse

from visidata import vd, VisiData, Path, modtime


vd.option('remote_cache_days', 1, 'default number of days to keep remote data cached', replay=True)
vd.option('remote_offline_fallback', True, 'fall back to stale cache when remote source is unavailable', replay=True)


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


@VisiData.global_api
def remote_key(vd, source_type, source_params):
    '''Generate cache key for remote source.  *source_type* is a short string label (e.g. 'http', 's3', 'zulip').  *source_params* is a dict/str/obj that uniquely identifies the resource.'''
    return _cache_key_from_spec(source_type, source_params)


@VisiData.global_api
def remote_cached(vd, source_type, source_params, days=None):
    '''Return Path to cached content if fresh, else None.'''
    days = days if days is not None else vd.options.remote_cache_days
    key = vd.remote_key(source_type, source_params)
    cp = _cache_path_for(key)
    if _cache_is_fresh(cp, days):
        return cp
    return None


@VisiData.global_api
def remote_has_stale(vd, source_type, source_params):
    '''Return True if any cached version (fresh or stale) exists.'''
    key = vd.remote_key(source_type, source_params)
    cp = _cache_path_for(key)
    return cp.exists()


@VisiData.global_api
def remote_invalidate(vd, source_type, source_params):
    '''Remove cached data for the given source.'''
    key = vd.remote_key(source_type, source_params)
    cp = _cache_path_for(key)
    if cp.exists():
        try:
            os.remove(str(cp))
        except OSError:
            pass


@VisiData.global_api
def remote_fetch(vd, source_type, source_params, fetch_fn, *,
                 days=None, text=True, force_refresh=False,
                 status_online=None, status_offline=None, error_msg=None):
    '''Unified remote data fetch with transparent caching and offline fallback.

    *source_type*: short label for the data source type (e.g. 'http', 's3', 'airtable')
    *source_params*: value that uniquely identifies the resource (used for cache key)
    *fetch_fn*: callable that fetches fresh data (returns bytes or str)
    *days*: cache TTL in days (default: options.remote_cache_days)
    *text*: if True, data is treated as UTF-8 text; otherwise binary
    *force_refresh*: if True, skip cache check and always call fetch_fn
    *status_online*: status message prefix when fetching live (e.g. 'fetching from https://...')
    *status_offline*: status message when falling back to stale cache (default auto-generated)
    *error_msg*: prefix for failure messages when fetch fails and no cache available

    Returns a Path to the local (cached or freshly written) content.
    '''
    days = days if days is not None else vd.options.remote_cache_days
    key = vd.remote_key(source_type, source_params)
    cp = _cache_path_for(key)

    if not force_refresh and _cache_is_fresh(cp, days):
        vd.debug(f'remote cache hit for {source_type}')
        return cp

    try:
        if status_online:
            vd.status(status_online)
        data = fetch_fn()

        if text and isinstance(data, bytes):
            data = data.decode('utf-8')
        elif not text and isinstance(data, str):
            data = data.encode('utf-8')

        if text:
            _cache_write_text(cp, data)
        else:
            _cache_write_bytes(cp, data)

        return cp

    except Exception as e:
        if cp.exists() and vd.options.remote_offline_fallback:
            offline_msg = status_offline or f'offline: using cached data for `{source_type}`'
            vd.warning(f'{offline_msg}; {e}')
            return cp

        if error_msg:
            vd.fail(f'{error_msg}; {e}')
        raise


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

    return vd.remote_fetch(
        'urlcache', url, _fetch,
        days=days, text=text,
        error_msg=f'cannot fetch url `{url}`'
    )


@VisiData.api
def enable_requests_cache(vd):
    try:
        import requests
        import requests_cache

        requests_cache.install_cache(str(Path(os.path.join(vd.options.visidata_dir, 'httpcache'))), backend='sqlite', expire_after=24*60*60)
    except ModuleNotFoundError:
        vd.warning('install requests_cache for less intrusive scraping')


vd.addGlobals({'urlcache': urlcache})
