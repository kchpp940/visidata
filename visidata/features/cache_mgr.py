'''Cache Sheet -- view and manage cached remote resources.

All cache operations go through vd.cache_manager exclusively.  The sheet
never touches cache files or metadata directly -- every refresh, open, and
delete is dispatched to the manager, which uses the stored CacheEntry
metadata (source_config, request_params, auth_hint, ...) to rebuild the
request without relying on any loader runtime state.
'''

import os
import time
import json

from visidata import (
    BaseSheet,
    Column,
    ItemColumn,
    Path,
    Sheet,
    TableSheet,
    VisiData,
    asyncthread,
    date,
    vd,
)


@VisiData.api
def format_bytes(vd, n):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024:
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} PB'


def _pp(obj):
    try:
        if obj is None or obj == '' or obj == {} or obj == []:
            return ''
        if isinstance(obj, (dict, list)):
            return json.dumps(obj, default=str)
        return str(obj)
    except Exception:
        return str(obj)


# rowdef: CacheEntry
class CacheSheet(Sheet):
    '''View and manage cached remote resources (HTTP/S3/API).

    Each row is a :class:`CacheEntry` loaded from the persistent index.
    The sheet verifies every entry against the real filesystem on each
    reload, and all cache operations (refresh, open, clear) go EXCLUSIVELY
    through :class:`CacheManager`.  No loader state is required.
    '''
    rowtype = 'cached files'
    columns = [
        Column('status', width=8, getter=lambda c, r: r.status),
        Column('descr', width=40, getter=lambda c, r: r.descr or r.summary()),
        Column('source_type', width=10, getter=lambda c, r: r.source_type),
        Column('cache_key', width=60, getter=lambda c, r: r.cache_key),
        Column('local_path', width=40, getter=lambda c, r: r.local_path),
        Column('size', type=int, width=10,
               getter=lambda c, r: r.size,
               fmtstr=lambda col, row, val: vd.format_bytes(val)),
        Column('mtime', type=date, width=18, getter=lambda c, r: r.mtime),
        Column('last_accessed', type=date, width=18, getter=lambda c, r: r.last_accessed),
        Column('cache_policy', width=12, getter=lambda c, r: r.cache_policy),
        Column('auth_hint', width=30, getter=lambda c, r: r.auth_hint),
        Column('source_config', width=0, getter=lambda c, r: _pp(r.source_config)),
        Column('request_params', width=0, getter=lambda c, r: _pp(r.request_params)),
        Column('response_format', width=0, getter=lambda c, r: _pp(r.response_format)),
        Column('etag', width=0, getter=lambda c, r: r.etag[:20] if r.etag else ''),
        Column('last_modified', width=0, getter=lambda c, r: r.last_modified),
        Column('content_type', width=0, getter=lambda c, r: r.content_type),
        Column('status_msg', width=0, getter=lambda c, r: r.status_msg),
        Column('extra', width=0, getter=lambda c, r: _pp(r.extra)),
    ]
    nKeys = 1

    def iterload(self):
        for entry in sorted(vd.cache_manager.list(verify=True),
                            key=lambda e: e.last_accessed, reverse=True):
            yield entry

    def commitDeleteRow(self, row):
        vd.cache_manager.remove(row.cache_key)

    def newRow(self):
        vd.fail('new cache entries not supported')

    @asyncthread
    def refresh_entries(self, rows):
        '''Refresh selected entries via CacheManager.refresh().'''
        for entry in vd.Progress(rows, gerund='refreshing'):
            vd.cache_manager.refresh(entry.cache_key, force=True)
        self.reload()

    @asyncthread
    def clear_all_cache(self):
        n = vd.cache_manager.clear_all()
        vd.status(f'cleared {n} cache entries')
        self.reload()

    def openCached(self, entry):
        '''Open the local cached copy.  Uses CacheManager.open_local() exclusively.

        Works without any loader state -- only the stored index entry is needed.
        '''
        local = vd.cache_manager.open_local(entry.cache_key)
        response_format = entry.response_format or {}
        filetype = response_format.get('filetype') or local.ext or 'txt'
        vs = vd.openSource(local, filetype=filetype)
        vs.name = entry.descr or f'{entry.source_type}:{os.path.basename(entry.local_path)}'
        return vs

    def reloadSource(self, entry):
        '''Drop cache and trigger a full CacheManager.refresh().

        The refresh handler rebuilds the request using only stored metadata
        (source_config, request_params, auth_hint) -- no loader state needed.
        '''
        vd.cache_manager.refresh(entry.cache_key, force=True)
        updated = vd.cache_manager.get(entry.cache_key)
        if not updated:
            vd.fail(f'refresh failed for {entry.cache_key}')
        return self.openCached(updated)


