"""
# Sheet Diagnostics Framework — Three-Layer Architecture

Layer 1: Rule Registry      (DiagnosticRule + subclasses)
Layer 2: Diagnostic Runner  (DiagnosticRunner — owns lifecycle, cache keys, invalidation)
Layer 3: Display Panel      (DiagnosticsSheet — pure view, never triggers execution)

## Lifecycle

The runner owns the authoritative lifecycle for every source sheet:

1. Each sheet is identified by a *cache key*: ``(sheet_id, visible_col_ids,
   key_col_ids, row_ids)``.
2. ``runner.ensure(sheet, extra_aggrs)`` compares the current cache key to
   the stored one; if they differ it drops the old results and re-runs rules.
3. All mutation hooks (``reload``, ``Column.type``, ``rows.setter``,
   ``Column.hidden``, ``Column.keycol``, ``setKeys``) *only* invalidate the
   runner cache — they never touch a DiagnosticsSheet.
4. A DiagnosticsSheet.loader simply calls ``ensure`` on each of its source
   sheets and then reads from the cache — it never runs rules itself.
"""

import collections
import statistics
from copy import copy

from visidata import (
    vd, VisiData, Column, ColumnAttr, vlen, RowColorizer,
    Progress, wrapply, BaseSheet, TableSheet,
    ColumnsSheet, IndexSheet, TypedExceptionWrapper, anytype,
    TypedWrapper, ExplodingMock, Sheet,
)


vd.option('describe_aggrs', 'mean stdev', 'numeric aggregators to calculate on Diagnostics sheet', help=vd.help_aggregators if hasattr(vd, 'help_aggregators') else '')


# =============================================================================
# Layer 1 — Rule Registry
# =============================================================================


class DiagnosticResult:
    """Single diagnostic finding."""
    __slots__ = ('rule', 'target', 'value', 'rows', 'message')

    def __init__(self, rule, target, value=None, rows=None, message=''):
        self.rule = rule
        self.target = target
        self.value = value
        self.rows = rows or []
        self.message = message

    def __bool__(self):
        return bool(self.value)


class DiagnosticRule:
    """Base class for diagnostic rules.

    Either override ``compute`` or use the ``aggregator`` classmethod to wrap
    an entry from ``vd.aggregators``.  Implementations must lean on
    ``Column.getValueRows`` / ``Column.getValues`` / ``wrapply`` so they share
    VisiData's null / TypedExceptionWrapper contract.
    """
    name = ''
    label = ''
    type = anytype
    helpstr = ''
    scope = 'column'

    def applies(self, target):
        return True

    def compute(self, target, rows):
        raise NotImplementedError

    @classmethod
    def aggregator(cls, aggrname, type=anytype, label='', helpstr='', scope='column'):
        aggr = vd.aggregators.get(aggrname)
        _label = label or (aggr.helpstr if aggr else aggrname)
        _helpstr = helpstr or (aggr.helpstr if aggr else '')
        _type = type or (aggr.type if aggr else anytype)

        class _AggrRule(cls):
            pass

        _AggrRule.name = aggrname
        _AggrRule.label = _label
        _AggrRule.helpstr = _helpstr
        _AggrRule.type = _type
        _AggrRule.scope = scope
        _AggrRule._aggrname = aggrname

        def compute(self, target, rows):
            aggr = vd.aggregators.get(self._aggrname)
            if not aggr:
                return DiagnosticResult(rule=self, target=target, value=None)
            val = wrapply(aggr.aggregate, target, rows)
            if aggrname in ('mode', 'min', 'max', 'median') and val is not None and not isinstance(val, TypedExceptionWrapper):
                try:
                    return DiagnosticResult(rule=self, target=target, value=target.format(val))
                except Exception:
                    pass
            return DiagnosticResult(rule=self, target=target, value=val)

        _AggrRule.compute = compute
        return _AggrRule()


class ColumnDiagnosticRule(DiagnosticRule):
    scope = 'column'

    def applies(self, col):
        return isinstance(col, Column) and not col.hidden


class SheetDiagnosticRule(DiagnosticRule):
    scope = 'sheet'

    def applies(self, sheet):
        return isinstance(sheet, TableSheet)


vd.diagnosticRules = collections.OrderedDict()


@VisiData.api
def diagnostic(vd, rule):
    if not rule.name:
        raise ValueError('diagnostic rules must have a .name')
    vd.diagnosticRules[rule.name] = rule
    return rule


@VisiData.api
def diagnostic_rules(vd, scope=None):
    rules = list(vd.diagnosticRules.values())
    if scope:
        return [r for r in rules if r.scope == scope]
    return rules


