'''Unified session state persistence for VisiData.

Provides a common StateStore interface used by options, macros, cmdlog,
profiles, named selections, cache manifest, and graph state.

Standard record envelope (all optional):
    _v      -- schema version of this record (int)
    _ts     -- ISO-8601 timestamp when record was written
    _id     -- stable unique identifier (string)
    _scope  -- sheet/global context where this record applies

Standard store features:
    - JSON Lines backend (backwards compatible with plain JSONL records)
    - Sensitive field redaction on save (opt-in via ``sensitive_fields``)
    - Relative path normalization against ``vd.data_dir``
    - Ordered restore phases (StatePhase enum)
'''

import json
import os
import time
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
    OPTIONS   = 10   # options, global settings
    PROFILES  = 20   # user profiles, environment
    MACROS    = 30   # macros and custom commands (need options first)
    SELECTIONS = 40  # named selections (need sheets loaded)
    CACHE     = 50   # cache manifest
    GRAPH     = 60   # graph state (reflines, viewport)
    LAYOUT    = 70   # window/layout state (restored last)

    _order = [OPTIONS, PROFILES, MACROS, SELECTIONS, CACHE, GRAPH, LAYOUT]

    @classmethod
    def all_phases(cls):
        return sorted(cls._order)


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


class StateStore:
    '''Abstract base for a persistent keyed collection of state records.

    Subclasses must implement ``_read_records``, ``_write_records``, and
    may override ``_serialize`` / ``_deserialize``.

    Each record is a dict-like (AttrDict) with a standard envelope:
        _v, _ts, _id, _scope
    plus caller-supplied data fields.
    '''

    phase = StatePhase.LAYOUT
    sensitive_fields = ()  # tuple of field names to redact on save

    def __init__(self, name: str, **kwargs):
        self.name = name
        self._records = []
        self._by_id = {}
        self._dirty = False
        for k, v in kwargs.items():
            setattr(self, k, v)

    # -- path helpers ---------------------------------------------------

    @property
    def path(self) -> Path:
        vdpath = vd.data_dir
        if not vdpath.exists():
            if vd.options.nothing:
                return None
            vdpath.mkdir(parents=True)
        return vdpath / (self.name + '.jsonl')

    def _normalize_relative_paths(self, record, base_path=None):
        '''Convert absolute paths inside record to relative against *base_path*.'''
        base = base_path or (self.path.parent if self.path else None)
        if not base:
            return record
        return _walk_paths(record, lambda p: _relpath_if_under(p, base))

    def _resolve_relative_paths(self, record, base_path=None):
        '''Resolve relative paths inside record against *base_path*.'''
        base = base_path or (self.path.parent if self.path else None)
        if not base:
            return record
        return _walk_paths(record, lambda p: _abspath_if_relative(p, base))

    # -- core CRUD ------------------------------------------------------

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
        '''Add or replace a record.  ``record`` is dict-like.'''
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

    # -- subclass hooks -------------------------------------------------

    def _ensure_envelope(self, rec):
        rec.setdefault('_v', STATE_VERSION)
        rec.setdefault('_ts', _now_iso())
        rec.setdefault('_scope', 'global')
        if not rec.get('_id'):
            rec['_id'] = f'{self.name}-{len(self._records)}-{int(time.time()*1000)}'

    def _deserialize(self, line: str):
        '''Parse one line from the backing store into a record dict.'''
        obj = json.loads(line)
        if isinstance(obj, dict):
            obj = AttrDict(obj)
            self._resolve_relative_paths(obj)
            return obj
        return AttrDict(data=obj)

    def _serialize(self, record) -> str:
        '''Convert one record to its on-disk string representation.'''
        out = self._normalize_relative_paths(dict(record))
        out = self._redact(out)
        return json.dumps(out, ensure_ascii=False, default=str)

    def _redact(self, record: dict) -> dict:
        '''Return a shallow copy with sensitive fields replaced.'''
        if not self.sensitive_fields:
            return record
        out = dict(record)
        for fname in self.sensitive_fields:
            if fname in out:
                out[fname] = _redact_value(out[fname])
        # also auto-redact fields whose names look sensitive
        for k, v in list(out.items()):
            if _is_sensitive_field(k):
                out[k] = _redact_value(v)
        return out

    # -- registration with global restore manager ----------------------

    def register_for_restore(self):
        '''Register this store so it is reloaded on startup in the right phase.'''
        if not hasattr(vd, '_state_stores'):
            vd._state_stores = {}
        vd._state_stores.setdefault(self.phase, []).append(self)
        return self

    # -- list compatibility (backwards compat with StoredList) ---------

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
        '''Drop-in compatible with ``list.append`` / ``StoredList.append``.'''
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
        # try to make relative; fall back to original
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


# -- global restore manager -----------------------------------------------

@VisiData.api
def restoreState(vd, phases=None):
    '''Reload all registered StateStores, in phase order.'''
    if phases is None:
        phases = StatePhase.all_phases()
    for phase in sorted(phases):
        for store in vd._state_stores.get(phase, []):
            vd.callNoExceptions(store.reload)


VisiData.init('_state_stores', dict)  # phase -> [StateStore]


# -- backwards-compatible StoredList lives in visidata.stored_list  -------
#    (see stored_list.py) which subclasses StateStore.


# -- convenience: define some standard stores -----------------------------

@VisiData.lazy_property
def optionsStore(vd):
    '''Persistent store for global option overrides.'''
    store = StateStore(name='options')
    store.phase = StatePhase.OPTIONS
    store.sensitive_fields = ()  # auto-detect redaction handles tokens etc.
    store.register_for_restore()
    return store


@VisiData.lazy_property
def profilesStore(vd):
    '''Persistent store for user profiles.'''
    store = StateStore(name='profiles')
    store.phase = StatePhase.PROFILES
    store.register_for_restore()
    return store


@VisiData.lazy_property
def cacheManifest(vd):
    '''Persistent store mapping cache keys to metadata.'''
    store = StateStore(name='cache_manifest')
    store.phase = StatePhase.CACHE
    store.register_for_restore()
    return store


@VisiData.lazy_property
def graphStateStore(vd):
    '''Persistent store for per-sheet graph view state (reflines, viewport).'''
    store = StateStore(name='graph_state')
    store.phase = StatePhase.GRAPH
    store.register_for_restore()
    return store


@VisiData.api
def applyPersistedOptions(vd):
    '''Apply option values that were persisted to optionsStore.'''
    try:
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
    except Exception as e:
        vd.debug(f'failed to apply persisted options: {e}')


@VisiData.before
@asyncthread
def run(vd, *args, **kwargs):
    '''Restore all registered state stores early in startup, then apply persisted options.'''
    vd.restoreState()
    vd.applyPersistedOptions()


VisiData.StateStore = StateStore
VisiData.StatePhase = StatePhase

vd.addGlobals(
    StateStore=StateStore,
    StatePhase=StatePhase,
)
