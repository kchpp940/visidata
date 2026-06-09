'''Unified session state persistence for VisiData.

This module provides two core abstractions:

**StateDescriptor** -- the common interface that every piece of session state
(options, cmdlog, macros, selections, cache manifest, graph state, etc.)
must implement.  Each descriptor has a name, a restore *phase*, and five
operations:  ``describe``, ``restore``, ``persist``, ``get_state``, ``set_state``.

**StateStore** -- a concrete ``StateDescriptor`` that stores a collection of
records as JSON Lines on disk.  Simple record-based state (options, selections,
cache manifest, graph reflines) can use ``StateStore`` directly.  More complex
state (cmdlog with its replay semantics, macros with per-macro .vdj files)
wraps a ``StateStore`` in a custom ``StateDescriptor`` that knows how to
translate between the on-disk records and the live in-memory objects.

Standard record envelope (all optional, auto-filled by ``StateStore.add``):
    _v      -- schema version of this record (int)
    _ts     -- ISO-8601 timestamp when record was written
    _id     -- stable unique identifier (string)
    _scope  -- sheet/global context where this record applies

Standard restore phases (lower = restored earlier):
    OPTIONS (10) -> PROFILES (20) -> MACROS (30) -> SELECTIONS (40) ->
    CACHE (50) -> GRAPH (60) -> LAYOUT (70)
'''

import json
import os
import time
from abc import ABC, abstractmethod
from copy import copy
from datetime import datetime, timezone

from visidata import vd, VisiData, Path, AttrDict, asyncthread


SENSITIVE_FIELD_PATTERNS = [
    'password', 'token', 'secret', 'api[_-]?key', 'auth',
    'private[_-]?key', 'credential', 'passwd',
]

STATE_VERSION = 1


class StatePhase:
    '''Ordered phases for session state restoration.
    Lower-numbered phases are restored before higher-numbered ones.
    '''
    OPTIONS    = 10   # options, global settings
    PROFILES   = 20   # user profiles, environment
    MACROS     = 30   # macros and custom commands (need options first)
    SELECTIONS = 40   # named selections (need sheets loaded)
    CACHE      = 50   # cache manifest
    GRAPH      = 60   # graph state (reflines, viewport)
    LAYOUT     = 70   # window/layout state (restored last)

    _order = [OPTIONS, PROFILES, MACROS, SELECTIONS, CACHE, GRAPH, LAYOUT]

    @classmethod
    def all_phases(cls):
        return sorted(cls._order)

    @classmethod
    def phase_name(cls, phase_val: int) -> str:
        for name, val in vars(cls).items():
            if isinstance(val, int) and val == phase_val:
                return name
        return f'PHASE_{phase_val}'


# ---------------------------------------------------------------------------
# StateDescriptor -- the common interface
# ---------------------------------------------------------------------------

class StateDescriptor(ABC):
    '''Abstract interface implemented by every piece of session state.

    Subclasses must define ``name`` (str) and ``phase`` (StatePhase value),
    and implement the five lifecycle methods.
    '''

    name: str = ''
    phase: int = StatePhase.LAYOUT

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    def describe(self) -> dict:
        '''Return a JSON-serialisable dict describing this state bundle.

        At minimum should contain keys ``name``, ``phase``, ``phase_name``
        and ``kind`` (a short label like "records", "cmdlog", "options").
        '''
        ...

    @abstractmethod
    def restore(self) -> None:
        '''Load persisted state from disk and apply it to the live session.

        Called on startup (in ``phase`` order) and on explicit user request.
        Must be idempotent.
        '''
        ...

    @abstractmethod
    def persist(self) -> None:
        '''Capture the current live state and write it to persistent storage.

        Called on explicit user request (e.g. ``save-session``).  May also be
        called automatically by the descriptor on every change.
        '''
        ...

    @abstractmethod
    def get_state(self):
        '''Return the current in-memory state (for inspection / debug).'''
        ...

    @abstractmethod
    def set_state(self, state) -> None:
        '''Replace the current in-memory state with *state*.'''
        ...

    # -- convenience -------------------------------------------------------

    def register(self):
        '''Register this descriptor with the global registry so it is
        included in ``restoreAllState`` / ``persistAllState`` / ``describeAllState``.
        '''
        if not hasattr(vd, '_state_descriptors'):
            vd._state_descriptors = {}
        vd._state_descriptors.setdefault(self.phase, [])
        if self not in vd._state_descriptors[self.phase]:
            vd._state_descriptors[self.phase].append(self)
        return self

    # Backwards-compat alias -- old code used ``register_for_restore``.
    # Deprecated; prefer ``register``.
    def register_for_restore(self):
        return self.register()

    def __repr__(self):
        return f'<{type(self).__name__} {self.name!r} phase={StatePhase.phase_name(self.phase)}>'


