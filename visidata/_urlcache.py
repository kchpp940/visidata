import time

from visidata import vd, VisiData, Path, modtime


@VisiData.global_api
def urlcache(vd, url, days=1, text=True, headers={}):
    'Return Path object to local cache of url contents.'
    from urllib.request import Request, urlopen
    import urllib.parse

    p = vd.runtime_paths.get_file('cache', urllib.parse.quote(url, safe=''), ensure_dir=True, writable=True)
    if p is None:
        return None

    if p.exists():
        secs = time.time() - modtime(p)
        if secs < days*24*60*60:
            return p

    req = Request(url)
    for k, v in headers.items():
        req.add_header(k, v)

    with urlopen(req) as fp:
        ret = fp.read()
        if text:
            ret = ret.decode('utf-8').strip()
            with p.open(mode='w', encoding='utf-8') as fpout:
                fpout.write(ret)
        else:
            with p.open_bytes(mode='w') as fpout:
                fpout.write(ret)

    return p


@VisiData.api
def enable_requests_cache(vd):
    try:
        import requests
        import requests_cache

        cache_path = vd.runtime_paths.get_file('cache', 'httpcache.sqlite', ensure_dir=True, writable=True)
        if cache_path:
            requests_cache.install_cache(str(cache_path.with_suffix('')), backend='sqlite', expire_after=24*60*60)
    except ModuleNotFoundError:
        vd.warning('install requests_cache for less intrusive scraping')


vd.addGlobals({'urlcache': urlcache})
