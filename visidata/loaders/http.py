import re

from visidata import Path, RepeatFile, vd, VisiData, __version_info__
from visidata.loaders.tsv import splitter

vd.option('http_max_next', 0, 'max next.url pages to follow in http response') #848
vd.option('http_req_headers', {'User-Agent': __version_info__}, 'http headers to send to requests')
vd.option('http_ssl_verify', True, 'verify host and certificates for https')
vd.option('http_use_cache', True, 'cache http responses locally via cache_manager', replay=True)


@VisiData.api
def guessurl_mimetype(vd, path, response):
    content_filetypes = {
        'tab-separated-values': 'tsv'
    }

    for k in dir(vd):
        if k.startswith('open_'):
            ft = k[5:]
            content_filetypes[ft] = ft

    contenttype = response.getheader('content-type')
    subtype = contenttype.split(';')[0].split('/')[-1]
    if subtype in content_filetypes:
        return dict(filetype=content_filetypes.get(subtype), _likelihood=10)



@VisiData.api
def _http_fetch_response(vd, url, *, ctx=None):
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url, **vd.options.getall('http_req_'))
    try:
        return urllib.request.urlopen(req, context=ctx)
    except urllib.error.HTTPError as e:
        vd.fail(f'cannot open URL: HTTP Error {e.code}: {e.reason}')
    except urllib.error.URLError as e:
        vd.fail(f'cannot open URL: {e.reason}')


@VisiData.api
def openurl_http(vd, path, filetype=None):
    schemes = path.scheme.split('+')
    if len(schemes) > 1:
        sch = schemes[0]
        openfunc = getattr(vd, f'openhttp_{sch}', vd.getGlobals().get(f'openhttp_{sch}'))
        if not openfunc:
            vd.fail(f'no handler for `{sch}` url scheme')
        return openfunc(Path(schemes[-1]+'://'+path.given.split('://')[1]))

    import urllib.request
    import urllib.error
    import mimetypes

    ctx = None
    if not vd.options.http_ssl_verify:
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    use_cache = vd.options.http_use_cache and vd.options.cache_enabled

    if use_cache:
        cached_path = vd.cache_open_http(path.given, headers=vd.options.getall('http_req_'))
        entry = vd.cache_manager.get(path.given)
        if entry:
            if getattr(cached_path, '_cache_hit', False):
                vd.status(f'using cached {path.given}')
            else:
                vd.status(f'cached {path.given}')
        src_path = cached_path
    else:
        response = vd._http_fetch_response(path.given, ctx=ctx)
        filetype = filetype or vd.guessFiletype(path, response, funcprefix='guessurl_').get('filetype')
        filetype = filetype or vd.guessFiletype(path, funcprefix='guess_').get('filetype')
        data = response.read()
        cached_path = vd.cache_manager._cache_path_for(path.given)
        with cached_path.open_bytes(mode='w') as fpout:
            fpout.write(data)
        src_path = cached_path
        if hasattr(response, 'headers'):
            src_path._http_headers = {h: v for h, v in response.headers.items()}

    if not filetype:
        ft_resp = type('FakeResp', (), {'getheader': lambda self, k: getattr(src_path, '_http_headers', {}).get(k, '')})()
        filetype = vd.guessFiletype(path, ft_resp, funcprefix='guessurl_').get('filetype')
        filetype = filetype or vd.guessFiletype(src_path, funcprefix='guess_').get('filetype')

    # Automatically paginate if a 'next' URL is given
    def _iter_lines(path=path, src_path=src_path, max_next=vd.options.http_max_next):
        path.responses = []
        n = 0
        cur_url = path.given
        cur_path = src_path
        while cur_path:
            with cur_path.open_bytes(mode='rb') as fp:
                for line in splitter(fp, delim=b'\n'):
                    yield line.decode(vd.options.encoding)

            linkhdr = getattr(cur_path, '_http_headers', {}).get('Link', '')
            src = None
            if linkhdr:
                links = parse_header_links(linkhdr)
                link_data = {}
                for link in links:
                    key = link.get('rel') or link.get('url')
                    link_data[key] = link
                src = link_data.get('next', {}).get('url', None)

            if not src:
                break

            n += 1
            if n > max_next:
                vd.warning(f'stopping at max next pages: {max_next} pages')
                break

            vd.status(f'fetching next page from {src}')
            if use_cache:
                cur_path = vd.cache_open_http(src, headers=vd.options.getall('http_req_'))
            else:
                resp = vd._http_fetch_response(src, ctx=ctx)
                cur_path = vd.cache_manager._cache_path_for(src)
                with cur_path.open_bytes(mode='w') as fpout:
                    fpout.write(resp.read())
                if hasattr(resp, 'headers'):
                    cur_path._http_headers = {h: v for h, v in resp.headers.items()}
            cur_url = src

    # add resettable iterator over contents as an already-open fp
    path.fptext = RepeatFile(_iter_lines())

    return vd.openSource(path, filetype=filetype)

def parse_header_links(link_header):
    '''Return a list of dictionaries:
    [{'url': 'https://example.com/content?page=1', 'rel': 'prev'},
     {'url': 'https://example.com/content?page=3', 'rel': 'next'}]
    Takes a link header string, of the form
    '<https://example.com/content?page=1>; rel="prev", <https://example.com/content?page=3>; rel="next"'
    See https://datatracker.ietf.org/doc/html/rfc8288#section-3
    '''

    links = []
    quote_space = ' \'"'
    link_header = link_header.strip(quote_space)
    if not link_header: return []
    for link_value in re.split(', *<', link_header):
        if ';' in link_value:
            url, params = link_value.split(';', maxsplit=1)
        else:
            url, params = link_value, ''
        link = {'url': url.strip('<>' + quote_space)}

        for param in params.split(';'):
            if '=' in param:
                key, value = param.split('=')
                key = key.strip(quote_space)
                value = value.strip(quote_space)
                link[key] = value
            else:
                break
        links.append(link)
    return links

VisiData.openurl_https = VisiData.openurl_http