# =============================================================================
# Layer 2 — Diagnostic Runner  (owns lifecycle: cache keys, invalidation, execution)
# =============================================================================


class DiagnosticRunner:
    """Executes rules, caches results, and tracks staleness via cache keys.

    The runner is the single authority for *when* diagnostics run.  Hooks on
    the source sheet / columns only ever call ``invalidate``; the runner
    decides on its own when to re-execute (via ``ensure``).
    """

    def __init__(self):
        self._cache = {}          # { sheet_id: { target_id: { rulename: DiagnosticResult } } }
        self._target_map = {}     # { sheet_id: { target_id: target } }  — keeps refs alive
        self._cache_keys = {}     # { sheet_id: cache_key_tuple }
        self._extra_aggrs = {}    # { sheet_id: tuple_of_aggrnames }

    # -- cache key ----------------------------------------------------------

    @staticmethod
    def _make_cache_key(sheet):
        """Return a tuple summarising the diagnostic inputs for *sheet*.

        Components:
        - sheet object identity
        - visible column ids (order matters — display order affects panel)
        - key column ids
        - row identity (``id(row)`` for each row; changes when rows are
          replaced, sorted, or filtered)
        """
        try:
            visible_ids = tuple(id(c) for c in getattr(sheet, 'visibleCols', []))
        except Exception:
            visible_ids = ()
        try:
            key_ids = tuple(id(c) for c in getattr(sheet, 'keyCols', []))
        except Exception:
            key_ids = ()
        try:
            row_ids = tuple(id(r) for r in sheet.rows)
        except Exception:
            row_ids = ()
        return (id(sheet), visible_ids, key_ids, row_ids)

    # -- lifecycle ----------------------------------------------------------

    def invalidate(self, sheet):
        """Drop cached results and recorded cache key for *sheet*.

        Called by mutation hooks; never triggers execution.
        """
        key = id(sheet)
        self._cache.pop(key, None)
        self._target_map.pop(key, None)
        self._cache_keys.pop(key, None)
        self._extra_aggrs.pop(key, None)

    def is_stale(self, sheet, extra_aggrs=()):
        """Return True if *sheet*'s cache key has changed or extra_aggrs differ."""
        cur = self._make_cache_key(sheet)
        stored = self._cache_keys.get(id(sheet))
        if stored is None or cur != stored:
            return True
        if tuple(extra_aggrs) != self._extra_aggrs.get(id(sheet), ()):
            return True
        return False

    def ensure(self, sheet, extra_aggrs=()):
        """Re-run rules if *sheet* is stale; otherwise no-op.

        This is the only public entry point that may trigger execution.
        """
        if self.is_stale(sheet, extra_aggrs):
            self.invalidate(sheet)
            self._run(sheet, extra_aggrs)

    # -- execution (private; call ensure() from outside) -------------------

    def _run(self, sheet, extra_aggrs=()):
        sid = id(sheet)
        per_sheet = self._cache.setdefault(sid, {})
        target_index = self._target_map.setdefault(sid, {})

        for rule in vd.diagnostic_rules(scope='sheet'):
            if not rule.applies(sheet):
                continue
            self._store(per_sheet, target_index, sheet, rule, rule.compute(sheet, sheet.rows))

        visible = [c for c in sheet.visibleCols if not c.hidden]
        for srccol in Progress(visible, 'diagnosing'):
            for rule in vd.diagnostic_rules(scope='column'):
                if not rule.applies(srccol):
                    continue
                try:
                    self._store(per_sheet, target_index, srccol, rule, rule.compute(srccol, sheet.rows))
                except Exception as e:
                    if vd.options.debug:
                        vd.exceptionCaught(e)

            if vd.isNumeric(srccol):
                for aggrname in extra_aggrs:
                    if aggrname in vd.diagnosticRules:
                        continue
                    aggr = vd.aggregators.get(aggrname)
                    if not aggr:
                        continue
                    val = wrapply(aggr.aggregate, srccol, sheet.rows)
                    sentinel = type('_S', (DiagnosticRule,), {'name': aggrname, 'type': float, 'scope': 'column'})()
                    self._store(per_sheet, target_index, srccol, sentinel,
                                DiagnosticResult(rule=sentinel, target=srccol, value=val))

        self._cache_keys[sid] = self._make_cache_key(sheet)
        self._extra_aggrs[sid] = tuple(extra_aggrs)

    # -- query API ----------------------------------------------------------

    def get(self, target, rulename):
        sheet = getattr(target, 'sheet', target)
        per_sheet = self._cache.get(id(sheet))
        if not per_sheet:
            return None
        return per_sheet.get(id(target), {}).get(rulename)

    def all_rules_for(self, target):
        sheet = getattr(target, 'sheet', target)
        per_sheet = self._cache.get(id(sheet))
        if not per_sheet:
            return {}
        return dict(per_sheet.get(id(target), {}))

    # -- internal -----------------------------------------------------------

    @staticmethod
    def _coerce(result):
        if isinstance(result, DiagnosticResult):
            yield result
        elif result is None:
            return
        else:
            for r in result or []:
                yield r

    def _store(self, per_sheet, target_index, target, rule, result):
        tid = id(target)
        target_index[tid] = target
        bucket = per_sheet.setdefault(tid, {})
        for r in self._coerce(result):
            bucket[rule.name] = r