# ---------------------------------------------------------------------------
# StateStore -- JSONL-backed record store that implements StateDescriptor
# ---------------------------------------------------------------------------

def _is_sensitive_field(name: str) -> bool:
    '''Return True if *name* looks like it holds a sensitive value.'''
    import re
    low = name.lower()
    for pat in SENSITIVE_FIELD_PATTERNS:
        if re.search(pat, low):
            return True
    return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _redact_value(v):
    if isinstance(v, str) and v:
        return '***REDACTED***'
    if isinstance(v, (list, tuple)):
        return [_redact_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _redact_value(x) for k, x in v.items()}
    return v


class StateStore(StateDescriptor):
    '''Persistent keyed collection of state records, stored as JSON Lines.

    Implements the full ``StateDescriptor`` interface.  Each record is a
    dict-like (``AttrDict``) with a standard envelope ``_v``, ``_ts``, ``_id``,
    ``_scope`` plus caller-supplied data fields.
    '''

    kind = 'records'
    sensitive_fields = ()  # tuple of field names to redact on save

    def __init__(self, name: str, phase: int = StatePhase.LAYOUT, **kwargs):
        self.name = name
        self.phase = phase
        self._records = []
        self._by_id = {}
        self._dirty = False
        for k, v in kwargs.items():
            setattr(self, k, v)

    # -- StateDescriptor interface ----------------------------------------

    def describe(self) -> dict:
        return {
            'name': self.name,
            'phase': self.phase,
            'phase_name': StatePhase.phase_name(self.phase),
            'kind': self.kind,
            'path': str(self.path) if self.path else None,
            'record_count': len(self._records),
        }

    def restore(self) -> None:
        self.reload()

    def persist(self) -> None:
        self.save()

    def get_state(self):
        return [dict(r) for r in self._records]

    def set_state(self, state) -> None:
        self._records = []
        self._by_id = {}
        for rec in state or []:
            self.add(rec)
        self._dirty = True

    # -- path helpers -----------------------------------------------------

    @property
    def path(self) -> Path:
        try:
            vdpath = vd.data_dir
        except Exception:
            return None
        if not vdpath.exists():
            try:
                if vd.options.nothing:
                    return None
                vdpath.mkdir(parents=True)
            except Exception:
                return None
        return vdpath / (self.name + '.jsonl')

    def _normalize_relative_paths(self, record, base_path=None):
        base = base_path or (self.path.parent if self.path else None)
        if not base:
            return record
        return _walk_paths(record, lambda p: _relpath_if_under(p, base))

    def _resolve_relative_paths(self, record, base_path=None):
        base = base_path or (self.path.parent if self.path else None)
        if not base:
            return record
        return _walk_paths(record, lambda p: _abspath_if_relative(p, base))

    # -- core CRUD --------------------------------------------------------

    def reload(self):
        '''(Re)load all records from disk.'''
        self._records = []
        self._by_id = {}
        p = self.path
        if not p or not p.exists():
            return
        with p.open(encoding='utf-8-sig') as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                rec = vd.callNoExceptions(self._deserialize, line)
                if rec is not None:
                    self._register(rec)

    def all(self):
        '''Return list of all records.'''
        return list(self._records)

    def get(self, id, default=None):
        '''Return record by _id, or *default*.'''
        return self._by_id.get(id, default)

    def add(self, record):
        '''Add or replace a record.  ``record`` is dict-like.  Returns it with envelope.'''
        if not isinstance(record, AttrDict):
            record = AttrDict(record)
        self._ensure_envelope(record)
        self._resolve_relative_paths(record)
        old = self._by_id.get(record._id) if record.get('_id') else None
        if old is not None:
            idx = self._records.index(old)
            self._records[idx] = record
        else:
            self._records.append(record)
        if record.get('_id'):
            self._by_id[record._id] = record
        self._dirty = True
        return record

    def remove(self, id):
        '''Remove record by _id.  Returns True if removed.'''
        rec = self._by_id.pop(id, None)
        if rec is not None:
            self._records.remove(rec)
            self._dirty = True
            return True
        return False

    def save(self):
        '''Persist all records to disk.'''
        if not self._dirty:
            return
        p = self.path
        if p is None:
            return
        with p.open(mode='w', encoding='utf-8') as fp:
            for rec in self._records:
                fp.write(self._serialize(rec) + '\n')
        self._dirty = False

    # -- internal helpers -------------------------------------------------

    def _register(self, rec):
        self._records.append(rec)
        if rec.get('_id'):
            self._by_id[rec._id] = rec

    def _ensure_envelope(self, rec):
        rec.setdefault('_v', STATE_VERSION)
        rec.setdefault('_ts', _now_iso())
        rec.setdefault('_scope', 'global')
        if not rec.get('_id'):
            rec['_id'] = f'{self.name}-{len(self._records)}-{int(time.time()*1000)}'

    def _deserialize(self, line: str):
        obj = json.loads(line)
        if isinstance(obj, dict):
            obj = AttrDict(obj)
            self._resolve_relative_paths(obj)
            return obj
        return AttrDict(data=obj)

    def _serialize(self, record) -> str:
        out = self._normalize_relative_paths(dict(record))
        out = self._redact(out)
        return json.dumps(out, ensure_ascii=False, default=str)

    def _redact(self, record: dict) -> dict:
        if not self.sensitive_fields and not any(_is_sensitive_field(k) for k in record):
            return record
        out = dict(record)
        for fname in self.sensitive_fields:
            if fname in out:
                out[fname] = _redact_value(out[fname])
        for k, v in list(out.items()):
            if _is_sensitive_field(k):
                out[k] = _redact_value(v)
        return out

    # -- list compatibility (backwards compat with StoredList) ------------

    def __iter__(self):
        return iter(self._records)

    def __len__(self):
        return len(self._records)

    def __getitem__(self, idx):
        return self._records[idx]

    def __delitem__(self, idx):
        rec = self._records.pop(idx)
        if rec.get('_id'):
            self._by_id.pop(rec._id, None)
        self._dirty = True

    def append(self, v):
        self.add(v)


