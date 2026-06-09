import re
import os
import json

from visidata import vd, date, asyncthread, VisiData, Progress, Sheet, Column, ItemColumn, deduceType, TypedWrapper, setitem, AttrDict, CacheEntry


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


def _airtable_page_key(base, table, view, page_offset):
    return f'airtable://{base}/{table}/page:{view or "default"}:{page_offset or "init"}'


def _airtable_fetch_pages(api, base, table, view):
    '''Generator yielding (page_number, page_cache_key, request_params, data_bytes, endpoint) tuples.

    Drives Airtable pagination and produces manifest-ready page entries.
    '''
    import pyairtable
    page_number = 0
    offset = None
    endpoint = f'{base}/{table}'
    table_obj = api.table(base, table)
    while True:
        kwargs = {'view': view} if view else {}
        if offset:
            kwargs['offset'] = offset
        page = table_obj.first_page(**kwargs) if not offset else table_obj.each_page_iter(**kwargs)
        if isinstance(page, (list, tuple)) and offset is None:
            records = list(page)
        elif isinstance(page, (list, tuple)):
            records = list(page)
            if not records:
                break
            page_number += 1
            page_key = _airtable_page_key(base, table, view, offset)
            params = {'view': view, 'offset': offset}
            data_bytes = json.dumps(records).encode('utf-8')
            yield page_number, page_key, params, data_bytes, endpoint
            break
        else:
            for records in page:
                page_number += 1
                page_key = _airtable_page_key(base, table, view, offset or 'init')
                params = {'view': view, 'offset': offset or ''}
                data_bytes = json.dumps(records).encode('utf-8')
                yield page_number, page_key, params, data_bytes, endpoint
                offset = getattr(table_obj, '_last_offset', None)
            break
        if offset is None and records:
            page_number += 1
            page_key = _airtable_page_key(base, table, view, 'init')
            params = {'view': view}
            data_bytes = json.dumps(records).encode('utf-8')
            yield page_number, page_key, params, data_bytes, endpoint
        break


def _airtable_refresh_entry(entry: CacheEntry):
    '''Refresh an Airtable cache entry using *only* the stored CacheEntry metadata.

    Supports both single-file and paginated (manifest) entries.
    '''
    import pyairtable
    cfg = entry.source_config or {}
    params = entry.request_params or {}
    base = cfg.get('base') or params.get('base') or ''
    table = cfg.get('table') or params.get('table') or ''
    view = cfg.get('view') or params.get('view')
    if view == 'default':
        view = None

    token = os.environ.get('AIRTABLE_AUTH_TOKEN') or vd.options.airtable_auth_token
    if not token:
        vd.requireOptions('airtable_auth_token',
                          help='https://support.airtable.com/docs/creating-and-using-api-keys-and-access-tokens')
    api = pyairtable.Api(token)
    descr = f'Airtable {base}/{table}' + (f' view={view}' if view else '')

    if entry.has_pages():
        vd.cache_manager.remove_pages(entry.cache_key)

        def _gen():
            for item in _airtable_fetch_pages(api, base, table, view):
                yield item

        return vd.cache_open_api(
            entry.cache_key, source_type='airtable',
            page_iterator=_gen(),
            source_config={'base': base, 'table': table,
                           'view': view or 'default'},
            request_params={'view': view},
            response_format={'filetype': 'json', 'encoding': 'utf-8'},
            merge_strategy='json_records_concat',
            parser_hint='airtable-records',
            auth_hint='env:AIRTABLE_AUTH_TOKEN / option:airtable_auth_token',
            descr=descr,
        )
    else:
        records = []
        for page in api.table(base, table).iterate(view=view):
            records.extend(page)
        data = json.dumps(records).encode('utf-8')
        cached_path = vd.cache_manager._cache_path_for(entry.cache_key)
        with cached_path.open_bytes(mode='w') as fpout:
            fpout.write(data)
        return vd.cache_manager.put(
            entry.cache_key, cached_path,
            source_type='airtable',
            content_type='application/json',
            cache_policy=entry.cache_policy,
            source_config=entry.source_config,
            request_params=entry.request_params,
            response_format={'filetype': 'json', 'encoding': 'utf-8'},
            auth_hint='env:AIRTABLE_AUTH_TOKEN / option:airtable_auth_token',
            descr=descr,
        )


vd.cache_manager.register_source_handler('airtable', _airtable_refresh_entry)


@VisiData.api
def open_airtable(vd, p):
    pyairtable = vd.importExternal('pyairtable')

    token = os.environ.get('AIRTABLE_AUTH_TOKEN') or vd.options.airtable_auth_token
    if not token:
        vd.requireOptions('airtable_auth_token',
                          help='https://support.airtable.com/docs/creating-and-using-api-keys-and-access-tokens')

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
        descr = f'Airtable {self.airtable_base}/{self.airtable_table}' + (
            f' view={self.airtable_view}' if self.airtable_view else '')

        if vd.options.airtable_use_cache and vd.options.cache_enabled:
            def _gen():
                for item in _airtable_fetch_pages(self.api, self.airtable_base,
                                                   self.airtable_table, self.airtable_view):
                    yield item

            cached_path = vd.cache_open_api(
                cache_key, source_type='airtable',
                page_iterator=_gen(),
                source_config={'base': self.airtable_base, 'table': self.airtable_table,
                                'view': self.airtable_view or 'default'},
                request_params={'view': self.airtable_view},
                response_format={'filetype': 'json', 'encoding': 'utf-8'},
                merge_strategy='json_records_concat',
                parser_hint='airtable-records',
                auth_hint='env:AIRTABLE_AUTH_TOKEN / option:airtable_auth_token',
                descr=descr,
            )
            if getattr(cached_path, '_cache_hit', False):
                vd.status(f'using cached {descr}')
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
