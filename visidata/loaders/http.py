import re

from visidata import Path, RepeatFile, vd, VisiData, __version_info__
from visidata.loaders.tsv import splitter

vd.option('http_max_next', 0, 'max next.url pages to follow in http response')
vd.option('http_req_headers', {'User-Agent': __version_info__}, 'http headers to send to requests')
vd.option('http_ssl_verify', True, 'verify host and certificates for https')


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


def _http_source_params(path):
    '''Return cache-key params dict for an HTTP source.'''
    return {
        'url': path.given,
        'headers': dict(vd.options.getall('http_req_')),
        'ssl_verify': vd.options.http_ssl_verify,
    }


def _http_fetch_response(url, ctx):
    '''Fetch a single URL.  Returns (response_or_None, body_bytes).  On cache hit, response is None.'''
    import urllib.request

    req = urllib.request.Request(url, **vd.options.getall('http_req_'))

    source_params = {'url': url, 'headers': dict(vd.options.getall('http_req_'))}

    def _fetch():
        resp = urllib.request.urlopen(req, context=ctx)
        body = resp.read()
        return body, resp

    try:
        cp = vd.remote_fetch(
            'http', source_params, lambda: _fetch()[0],
            text=False,
            status_online=f'fetching {url}',
            status_offline=f'offline: using cached data for `{url}`',
            error_msg=f'cannot open URL `{url}`',
        )
    except Exception as e:
        raise

    with cp.open_bytes() as fp:
        cached_body = fp.read()

    try:
        resp = urllib.request.urlopen(req, context=ctx)
        return resp, resp.read()
    except Exception:
        return None, cached_body


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

    req = urllib.request.Request(path.given, **vd.options.getall('http_req_'))
    source_params = _http_source_params(path)

    def _live_fetch():
        return urllib.request.urlopen(req, context=ctx)

    response = None
    body_bytes = None

    cached = vd.remote_cached('http', source_params)
    try:
        try:
            response = _live_fetch()
            body_bytes = response.read()
            vd.remote_fetch(
                'http', source_params, lambda: body_bytes,
                text=False,
                force_refresh=True,
            )
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            if cached and vd.options.remote_offline_fallback:
                vd.warning(f'offline: using cached data for `{path.given}`; {e}')
                with cached.open_bytes() as fp:
                    body_bytes = fp.read()
            else:
                if isinstance(e, urllib.error.HTTPError):
                    vd.fail(f'cannot open URL: HTTP Error {e.code}: {e.reason}')
                else:
                    vd.fail(f'cannot open URL: {e.reason}')
    except Exception as e:
        if cached and vd.options.remote_offline_fallback:
            vd.warning(f'offline: using cached data for `{path.given}`; {e}')
            with cached.open_bytes() as fp:
                body_bytes = fp.read()
        else:
            vd.fail(f'cannot open URL: {e}')

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

            vd.status(f'fetching next page from {src}')
            next_req = urllib.request.Request(src, **vd.options.getall('http_req_'))
            try:
                cur_resp = urllib.request.urlopen(next_req, context=ctx)
                cur_body = cur_resp.read()
                next_params = {'url': src, 'headers': dict(vd.options.getall('http_req_'))}
                vd.remote_fetch(
                    'http', next_params, lambda: cur_body,
                    text=False, force_refresh=True,
                )
            except Exception as e:
                next_cached = vd.remote_cached('http', {'url': src, 'headers': dict(vd.options.getall('http_req_'))})
                if next_cached and vd.options.remote_offline_fallback:
                    vd.warning(f'offline: using cached data for `{src}`; {e}')
                    with next_cached.open_bytes() as fp:
                        cur_body = fp.read()
                    cur_resp = None
                else:
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