# -- path helpers ---------------------------------------------------------

_PATHLIKE_KEYS = {'source', 'path', 'file', 'filename', 'input_file', 'output_file'}


def _walk_paths(obj, fn):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _PATHLIKE_KEYS and isinstance(v, str):
                out[k] = fn(v)
            else:
                out[k] = _walk_paths(v, fn)
        return out
    if isinstance(obj, list):
        return [_walk_paths(x, fn) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_walk_paths(x, fn) for x in obj)
    return obj


def _relpath_if_under(pathstr: str, base: Path) -> str:
    try:
        p = Path(pathstr)
        base_p = Path(str(base))
        abs_p = p if p.is_absolute() else base_p / p
        if not abs_p.is_absolute():
            return pathstr
        common = os.path.commonpath([str(abs_p), str(base_p)])
        if common:
            return os.path.relpath(str(abs_p), str(base_p))
    except Exception:
        pass
    return pathstr


def _abspath_if_relative(pathstr: str, base: Path) -> str:
    try:
        p = Path(pathstr)
        if not p.is_absolute():
            return str(Path(str(base)) / pathstr)
    except Exception:
        pass
    return pathstr


# ---------------------------------------------------------------------------
# Global descriptor registry and orchestration
# ---------------------------------------------------------------------------

VisiData.init('_state_descriptors', dict)  # phase -> [StateDescriptor]
VisiData.init('_state_stores', dict)       # legacy alias, kept for compat


