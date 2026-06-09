'''Unified session snapshot / export / import for VisiData.

This module walks every registered :class:`~visidata.session_state.StateDescriptor`
and produces a single, self-describing snapshot file (``.vdsj`` -- JSON Lines).
The format is intentionally simple:

    Line 1       -- session envelope (``{"_kind":"session", "_v":1, "vd_version":"..."}``)
    Lines 2..N   -- one descriptor per line::

        {
          "_kind": "descriptor",
          "descriptor": "options",
          "phase": 10,
          "phase_name": "OPTIONS",
          "describe": { ...output of StateDescriptor.describe()... },
          "state":    { ...output of StateDescriptor.get_state()...  }
        }

Restoration reverses the process: for each descriptor line, the matching
registered descriptor (by name) receives the state via ``set_state`` and
``restore``, applied in ascending phase order.

**Format compatibility**

Old formats (``.vd`` TSV cmdlog, ``.vdj`` JSONL cmdlog, ``.vdx`` simplified
text cmdlog, ``.jsonl`` StoredList files) are **not** the snapshot format.
They remain available as import/export entry points via
``open_vd``/``open_vdj``/``open_vdx``/``save_vd``/``save_vdj`` and the
``StoredList`` backward-compat API.  The unified snapshot always goes through
the ``StateDescriptor`` registry.
'''

import json
from copy import copy

from visidata import vd, VisiData, Path, BaseSheet, Sheet, asyncthread, Progress


SNAPSHOT_VERSION = 1


# ---------------------------------------------------------------------------
# Legacy field mapping
# ---------------------------------------------------------------------------

# Each descriptor kind has its own field map.  When *kind* is known,
# ``_normalize_record`` uses the appropriate map; when unknown, it falls
# back to the shared ``_DEFAULT_LEGACY_MAP`` (ambiguous keys skipped).

_CMDLOG_LEGACY_MAP = {
    'sheet_name': 'sheet', 'col_name': 'col', 'row_key': 'row',
    'cmd':        'longname',
    'longname':   'longname',
    'keys':       'keystrokes',
    'keystrokes': 'keystrokes',
    'user_input': 'input',
    'input':      'input',
    'note':       'comment',
    'comment':    'comment',
}

_OPTIONS_LEGACY_MAP = {
    'opt':     'optname',
    'name':    'optname',
    'val':     'value',
    'value':   'value',
    'context': 'scope',
    'scope':   'scope',
}

_MACROS_LEGACY_MAP = {
    'macro_name': 'binding',
    'binding':    'binding',
    'macro_body': 'source',
    'source':     'source',
    'macro_help': 'helpstr',
    'helpstr':    'helpstr',
    'keys':       'keystroke',
    'keystroke':  'keystroke',
}

_SELECTIONS_LEGACY_MAP = {
    'sel_name': 'name',
    'name':     'name',
    'sheet':    'sheet',
    'x1':       'xmin',
    'x2':       'xmax',
    'y1':       'ymin',
    'y2':       'ymax',
}

_GRAPH_LEGACY_MAP = {
    'x_lines': 'reflines_x',
    'y_lines': 'reflines_y',
    'x_min':   'xmin',
    'x_max':   'xmax',
    'y_min':   'ymin',
    'y_max':   'ymax',
}

_CACHE_LEGACY_MAP = {
    'url':     'url',
    'path':    'local_path',
    'local':   'local_path',
    'ts':      'cached_at',
    'expires': 'expires_at',
}

_PROFILES_LEGACY_MAP = {
    'thread':  'thread',
    'stats':   'entries',
    'entries': 'entries',
}

# Ambiguous keys like 'keys', 'name' are intentionally left out of the
# default map so that kind-agnostic callers don't silently translate them
# to the wrong target.
_DEFAULT_LEGACY_MAP = {
    'sheet_name': 'sheet', 'col_name': 'col', 'row_key': 'row',
    'cmd':        'longname',
    'keystrokes': 'keystrokes',
    'user_input': 'input',
    'input':      'input',
    'note':       'comment',
    'comment':    'comment',
    'opt':        'optname',
    'val':        'value',
    'value':      'value',
    'context':    'scope',
    'scope':      'scope',
    'macro_name': 'binding',
    'binding':    'binding',
    'macro_body': 'source',
    'source':     'source',
    'macro_help': 'helpstr',
    'helpstr':    'helpstr',
    'keystroke':  'keystroke',
    'sel_name':   'name',
    'x1':         'xmin',
    'x2':         'xmax',
    'y1':         'ymin',
    'y2':         'ymax',
    'x_lines':    'reflines_x',
    'y_lines':    'reflines_y',
    'x_min':      'xmin',
    'x_max':      'xmax',
    'y_min':      'ymin',
    'y_max':      'ymax',
    'local':      'local_path',
    'ts':         'cached_at',
    'expires':    'expires_at',
    'stats':      'entries',
    'entries':    'entries',
}

