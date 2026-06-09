'Custom VisiData save format'

import json

from visidata import vd, VisiData, JsonSheet, Progress, IndexSheet
from visidata.snapshot import _sheet_snapshot_columns, _restore_columns


NL='\n'

@VisiData.api
def open_vds(vd, p):
    return VdsIndexSheet(p.base_stem, source=p)


@VisiData.api
def save_vds(vd, p, *sheets):
    'Save in custom VisiData format, preserving columns and their attributes.'

    with p.open(mode='w', encoding='utf-8') as fp:
        for vs in sheets:
            d = {'name': vs.name}
            fp.write('#'+json.dumps(d)+NL)

            for d in _sheet_snapshot_columns(vs):
                fp.write('#'+json.dumps(d)+NL)

            if not vs.rows:
                fp.write(NL)  #2342  blank line to separate sheets without rows
                continue

            with Progress(gerund='saving'):
                for row in vs.iterdispvals(*vs.columns, format=False):
                    d = {col.name:val for col, val in row.items()}
                    fp.write(json.dumps(d, default=str)+NL)


class VdsIndexSheet(IndexSheet):
    def iterload(self):
        vs = None
        with self.source.open(encoding='utf-8') as fp:
            line = fp.readline()
            while line:
                if line.startswith('#{'):
                    d = json.loads(line[1:])
                    if 'col' not in d:
                        vs = VdsSheet(d.pop('name'), columns=[], source=self.source, source_fpos=fp.tell())
                        yield vs
                line = fp.readline()


class VdsSheet(JsonSheet):
    def newRow(self):
        return {}   # rowdef: dict

    def iterload(self):
        self.colnames = {}
        self.columns = []

        with self.source.open(encoding='utf-8') as fp:
            fp.seek(self.source_fpos)

            col_states = []
            line = fp.readline()
            while line and line.startswith('#{'):
                d = json.loads(line[1:])
                if 'col' not in d:
                    raise Exception(d)
                col_states.append(d)
                line = fp.readline()

            _restore_columns(self, col_states)
            for c in self.columns:
                self.colnames[c.name] = c

            while line and not line.startswith('#{'):
                d = json.loads(line)
                yield d
                line = fp.readline()
