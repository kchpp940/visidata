"""
# Sheet Diagnostics Framework

Reusable diagnostic rule registry and results display panel.

## Registering a Rule

```python
from visidata.features.diagnostics import ColumnDiagnosticRule, vd

class NullsRule(ColumnDiagnosticRule):
    name = 'nulls'
    label = 'Null Values'
    type = int  # column type for the result cell
    helpstr = 'count of null/missing values'

    def apply(self, col, rows):
        isNull = col.sheet.isNullFunc()
        bad = [r for r in rows if isNull(col.getValue(r))]
        return DiagnosticResult(
            rule=self,
            target=col,
            value=len(bad),
            rows=bad,
        )

vd.diagnostic(NullsRule())
```

## Scope

Two rule scopes exist:

- ``ColumnDiagnosticRule`` -- operates per-column (e.g. nulls, type errors)
- ``SheetDiagnosticRule`` -- operates per-sheet (e.g. duplicate rows)

Each rule returns zero or more ``DiagnosticResult`` objects.

## Results Panel

``DiagnosticsSheet`` is a ColumnsSheet-like view that:
1. Runs all registered rules over the source sheet(s).
2. Displays every result as a cell whose value is the reported metric.
3. Delegates ``openCell`` to jump to the offending rows.
4. Re-runs automatically when the source sheet reloads, a column type changes,
   or a derived view is opened.
"""

import collections
import statistics
from copy import copy

from visidata import (
    vd, VisiData, Column, ColumnAttr, vlen, RowColorizer,
    asyncthread, Progress, wrapply, BaseSheet, TableSheet,
    ColumnsSheet, IndexSheet, TypedExceptionWrapper, anytype,
    TypedWrapper, ExplodingMock,
)


vd.option('describe_aggrs', 'mean stdev', 'numeric aggregators to calculate on Diagnostics sheet', help=vd.help_aggregators if hasattr(vd, 'help_aggregators') else '')


