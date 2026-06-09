import json
import re

import visidata
from visidata import VisiData, CommandLogBase, BaseSheet, Sheet, AttrDict, Progress

VDX_VD_COLUMNS = ['sheet', 'col', 'row', 'longname', 'input', 'keystrokes', 'comment']


@VisiData.api
def open_vdx(vd, p):
    return CommandLogSimple(p.base_stem, source=p, precious=True)


VDX_CONTEXT_COMMANDS = {'sheet', 'col', 'row'}

class CommandLogSimple(CommandLogBase, Sheet):
    filetype = 'vdx'
    def iterload(self):
        context = {}  # pending sheet/col/row for next command
        for line in self.source:
            if not line or line[0] == '#':
                continue
            if line[0] == '{':
                # .vdj json line
                yield AttrDict(json.loads(line))
                context = {}
            elif '\t' in line:
                # .vd tsv line; skip header
                fields = line.split('\t')
                if fields == VDX_VD_COLUMNS[:len(fields)]:
                    continue
                d = {k: v for k, v in zip(VDX_VD_COLUMNS, fields) if v}
                yield AttrDict(d)
                context = {}
            else:
                # .vdx minimal line
                longname, *rest = line.split(' ', maxsplit=1)
                if longname == 'replay-reset':
                    context = {}
                    yield AttrDict(longname=longname,
                                   input=rest[0] if rest else '')
                elif longname in VDX_CONTEXT_COMMANDS:
                    context[longname] = rest[0] if rest else ''
                elif longname == 'option':
                    # option scope name value -> set-option
                    parts = (rest[0] if rest else '').split(' ', maxsplit=2)
                    scope = parts[0] if len(parts) > 0 else 'global'
                    name = parts[1] if len(parts) > 1 else ''
                    value = parts[2] if len(parts) > 2 else ''
                    yield AttrDict(longname='set-option',
                                   sheet=scope, col='', row=name, input=value)
                else:
                    yield AttrDict(longname=longname,
                                   input=rest[0] if rest else '',
                                   **context)
                    context = {}


@VisiData.api
def save_vdx(vd, p, *vsheets):
    snap = {
        'version': visidata.__version_info__,
        'cmdlog': [],
    }
    for vs in vsheets:
        for r in vs.rows:
            snap['cmdlog'].append({k: getattr(r, k, '') for k in ('sheet', 'col', 'row', 'longname', 'input', 'keystrokes', 'comment')})
    vd.write_snapshot(p, 'vdx', snap, encoding=vsheets[0].options.save_encoding if vsheets else 'utf-8')


@VisiData.api
def runvdx(vd, vdx:str):
    for line in Progress(vdx.splitlines()):
        vs = vd.sheet or Sheet()
        vd.sync(vs.ensureLoaded())
        line = line.strip()
        if not line or line[0] == '#':
            continue

        m = re.match(r'^(\+(\S+) )?(\S+)(.*)$', line)
        if not m:
            print('bad:', line)
            continue

        _, pos, longname, rest = m.groups()
        vd.currentReplayRow = AttrDict(longname=longname, input=rest)
        if pos:
            vd.moveToPos(vd.sheets, *vd.parsePos(pos))
        print(vs.name, longname)
        vs.execCommand(longname)
        vd.sync()