@VisiData.api
def ensureAllDescriptorsRegistered(vd):
    '''Force every standard lazy_property descriptor / store to be evaluated
    and registered.  Safe to call multiple times.

    All public orchestration APIs (``describeAllState``, ``restoreAllState``,
    ``persistAllState``, and ``saveSnapshot`` / ``loadSnapshot``) call this
    internally so that callers do not have to worry about lazy-property
    timing.
    '''
    _ = vd.optionsState
    _ = vd.profilesState
    _ = vd.cmdlogState
    _ = vd.optionsStore
    _ = vd.profilesStore
    _ = vd.cmdlogStore
    _ = vd.cacheManifest
    _ = vd.graphStateStore
    # These use StoredList (not lazy_property) but are assigned at module
    # import time in macros.py, canvas.py, input_history.py.  Accessing them
    # here is a no-op in normal operation but guards against import-order
    # surprises in tests and scripts.
    try:
        vd.macros.register()
    except Exception:
        pass
    try:
        vd.selections.register()
    except Exception:
        pass
    try:
        vd._inputHistoryList.register()
    except Exception:
        pass


@VisiData.api
def describeAllState(vd) -> list:
    '''Return a list of ``describe()`` dicts for every registered state
    descriptor, sorted by phase.  Useful for debugging and UI.
    '''
    vd.ensureAllDescriptorsRegistered()
    out = []
    for phase in sorted(vd._state_descriptors.keys()):
        for desc in vd._state_descriptors[phase]:
            try:
                out.append(desc.describe())
            except Exception as e:
                out.append({'name': getattr(desc, 'name', '?'), 'phase': phase,
                            'phase_name': StatePhase.phase_name(phase),
                            'error': str(e)})
    return out


@VisiData.api
def restoreAllState(vd, phases=None) -> None:
    '''Call ``restore()`` on every registered state descriptor, in phase order.

    If *phases* is given, only descriptors whose phase is in the set/list
    are restored.  Called automatically from the ``run`` startup hook.
    '''
    vd.ensureAllDescriptorsRegistered()
    if phases is None:
        phases = StatePhase.all_phases()
    phases = set(phases)
    for phase in sorted(vd._state_descriptors.keys()):
        if phase not in phases:
            continue
        for desc in vd._state_descriptors.get(phase, []):
            vd.callNoExceptions(desc.restore)


@VisiData.api
def persistAllState(vd, phases=None) -> None:
    '''Call ``persist()`` on every registered state descriptor, in phase order.'''
    vd.ensureAllDescriptorsRegistered()
    if phases is None:
        phases = StatePhase.all_phases()
    phases = set(phases)
    for phase in sorted(vd._state_descriptors.keys()):
        if phase not in phases:
            continue
        for desc in vd._state_descriptors.get(phase, []):
            vd.callNoExceptions(desc.persist)


# Legacy name alias -- ``restoreState`` used to only reload StateStores; now
# it is a thin wrapper over ``restoreAllState`` which handles all descriptors.
@VisiData.api
def restoreState(vd, phases=None):
    vd.restoreAllState(phases=phases)


# ---------------------------------------------------------------------------
# OptionsState -- manages option persistence via StateStore + legacy .visidatarc
# ---------------------------------------------------------------------------

