"""Unified Workspace Snapshot layer.

Two-layer architecture
======================

Layer 1  -- Collect
    ``vd.generate_snapshot()`` produces a single snapshot dict containing
    every piece of recoverable workspace state: sheets, columns, options,
    graph state, macros, and the command log.

Layer 2  -- Read / Write / Restore
    ``vd.write_snapshot(path, fmt, snap)`` dispatches to a registered
    per-format writer.
    ``vd.read_snapshot(path, fmt)`` dispatches to a registered per-format
    reader and returns a snapshot dict.
    ``vd.load_snapshot(snap)`` restores the workspace from the dict in a
    canonical, format-independent order.

Every on-disk format (vds, vdj, vd, vdx, macro, delivery-package, manifest)
plugs in as a reader/writer pair.  No format needs to know how to collect
state or how to restore it -- those concerns live exclusively in Layer 1.
"""

import json
import os
import stat
import shutil
import tempfile
import zipfile

import visidata
from visidata import vd, VisiData, BaseSheet, TableSheet, UNLOADED
from visidata import Column, SettableColumn, ItemColumn, ExprColumn, AttrDict, Path, Progress
from visidata import IndexSheet


SNAPSHOT_VERSION_KEY = 'version'
SNAPSHOT_ORDER_KEY = 'order'
SNAPSHOT_SHEETS_KEY = 'sheets'
SNAPSHOT_GLOBAL_OPTIONS_KEY = 'global_options'
SNAPSHOT_MACROS_KEY = 'macros'
SNAPSHOT_CMDLOG_KEY = 'cmdlog'
SNAPSHOT_DATA_KEY = 'data'

GRAPH_STATE_KEYS = ('reflines_x', 'reflines_y', 'xzoomlevel', 'yzoomlevel')

CMDLOG_FIELDS = ('sheet', 'col', 'row', 'longname', 'input', 'keystrokes', 'comment')


_snapshot_writers = {}
_snapshot_readers = {}


def register_snapshot_format(fmt, writer=None, reader=None):
    """Register (or decorate) writer/reader functions for a snapshot format."""
    def _reg(fn):
        if writer is None and 'write' in fn.__name__:
            _snapshot_writers[fmt] = fn
        elif reader is None and 'read' in fn.__name__:
            _snapshot_readers[fmt] = fn
        return fn
    if writer:
        _snapshot_writers[fmt] = writer
    if reader:
        _snapshot_readers[fmt] = reader
    return _reg


# ---------------------------------------------------------------------------
# Layer 1  --  Collect
# ---------------------------------------------------------------------------

def _sheet_snapshot_options(vs):
    out = {}
    for optname in vs.options.keys(vs):
        opt = vs.options._get(optname, vs)
        default = vs.options.getdefault(optname)
        if opt.value != default:
            out[optname] = opt.value
    return out


def _global_snapshot_options():
    out = {}
    for optname in vd.options.keys('global'):
        opt = vd.options._get(optname, 'global')
        default = vd.options.getdefault(optname)
        if opt.value != default:
            out[optname] = opt.value
    return out


def _sheet_snapshot_graph(vs):
    state = {}
    for k in GRAPH_STATE_KEYS:
        if hasattr(vs, k):
            v = getattr(vs, k)
            try:
                json.dumps(v, default=str)
                state[k] = v
            except (TypeError, ValueError):
                pass
    return state or None


def _sheet_snapshot_columns(vs):
    out = []
    for col in vs.columns:
        if not hasattr(col, '__getstate__'):
            continue
        d = col.__getstate__()
        if isinstance(col, SettableColumn) and not isinstance(col, ItemColumn):
            d['col'] = 'Column'
        elif isinstance(col, ItemColumn):
            d['col'] = 'Column'
            d['expr'] = col.name
        elif isinstance(col, ExprColumn):
            d['col'] = 'ExprColumn'
        else:
            d['col'] = type(col).__name__
        out.append(d)
    return out


def _sheet_snapshot_data(vs):
    """Return list of row-dicts for the given sheet."""
    if not getattr(vs, 'rows', None):
        return []
    rows = []
    for row in vs.iterdispvals(*vs.columns, format=False):
        rows.append({col.name: val for col, val in row.items()})
    return rows


def _collect_derived_sheets(root):
    out = []
    for vs in vd.allSheets:
        src = getattr(vs, 'source', None)
        while isinstance(src, BaseSheet):
            if src is root:
                out.append(vs)
                break
            src = getattr(src, 'source', None)
    return out


