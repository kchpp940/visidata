import re
import os
import json

from visidata import vd, date, asyncthread, VisiData, Progress, Sheet, Column, ItemColumn, deduceType, TypedWrapper, setitem, AttrDict


vd.option('airtable_auth_token', '', 'Airtable API key from https://airtable.com/account')
vd.option('airtable_use_cache', True, 'cache airtable responses locally via cache_manager', replay=True)

airtable_regex = r'^https://airtable.com/(app[A-Za-z0-9]+)/(tbl[A-Za-z0-9]+)/?(viw[A-z0-9]+)?'

@VisiData.api
def guessurl_airtable(vd, p, response):
    m = re.search(airtable_regex, p.given)
    if m:
        return dict(filetype='airtable', _likelihood=10)


def _airtable_cache_key(base, table, view):
    return f'airtable://{base}/{table}?view={view or "default"}'


def _airtable_refresh(url):
    import pyairtable
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(url)
    parts = parsed.path.strip('/').split('/')
    base = parts[0] if len(parts) > 0 else ''
    table = parts[1] if len(parts) > 1 else ''
    view = parse_qs(parsed.query).get('view', ['default'])[0]
    token = os.environ.get('AIRTABLE_AUTH_TOKEN') or vd.options.airtable_auth_token
    api = pyairtable.Api(token)
    records = []
    for page in api.table(base, table).iterate(view=view if view != 'default' else None):
        records.extend(page)
    return json.dumps(records).encode('utf-8')


vd.cache_manager.register_source_handler('airtable', lambda url: vd.cache_open_api(
    url, source_type='airtable', fetcher=lambda: _airtable_refresh(url)))


@VisiData.api
def open_airtable(vd, p):
    pyairtable = vd.importExternal('pyairtable')

    token = os.environ.get('AIRTABLE_AUTH_TOKEN') or vd.options.airtable_auth_token
    if not token:
        vd.requireOptions('airtable_auth_token', help='https://support.airtable.com/docs/creating-and-using-api-keys-and-access-tokens')

    m = re.search(airtable_regex, p.given)
    if not m:
        vd.fail('invalid airtable url')

    app, tbl, viw = m.groups()
    return AirtableSheet('airtable', source=p,
                         airtable_auth_token=token,
                         airtable_base=app,
                         airtable_table=tbl,
                         airtable_view=viw)


class AirtableSheet(Sheet):
    guide = '''
        # Airtable
        This sheet is a read-only download of all records in a table at _airtable.com_.
    '''
    rowtype = 'records'  # rowdef: dict

    columns = [
        ItemColumn('id', 'id', type=str, width=0),
        ItemColumn('createdTime', 'createdTime', type=date, width=0)
    ]

    def iterload(self):
        self.fields = set()
        cache_key = _airtable_cache_key(self.airtable_base, self.airtable_table, self.airtable_view)

        if vd.options.airtable_use_cache and vd.options.cache_enabled:
            def _fetch():
                records = []
                table = self.api.table(self.airtable_base, self.airtable_table)
                for page in table.iterate(view=self.airtable_view):
                    records.extend(page)
                return json.dumps(records).encode('utf-8')
            cached_path = vd.cache_open_api(
                cache_key, source_type='airtable', fetcher=_fetch,
                extra={'base': self.airtable_base, 'table': self.airtable_table, 'view': self.airtable_view})
            if getattr(cached_path, '_cache_hit', False):
                vd.status(f'using cached airtable:{self.airtable_base}/{self.airtable_table}')
            with cached_path.open_bytes(mode='rb') as fp:
                records = json.loads(fp.read().decode('utf-8'))
            for row in records:
                yield row
                for field, value in row.get('fields', {}).items():
                    if field not in self.fields:
                        col = ItemColumn('fields.'+field, type=deduceType(value))
                        self.addColumn(col)
                        self.fields.add(field)
            return

        table = self.api.table(self.airtable_base, self.airtable_table)
        for page in table.iterate(view=self.airtable_view):
            for row in page:
                yield row
                for field, value in row['fields'].items():
                    if field not in self.fields:
                        col = ItemColumn('fields.'+field, type=deduceType(value))
                        self.addColumn(col)
                        self.fields.add(field)

    def newRow(self):
        return AttrDict(fields=AttrDict())


@AirtableSheet.lazy_property
def api(self):
    import pyairtable
    return pyairtable.Api(self.airtable_auth_token)