class DiagnosticResult:
    """Single diagnostic finding for one target (column or sheet).

    Attributes:
        rule: the DiagnosticRule that produced this result.
        target: the Column or Sheet that was diagnosed.
        value: the scalar metric displayed in the cell (int, float, str, ...).
        rows: optional list of offending source rows (used by openCell).
        message: optional human-readable detail string shown on hover.
    """
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

    Subclasses must override ``apply``.

    Attributes:
        name: unique kebab-case identifier (used as column name).
        label: short human-readable label.
        type: expected type of the ``DiagnosticResult.value``.
        helpstr: longer description shown in help.
        applies: predicate ``(target) -> bool``; True if rule can run on target.
    """
    name = ''
    label = ''
    type = anytype
    helpstr = ''

    def applies(self, target):
        return True

    def apply(self, target, rows):
        """Run the diagnostic on *target* over *rows*.

        Must return a single ``DiagnosticResult`` or an iterable of them.
        """
        raise NotImplementedError


class ColumnDiagnosticRule(DiagnosticRule):
    """Rule that runs once per visible column on the source sheet."""

    def applies(self, col):
        return isinstance(col, Column) and not col.hidden


class SheetDiagnosticRule(DiagnosticRule):
    """Rule that runs once per source sheet."""

    def applies(self, sheet):
        return isinstance(sheet, TableSheet)


class _SentinelRule(DiagnosticRule):
    """Lightweight rule used on the fly for describe_aggrs aggregators."""

    def __init__(self, name, type=anytype):
        self.name = name
        self.type = type
        self.label = name
        self.helpstr = ''

    def apply(self, target, rows):
        return None


vd.diagnosticRules = collections.OrderedDict()  # [rulename] -> DiagnosticRule


@VisiData.api
def diagnostic(vd, rule):
    """Register a DiagnosticRule *rule* so DiagnosticsSheet will run it."""
    if not rule.name:
        raise ValueError('diagnostic rules must have a .name')
    vd.diagnosticRules[rule.name] = rule
    return rule


@VisiData.api
def diagnostic_rules(vd, scope=None):
    """Return the list of registered rules, optionally filtered by *scope*.

    *scope* may be ``'column'``, ``'sheet'``, or ``None`` (all).
    """
    rules = list(vd.diagnosticRules.values())
    if scope == 'column':
        return [r for r in rules if isinstance(r, ColumnDiagnosticRule)]
    if scope == 'sheet':
        return [r for r in rules if isinstance(r, SheetDiagnosticRule)]
    return rules


class DiagnosticColumn(Column):
    """A column on DiagnosticsSheet that renders a single DiagnosticRule.

    ``expr`` holds the rule name; the cell value is looked up from
    ``sheet.diagnosticData[target][rulename]``.
    """
    def __init__(self, name, **kwargs):
        kwargs.setdefault('width', 10)
        super().__init__(
            name,
            getter=lambda col, target: (
                col.sheet.diagnosticData
                .get(target, {})
                .get(col.expr, DiagnosticResult(rule=None, target=target))
            ).value,
            expr=name,
            **kwargs,
        )


class DiagnosticsSheet(ColumnsSheet):
    """Display diagnostic findings over columns of the source sheet(s).

    Rows are the source sheet's columns; columns correspond to registered
    diagnostic rules.  Cell values are the metric reported by each rule;
    ``openCell`` opens the offending rows on a copy of the source sheet.
    """
    guide = '''
        # Diagnostics Sheet

        This sheet shows data-quality findings for columns on
        {sheet.displaySource}.  Each row corresponds to a source column; each
        column corresponds to a diagnostic rule.

        - `Enter` on a cell with a list of rows to open a filtered copy.
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.diagnosticData = {}  # { target: { rulename: DiagnosticResult } }

    def loader(self):
        super().loader()
        self.rows = [c for c in self.rows if not c.hidden]
        self.diagnosticData = {}
        self.resetCols()

        for rule in vd.diagnostic_rules(scope='column'):
            self.addColumn(DiagnosticColumn(rule.name, type=rule.type))

        for aggrname in vd.options.describe_aggrs.split():
            if aggrname in vd.diagnosticRules:
                continue
            aggr = vd.aggregators.get(aggrname)
            if aggr:
                self.addColumn(DiagnosticColumn(aggrname, type=float))

        self._runAll()

    # -- internal ----------------------------------------------------------

    def _runAll(self):
        col_rules = vd.diagnostic_rules(scope='column')
        sheet_rules = vd.diagnostic_rules(scope='sheet')
        extra_aggrs = [
            (name, vd.aggregators[name])
            for name in vd.options.describe_aggrs.split()
            if name in vd.aggregators and name not in vd.diagnosticRules
        ]

        for srcsheet in self._sourceSheets():
            for rule in sheet_rules:
                if not rule.applies(srcsheet):
                    continue
                for res in self._coerce(rule.apply(srcsheet, srcsheet.rows)):
                    self._record(res)

            for srccol in Progress(self.rows, 'diagnosing'):
                if srccol.sheet is not srcsheet:
                    continue
                for rule in col_rules:
                    if not rule.applies(srccol):
                        continue
                    try:
                        for res in self._coerce(rule.apply(srccol, srcsheet.rows)):
                            self._record(res)
                    except Exception as e:
                        if vd.options.debug:
                            vd.exceptionCaught(e)

                if vd.isNumeric(srccol):
                    for aggrname, aggr in extra_aggrs:
                        aggrs = aggr if isinstance(aggr, list) else [aggr]
                        for a in aggrs:
                            try:
                                val = wrapply(a.aggregate, srccol, srcsheet.rows)
                                self._record(DiagnosticResult(
                                    rule=_SentinelRule(aggrname, type=float),
                                    target=srccol,
                                    value=val,
                                ))
                            except Exception as e:
                                if vd.options.debug:
                                    vd.exceptionCaught(e)

    def _sourceSheets(self):
        sheets = []
        seen = set()
        for target in self.rows:
            s = getattr(target, 'sheet', None)
            if s and id(s) not in seen:
                seen.add(id(s))
                sheets.append(s)
        return sheets

    @staticmethod
    def _coerce(result):
        if isinstance(result, DiagnosticResult):
            yield result
        elif result is None:
            return
        else:
            for r in result or []:
                yield r

    def _record(self, result):
        self.diagnosticData.setdefault(result.target, {})[result.rule.name] = result

    # -- user-facing actions -----------------------------------------------

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

    def _resultAt(self, col, row):
        if not isinstance(col, DiagnosticColumn):
            return None
        return self.diagnosticData.get(row, {}).get(col.expr)


# -- auto-reload hooks ---------------------------------------------------------

def _invalidateDiagnostics(sheet):
    """Mark diagnostics on dependent DiagnosticsSheet instances as stale."""
    for vs in list(vd.sheets):
        if isinstance(vs, DiagnosticsSheet) and sheet in vs._sourceSheets():
            vs.diagnosticData.clear()
            vs.reload()


@TableSheet.after
def reload(sheet):
    _invalidateDiagnostics(sheet)


_orig_set_type = Column.type.fset


def _patched_type_setter(col, t):
    old_type = col._type
    _orig_set_type(col, t)
    if col._type != old_type:
        sheet = getattr(col, 'sheet', None)
        if sheet is not None and not isinstance(sheet, ExplodingMock):
            _invalidateDiagnostics(sheet)


Column.type = Column.type.setter(_patched_type_setter)


# -- commands -----------------------------------------------------------------

TableSheet.addCommand('', 'diagnostics-sheet', 'vd.push(DiagnosticsSheet(sheet.name+"_diagnostics", source=[sheet]))', 'open Diagnostics Sheet with data-quality findings for all visible columns')

DiagnosticsSheet.addCommand('zs', 'select-cell', 'sheet.selectCell(cursorCol, cursorRow)', 'select rows on source sheet reported in current cell')
DiagnosticsSheet.addCommand('zu', 'unselect-cell', 'sheet.unselectCell(cursorCol, cursorRow)', 'unselect rows on source sheet reported in current cell')

# -- built-in diagnostic rules -----------------------------------------------


class _NullsRule(ColumnDiagnosticRule):
    name = 'nulls'
    label = 'Nulls'
    type = vlen
    helpstr = 'count of null/missing values'

    def apply(self, col, rows):
        isNull = col.sheet.isNullFunc()
        bad = []
        for r in rows:
            try:
                if isNull(col.getValue(r)):
                    bad.append(r)
            except Exception:
                pass
        return DiagnosticResult(rule=self, target=col, value=len(bad), rows=bad)


class _ErrorsRule(ColumnDiagnosticRule):
    name = 'errors'
    label = 'Type Errors'
    type = vlen
    helpstr = 'count of values that fail type coercion'

    def apply(self, col, rows):
        bad = []
        isNull = col.sheet.isNullFunc()
        for r in rows:
            try:
                v = col.getValue(r)
                if v is not None and not isNull(v):
                    col.type(v)
            except Exception:
                bad.append(r)
        return DiagnosticResult(rule=self, target=col, value=len(bad), rows=bad)


class _DistinctRule(ColumnDiagnosticRule):
    name = 'distinct'
    label = 'Distinct'
    type = vlen
    helpstr = 'number of distinct non-null values'

    def apply(self, col, rows):
        vals = set()
        isNull = col.sheet.isNullFunc()
        for r in rows:
            try:
                v = col.getTypedValue(r)
                if not isNull(v):
                    vals.add(v)
            except Exception:
                pass
        return DiagnosticResult(rule=self, target=col, value=len(vals))


class _ModeRule(ColumnDiagnosticRule):
    name = 'mode'
    label = 'Mode'
    type = str
    helpstr = 'most common value'

    def applies(self, col):
        return super().applies(col) and col.type is not None

    def apply(self, col, rows):
        vals = []
        isNull = col.sheet.isNullFunc()
        for r in rows:
            try:
                v = col.getTypedValue(r)
                if not isNull(v):
                    vals.append(v)
            except Exception:
                pass
        if not vals:
            return DiagnosticResult(rule=self, target=col, value=None)
        try:
            m = statistics.mode(vals)
        except statistics.StatisticsError:
            m = vals[0]
        return DiagnosticResult(rule=self, target=col, value=col.format(m))


class _MinRule(ColumnDiagnosticRule):
    name = 'min'
    label = 'Min'
    type = str
    helpstr = 'minimum value'

    def applies(self, col):
        return super().applies(col) and vd.isNumeric(col)

    def apply(self, col, rows):
        vals = []
        isNull = col.sheet.isNullFunc()
        for r in rows:
            try:
                v = col.getTypedValue(r)
                if not isNull(v):
                    vals.append(v)
            except Exception:
                pass
        if not vals:
            return DiagnosticResult(rule=self, target=col, value=None)
        return DiagnosticResult(rule=self, target=col, value=col.format(min(vals)))


class _MaxRule(ColumnDiagnosticRule):
    name = 'max'
    label = 'Max'
    type = str
    helpstr = 'maximum value'

    def applies(self, col):
        return super().applies(col) and vd.isNumeric(col)

    def apply(self, col, rows):
        vals = []
        isNull = col.sheet.isNullFunc()
        for r in rows:
            try:
                v = col.getTypedValue(r)
                if not isNull(v):
                    vals.append(v)
            except Exception:
                pass
        if not vals:
            return DiagnosticResult(rule=self, target=col, value=None)
        return DiagnosticResult(rule=self, target=col, value=col.format(max(vals)))


class _SumRule(ColumnDiagnosticRule):
    name = 'sum'
    label = 'Sum'
    helpstr = 'sum of numeric values'

    def applies(self, col):
        return super().applies(col) and vd.isNumeric(col)

    def apply(self, col, rows):
        total = 0
        started = False
        isNull = col.sheet.isNullFunc()
        for r in rows:
            try:
                v = col.getTypedValue(r)
                if not isNull(v):
                    total = total + v
                    started = True
            except Exception:
                pass
        return DiagnosticResult(rule=self, target=col, value=total if started else None)


class _MedianRule(ColumnDiagnosticRule):
    name = 'median'
    label = 'Median'
    type = str
    helpstr = 'median of numeric values'

    def applies(self, col):
        return super().applies(col) and vd.isNumeric(col)

    def apply(self, col, rows):
        vals = []
        isNull = col.sheet.isNullFunc()
        for r in rows:
            try:
                v = col.getTypedValue(r)
                if not isNull(v):
                    vals.append(v)
            except Exception:
                pass
        if not vals:
            return DiagnosticResult(rule=self, target=col, value=None)
        return DiagnosticResult(rule=self, target=col, value=col.format(statistics.median(vals)))


class _HighCardinalityRule(ColumnDiagnosticRule):
    name = 'high_cardinality'
    label = 'High Card'
    type = vlen
    helpstr = 'count if string column has >90% distinct values'

    def applies(self, col):
        return super().applies(col) and col.type is str

    def apply(self, col, rows):
        isNull = col.sheet.isNullFunc()
        seen = set()
        total = 0
        for r in rows:
            try:
                v = col.getTypedValue(r)
                if not isNull(v):
                    total += 1
                    seen.add(v)
            except Exception:
                pass
        if total == 0 or len(seen) / total < 0.9:
            return DiagnosticResult(rule=self, target=col, value=0)
        return DiagnosticResult(
            rule=self, target=col, value=len(seen),
            message='string column with >=90% distinct values'
        )


class _DateParseFailuresRule(ColumnDiagnosticRule):
    name = 'date_parse_failures'
    label = 'Date Failures'
    type = vlen
    helpstr = 'rows that fail to parse as date'

    def applies(self, col):
        if not super().applies(col):
            return False
        return getattr(col.type, '__name__', '') == 'date'

    def apply(self, col, rows):
        from visidata.type_date import date as _date_type
        bad = []
        isNull = col.sheet.isNullFunc()
        for r in rows:
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

    def apply(self, col, rows):
        vals = []
        isNull = col.sheet.isNullFunc()
        for r in rows:
            try:
                v = float(col.getTypedValue(r))
                if not isNull(v):
                    vals.append((r, v))
            except Exception:
                pass
        if len(vals) < 4:
            return DiagnosticResult(rule=self, target=col, value=0)
        sorted_vals = sorted(v for _, v in vals)
        n = len(sorted_vals)
        q1 = sorted_vals[n // 4]
        q3 = sorted_vals[(3 * n) // 4]
        iqr = q3 - q1
        low = q1 - 1.5 * iqr
        high = q3 + 1.5 * iqr
        bad = [r for r, v in vals if v < low or v > high]
        return DiagnosticResult(rule=self, target=col, value=len(bad), rows=bad)


class _DuplicateRowsRule(SheetDiagnosticRule):
    name = 'duplicates'
    label = 'Duplicate Rows'
    type = vlen
    helpstr = 'count of rows duplicated by key columns (or all visible columns)'

    def apply(self, sheet, rows):
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
    _DistinctRule(),
    _ModeRule(),
    _MinRule(),
    _MaxRule(),
    _SumRule(),
    _MedianRule(),
    _HighCardinalityRule(),
    _DateParseFailuresRule(),
    _OutliersRule(),
    _DuplicateRowsRule(),
]:
    vd.diagnostic(_rule)


vd.addGlobals({
    'DiagnosticsSheet': DiagnosticsSheet,
    'DiagnosticRule': DiagnosticRule,
    'ColumnDiagnosticRule': ColumnDiagnosticRule,
    'SheetDiagnosticRule': SheetDiagnosticRule,
    'DiagnosticResult': DiagnosticResult,
    'DiagnosticColumn': DiagnosticColumn,
})