vd.diagnosticRunner = DiagnosticRunner()


# -- invalidation hooks  (ONLY touch the runner — NEVER the views) ------------


@TableSheet.after
def reload(sheet):
    vd.diagnosticRunner.invalidate(sheet)


_orig_set_type = Column.type.fset


def _patched_type_setter(col, t):
    old_type = col._type
    _orig_set_type(col, t)
    if col._type != old_type:
        sheet = getattr(col, 'sheet', None)
        if sheet is not None and not isinstance(sheet, ExplodingMock):
            vd.diagnosticRunner.invalidate(sheet)


Column.type = Column.type.setter(_patched_type_setter)


_orig_set_rows = BaseSheet.rows.fset


def _patched_rows_setter(sheet, rows):
    _orig_set_rows(sheet, rows)
    if not isinstance(sheet, ExplodingMock):
        vd.diagnosticRunner.invalidate(sheet)


BaseSheet.rows = BaseSheet.rows.setter(_patched_rows_setter)


try:
    if hasattr(Column, 'setWidth'):
        _orig_setWidth = Column.setWidth

        def _patched_setWidth(col, w):
            old_hidden = col.hidden if col.width is not None else False
            _orig_setWidth(col, w)
            new_hidden = col.hidden
            if old_hidden != new_hidden:
                sheet = getattr(col, 'sheet', None)
                if sheet is not None and not isinstance(sheet, ExplodingMock):
                    vd.diagnosticRunner.invalidate(sheet)

        Column.setWidth = _patched_setWidth
except Exception:
    pass


try:
    _orig_set_width = Column.width.fset

    def _patched_width_setter(col, w):
        old_hidden = col.hidden if col.width is not None else False
        _orig_set_width(col, w)
        new_hidden = col.hidden
        if old_hidden != new_hidden:
            sheet = getattr(col, 'sheet', None)
            if sheet is not None and not isinstance(sheet, ExplodingMock):
                vd.diagnosticRunner.invalidate(sheet)

    Column.width = Column.width.setter(_patched_width_setter)
except Exception:
    pass


@Column.after
def hide(col, *args, **kwargs):
    sheet = getattr(col, 'sheet', None)
    if sheet is not None and not isinstance(sheet, ExplodingMock):
        vd.diagnosticRunner.invalidate(sheet)


try:
    _orig_col_setattr = Column.__setattr__

    def _patched_col_setattr(col, name, value):
        if name == 'keycol':
            old_keycol = getattr(col, 'keycol', 0)
            _orig_col_setattr(col, name, value)
            if int(old_keycol or 0) != int(value or 0):
                sheet = getattr(col, 'sheet', None)
                if sheet is not None and not isinstance(sheet, ExplodingMock):
                    vd.diagnosticRunner.invalidate(sheet)
        else:
            _orig_col_setattr(col, name, value)

    Column.__setattr__ = _patched_col_setattr
except Exception:
    pass


@TableSheet.after
def setKeys(sheet, cols):
    vd.diagnosticRunner.invalidate(sheet)


# =============================================================================
# Layer 3 — Display Panel  (pure view: show, filter, jump, export)
# =============================================================================


class DiagnosticColumn(Column):
    """Column on DiagnosticsSheet — reads only from ``vd.diagnosticRunner``.

    Owns no cache and triggers no computation.  Automatically coerces sets to
    their length so aggregators like ``distinct`` display as counts.
    """
    def __init__(self, name, **kwargs):
        kwargs.setdefault('width', 10)
        super().__init__(
            name,
            getter=self._get_value,
            expr=name,
            **kwargs,
        )

    def _get_value(self, col, target):
        r = vd.diagnosticRunner.get(target, col.expr) or DiagnosticResult(rule=None, target=target)
        v = r.value
        if isinstance(v, set):
            return len(v)
        return v