_KIND_MAP = {
    'cmdlog':     _CMDLOG_LEGACY_MAP,
    'options':    _OPTIONS_LEGACY_MAP,
    'macros':     _MACROS_LEGACY_MAP,
    'selections': _SELECTIONS_LEGACY_MAP,
    'graph':      _GRAPH_LEGACY_MAP,
    'cache':      _CACHE_LEGACY_MAP,
    'profiles':   _PROFILES_LEGACY_MAP,
}


def _normalize_record(rec: dict, kind: str = None) -> dict:
    '''Translate legacy field names in *rec* to the canonical names used by
    ``StateDescriptor`` output.  *kind* optionally hints at which descriptor
    owns the record (one of ``cmdlog``, ``options``, ``macros``,
    ``selections``, ``graph``, ``cache``, ``profiles``).  Returns a new dict.
    '''
    if not isinstance(rec, dict):
        return rec
    mapping = _KIND_MAP.get(kind, _DEFAULT_LEGACY_MAP)
    out = {}
    for k, v in rec.items():
        if k in ('_v', '_ts', '_id', '_scope'):
            out[k] = v
            continue
        out[mapping.get(k, k)] = v
    return out


# ---------------------------------------------------------------------------
# Core snapshot API
# ---------------------------------------------------------------------------

@VisiData.api
def saveSnapshot(vd, path, phases=None):
    '''Write a unified session snapshot to *path* (``.vdsj``).

    Walks every registered :class:`StateDescriptor` in phase order, calls
    ``persist()`` on each (so the descriptor has a chance to capture its
    current live state), then writes the session envelope followed by one
    ``{describe, state}`` record per descriptor.

    If *phases* is given, only descriptors whose phase is in the iterable
    are included.
    '''
    if not isinstance(path, Path):
        path = Path(path)

    # 1. Let every descriptor capture its live state first.
    vd.persistAllState(phases=phases)

    # 2. Collect descriptors in phase order.
    if phases is not None:
        phases = set(phases)
    included = []
    for phase in sorted(vd._state_descriptors.keys()):
        if phases is not None and phase not in phases:
            continue
        for desc in vd._state_descriptors.get(phase, []):
            try:
                included.append(desc)
            except Exception as e:
                vd.debug(f'snapshot: skipping descriptor: {e}')

    # 3. Write the snapshot file.
    with path.open(mode='w', encoding='utf-8') as fp:
        # session envelope
        envelope = {
            '_kind': 'session',
            '_v': SNAPSHOT_VERSION,
            'vd_version': getattr(vd, '__version__', 'unknown'),
            'num_descriptors': len(included),
            'phases': sorted({d.phase for d in included}),
        }
        fp.write(json.dumps(envelope, ensure_ascii=False) + '\n')

        # per-descriptor records
        for desc in Progress(included, gerund='saving session'):
            try:
                record = {
                    '_kind': 'descriptor',
                    'descriptor': desc.name,
                    'phase': desc.phase,
                    'phase_name': _phase_name(desc.phase),
                    'describe': desc.describe(),
                    'state': desc.get_state(),
                }
                fp.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
            except Exception as e:
                vd.debug(f'snapshot: failed to serialize {desc.name}: {e}')

    vd.status(f'saved session snapshot ({len(included)} descriptors) to {path}')