def _cmdlog_row_to_dict(r):
    return {k: getattr(r, k, '') for k in CMDLOG_FIELDS}


def _cmdlog_rows_from_list(L):
    return [_cmdlog_row_to_dict(r) if not isinstance(r, dict) else dict(r) for r in L]


@VisiData.api
def generate_snapshot(vd, scope='current', include_cmdlog=True, include_macros=True,
                      include_data=False):
    """Build a workspace snapshot dict.

    *scope*
        ``"current"``  -- active sheet plus derived sheets
        ``"all"``      -- every sheet on the stack
        ``"selected"`` -- (IndexSheet only) selected rows as sheets
        otherwise      -- iterable of sheet objects
    *include_data*
        When True, embed per-sheet data rows under ``snap["sheets"][name]["data"]``.
        Formats that store data externally (e.g. delivery-package) should leave
        this False and use the writer to emit sidecar files.
    """
    if isinstance(scope, str):
        if scope == 'current':
            sheets = [vd.activeSheet] if vd.activeSheet else []
            for vs in list(sheets):
                sheets.extend(_collect_derived_sheets(vs))
        elif scope == 'all':
            sheets = list(vd.stackedSheets)
        elif scope == 'selected':
            if isinstance(vd.activeSheet, IndexSheet):
                sheets = list(vd.activeSheet.selectedRows)
            else:
                sheets = [vd.activeSheet] if vd.activeSheet else []
        else:
            sheets = []
    else:
        sheets = list(scope)

    vd.sync(*vd.ensureLoaded([vs for vs in sheets if vs and getattr(vs, 'rows', UNLOADED) is UNLOADED]))

    sheets_dict = {}
    order = []
    for vs in sheets:
        if vs is None:
            continue
        if vs.name in sheets_dict:
            continue
        source_sheet = None
        src = getattr(vs, 'source', None)
        if isinstance(src, BaseSheet):
            source_sheet = src.name
        entry = {
            'class': type(vs).__name__,
            'columns': _sheet_snapshot_columns(vs),
            'options': _sheet_snapshot_options(vs),
            'graph_state': _sheet_snapshot_graph(vs),
            'source_sheet': source_sheet,
        }
        if include_data:
            entry['data'] = _sheet_snapshot_data(vs)
        sheets_dict[vs.name] = entry
        order.append(vs.name)

    snap = {
        SNAPSHOT_VERSION_KEY: visidata.__version_info__,
        SNAPSHOT_ORDER_KEY: order,
        SNAPSHOT_SHEETS_KEY: sheets_dict,
        SNAPSHOT_GLOBAL_OPTIONS_KEY: _global_snapshot_options(),
    }

    if include_cmdlog and getattr(vd, 'cmdlog', None):
        snap[SNAPSHOT_CMDLOG_KEY] = _cmdlog_rows_from_list(vd.cmdlog.rows)

    if include_macros:
        macros = []
        for binding, cmdlog in getattr(vd, 'macrobindings', {}).items():
            macros.append({
                'binding': binding,
                'keystroke': getattr(cmdlog, 'keystroke', ''),
                'helpstr': getattr(cmdlog, 'helpstr', ''),
                'source': getattr(cmdlog, 'source', ''),
                'rows': _cmdlog_rows_from_list(getattr(cmdlog, 'rows', [])),
            })
        snap[SNAPSHOT_MACROS_KEY] = macros

    return snap


# ---------------------------------------------------------------------------
# Layer 2  --  Restore
# ---------------------------------------------------------------------------

def _restore_columns(vs, col_states):
    existing_names = {c.name for c in vs.columns}
    for d in col_states:
        d2 = dict(d)
        classname = d2.pop('col', 'ItemColumn')
        name = d2.get('name', '')
        if classname == 'Column':
            classname = 'ItemColumn'
            d2.setdefault('expr', name)
        cls = vd.getGlobals().get(classname, ItemColumn)
        c = cls(name, sheet=vs)
        vs.addColumn(c)
        if hasattr(c, '__setstate__'):
            c.__setstate__(d2)


def _restore_data_rows(vs, rows):
    if not rows:
        return
    vs.rows = []
    for r in rows:
        if isinstance(r, dict):
            vs.addRow(r)
        else:
            vs.addRow(r)


def _restore_graph_state(vs, state):
    for k, v in (state or {}).items():
        if hasattr(vs, k):
            try:
                setattr(vs, k, v)
            except Exception:
                pass