CacheSheet.addCommand(
    'Enter',
    'cache-open-cached',
    'vd.push(sheet.openCached(cursorRow))',
    'open cached file from local copy (offline-safe, no loader state needed)',
)

CacheSheet.addCommand(
    'zEnter',
    'cache-reload-source',
    'vd.push(sheet.reloadSource(cursorRow))',
    'reload original source via CacheManager refresh (uses stored metadata only)',
)

CacheSheet.addCommand(
    'gEnter',
    'cache-open-cached-selected',
    'for r in selectedRows: vd.push(sheet.openCached(r))',
    'open selected cached files locally',
)

CacheSheet.addCommand(
    'gzEnter',
    'cache-reload-sources',
    'for r in selectedRows: vd.push(sheet.reloadSource(r))',
    'reload original sources for selected rows via CacheManager refresh',
)

CacheSheet.addCommand(
    'Ctrl+R',
    'cache-refresh',
    'sheet.refresh_entries([cursorRow])',
    'refresh current cache entry via CacheManager.refresh()',
)

CacheSheet.addCommand(
    'gCtrl+R',
    'cache-refresh-all',
    'sheet.refresh_entries(selectedRows)',
    'refresh selected cache entries via CacheManager.refresh()',
)

CacheSheet.addCommand(
    'd',
    'cache-delete-row',
    'deleteRow(cursorRow)',
    'delete current cache entry via CacheManager.remove()',
)

CacheSheet.addCommand(
    'gd',
    'cache-clear-all',
    'sheet.clear_all_cache()',
    'clear all cache entries and files via CacheManager.clear_all()',
)

CacheSheet.addCommand(
    'y',
    'cache-yank-url',
    'copy(cursorRow.cache_key)',
    'copy cache key (original URL or API logical key) to clipboard',
)

CacheSheet.addCommand(
    'gy',
    'cache-yank-urls',
    "copy(chr(10).join(r.cache_key for r in selectedRows))",
    'copy all selected cache keys to clipboard',
)

CacheSheet.addCommand(
    'e',
    'cache-set-policy',
    'policy = input("cache policy (days:N|etag|last-modified|never): ", value=cursorRow.cache_policy); vd.cache_manager.refresh_policy(cursorRow.cache_key, policy); sheet.reload()',
    'edit cache invalidation policy for current row',
)


@VisiData.lazy_property
def cacheSheet(vd):
    return CacheSheet('cache', source=vd.cache_manager)


BaseSheet.addCommand(
    '',
    'open-cache',
    'vd.push(vd.cacheSheet)',
    'open Cache Sheet: view and manage cached remote resources',
)

TableSheet.addCommand(
    'z^R',
    'cache-refresh-source',
    "src = sheet.source\nif hasattr(src, 'is_url') and src.is_url():\n    vd.cache_manager.refresh(src.given, force=True)\n    sheet.reload()\nelse:\n    vd.fail('current sheet source is not a remote URL')",
    'drop cache for current sheet source and reload via CacheManager',
)


vd.addGlobals(CacheSheet=CacheSheet)
