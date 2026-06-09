import re

from visidata import Path, RepeatFile, vd, VisiData, __version_info__
from visidata.loaders.tsv import splitter

vd.option('http_max_next', 0, 'max next.url pages to follow in http response')
vd.option('http_req_headers', {'User-Agent': __version_info__}, 'http headers to send to requests')
vd.option('http_ssl_verify', True, 'verify host and certificates for https')


class _HttpCachedResponse:
    '''Lightweight proxy mimicking the subset of urllib response we use: .getheader().'''
    def __init__(self, headers):
        self._headers = dict(headers or {})

    def getheader(self, name, default=None):
        return self._headers.get(name.lower(), self._headers.get(name, default))


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
    if not contenttype:
        return None
    subtype = contenttype.split(';')[0].split('/')[-1]
    if subtype in content_filetypes:
        return dict(filetype=content_filetypes.get(subtype), _likelihood=10)


def _http_source_params(url):
    '''Return cache-key params dict for an HTTP source.'''
    return {
        'url': url,
        'headers': dict(vd.options.getall('http_req_')),
        'ssl_verify': vd.options.http_ssl_verify,
    }


def _http_error_message(e, url):
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        return f'cannot open URL: HTTP Error {e.code}: {e.reason}'
    if isinstance(e, urllib.error.URLError):
        return f'cannot open URL: {e.reason}'
    return f'cannot open URL: {e}'


def _http_extract_meta(resp):
    '''Extract JSON-serializable metadata from a live urllib response.'''
    headers = {}
    if hasattr(resp, 'headers'):
        for k, v in resp.headers.items():
            headers[k.lower()] = v
    elif hasattr(resp, 'getheaders'):
        for k, v in resp.getheaders():
            headers[k.lower()] = v
    return {
        'headers': headers,
        'content-type': headers.get('content-type'),
        'Link': headers.get('link'),
    }


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
    import mimetypes

    ctx = None
    if not vd.options.http_ssl_verify:
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(path.given, **vd.options.getall('http_req_'))
    source_params = _http_source_params(path.given)

    def _fetch():
        resp = urllib.request.urlopen(req, context=ctx)
        body = resp.read()
        meta = _http_extract_meta(resp)
        return body, meta

    spec = vd.make_remote_spec(
        'http', source_params, _fetch,
        text=False,
        with_meta=True,
        required_meta_keys=['headers'],
        status_online=f'fetching {path.given}',
        status_offline=f'offline: using cached data for `{path.given}`',
    )

    try:
        body_cp, meta = vd.remote_open(spec)
    except Exception as e:
        vd.fail(_http_error_message(e, path.given))

    with body_cp.open_bytes() as fp:
        body_bytes = fp.read()

    degraded = bool(meta and meta.get('_meta_degraded'))
    if meta and meta.get('headers'):
        response = _HttpCachedResponse(meta['headers'])
    else:
        response = None
        if not degraded:
            degraded = True

    if degraded:
        vd.warning(f'HTTP cache for `{path.given}` missing response headers; '
                   'filetype guessing and pagination will fall back to URL/path only')

    if response is not None:
        filetype = filetype or vd.guessFiletype(path, response, funcprefix='guessurl_').get('filetype')
    filetype = filetype or vd.guessFiletype(path, funcprefix='guess_').get('filetype')

    def _iter_lines(initial_body=body_bytes, initial_resp=response, max_next=vd.options.http_max_next):
        path.responses = []
        n = 0
        cur_body = initial_body
        cur_resp = initial_resp

        while True:
            if cur_resp is not None:
                path.responses.append(cur_resp)

            import io
            fp = io.BytesIO(cur_body)
            for line in splitter(fp, delim=b'\n'):
                yield line.decode(vd.options.encoding)

            linkhdr = cur_resp and cur_resp.getheader('Link')
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

            next_req = urllib.request.Request(src, **vd.options.getall('http_req_'))
            next_params = _http_source_params(src)

            def _next_fetch():
                resp = urllib.request.urlopen(next_req, context=ctx)
                body = resp.read()
                meta = _http_extract_meta(resp)
                return body, meta

            next_spec = vd.make_remote_spec(
                'http', next_params, _next_fetch,
                text=False,
                with_meta=True,
                required_meta_keys=['headers'],
                status_online=f'fetching next page from {src}',
                status_offline=f'offline: using cached data for `{src}`',
            )

            try:
                next_cp, next_meta = vd.remote_open(next_spec)
                with next_cp.open_bytes() as fp:
                    cur_body = fp.read()
                if next_meta and next_meta.get('headers'):
                    cur_resp = _HttpCachedResponse(next_meta['headers'])
                else:
                    cur_resp = None
                    if not (next_meta and next_meta.get('_meta_degraded')):
                        vd.warning(f'next page cache for `{src}` missing response headers; pagination will stop')
            except Exception as e:
                vd.warning(f'cannot fetch next page from {src}: {e}')
                break

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