def _restore_sheet_options(vs, opts):
    for k, v in (opts or {}).items():
        try:
            vs.options.set(k, v, vs, cmdlog=False)
        except Exception:
            pass


def _restore_global_options(opts):
    for k, v in (opts or {}).items():
        try:
            vd.options.set(k, v, 'global', cmdlog=False)
        except Exception:
            pass


@VisiData.api
def load_snapshot(vd, snap, load_order=None, apply_cmdlog=True, apply_macros=True,
                  apply_options=True, apply_data=True, existing_sheets=None):
    """Restore workspace from a snapshot dict.

    Canonical restore order:
      global options → macros → sheets (data → columns → options → graph) → cmdlog
    """
    snap = snap or {}
    if existing_sheets is None:
        existing_sheets = {}

    if apply_options:
        _restore_global_options(snap.get(SNAPSHOT_GLOBAL_OPTIONS_KEY, {}))

    if apply_macros:
        from visidata.cmdlog import CommandLogJsonl
        for m in snap.get(SNAPSHOT_MACROS_KEY, []) or []:
            try:
                rows = [AttrDict(r) for r in m.get('rows', [])]
                vs = CommandLogJsonl(m.get('binding', 'macro'), rows=rows)
                vs.keystroke = m.get('keystroke', '')
                vs.helpstr = m.get('helpstr', '')
                vs.source = m.get('source', '')
                vd.setMacro(m['binding'], vs, helpstr=vs.helpstr, keystroke=vs.keystroke)
            except Exception as e:
                vd.warning(f'failed to restore macro {m.get("binding")}: {e}')

    sheets_info = snap.get(SNAPSHOT_SHEETS_KEY, {}) or {}
    order = load_order or snap.get(SNAPSHOT_ORDER_KEY, []) or list(sheets_info.keys())

    restored = {}
    for name in order:
        info = sheets_info.get(name)
        if not info:
            continue
        vs = existing_sheets.get(name)
        if vs is None:
            clsname = info.get('class', 'TableSheet')
            cls = vd.getGlobals().get(clsname, TableSheet)
            try:
                vs = cls(name)
            except Exception:
                vs = TableSheet(name)
            vs.rows = []
            vd.push(vs, load=False)
        restored[name] = vs
        if apply_data and info.get('data'):
            _restore_data_rows(vs, info['data'])
        if apply_options:
            _restore_sheet_options(vs, info.get('options'))
        if info.get('columns') and hasattr(vs, 'addColumn'):
            _restore_columns(vs, info['columns'])
        if info.get('graph_state'):
            _restore_graph_state(vs, info['graph_state'])

    for name, info in sheets_info.items():
        src_name = info.get('source_sheet')
        vs = restored.get(name)
        if vs and src_name and src_name in restored:
            try:
                vs.source = restored[src_name]
            except Exception:
                pass

    if apply_cmdlog and snap.get(SNAPSHOT_CMDLOG_KEY):
        from visidata.cmdlog import CommandLogJsonl
        rows = [AttrDict(r) for r in snap[SNAPSHOT_CMDLOG_KEY]]
        cl = CommandLogJsonl('snapshot_cmdlog', rows=rows)
        vd.replay_sync(cl)

    return restored


# ---------------------------------------------------------------------------
# Layer 2  --  Write / Read dispatch
# ---------------------------------------------------------------------------

@VisiData.api
def write_snapshot(vd, path, fmt, snap, **kwargs):
    """Write *snap* to *path* using the registered writer for *fmt*.

    Available formats: vds, vdj, vd, vdx, macro, manifest, delivery
    """
    writer = _snapshot_writers.get(fmt)
    if writer is None:
        vd.fail(f'no snapshot writer for format `{fmt}`')
    return writer(path, snap, **kwargs)


@VisiData.api
def read_snapshot(vd, path, fmt=None, **kwargs):
    """Read a snapshot dict from *path* using the registered reader for *fmt*.

    If *fmt* is None, it is inferred from the file extension.
    """
    if fmt is None:
        if isinstance(path, Path):
            fmt = path.ext
        else:
            ext = os.path.splitext(str(path))[1].lstrip('.').lower()
            fmt = ext
    reader = _snapshot_readers.get(fmt)
    if reader is None:
        vd.fail(f'no snapshot reader for format `{fmt}`')
    snap = reader(path, **kwargs)
    return snap


# ---------------------------------------------------------------------------
# Format:  vds   (inline columns + data in JSONL)
# ---------------------------------------------------------------------------

