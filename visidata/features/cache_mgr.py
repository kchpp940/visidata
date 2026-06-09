'''Cache Sheet -- view and manage cached remote resources.'''

import os
import time

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


# rowdef: CacheEntry
class CacheSheet(Sheet):
    '''View and manage cached remote resources (HTTP/S3/API).

    All cache operations go through vd.cache_manager -- this sheet does NOT
    maintain any cache state on its own.
    '''
    rowtype = 'cached files'
    columns = [
        Column('status', width=8, getter=lambda c, r: r.status),
        Column('source_type', width=8, getter=lambda c, r: r.source_type),
        Column('url', width=60, getter=lambda c, r: r.url),
        Column('local_path', width=40, getter=lambda c, r: r.local_path),
        Column('size', type=int, width=10,
               getter=lambda c, r: r.size,
               fmtstr=lambda col, row, val: vd.format_bytes(val)),
        Column('mtime', type=date, width=18, getter=lambda c, r: r.mtime),
        Column('last_accessed', type=date, width=18, getter=lambda c, r: r.last_accessed),
        Column('created_at', type=date, width=0, getter=lambda c, r: r.created_at),
        Column('cache_policy', width=12, getter=lambda c, r: r.cache_policy),
        Column('etag', width=0, getter=lambda c, r: r.etag[:20] if r.etag else ''),
        Column('last_modified', width=0, getter=lambda c, r: r.last_modified),
        Column('content_type', width=0, getter=lambda c, r: r.content_type),
        Column('status_msg', width=30, getter=lambda c, r: r.status_msg),
    ]
    nKeys = 1

    def iterload(self):
        for entry in sorted(vd.cache_manager.list(verify=True), key=lambda e: e.last_accessed, reverse=True):
            yield entry

    def commitDeleteRow(self, row):
        vd.cache_manager.remove(row.url)

    def newRow(self):
        vd.fail('new cache entries not supported')

    @asyncthread
    def refresh_entries(self, rows):
        '''Refresh selected entries.  Dispatches to CacheManager.refresh().'''
        for entry in vd.Progress(rows, gerund='refreshing'):
            vd.cache_manager.refresh(entry.url, force=True)
        self.reload()

    @asyncthread
    def clear_all_cache(self):
        n = vd.cache_manager.clear_all()
        vd.status(f'cleared {n} cache entries')
        self.reload()

    def openCached(self, entry):
        '''Open the local cached copy.  Uses CacheManager.open_local() exclusively.'''
        local = vd.cache_manager.open_local(entry.url)
        filetype = local.ext or 'txt'
        vs = vd.openSource(local, filetype=filetype)
        vs.name = f'{entry.source_type}:{os.path.basename(entry.local_path)}'
        return vs

    def reloadSource(self, entry):
        '''Drop cache and reload from the original source.'''
        vd.cache_manager.remove(entry.url)
        return vd.openSource(entry.url)


CacheSheet.addCommand(
    'Enter',
    'cache-open-cached',
    'vd.push(sheet.openCached(cursorRow))',
    'open cached file from local copy (offline-safe)',
)

CacheSheet.addCommand(
    'zEnter',
    'cache-reload-source',
    'vd.push(sheet.reloadSource(cursorRow))',
    'reload original URL/S3 source (drops cache first)',
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
    'reload original sources for selected rows',
)

CacheSheet.addCommand(
    'Ctrl+R',
    'cache-refresh',
    'sheet.refresh_entries([cursorRow])',
    'refresh current cache entry via CacheManager',
)

CacheSheet.addCommand(
    'gCtrl+R',
    'cache-refresh-all',
    'sheet.refresh_entries(selectedRows)',
    'refresh selected cache entries via CacheManager',
)

CacheSheet.addCommand(
    'd',
    'cache-delete-row',
    'deleteRow(cursorRow)',
    'delete current cache entry via CacheManager',
)

CacheSheet.addCommand(
    'gd',
    'cache-clear-all',
    'sheet.clear_all_cache()',
    'clear all cache entries and files via CacheManager',
)

CacheSheet.addCommand(
    'y',
    'cache-yank-url',
    'copy(cursorRow.url)',
    'copy original URL to clipboard',
)

CacheSheet.addCommand(
    'gy',
    'cache-yank-urls',
    "copy(chr(10).join(r.url for r in selectedRows))",
    'copy all selected URLs to clipboard',
)

CacheSheet.addCommand(
    'e',
    'cache-set-policy',
    'policy = input("cache policy (days:N|etag|last-modified|never): ", value=cursorRow.cache_policy); vd.cache_manager.refresh_policy(cursorRow.url, policy); sheet.reload()',
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
    'drop cache for current sheet source and reload',
)


vd.addGlobals(CacheSheet=CacheSheet)
