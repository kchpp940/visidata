"""Unified Workspace Snapshot layer.

Provides a single interface to serialize/deserialize a complete VisiData
workspace.  Export formats (vds, vdj, vdx, delivery-package, macros) consume
the snapshot dict produced here; recovery always flows through
``vd.load_snapshot`` which applies state in a canonical order.

Snapshot dict schema::

    {
      "version": "VisiData vX.Y",
      "order":   ["sheetA", "sheetB"],           # sheet load order
      "sheets": {
        "sheetA": {
          "class":     "TableSheet",
          "columns":   [ {name,typestr,width,...}, ... ],  # Column.__getstate__
          "options":   {optname: value, ...},              # non-default sheet options
          "graph_state": {"reflines_x": [...], "reflines_y": [...],
                          "xzoomlevel": 1.0, "yzoomlevel": 1.0},
          "source_sheet": "parentSheet",    # name of source if derived
        }, ...
      },
      "global_options": {optname: value, ...}, # non-default global options
      "macros":  [ {binding, keystroke, helpstr, source, rows:[...]}, ... ],
      "cmdlog":  [ CommandLogRow-dict, ... ],  # full session command log
    }
"""

import json

import visidata
from visidata import vd, VisiData, BaseSheet, TableSheet, UNLOADED
from visidata import Column, SettableColumn, ItemColumn, ExprColumn, AttrDict


SNAPSHOT_VERSION_KEY = 'version'
SNAPSHOT_ORDER_KEY = 'order'
SNAPSHOT_SHEETS_KEY = 'sheets'
SNAPSHOT_GLOBAL_OPTIONS_KEY = 'global_options'
SNAPSHOT_MACROS_KEY = 'macros'
SNAPSHOT_CMDLOG_KEY = 'cmdlog'

GRAPH_STATE_KEYS = ('reflines_x', 'reflines_y', 'xzoomlevel', 'yzoomlevel')


def _sheet_snapshot_options(vs):
    """Return dict of non-default sheet-level options."""
    out = {}
    for optname in vs.options.keys(vs):
        opt = vs.options._get(optname, vs)
        default = vs.options.getdefault(optname)
        if opt.value != default:
            out[optname] = opt.value
    return out


def _global_snapshot_options():
    """Return dict of non-default global options."""
    out = {}
    for optname in vd.options.keys('global'):
        opt = vd.options._get(optname, 'global')
        default = vd.options.getdefault(optname)
        if opt.value != default:
            out[optname] = opt.value
    return out


def _sheet_snapshot_graph(vs):
    """Return graph_state dict for graph-like sheets, or None."""
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
    """Return list of column state dicts for every column on vs."""
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


def _collect_derived_sheets(root):
    """Walk allSheets and return every sheet whose source-chain reaches root."""
    out = []
    for vs in vd.allSheets:
        src = getattr(vs, 'source', None)
        while isinstance(src, BaseSheet):
            if src is root:
                out.append(vs)
                break
            src = getattr(src, 'source', None)
    return out


@VisiData.api
def generate_snapshot(vd, scope='current', include_cmdlog=True, include_macros=True,
                      include_data=False):
    """Build a workspace snapshot dict.

    *scope*
        ``"current"``  — active sheet plus derived sheets
        ``"all"``      — every sheet on the stack
        ``"selected"`` — (IndexSheet only) selected rows as sheets
        otherwise      — iterable of sheet objects
    """
    if isinstance(scope, str):
        if scope == 'current':
            sheets = [vd.activeSheet] if vd.activeSheet else []
            for vs in list(sheets):
                sheets.extend(_collect_derived_sheets(vs))
        elif scope == 'all':
            sheets = list(vd.stackedSheets)
        elif scope == 'selected':
            from visidata import IndexSheet
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
        sheets_dict[vs.name] = {
            'class': type(vs).__name__,
            'columns': _sheet_snapshot_columns(vs),
            'options': _sheet_snapshot_options(vs),
            'graph_state': _sheet_snapshot_graph(vs),
            'source_sheet': source_sheet,
        }
        order.append(vs.name)

    snap = {
        SNAPSHOT_VERSION_KEY: visidata.__version_info__,
        SNAPSHOT_ORDER_KEY: order,
        SNAPSHOT_SHEETS_KEY: sheets_dict,
        SNAPSHOT_GLOBAL_OPTIONS_KEY: _global_snapshot_options(),
    }

    if include_cmdlog and getattr(vd, 'cmdlog', None):
        rows = []
        for r in vd.cmdlog.rows:
            d = {}
            for k in ('sheet', 'col', 'row', 'longname', 'input', 'keystrokes', 'comment'):
                d[k] = getattr(r, k, '')
            rows.append(d)
        snap[SNAPSHOT_CMDLOG_KEY] = rows

    if include_macros:
        macros = []
        for binding, cmdlog in getattr(vd, 'macrobindings', {}).items():
            macros.append({
                'binding': binding,
                'keystroke': getattr(cmdlog, 'keystroke', ''),
                'helpstr': getattr(cmdlog, 'helpstr', ''),
                'source': getattr(cmdlog, 'source', ''),
                'rows': [dict(r) if isinstance(r, dict) else
                         {k: getattr(r, k, '') for k in ('sheet', 'col', 'row', 'longname', 'input', 'keystrokes', 'comment')}
                         for r in getattr(cmdlog, 'rows', [])],
            })
        snap[SNAPSHOT_MACROS_KEY] = macros

    return snap


def _restore_columns(vs, col_states):
    """Apply ``col_states`` list (from Column.__getstate__) onto vs."""
    existing_names = {c.name for c in vs.columns}
    for d in col_states:
        classname = d.pop('col', 'ItemColumn')
        name = d.get('name', '')
        if classname == 'Column':
            classname = 'ItemColumn'
            d.setdefault('expr', name)
        cls = vd.getGlobals().get(classname, ItemColumn)
        c = cls(name, sheet=vs)
        vs.addColumn(c)
        if hasattr(c, '__setstate__'):
            c.__setstate__(d)


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
                  apply_options=True, existing_sheets=None):
    """Restore workspace from a snapshot dict.

    Applies state in canonical order:
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


@VisiData.api
def snapshot_to_json(vd, snap, **kwargs):
    """Compact helper to stringify a snapshot as JSON."""
    return json.dumps(snap, default=str, **kwargs)


@VisiData.api
def snapshot_from_json(vd, text):
    """Parse a snapshot from a JSON string."""
    return json.loads(text)


vd.addGlobals({
    'generate_snapshot': generate_snapshot,
    'load_snapshot': load_snapshot,
    'snapshot_to_json': snapshot_to_json,
    'snapshot_from_json': snapshot_from_json,
    '_collect_derived_sheets': _collect_derived_sheets,
})