@register_snapshot_format('vds')
def write_snapshot_vds(path, snap, **kwargs):
    NL = '\n'
    with Path(path).open(mode='w', encoding='utf-8') as fp:
        order = snap.get(SNAPSHOT_ORDER_KEY, [])
        sheets = snap.get(SNAPSHOT_SHEETS_KEY, {})
        for name in order:
            info = sheets.get(name)
            if not info:
                continue
            fp.write('#' + json.dumps({'name': name}) + NL)
            for d in info.get('columns', []):
                fp.write('#' + json.dumps(d) + NL)
            data_rows = info.get('data') or []
            if not data_rows:
                fp.write(NL)
                continue
            for r in data_rows:
                fp.write(json.dumps(r, default=str) + NL)


@register_snapshot_format('vds')
def read_snapshot_vds(path, **kwargs):
    snap = {SNAPSHOT_VERSION_KEY: visidata.__version_info__,
            SNAPSHOT_ORDER_KEY: [],
            SNAPSHOT_SHEETS_KEY: {}}
    current_name = None
    with Path(path).open(encoding='utf-8') as fp:
        for line in fp:
            if line.startswith('#{'):
                d = json.loads(line[1:])
                if 'col' not in d:
                    current_name = d.pop('name')
                    snap[SNAPSHOT_ORDER_KEY].append(current_name)
                    snap[SNAPSHOT_SHEETS_KEY][current_name] = {
                        'class': 'TableSheet',
                        'columns': [],
                        'options': {},
                        'graph_state': None,
                        'source_sheet': None,
                        'data': [],
                    }
                elif current_name:
                    snap[SNAPSHOT_SHEETS_KEY][current_name]['columns'].append(d)
            elif line.strip() and current_name:
                d = json.loads(line)
                snap[SNAPSHOT_SHEETS_KEY][current_name]['data'].append(d)
    return snap


# ---------------------------------------------------------------------------
# Format:  vdj   (JSONL command log)
# ---------------------------------------------------------------------------

@register_snapshot_format('vdj')
def write_snapshot_vdj(path, snap, encoding='utf-8', **kwargs):
    with Path(path).open(mode='w', encoding=encoding) as fp:
        fp.write("#!/usr/bin/env -S vd -p\n")
        fp.write(f"# {visidata.__version_info__}\n")
        for r in snap.get(SNAPSHOT_CMDLOG_KEY, []) or []:
            fp.write(json.dumps(r, default=str) + '\n')


@register_snapshot_format('vdj')
def read_snapshot_vdj(path, **kwargs):
    rows = []
    with Path(path).open(encoding='utf-8') as fp:
        for line in fp:
            if not line or line.startswith('#'):
                continue
            rows.append(json.loads(line))
    return {SNAPSHOT_VERSION_KEY: visidata.__version_info__,
            SNAPSHOT_CMDLOG_KEY: rows,
            SNAPSHOT_ORDER_KEY: [],
            SNAPSHOT_SHEETS_KEY: {}}


# ---------------------------------------------------------------------------
# Format:  vd    (TSV command log)
# ---------------------------------------------------------------------------

VD_VDJ_COLUMNS = ['sheet', 'col', 'row', 'longname', 'input', 'keystrokes', 'comment']


@register_snapshot_format('vd')
def write_snapshot_vd(path, snap, encoding='utf-8', **kwargs):
    import csv
    with Path(path).open(mode='w', encoding=encoding, newline='') as fp:
        writer = csv.writer(fp, delimiter='\t')
        writer.writerow(VD_VDJ_COLUMNS)
        for r in snap.get(SNAPSHOT_CMDLOG_KEY, []) or []:
            writer.writerow([str(r.get(k, '')) for k in VD_VDJ_COLUMNS])


@register_snapshot_format('vd')
def read_snapshot_vd(path, **kwargs):
    import csv
    rows = []
    with Path(path).open(encoding='utf-8', newline='') as fp:
        reader = csv.DictReader(fp, delimiter='\t')
        for r in reader:
            rows.append(r)
    return {SNAPSHOT_VERSION_KEY: visidata.__version_info__,
            SNAPSHOT_CMDLOG_KEY: rows,
            SNAPSHOT_ORDER_KEY: [],
            SNAPSHOT_SHEETS_KEY: {}}


# ---------------------------------------------------------------------------
# Format:  vdx   (minimal command log)
# ---------------------------------------------------------------------------

VDX_CONTEXT_COMMANDS = {'sheet', 'col', 'row'}