class OptionsState(StateDescriptor):
    '''StateDescriptor for VisiData options.

    Primary persistence: ``optionsStore`` (JSON Lines under the data dir).
    Legacy compatibility:  on first-run migration, reads any existing
    ``.visidatarc`` and imports the ``options.name=value`` assignments into
    the new JSONL store.  After migration the .visidatarc is not written to
    by the unified persistence path (though ``OptionsSheet.commit`` still
    appends to it for backwards compatibility with users who rely on it).
    '''

    name = 'options'
    phase = StatePhase.OPTIONS
    kind = 'options'

    def describe(self) -> dict:
        return {
            'name': self.name,
            'phase': self.phase,
            'phase_name': StatePhase.phase_name(self.phase),
            'kind': self.kind,
            'store_path': str(vd.optionsStore.path) if vd.optionsStore.path else None,
            'store_records': len(vd.optionsStore.all()),
            'legacy_config': vd.options.config,
        }

    def restore(self) -> None:
        '''Reload the options JSONL store, then apply every record to the
        live options object.  On first run (store empty, legacy config has
        content), attempt a one-way migration from .visidatarc.
        '''
        store = vd.optionsStore
        store.restore()  # reload JSONL

        if not store.all():
            self._migrate_from_visidatarc()

        self.applyToLive()

    def persist(self) -> None:
        '''Snapshot all current non-default option overrides into the store
        and write to disk.
        '''
        store = vd.optionsStore
        # collect overrides from vd.options._opts
        try:
            for optname, scopedict in vd.options._opts.items():
                for scope, opt in scopedict.items():
                    if scope in ('default',):
                        continue
                    default_opt = scopedict.get('default')
                    default_val = default_opt.value if default_opt else None
                    if opt.value == default_val:
                        continue
                    rec_id = f'opt_{scope}_{optname}'
                    store.add({
                        '_id': rec_id,
                        '_scope': scope,
                        'optname': optname,
                        'value': opt.value,
                        'scope': scope,
                    })
        except Exception as e:
            vd.debug(f'options.persist: {e}')
        store.save()

    def get_state(self):
        return vd.optionsStore.get_state()

    def set_state(self, state) -> None:
        vd.optionsStore.set_state(state)
        self.applyToLive()

    # -- helpers ----------------------------------------------------------

    def applyToLive(self) -> None:
        store = vd.optionsStore
        for rec in store.all():
            optname = rec.get('optname')
            value = rec.get('value')
            scope = rec.get('scope', 'global')
            if optname is None or value is None:
                continue
            try:
                if scope == 'global':
                    vd.options.set(optname, value, 'global', cmdlog=False)
                else:
                    vs = vd.getSheet(scope)
                    if vs:
                        vs.options.set(optname, value, vs, cmdlog=False)
            except Exception as e:
                vd.debug(f'failed to apply persisted option {optname}: {e}')

    def _migrate_from_visidatarc(self) -> None:
        '''Best-effort one-way import of ``options.name=value`` lines from
        the user's .visidatarc into the new JSONL store.
        '''
        try:
            cfg_path = Path(vd.options.config)
            if not cfg_path.exists():
                return
            store = vd.optionsStore
            imported = 0
            with cfg_path.open(encoding='utf-8') as fp:
                for line in fp:
                    line = line.strip()
                    # match: options.<name>=<python literal>
                    if line.startswith('options.') and '=' in line:
                        rest = line[len('options.'):]
                        eq = rest.index('=')
                        optname = rest[:eq].strip()
                        valstr = rest[eq+1:].strip()
                        try:
                            import ast
                            value = ast.literal_eval(valstr)
                        except Exception:
                            value = valstr
                        rec_id = f'opt_global_{optname}'
                        store.add({
                            '_id': rec_id,
                            '_scope': 'global',
                            'optname': optname,
                            'value': value,
                            'scope': 'global',
                        })
                        imported += 1
            if imported:
                store.save()
                vd.status(f'imported {imported} options from {cfg_path}')
        except Exception as e:
            vd.debug(f'visidatarc migration: {e}')


@VisiData.lazy_property
def optionsState(vd):
    desc = OptionsState()
    desc.register()
    return desc


# ---------------------------------------------------------------------------
# CmdlogState -- manages command log persistence
# ---------------------------------------------------------------------------