class DiagnosticsSheet(ColumnsSheet):
    """Display panel — pure view, owns no execution logic.

    ``loader`` asks the runner to ``ensure`` each source sheet is up-to-date
    and then renders whatever is in the cache.  It never calls ``runner.run``
    directly.
    """
    guide = '''
        # Diagnostics Sheet

        This sheet shows data-quality findings for columns on
        {sheet.displaySource}.  Each row corresponds to a source column; each
        column corresponds to a diagnostic rule.

        - `Enter` on a cell with offending rows to open a filtered copy.
        - `zs` / `zu` select / unselect those rows on the original sheet.
    '''
    precious = True
    rowtype = 'columns'
    columns = [
        ColumnAttr('sheet', 'sheet', width=0),
        ColumnAttr('column', 'name'),
        ColumnAttr('type', 'typestr', width=0),
    ]
    colorizers = [
        RowColorizer(7, 'color_key_col', lambda s, c, r, v: r and r in r.sheet.keyCols),
    ]
    nKeys = 2

    def loader(self):
        ColumnsSheet.loader(self)
        self.rows = [c for c in self.rows if not c.hidden]
        self.resetCols()

        extra_aggrs = tuple(vd.options.describe_aggrs.split())

        # Ask the runner to bring each source sheet up-to-date if stale
        for srcsheet in self._sourceSheets():
            vd.diagnosticRunner.ensure(srcsheet, extra_aggrs=extra_aggrs)

        for rule in vd.diagnostic_rules(scope='column'):
            self.addColumn(DiagnosticColumn(rule.name, type=rule.type))
        for aggrname in extra_aggrs:
            if aggrname not in vd.diagnosticRules:
                self.addColumn(DiagnosticColumn(aggrname, type=float))

    # -- helpers ------------------------------------------------------------

    def _sourceSheets(self):
        sheets = []
        seen = set()
        for target in self.rows:
            s = getattr(target, 'sheet', None)
            if s and id(s) not in seen:
                seen.add(id(s))
                sheets.append(s)
        return sheets

    def _resultAt(self, col, row):
        if not isinstance(col, DiagnosticColumn):
            return None
        return vd.diagnosticRunner.get(row, col.expr)

    # -- user-facing actions ------------------------------------------------

    def openCell(self, col, row):
        """Open a copy of the source sheet filtered to the offending rows."""
        result = self._resultAt(col, row)
        if result and result.rows:
            vs = copy(row.sheet)
            vs.rows = list(result.rows)
            vs.name += '_%s_%s' % (row.name, col.name)
            return vs
        vd.warning(result.message if result else 'no rows')

    def selectCell(self, col, row):
        result = self._resultAt(col, row)
        if result and result.rows:
            row.sheet.select(result.rows)
        else:
            vd.warning('no rows to select')

    def unselectCell(self, col, row):
        result = self._resultAt(col, row)
        if result and result.rows:
            row.sheet.unselect(result.rows)
        else:
            vd.warning('no rows to unselect')


# =============================================================================
# Built-in Rules  (all reuse vd.aggregators, getValueRows, or wrapply)
# =============================================================================


class _NullsRule(ColumnDiagnosticRule):
    name = 'nulls'
    label = 'Nulls'
    type = vlen
    helpstr = 'count of null/missing values'

    def compute(self, col, rows):
        isNull = col.sheet.isNullFunc()
        valid_ids = {id(r) for _, r in col.getValueRows(rows)}
        bad = [r for r in rows if id(r) not in valid_ids and isNull(col.getValue(r))]
        return DiagnosticResult(rule=self, target=col, value=len(bad), rows=bad)


class _ErrorsRule(ColumnDiagnosticRule):
    name = 'errors'
    label = 'Type Errors'
    type = vlen
    helpstr = 'count of values that fail type coercion'

    def compute(self, col, rows):
        isNull = col.sheet.isNullFunc()
        valid_ids = {id(r) for _, r in col.getValueRows(rows)}
        bad = []
        for r in rows:
            if id(r) in valid_ids:
                continue
            try:
                v = col.getValue(r)
                if not isNull(v):
                    col.type(v)
            except Exception:
                bad.append(r)
        return DiagnosticResult(rule=self, target=col, value=len(bad), rows=bad)


class _HighCardinalityRule(ColumnDiagnosticRule):
    name = 'high_cardinality'
    label = 'High Card'
    type = vlen
    helpstr = 'count if string column has >90% distinct values'

    def applies(self, col):
        return super().applies(col) and col.type is str

    def compute(self, col, rows):
        distinct_agg = vd.aggregators['distinct']
        distinct_set = wrapply(distinct_agg.aggregate, col, rows) or set()
        count_agg = vd.aggregators['count']
        total = wrapply(count_agg.aggregate, col, rows) or 0
        ndistinct = len(distinct_set)
        if total == 0 or ndistinct / total < 0.9:
            return DiagnosticResult(rule=self, target=col, value=0)
        return DiagnosticResult(rule=self, target=col, value=ndistinct,
                                message='string column with >=90% distinct values')