@register_snapshot_format('vdx')
def write_snapshot_vdx(path, snap, encoding='utf-8', **kwargs):
    with Path(path).open(mode='w', encoding=encoding) as fp:
        fp.write("#!/usr/bin/env -S vd -p\n")
        fp.write(f"# {visidata.__version_info__}\n")
        prevrow = None
        for r in snap.get(SNAPSHOT_CMDLOG_KEY, []) or []:
            if prevrow is not None and r.get('sheet') and prevrow.get('sheet') != r.get('sheet'):
                fp.write(f"sheet {r['sheet']}\n")
            if r.get('col') and (prevrow is None or prevrow.get('col') != r.get('col')):
                fp.write(f"col {r['col']}\n")
            if r.get('row') and (prevrow is None or prevrow.get('row') != r.get('row')):
                fp.write(f"row {r['row']}\n")
            line = r.get('longname', '')
            if r.get('input'):
                line += ' ' + str(r['input'])
            fp.write(line + '\n')
            prevrow = r


@register_snapshot_format('vdx')
def read_snapshot_vdx(path, **kwargs):
    rows = []
    context = {}
    with Path(path).open(encoding='utf-8') as fp:
        for line in fp:
            line = line.rstrip('\n\r')
            if not line or line[0] == '#':
                continue
            if line[0] == '{':
                rows.append(json.loads(line))
                context = {}
                continue
            if '\t' in line:
                fields = line.split('\t')
                if fields == VD_VDJ_COLUMNS[:len(fields)]:
                    continue
                d = {k: v for k, v in zip(VD_VDJ_COLUMNS, fields) if v}
                rows.append(d)
                context = {}
                continue
            longname, *rest = line.split(' ', maxsplit=1)
            if longname == 'replay-reset':
                context = {}
                rows.append({'longname': longname, 'input': rest[0] if rest else ''})
            elif longname in VDX_CONTEXT_COMMANDS:
                context[longname] = rest[0] if rest else ''
            elif longname == 'option':
                parts = (rest[0] if rest else '').split(' ', maxsplit=2)
                scope = parts[0] if len(parts) > 0 else 'global'
                name = parts[1] if len(parts) > 1 else ''
                value = parts[2] if len(parts) > 2 else ''
                rows.append({'longname': 'set-option', 'sheet': scope, 'col': '', 'row': name, 'input': value})
            else:
                d = {'longname': longname, 'input': rest[0] if rest else ''}
                d.update(context)
                rows.append(d)
                context = {}
    return {SNAPSHOT_VERSION_KEY: visidata.__version_info__,
            SNAPSHOT_CMDLOG_KEY: rows,
            SNAPSHOT_ORDER_KEY: [],
            SNAPSHOT_SHEETS_KEY: {}}


# ---------------------------------------------------------------------------
# Format:  macro   (single-macro vdj)
# ---------------------------------------------------------------------------

@register_snapshot_format('macro')
def write_snapshot_macro(path, snap, binding=None, encoding='utf-8', **kwargs):
    macros = snap.get(SNAPSHOT_MACROS_KEY, []) or []
    rows = []
    if binding:
        for m in macros:
            if m.get('binding') == binding:
                rows = m.get('rows', [])
                break
    elif macros:
        rows = macros[0].get('rows', [])
    with Path(path).open(mode='w', encoding=encoding) as fp:
        fp.write("#!/usr/bin/env -S vd -p\n")
        fp.write(f"# {visidata.__version_info__}\n")
        for r in rows:
            fp.write(json.dumps(r, default=str) + '\n')


@register_snapshot_format('macro')
def read_snapshot_macro(path, binding=None, **kwargs):
    snap = read_snapshot_vdj(path, **kwargs)
    rows = snap.get(SNAPSHOT_CMDLOG_KEY, [])
    macros = [{'binding': binding or Path(path).stem,
               'keystroke': '', 'helpstr': '', 'source': str(path),
               'rows': rows}]
    return {SNAPSHOT_VERSION_KEY: visidata.__version_info__,
            SNAPSHOT_MACROS_KEY: macros,
            SNAPSHOT_ORDER_KEY: [],
            SNAPSHOT_SHEETS_KEY: {}}


# ---------------------------------------------------------------------------
# Format:  manifest   (just metadata, no data / cmdlog)
# ---------------------------------------------------------------------------