class CmdlogState(StateDescriptor):
    '''StateDescriptor for the command log.

    Primary persistence: ``cmdlogStore`` (JSON Lines of cmdlog rows under
    the data dir).  The legacy ``.vd`` (TSV), ``.vdj`` (JSONL), and ``.vdx``
    (simple text) formats are kept as *import/export* formats only -- they
    are read by ``open_vd`` / ``open_vdj`` / ``open_vdx`` and written by
    ``save_vd`` / ``save_vdj`` / ``save_vdx``, but the unified session
    persistence always goes through ``cmdlogStore``.
    '''

    name = 'cmdlog'
    phase = StatePhase.LAYOUT
    kind = 'cmdlog'

    def describe(self) -> dict:
        return {
            'name': self.name,
            'phase': self.phase,
            'phase_name': StatePhase.phase_name(self.phase),
            'kind': self.kind,
            'store_path': str(vd.cmdlogStore.path) if vd.cmdlogStore.path else None,
            'store_records': len(vd.cmdlogStore.all()),
            'live_rows': len(vd.cmdlog.rows),
        }

    def restore(self) -> None:
        '''Reload cmdlog rows from ``cmdlogStore`` into the live cmdlog sheet.'''
        store = vd.cmdlogStore
        store.restore()
        # Load records into the live vd.cmdlog, but only if it is empty so
        # we don't clobber commands already typed during startup.
        if not vd.cmdlog.rows:
            for rec in store.all():
                try:
                    row = vd.cmdlog.newRow(**{k: rec.get(k) for k in
                        ['sheet', 'col', 'row', 'longname', 'input',
                         'keystrokes', 'comment', 'undofuncs'] if k in rec})
                    vd.cmdlog.addRow(row)
                except Exception as e:
                    vd.debug(f'cmdlog.restore row: {e}')

    def persist(self) -> None:
        '''Snapshot the live cmdlog rows into the JSONL store and save.'''
        store = vd.cmdlogStore
        for i, r in enumerate(vd.cmdlog.rows):
            rec = {
                '_id': f'cmdlog_{i}',
                '_scope': r.sheet or 'global',
                'sheet': r.sheet,
                'col': r.col,
                'row': r.row,
                'longname': r.longname,
                'input': r.input,
                'keystrokes': r.keystrokes,
                'comment': r.comment,
            }
            store.add(rec)
        store.save()

    def get_state(self):
        return vd.cmdlogStore.get_state()

    def set_state(self, state) -> None:
        vd.cmdlogStore.set_state(state)
        self.restore()


@VisiData.lazy_property
def cmdlogStore(vd):
    '''Backing StateStore for the command log.'''
    store = StateStore(name='cmdlog', phase=StatePhase.LAYOUT)
    store.register()
    return store


@VisiData.lazy_property
def cmdlogState(vd):
    desc = CmdlogState()
    desc.register()
    return desc


# ---------------------------------------------------------------------------
# ProfileState -- manages cProfile profile persistence
# ---------------------------------------------------------------------------

