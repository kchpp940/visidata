'Custom VisiData save format'

import json

from visidata import vd, VisiData, JsonSheet, Progress, IndexSheet, Path


NL='\n'


@VisiData.api
def open_vds(vd, p):
    return VdsIndexSheet(p.base_stem, source=p)


@VisiData.api
def save_vds(vd, p, *sheets):
    'Save in custom VisiData format, preserving columns and their attributes.'
    snap = vd.generate_snapshot(scope=list(sheets), include_cmdlog=False, include_macros=False, include_data=True)
    vd.write_snapshot(p, 'vds', snap)


class VdsIndexSheet(IndexSheet):
    def iterload(self):
        snap = vd.read_snapshot(self.source, fmt='vds')
        for name in snap.get('order', []):
            info = snap['sheets'].get(name, {})
            fpos = getattr(self.source, '_vds_fpos_cache', {}).get(name, 0)
            vs = VdsSheet(name, columns=[], source=self.source, source_fpos=fpos)
            if info.get('data'):
                vs.rows = info['data']
                vs._has_preloaded_data = True
            yield vs


class VdsSheet(JsonSheet):
    def newRow(self):
        return {}

    def iterload(self):
        if getattr(self, '_has_preloaded_data', False):
            for r in self.rows:
                yield r
            return

        snap = vd.read_snapshot(self.source, fmt='vds')
        info = snap['sheets'].get(self.name, {})
        _sheet_snapshot_columns = vd.getGlobals().get('_sheet_snapshot_columns')
        _restore_columns = vd.getGlobals().get('_restore_columns')
        if _restore_columns and info.get('columns'):
            _restore_columns(self, info['columns'])

        for r in info.get('data', []):
            yield r