@register_snapshot_format('manifest')
def write_snapshot_manifest(path, snap, **kwargs):
    manifest = {
        SNAPSHOT_VERSION_KEY: snap.get(SNAPSHOT_VERSION_KEY),
        SNAPSHOT_ORDER_KEY: snap.get(SNAPSHOT_ORDER_KEY, []),
        SNAPSHOT_SHEETS_KEY: snap.get(SNAPSHOT_SHEETS_KEY, {}),
    }
    if snap.get(SNAPSHOT_GLOBAL_OPTIONS_KEY):
        manifest[SNAPSHOT_GLOBAL_OPTIONS_KEY] = snap[SNAPSHOT_GLOBAL_OPTIONS_KEY]
    if snap.get(SNAPSHOT_MACROS_KEY):
        manifest[SNAPSHOT_MACROS_KEY] = snap[SNAPSHOT_MACROS_KEY]
    with Path(path).open(mode='w', encoding='utf-8') as fp:
        json.dump(manifest, fp, default=str, indent=2)


@register_snapshot_format('manifest')
def read_snapshot_manifest(path, **kwargs):
    with Path(path).open(encoding='utf-8') as fp:
        data = json.load(fp)
    data.setdefault(SNAPSHOT_ORDER_KEY, list(data.get(SNAPSHOT_SHEETS_KEY, {}).keys()))
    data.setdefault(SNAPSHOT_SHEETS_KEY, {})
    return data


# ---------------------------------------------------------------------------
# Helpers for delivery-package format
# ---------------------------------------------------------------------------

def _sanitize_filename(name):
    keep = (' ', '.', '_', '-')
    return ''.join(c if c.isalnum() or c in keep else '_' for c in str(name)).strip() or 'sheet'


def _delivery_readme(snap, data_format):
    sheet_names = snap.get(SNAPSHOT_ORDER_KEY, [])
    lines = [
        '# VisiData Delivery Package',
        '',
        f'Generated by {snap.get(SNAPSHOT_VERSION_KEY, "VisiData")}.',
        '',
        '## Contents',
        '',
        f'- Data format: **{data_format}**',
        f'- Sheets ({len(sheet_names)}):',
    ]
    for n in sheet_names:
        lines.append(f'  - `{n}`')
    lines.extend([
        '',
        '## Quick start',
        '',
        '```bash',
        '# Replay the full session (requires VisiData):',
        '  ./start.sh',
        '',
        '# Or open the data directly:',
        '  vd data/',
        '```',
        '',
    ])
    return '\n'.join(lines)


def _delivery_start_sh():
    return (
        '#!/usr/bin/env bash\n'
        'set -e\n'
        'cd "$(dirname "$0")"\n'
        'if [ -f replay.vdj ] && command -v vd >/dev/null 2>&1; then\n'
        '  vd -p replay.vdj\n'
        'elif command -v vd >/dev/null 2>&1; then\n'
        '  vd data/\n'
        'else\n'
        '  echo "VisiData is not installed.  See https://www.visidata.org/"\n'
        '  exit 1\n'
        'fi\n'
    )


def _delivery_start_bat():
    return (
        '@echo off\r\n'
        'cd /d "%~dp0"\r\n'
        'where vd >nul 2>&1\r\n'
        'if %errorlevel%==0 (\r\n'
        '  if exist replay.vdj (\r\n'
        '    vd -p replay.vdj\r\n'
        '  ) else (\r\n'
        '    vd data/\r\n'
        '  )\r\n'
        ') else (\r\n'
        '  echo VisiData is not installed.  See https://www.visidata.org/\r\n'
        '  exit /b 1\r\n'
        ')\r\n'
    )