class ProfileState(StateDescriptor):
    '''StateDescriptor for cProfile profiling data.

    Captures the output of ``cProfile.Profile.getstats()`` (or dumped .prof
    files) into ``profilesStore`` as serialisable records.  Profiles are
    restored as raw data (since we cannot rehydrate a live ``cProfile.Profile``
    from disk) and can be loaded into ``ProfileSheet`` for inspection.
    '''

    name = 'profiles'
    phase = StatePhase.PROFILES
    kind = 'profiles'

    def describe(self) -> dict:
        return {
            'name': self.name,
            'phase': self.phase,
            'phase_name': StatePhase.phase_name(self.phase),
            'kind': self.kind,
            'store_path': str(vd.profilesStore.path) if vd.profilesStore.path else None,
            'store_records': len(vd.profilesStore.all()),
            'live_profiles': self._count_live_profiles(),
        }

    def restore(self) -> None:
        '''Reload persisted profile stats from the store.  Live cProfile
        objects cannot be rehydrated; data is left in the store and can be
        loaded into ProfileSheet via ``load-profile``.
        '''
        vd.profilesStore.restore()

    def persist(self) -> None:
        '''Snapshot every live ``cProfile.Profile`` attached to VisiData
        threads into the profiles store.
        '''
        store = vd.profilesStore
        try:
            import cProfile
            import threading
            for t in threading.enumerate():
                prof = getattr(t, 'profile', None)
                if not isinstance(prof, cProfile.Profile):
                    continue
                self._capture_profile(store, t.name, prof)
            main_prof = getattr(vd.mainThread, 'profile', None)
            if isinstance(main_prof, cProfile.Profile):
                self._capture_profile(store, 'main', main_prof)
        except Exception as e:
            vd.debug(f'profiles.persist: {e}')
        store.save()

    def get_state(self):
        return vd.profilesStore.get_state()

    def set_state(self, state) -> None:
        vd.profilesStore.set_state(state)

    # -- helpers ----------------------------------------------------------

    def _count_live_profiles(self) -> int:
        try:
            import cProfile
            import threading
            n = 0
            for t in threading.enumerate():
                if isinstance(getattr(t, 'profile', None), cProfile.Profile):
                    n += 1
            return n
        except Exception:
            return 0

    @staticmethod
    def _capture_profile(store, thread_name: str, prof) -> None:
        '''Serialize a live cProfile.Profile's stats into a record.'''
        try:
            stats = prof.getstats()
            serialised = []
            for entry in stats:
                code = entry.code
                serialised.append({
                    'func': repr(code),
                    'filename': getattr(code, 'co_filename', None),
                    'lineno': getattr(code, 'co_firstlineno', None),
                    'callcount': entry.callcount,
                    'reccallcount': getattr(entry, 'reccallcount', 0),
                    'inlinetime': entry.inlinetime,
                    'totaltime': entry.totaltime,
                })
            rec_id = f'profile_{thread_name}_{int(time.time())}'
            store.add({
                '_id': rec_id,
                '_scope': thread_name,
                'thread': thread_name,
                'captured_at': _now_iso(),
                'num_entries': len(serialised),
                'entries': serialised,
            })
        except Exception as e:
            vd.debug(f'capture profile {thread_name}: {e}')


@VisiData.lazy_property
def profilesState(vd):
    desc = ProfileState()
    desc.register()
    return desc


# ---------------------------------------------------------------------------
# Standard stores (wired through the descriptor registry)
# ---------------------------------------------------------------------------

@VisiData.lazy_property
def optionsStore(vd):
    store = StateStore(name='options', phase=StatePhase.OPTIONS)
    store.register()
    return store


@VisiData.lazy_property
def profilesStore(vd):
    store = StateStore(name='profiles', phase=StatePhase.PROFILES)
    store.register()
    return store


@VisiData.lazy_property
def cacheManifest(vd):
    store = StateStore(name='cache_manifest', phase=StatePhase.CACHE)
    store.register()
    return store


@VisiData.lazy_property
def graphStateStore(vd):
    store = StateStore(name='graph_state', phase=StatePhase.GRAPH)
    store.register()
    return store


# ---------------------------------------------------------------------------
# Startup hook
# ---------------------------------------------------------------------------

@VisiData.before
@asyncthread
def run(vd, *args, **kwargs):
    '''Restore every registered StateDescriptor on startup, in phase order.'''
    vd.optionsState
    vd.profilesState
    vd.cmdlogState
    vd.optionsStore
    vd.profilesStore
    vd.cacheManifest
    vd.graphStateStore
    vd.restoreAllState()


# ---------------------------------------------------------------------------
# Export the public API on the VisiData singleton and module globals
# ---------------------------------------------------------------------------

VisiData.StateDescriptor = StateDescriptor
VisiData.StateStore = StateStore
VisiData.StatePhase = StatePhase
VisiData.OptionsState = OptionsState
VisiData.CmdlogState = CmdlogState
VisiData.ProfileState = ProfileState

vd.addGlobals(
    StateDescriptor=StateDescriptor,
    StateStore=StateStore,
    StatePhase=StatePhase,
    OptionsState=OptionsState,
    CmdlogState=CmdlogState,
    ProfileState=ProfileState,
)
