'Custom VisiData save format'

import json

from visidata import vd, VisiData, JsonSheet, Progress, IndexSheet, Path


NL='\n'


@VisiData.api
def open_vds(vd, p):
    if not isinstance(p, Path):
        p = Path(p)
    return VdsIndexSheet(p.base_stem, source=p)


@VisiData.api
def save_vds(vd, p, *sheets):
    'Save in custom VisiData format, preserving columns and their attributes.'
    if not isinstance(p, Path):
        p = Path(p)
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
                vs._preloaded_data = list(info['data'])
            vs._preloaded_columns = list(info.get('columns', []))
            yield vs


class VdsSheet(JsonSheet):
    def newRow(self):
        return {}

    def iterload(self):
        snap = vd.read_snapshot(self.source, fmt='vds')
        info = snap['sheets'].get(self.name, {})
        cols = getattr(self, '_preloaded_columns', None)
        if cols is None:
            cols = info.get('columns', [])
        if cols:
            _restore_columns = vd.getGlobals().get('_restore_columns')
            if _restore_columns:
                _restore_columns(self, cols)

        preloaded = getattr(self, '_preloaded_data', None)
        if preloaded is not None:
            for r in preloaded:
                yield r
            return

        for r in info.get('data', []):
            yield r