def _write_sheet_data_files(pkgdir, snap, data_format, data_sheets=None):
    """Write per-sheet data files into pkgdir/data/.

    *data_sheets* -- optional mapping {sheet_name: sheet_obj}.  If provided,
    data is sourced from the live sheet objects; otherwise embedded
    ``snap["sheets"][name]["data"]`` is used (falling back to vds format for
    live sheets).
    """
    data_dir = os.path.join(pkgdir, 'data')
    os.makedirs(data_dir, exist_ok=True)
    data_files = {}

    order = snap.get(SNAPSHOT_ORDER_KEY, [])
    sheets_info = snap.get(SNAPSHOT_SHEETS_KEY, {})

    for name in order:
        info = sheets_info.get(name)
        if not info:
            continue
        safe = _sanitize_filename(name)
        fn = f'{safe}.{data_format}'
        target_path = Path(os.path.join(data_dir, fn))
        data_files[name] = f'data/{fn}'

        vs = data_sheets.get(name) if data_sheets else None

        if data_format == 'vds':
            sheet_snap = {
                SNAPSHOT_VERSION_KEY: snap.get(SNAPSHOT_VERSION_KEY),
                SNAPSHOT_ORDER_KEY: [name],
                SNAPSHOT_SHEETS_KEY: {
                    name: {
                        'class': info.get('class', 'TableSheet'),
                        'columns': info.get('columns', []),
                        'options': {},
                        'graph_state': None,
                        'source_sheet': None,
                        'data': info.get('data') or _sheet_snapshot_data(vs) if vs else [],
                    }
                },
            }
            write_snapshot_vds(target_path, sheet_snap)
        elif vs is not None and hasattr(vd, f'save_{data_format}'):
            vd.sync(getattr(vd, f'save_{data_format}')(target_path, vs))
        elif vs is not None and data_format == 'tsv':
            vd.sync(vd.save_tsv(target_path, vs))
        elif info.get('data'):
            with target_path.open(mode='w', encoding='utf-8') as fp:
                for r in info['data']:
                    fp.write(json.dumps(r, default=str) + '\n')

    return data_files


# ---------------------------------------------------------------------------
# Format:  delivery   (directory or zip package)
# ---------------------------------------------------------------------------