class _DateParseFailuresRule(ColumnDiagnosticRule):
    name = 'date_parse_failures'
    label = 'Date Failures'
    type = vlen
    helpstr = 'rows that fail to parse as date'

    def applies(self, col):
        return super().applies(col) and getattr(col.type, '__name__', '') == 'date'

    def compute(self, col, rows):
        from visidata.type_date import date as _date_type
        isNull = col.sheet.isNullFunc()
        valid_ids = {id(r) for _, r in col.getValueRows(rows)}
        bad = []
        for r in rows:
            if id(r) in valid_ids:
                continue
            try:
                v = col.getValue(r)
                if not isNull(v) and not isinstance(v, _date_type):
                    _date_type(v)
            except Exception:
                bad.append(r)
        return DiagnosticResult(rule=self, target=col, value=len(bad), rows=bad)


class _OutliersRule(ColumnDiagnosticRule):
    name = 'outliers'
    label = 'Outliers'
    type = vlen
    helpstr = 'count of values outside 1.5 * IQR'

    def applies(self, col):
        return super().applies(col) and vd.isNumeric(col)

    def compute(self, col, rows):
        val_rows = list(col.getValueRows(rows))
        if len(val_rows) < 4:
            return DiagnosticResult(rule=self, target=col, value=0)
        sorted_vals = sorted(float(v) for v, _ in val_rows)
        n = len(sorted_vals)
        q1 = sorted_vals[n // 4]
        q3 = sorted_vals[(3 * n) // 4]
        iqr = q3 - q1
        low = q1 - 1.5 * iqr
        high = q3 + 1.5 * iqr
        bad = [r for v, r in val_rows if v < low or v > high]
        return DiagnosticResult(rule=self, target=col, value=len(bad), rows=bad)


class _DuplicateRowsRule(SheetDiagnosticRule):
    name = 'duplicates'
    label = 'Duplicate Rows'
    type = vlen
    helpstr = 'rows duplicated by key columns (or all visible columns)'

    def compute(self, sheet, rows):
        cols = sheet.keyCols or sheet.visibleCols
        seen = set()
        dupes = []
        for r in rows:
            key = tuple()
            ok = True
            for c in cols:
                try:
                    key += (c.getValue(r),)
                except Exception:
                    ok = False
                    break
            if not ok:
                continue
            if key in seen:
                dupes.append(r)
            else:
                seen.add(key)
        return DiagnosticResult(rule=self, target=sheet, value=len(dupes), rows=dupes)


for _rule in [
    _NullsRule(),
    _ErrorsRule(),
    ColumnDiagnosticRule.aggregator('distinct', type=vlen),
    ColumnDiagnosticRule.aggregator('mode', type=str),
    ColumnDiagnosticRule.aggregator('min', type=str),
    ColumnDiagnosticRule.aggregator('max', type=str),
    ColumnDiagnosticRule.aggregator('sum'),
    ColumnDiagnosticRule.aggregator('median', type=str),
    _HighCardinalityRule(),
    _DateParseFailuresRule(),
    _OutliersRule(),
    _DuplicateRowsRule(),
]:
    vd.diagnostic(_rule)


# =============================================================================
# Commands
# =============================================================================


TableSheet.addCommand('', 'diagnostics-sheet', 'vd.push(DiagnosticsSheet(sheet.name+"_diagnostics", source=[sheet]))', 'open Diagnostics Sheet with data-quality findings for all visible columns')

DiagnosticsSheet.addCommand('zs', 'select-cell', 'sheet.selectCell(cursorCol, cursorRow)', 'select rows on source sheet reported in current cell')
DiagnosticsSheet.addCommand('zu', 'unselect-cell', 'sheet.unselectCell(cursorCol, cursorRow)', 'unselect rows on source sheet reported in current cell')


vd.addGlobals({
    'DiagnosticsSheet': DiagnosticsSheet,
    'DiagnosticRule': DiagnosticRule,
    'ColumnDiagnosticRule': ColumnDiagnosticRule,
    'SheetDiagnosticRule': SheetDiagnosticRule,
    'DiagnosticResult': DiagnosticResult,
    'DiagnosticColumn': DiagnosticColumn,
    'DiagnosticRunner': DiagnosticRunner,
})