@VisiData.api
def loadSnapshot(vd, path, phases=None):
    '''Read a unified session snapshot from *path* and apply it to the live session.

    For each descriptor record in the file, the matching registered
    :class:`StateDescriptor` (matched by ``name``) receives the record's
    ``state`` via ``set_state`` followed by ``restore``.  Descriptors are
    applied in ascending phase order regardless of their order in the file.

    Unknown descriptor names in the file are skipped with a debug log.
    If *phases* is given, only descriptors whose phase is in the iterable
    are restored.
    '''
    if not isinstance(path, Path):
        path = Path(path)

    vd.ensureAllDescriptorsRegistered()

    if phases is not None:
        phases = set(phases)

    # Build lookup from descriptor name -> descriptor object.
    by_name = {}
    for descs in vd._state_descriptors.values():
        for d in descs:
            by_name.setdefault(d.name, d)

    # Read all records from the file.
    pending = []  # list of (phase, descriptor, state)
    with path.open(encoding='utf-8-sig') as fp:
        first = True
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                vd.debug(f'loadSnapshot: bad JSON line: {e}')
                continue

            kind = rec.get('_kind')
            if first and kind == 'session':
                first = False
                if rec.get('_v', 0) > SNAPSHOT_VERSION:
                    vd.warning(f'snapshot version {rec["_v"]} is newer than '
                               f'supported {SNAPSHOT_VERSION}; some fields may be ignored')
                continue
            first = False

            if kind != 'descriptor':
                continue

            name = rec.get('descriptor')
            state = rec.get('state')
            phase = rec.get('phase')

            if phases is not None and phase not in phases:
                continue
            if name not in by_name:
                vd.debug(f'loadSnapshot: skipping unknown descriptor {name!r}')
                continue

            pending.append((phase, by_name[name], state))

    # Apply in ascending phase order.
    pending.sort(key=lambda t: t[0])
    for phase, desc, state in Progress(pending, gerund='restoring session'):
        try:
            desc.set_state(state)
            desc.restore()
        except Exception as e:
            vd.debug(f'loadSnapshot: failed to restore {desc.name}: {e}')

    vd.status(f'restored session snapshot ({len(pending)} descriptors) from {path}')


# ---------------------------------------------------------------------------
# Legacy format adapters (read-only; for importing old data into descriptors)
# ---------------------------------------------------------------------------

@VisiData.api
def importCmdlogIntoSnapshot(vd, cmdlog_path, snapshot_path):
    '''Import a legacy ``.vd``/``.vdj``/``.vdx`` cmdlog file into the
    ``CmdlogState`` descriptor and write a new session snapshot.

    This is a one-way helper: old cmdlogs are not the native snapshot
    format, but users can migrate them via this function.
    '''
    # Load the cmdlog using existing openers (they produce a CommandLog sheet).
    from visidata.cmdlog import CommandLog, CommandLogJsonl
    opener = getattr(vd, f'open_{Path(cmdlog_path).ext}', vd.open_vd)
    cl_sheet = opener(Path(cmdlog_path))
    cl_sheet.reload()

    # Push rows into the live cmdlog (normalising legacy field names) and snapshot.
    for r in cl_sheet.rows:
        rowdict = {c.name: c.getValue(r) for c in cl_sheet.columns}
        norm = _normalize_record(rowdict, kind='cmdlog')
        vd.cmdlog.addRow(vd.cmdlog.newRow(**{k: norm.get(k) for k in
            ['sheet', 'col', 'row', 'longname', 'input', 'keystrokes', 'comment']}))

    vd.saveSnapshot(Path(snapshot_path))


@VisiData.api
def importStoredListIntoSnapshot(vd, storedlist_path, descriptor_name, snapshot_path):
    '''Import a legacy StoredList ``.jsonl`` file into the named
    :class:`StateDescriptor` and write a new session snapshot.

    *descriptor_name* must match one of the registered descriptors
    (``macros``, ``selections``, ``input_history``, etc.).
    '''
    by_name = {}
    for descs in vd._state_descriptors.values():
        for d in descs:
            by_name.setdefault(d.name, d)

    if descriptor_name not in by_name:
        vd.fail(f'no registered descriptor named {descriptor_name!r}')

    desc = by_name[descriptor_name]
    # StoredList items are JSONL records, possibly with legacy field names.
    records = []
    with Path(storedlist_path).open(encoding='utf-8-sig') as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(_normalize_record(rec, kind=descriptor_name))

    desc.set_state(records)
    vd.saveSnapshot(Path(snapshot_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _phase_name(phase_val: int) -> str:
    from visidata.session_state import StatePhase
    return StatePhase.phase_name(phase_val)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

BaseSheet.addCommand('', 'save-session',
    'vd.saveSnapshot(vd.inputPath("save session snapshot to: ", value="session.vdsj"))',
    'save the current session (all registered state descriptors) to a .vdsj file')

BaseSheet.addCommand('', 'load-session',
    'vd.loadSnapshot(vd.inputPath("load session snapshot from: "))',
    'restore session state from a .vdsj file')

BaseSheet.addCommand('', 'describe-session',
    'sheet = vd.Sheet("session-state", rows=vd.describeAllState()); '
    'sheet.columns = [vd.ItemColumn(c) for c in ["name", "phase", "phase_name", "kind"]]; '
    'vd.push(sheet)',
    'open a sheet listing every registered session state descriptor')

vd.addMenuItems('''
    File > Session > save snapshot > save-session
    File > Session > load snapshot > load-session
    File > Session > describe all > describe-session
''')