@register_snapshot_format('delivery')
def write_snapshot_delivery(path, snap, data_format='vds', data_sheets=None, **kwargs):
    """Write a delivery package.  *path* is either a directory or a .zip file.

    *data_sheets* -- optional ``{name: sheet_obj}`` mapping providing live
    sheet objects whose data should be emitted via the format-specific
    saver (e.g. vd.save_tsv).  When omitted, data comes from the embedded
    ``snap["sheets"][name]["data"]`` (and only vds/jsonl formats are
    supported).
    """
    path_str = str(path)
    is_zip = path_str.lower().endswith('.zip')

    if is_zip:
        workdir = tempfile.mkdtemp(prefix='vd_pkg_')
    else:
        workdir = path_str
        os.makedirs(workdir, exist_ok=True)

    try:
        data_files = _write_sheet_data_files(workdir, snap, data_format, data_sheets=data_sheets)

        if snap.get(SNAPSHOT_CMDLOG_KEY):
            replay_snap = {SNAPSHOT_CMDLOG_KEY: snap[SNAPSHOT_CMDLOG_KEY]}
            write_snapshot_vdj(os.path.join(workdir, 'replay.vdj'), replay_snap)

        manifest = {
            SNAPSHOT_VERSION_KEY: snap.get(SNAPSHOT_VERSION_KEY),
            'data_format': data_format,
            SNAPSHOT_ORDER_KEY: snap.get(SNAPSHOT_ORDER_KEY, []),
            'sheets': [],
        }
        for name in snap.get(SNAPSHOT_ORDER_KEY, []):
            info = snap.get(SNAPSHOT_SHEETS_KEY, {}).get(name, {})
            entry = {
                'name': name,
                'class': info.get('class', 'TableSheet'),
                'data_file': data_files.get(name),
            }
            if info.get('options'):
                entry['options'] = info['options']
            if info.get('columns'):
                entry['columns'] = info['columns']
            if info.get('graph_state'):
                entry['graph_state'] = info['graph_state']
            if info.get('source_sheet'):
                entry['source_sheet'] = info['source_sheet']
            manifest['sheets'].append(entry)
        if snap.get(SNAPSHOT_GLOBAL_OPTIONS_KEY):
            manifest[SNAPSHOT_GLOBAL_OPTIONS_KEY] = snap[SNAPSHOT_GLOBAL_OPTIONS_KEY]
        if snap.get(SNAPSHOT_MACROS_KEY):
            manifest[SNAPSHOT_MACROS_KEY] = snap[SNAPSHOT_MACROS_KEY]

        with open(os.path.join(workdir, 'manifest.json'), 'w', encoding='utf-8') as fp:
            json.dump(manifest, fp, default=str, indent=2)

        with open(os.path.join(workdir, 'README.md'), 'w', encoding='utf-8') as fp:
            fp.write(_delivery_readme(snap, data_format))

        sh_path = os.path.join(workdir, 'start.sh')
        with open(sh_path, 'w', encoding='utf-8') as fp:
            fp.write(_delivery_start_sh())
        st = os.stat(sh_path)
        os.chmod(sh_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        with open(os.path.join(workdir, 'start.bat'), 'w', encoding='utf-8') as fp:
            fp.write(_delivery_start_bat())

        if is_zip:
            with zipfile.ZipFile(str(path), 'w', zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=9) as zfp:
                for root, _, files in os.walk(workdir):
                    for fn in files:
                        full = os.path.join(root, fn)
                        arcname = os.path.relpath(full, workdir)
                        zfp.write(full, arcname)
            vd.status(f'delivery package written to {path_str}')
        else:
            vd.status(f'delivery package written to {workdir}')
    finally:
        if is_zip:
            shutil.rmtree(workdir, ignore_errors=True)


@register_snapshot_format('delivery')
def read_snapshot_delivery(path, **kwargs):
    """Read a delivery package back into a snapshot dict.

    Supports both directory paths and .zip files.
    """
    path_str = str(path)
    is_zip = path_str.lower().endswith('.zip')

    if is_zip:
        workdir = tempfile.mkdtemp(prefix='vd_pkg_')
        try:
            with zipfile.ZipFile(path_str, 'r') as zfp:
                zfp.extractall(workdir)
            snap = _read_delivery_dir(workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return snap
    else:
        return _read_delivery_dir(path_str)


def _read_delivery_dir(pkgdir):
    manifest_path = os.path.join(pkgdir, 'manifest.json')
    if not os.path.exists(manifest_path):
        vd.fail(f'no manifest.json found in delivery package {pkgdir}')
    with open(manifest_path, encoding='utf-8') as fp:
        manifest = json.load(fp)

    sheets_dict = {}
    order = manifest.get(SNAPSHOT_ORDER_KEY, [])
    for entry in manifest.get('sheets', []):
        name = entry['name']
        info = {
            'class': entry.get('class', 'TableSheet'),
            'columns': entry.get('columns', []),
            'options': entry.get('options', {}),
            'graph_state': entry.get('graph_state'),
            'source_sheet': entry.get('source_sheet'),
            'data': [],
        }
        data_file = entry.get('data_file')
        if data_file:
            full = os.path.join(pkgdir, data_file)
            if os.path.exists(full):
                ext = os.path.splitext(full)[1].lstrip('.').lower()
                reader = _snapshot_readers.get(ext)
                if reader:
                    sub = reader(full)
                    for sname, sinfo in sub.get(SNAPSHOT_SHEETS_KEY, {}).items():
                        if sinfo.get('data'):
                            info['data'] = sinfo['data']
                            break
        sheets_dict[name] = info

    replay_path = os.path.join(pkgdir, 'replay.vdj')
    cmdlog_rows = []
    if os.path.exists(replay_path):
        sub = read_snapshot_vdj(replay_path)
        cmdlog_rows = sub.get(SNAPSHOT_CMDLOG_KEY, [])

    snap = {
        SNAPSHOT_VERSION_KEY: manifest.get(SNAPSHOT_VERSION_KEY, visidata.__version_info__),
        SNAPSHOT_ORDER_KEY: order,
        SNAPSHOT_SHEETS_KEY: sheets_dict,
        SNAPSHOT_GLOBAL_OPTIONS_KEY: manifest.get(SNAPSHOT_GLOBAL_OPTIONS_KEY, {}),
        SNAPSHOT_MACROS_KEY: manifest.get(SNAPSHOT_MACROS_KEY, []),
        SNAPSHOT_CMDLOG_KEY: cmdlog_rows,
    }
    return snap


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

@VisiData.api
def snapshot_to_json(vd, snap, **kwargs):
    return json.dumps(snap, default=str, **kwargs)


@VisiData.api
def snapshot_from_json(vd, text):
    return json.loads(text)


@VisiData.api
def open_snapshot(vd, path, fmt=None, **kwargs):
    """Read a snapshot from *path* and restore it into the active workspace."""
    snap = vd.read_snapshot(path, fmt=fmt, **kwargs)
    return vd.load_snapshot(snap)


vd.addGlobals({
    'generate_snapshot': generate_snapshot,
    'load_snapshot': load_snapshot,
    'write_snapshot': write_snapshot,
    'read_snapshot': read_snapshot,
    'open_snapshot': open_snapshot,
    'snapshot_to_json': snapshot_to_json,
    'snapshot_from_json': snapshot_from_json,
    '_collect_derived_sheets': _collect_derived_sheets,
    '_sheet_snapshot_columns': _sheet_snapshot_columns,
    '_restore_columns': _restore_columns,
    'register_snapshot_format': register_snapshot_format,
})
