from copy import copy
from collections import Counter
import statistics

from visidata import vd, Sheet, TableSheet, Column
from visidata import Progress, TypedExceptionWrapper, TypedWrapper
from visidata import RowColorizer, vlen, anytype, date


vd.option('dq_high_cardinality_threshold', 0.9, 'ratio threshold for flagging high-cardinality string columns')
vd.option('dq_iqr_multiplier', 1.5, 'IQR multiplier for outlier detection')
vd.option('dq_min_unique_for_cardinality', 10, 'minimum unique values before considering cardinality ratio')


Sheet.init('_dq_version', lambda: 0)
Sheet.init('_dq_dependents', list)


def _bump_dq_version(sheet):
    'Increment the data-quality version counter on *sheet* and notify dependents.'
    sheet._dq_version += 1
    for dep in list(sheet._dq_dependents):
        if hasattr(dep, '_dq_source_changed'):
            dep._dq_source_changed()


@Sheet.after
def reload(sheet):
    _bump_dq_version(sheet)


@Sheet.after
def recalc(sheet):
    _bump_dq_version(sheet)


class DQIssue:
    '''Represents a single data quality issue found on a source sheet.

    Stores row identity (rowids) rather than row object references,
    so the issue remains valid across source sheet reloads as long as
    the row objects retain their identity.
    '''

    def __init__(self, issue_type, column, severity, description, *,
                 source_sheet=None, rowids=None, example_value=None):
        self.issue_type = issue_type
        self._column = column
        self.severity = severity
        self.description = description
        self._source_sheet = source_sheet or (column.sheet if column else None)
        self._rowids = set(rowids) if rowids is not None else set()
        self.example_value = example_value

    @property
    def colname(self):
        return self._column.name if self._column else ''

    @property
    def column(self):
        return self._column

    @property
    def source_sheet(self):
        return self._source_sheet

    @property
    def count(self):
        return len(self._rowids)

    def add_rowid(self, rowid):
        self._rowids.add(rowid)

    def resolve_rows(self):
        'Return list of actual row objects from the source sheet by rowid. Skips rows that no longer exist.'
        if not self._source_sheet:
            return []
        id_to_row = {self._source_sheet.rowid(r): r for r in self._source_sheet.rows}
        return [id_to_row[rid] for rid in self._rowids if rid in id_to_row]

    def resolve_first_row(self):
        'Return the first still-existing row object, or None.'
        rows = self.resolve_rows()
        return rows[0] if rows else None


class DQRule:
    '''Base class for reusable data quality rules.

    Subclasses must implement:
      - rule_name (class attr): short stable identifier
      - default_severity (class attr): 'error' / 'warning' / 'info'
      - applies_to(cls, sheet, col_or_None): classmethod, whether rule applies per-column or per-sheet
      - scan(cls, sheet, col_or_None, rows, isNull): classmethod, returns DQIssue or None

    The rule should prefer:
      - ``col.getTypedValue(row)`` for typed values (yields TypedExceptionWrapper on errors)
      - ``sheet.isNullFunc()`` for null detection
      - ``sheet.rowid(row)`` for stable row identity
      - ``vd.isNumeric(col)`` for numeric detection
    '''

    rule_name = 'base'
    default_severity = 'info'
    per_column = True

    @classmethod
    def applies_to(cls, sheet, col):
        return True

    @classmethod
    def scan(cls, sheet, col, rows, isNull):
        return None


class NullValuesRule(DQRule):
    rule_name = 'null_values'
    default_severity = 'warning'
    per_column = True

    @classmethod
    def applies_to(cls, sheet, col):
        return True

    @classmethod
    def scan(cls, sheet, col, rows, isNull):
        null_rowids = []
        total = 0
        for r in rows:
            total += 1
            try:
                v = col.getTypedValue(r)
                if isNull(v) or (isinstance(v, TypedWrapper) and v.val is None):
                    null_rowids.append(sheet.rowid(r))
            except Exception:
                pass
        if not null_rowids:
            return None
        severity = 'error' if len(null_rowids) >= total * 0.5 else 'warning'
        return DQIssue(
            cls.rule_name, col, severity,
            f'{len(null_rowids)} null value(s) in {total} row(s)',
            source_sheet=sheet, rowids=null_rowids,
        )


class TypeErrorsRule(DQRule):
    rule_name = 'type_errors'
    default_severity = 'error'
    per_column = True

    @classmethod
    def applies_to(cls, sheet, col):
        return col.type is not anytype

    @classmethod
    def scan(cls, sheet, col, rows, isNull):
        error_rowids = []
        example = None
        for r in rows:
            try:
                v = col.getTypedValue(r)
                if isNull(v):
                    continue
                if isinstance(v, TypedExceptionWrapper):
                    error_rowids.append(sheet.rowid(r))
                    if example is None:
                        example = v
            except Exception:
                error_rowids.append(sheet.rowid(r))
        if not error_rowids:
            return None
        return DQIssue(
            cls.rule_name, col, 'error',
            f'{len(error_rowids)} type/parsing error(s)',
            source_sheet=sheet, rowids=error_rowids,
            example_value=str(example) if example is not None else None,
        )


class DateParseFailuresRule(DQRule):
    rule_name = 'date_parse_failures'
    default_severity = 'error'
    per_column = True

    @classmethod
    def applies_to(cls, sheet, col):
        return col.type is date

    @classmethod
    def scan(cls, sheet, col, rows, isNull):
        fail_rowids = []
        for r in rows:
            try:
                v = col.getTypedValue(r)
                if isNull(v):
                    continue
                if isinstance(v, TypedExceptionWrapper):
                    fail_rowids.append(sheet.rowid(r))
            except Exception:
                fail_rowids.append(sheet.rowid(r))
        if not fail_rowids:
            return None
        return DQIssue(
            cls.rule_name, col, 'error',
            f'{len(fail_rowids)} date parse failure(s)',
            source_sheet=sheet, rowids=fail_rowids,
        )


class NumericOutliersRule(DQRule):
    rule_name = 'numeric_outliers'
    default_severity = 'warning'
    per_column = True

    @classmethod
    def applies_to(cls, sheet, col):
        return vd.isNumeric(col)

    @classmethod
    def scan(cls, sheet, col, rows, isNull):
        vals = []
        val_rowids = {}
        for r in rows:
            try:
                v = col.getTypedValue(r)
                if isinstance(v, TypedExceptionWrapper):
                    continue
                if isNull(v):
                    continue
                if isinstance(v, (int, float)):
                    vals.append(v)
                    val_rowids.setdefault(v, set()).add(sheet.rowid(r))
            except Exception:
                pass
        if len(vals) < 4:
            return None
        try:
            vals_sorted = sorted(vals)
            mid = len(vals_sorted) // 2
            q1 = statistics.median(vals_sorted[:mid])
            q3 = statistics.median(vals_sorted[mid + (len(vals_sorted) % 2):])
            iqr = q3 - q1
            multiplier = sheet.options.dq_iqr_multiplier
            lower_bound = q1 - multiplier * iqr
            upper_bound = q3 + multiplier * iqr
            outlier_rowids = set()
            for v in vals:
                if v < lower_bound or v > upper_bound:
                    outlier_rowids.update(val_rowids.get(v, set()))
            if not outlier_rowids:
                return None
            return DQIssue(
                cls.rule_name, col, 'warning',
                f'{len(outlier_rowids)} outlier(s) outside [{lower_bound:.4g}, {upper_bound:.4g}] (IQR method)',
                source_sheet=sheet, rowids=outlier_rowids,
                example_value=f'min={min(vals):.4g}, max={max(vals):.4g}',
            )
        except Exception:
            return None


class DuplicateRowsRule(DQRule):
    rule_name = 'duplicate_rows'
    default_severity = 'warning'
    per_column = False

    @classmethod
    def applies_to(cls, sheet, col):
        return True

    @classmethod
    def scan(cls, sheet, col, rows, isNull):
        keyCols = sheet.keyCols
        cols_to_check = keyCols if len(keyCols) > 0 else [c for c in sheet.visibleCols if not c.hidden]
        if not cols_to_check:
            return None

        seen = {}
        dup_rowids = []
        for r in rows:
            try:
                vals = tuple(c.getTypedValue(r) for c in cols_to_check)
                if any(isinstance(v, TypedExceptionWrapper) for v in vals):
                    continue
                rid = sheet.rowid(r)
                if vals in seen:
                    dup_rowids.append(rid)
                    first_rid = seen[vals]
                    if first_rid not in dup_rowids:
                        dup_rowids.insert(0, first_rid)
                else:
                    seen[vals] = rid
            except Exception:
                pass
        if not dup_rowids:
            return None
        colname = '+'.join(c.name for c in cols_to_check)
        return DQIssue(
            cls.rule_name, cols_to_check[0], 'warning',
            f'{len(dup_rowids)} duplicate row(s) based on {colname}',
            source_sheet=sheet, rowids=dup_rowids,
        )


class HighCardinalityRule(DQRule):
    rule_name = 'high_cardinality'
    default_severity = 'info'
    per_column = True

    @classmethod
    def applies_to(cls, sheet, col):
        return col.type in (str, anytype)

    @classmethod
    def scan(cls, sheet, col, rows, isNull):
        vals = []
        for r in rows:
            try:
                v = col.getTypedValue(r)
                if isinstance(v, TypedExceptionWrapper):
                    continue
                if isNull(v) or v is None:
                    continue
                vals.append(str(v))
            except Exception:
                pass
        if len(vals) < sheet.options.dq_min_unique_for_cardinality:
            return None
        unique_count = len(set(vals))
        ratio = unique_count / len(vals) if vals else 0
        if ratio < sheet.options.dq_high_cardinality_threshold:
            return None
        return DQIssue(
            cls.rule_name, col, 'info',
            f'high cardinality: {unique_count}/{len(vals)} unique ({ratio*100:.1f}%)',
            source_sheet=sheet, rowids=set(),
            example_value=f'top: {Counter(vals).most_common(3)}',
        )


class TypeMismatchRule(DQRule):
    rule_name = 'type_mismatch'
    default_severity = 'warning'
    per_column = True

    @classmethod
    def applies_to(cls, sheet, col):
        return col.type is not anytype and col.type is not str

    @classmethod
    def scan(cls, sheet, col, rows, isNull):
        str_rowids = []
        str_count = 0
        non_null_count = 0
        for r in rows:
            try:
                raw_v = col.getValue(r)
                if isNull(raw_v) or raw_v is None:
                    continue
                if isinstance(raw_v, TypedWrapper):
                    continue
                non_null_count += 1
                if isinstance(raw_v, str) and raw_v.strip() != '':
                    str_count += 1
                    if len(str_rowids) < 5:
                        str_rowids.append(sheet.rowid(r))
            except Exception:
                pass
        if non_null_count == 0 or str_count / non_null_count <= 0.5:
            return None
        return DQIssue(
            cls.rule_name, col, 'warning',
            f'{str_count}/{non_null_count} non-null values are strings but column type is {col.typestr or "anytype"}; consider changing column type',
            source_sheet=sheet, rowids=set(str_rowids),
        )


ALL_DQ_RULES = [
    NullValuesRule,
    TypeErrorsRule,
    DateParseFailuresRule,
    NumericOutliersRule,
    DuplicateRowsRule,
    HighCardinalityRule,
    TypeMismatchRule,
]


@Sheet.api
def scan_data_quality(sheet, rules=None):
    '''Scan *sheet* for data quality issues using *rules* (default: ALL_DQ_RULES).
    Yields ``DQIssue`` objects.
    '''
    if sheet.nRows == 0:
        return

    rules = rules or ALL_DQ_RULES
    isNull = sheet.isNullFunc()
    visible_cols = [c for c in sheet.visibleCols if not c.hidden]
    rows = sheet.rows

    for rule in rules:
        if not rule.per_column:
            try:
                if rule.applies_to(sheet, None):
                    issue = rule.scan(sheet, None, rows, isNull)
                    if issue:
                        yield issue
            except Exception as e:
                vd.exceptionCaught(e)

    for col in Progress(visible_cols, gerund='scanning columns'):
        for rule in rules:
            if not rule.per_column:
                continue
            try:
                if rule.applies_to(sheet, col):
                    issue = rule.scan(sheet, col, rows, isNull)
                    if issue:
                        yield issue
            except Exception as e:
                vd.exceptionCaught(e)


def _column_signature(sheet):
    'Return a hashable signature of sheet column state (identity+name+type+visibility).'
    return tuple(
        (id(c), c.name, c.typestr, c.hidden, c.keycol)
        for c in sheet.columns
    )


class IssueColumn(Column):
    def __init__(self, name, **kwargs):
        kwargs.setdefault('width', 12)
        super().__init__(name, **kwargs)


# rowdef: DQIssue
class DataQualitySheet(Sheet):
    'Data quality inspection results panel with row-identity-based source linking.'
    guide = '''
# Data Quality Inspection Panel
This sheet shows data quality issues found in *{sheet.displaySource}*.

Each row represents one type of data quality issue detected.

## Commands
- `Enter` on an issue row to open the source sheet filtered to only the problem rows
- `g Enter` to open filtered sheet for all selected issues
- `s`/`u`/`t` to select/unselect/toggle the corresponding rows on the source sheet
- `goto-source` to jump to the first problem row on the source sheet
- `Ctrl+R` to rescan and refresh the inspection results
- `export-dq` to export the results as a regular table

## Note
Issue rows are tracked by stable row-identity of the source sheet.
If the source sheet reloads or its columns change, the DQ panel will be
automatically marked stale and refresh on next access.
'''
    rowtype = 'issues'
    precious = True
    columns = [
        IssueColumn('severity', width=8, getter=lambda c, r: r.severity),
        IssueColumn('issue_type', width=20, getter=lambda c, r: r.issue_type),
        IssueColumn('column', width=20, getter=lambda c, r: r.colname),
        IssueColumn('count', type=vlen, getter=lambda c, r: r.count),
        IssueColumn('description', width=60, getter=lambda c, r: r.description),
        IssueColumn('example', width=40, getter=lambda c, r: str(r.example_value) if r.example_value else ''),
    ]
    colorizers = [
        RowColorizer(8, 'color_error', lambda s, c, r, v: r and r.severity == 'error'),
        RowColorizer(8, 'color_warning', lambda s, c, r, v: r and r.severity == 'warning'),
        RowColorizer(8, 'color_note', lambda s, c, r, v: r and r.severity == 'info'),
    ]
    nKeys = 2

    def __init__(self, *names, source=None, **kwargs):
        super().__init__(*names, **kwargs)
        self.source = source
        self._dq_source_version = -1
        self._dq_column_signature = None
        self._dq_nrows = -1
        if isinstance(source, Sheet):
            if self not in source._dq_dependents:
                source._dq_dependents.append(self)

    def _dq_source_changed(self):
        'Called by source sheet when its version counter increments.'
        self._dq_mark_stale()

    def _dq_mark_stale(self):
        self._dq_source_version = -1

    def _dq_is_stale(self):
        src = self.source
        if not isinstance(src, Sheet):
            return False
        return (self._dq_source_version != src._dq_version or
                self._dq_column_signature != _column_signature(src) or
                self._dq_nrows != src.nRows)

    def _dq_refresh_if_stale(self):
        if self._dq_is_stale():
            vd.debug(f'{self.name}: source changed, refreshing DQ results')
            self.reload()

    def loader(self):
        if not isinstance(self.source, Sheet):
            self.rows = []
            return
        self.rows = list(scan_data_quality(self.source))
        self._dq_source_version = self.source._dq_version
        self._dq_column_signature = _column_signature(self.source)
        self._dq_nrows = self.source.nRows

    def ensureLoaded(self):
        self._dq_refresh_if_stale()
        return super().ensureLoaded()


@DataQualitySheet.api
def _collect_resolved_rows(dqsheet, issues):
    'Given a list of DQIssue, return (source_sheet, unique_rows) resolved by row identity.'
    all_rowids = set()
    src_sheet = None
    for issue in issues:
        all_rowids.update(issue._rowids)
        if issue.source_sheet:
            src_sheet = issue.source_sheet
    if not all_rowids or not src_sheet:
        return None, []
    id_to_row = {src_sheet.rowid(r): r for r in src_sheet.rows}
    unique_rows = [id_to_row[rid] for rid in all_rowids if rid in id_to_row]
    return src_sheet, unique_rows


@DataQualitySheet.api
def open_issue_rows(dqsheet, issues):
    'Open source sheet filtered to rows (resolved by identity) referenced in *issues*.'
    dqsheet._dq_refresh_if_stale()
    src_sheet, rows = _collect_resolved_rows(dqsheet, issues)
    if not rows or not src_sheet:
        vd.warning('no rows to open')
        return None
    vs = copy(src_sheet)
    vs.names = vs.names + ['dq_filtered']
    vs.rows = rows
    return vs


@DataQualitySheet.api
def select_issue_rows(dqsheet, issues, status=True):
    'Select/unselect source rows (resolved by identity) for *issues*.'
    dqsheet._dq_refresh_if_stale()
    src_sheet, rows = _collect_resolved_rows(dqsheet, issues)
    if not rows or not src_sheet:
        return
    count = 0
    for r in rows:
        if status:
            src_sheet.selectRow(r)
        else:
            src_sheet.unselectRow(r)
        count += 1
    vd.status(f'{"selected" if status else "unselected"} {count} row(s) on source sheet')


@DataQualitySheet.api
def toggle_issue_row(dqsheet, issue):
    'Toggle selection of source rows for *issue*.'
    dqsheet._dq_refresh_if_stale()
    if not issue or not issue._rowids or not issue.source_sheet:
        return
    first = issue.resolve_first_row()
    if first is None:
        return
    currently_selected = issue.source_sheet.isSelected(first)
    select_issue_rows(dqsheet, [issue], not currently_selected)


@DataQualitySheet.api
def goto_first_issue_row(dqsheet, issue):
    'Jump to the first still-existing row of *issue* on the source sheet.'
    dqsheet._dq_refresh_if_stale()
    if not issue or not issue._rowids or not issue.source_sheet:
        vd.warning('no source row to go to')
        return
    src = issue.source_sheet
    first = issue.resolve_first_row()
    if first is None:
        vd.warning('no matching row found on source sheet')
        return
    vd.push(src)
    try:
        idx = src.rows.index(first)
        src.cursorRowIndex = idx
        if issue.column and issue.column in src.columns:
            src.cursorColIndex = src.columns.index(issue.column)
    except (ValueError, IndexError):
        pass


@DataQualitySheet.api
def export_dq_results(dqsheet):
    'Export data quality results as a regular table with plain dict rows.'
    dqsheet._dq_refresh_if_stale()
    vs = Sheet(dqsheet.name + '_export')
    vs.columns = [
        Column('severity', type=str, getter=lambda c, r: r.get('severity', '')),
        Column('issue_type', type=str, getter=lambda c, r: r.get('issue_type', '')),
        Column('column', type=str, getter=lambda c, r: r.get('column', '')),
        Column('count', type=vlen, getter=lambda c, r: r.get('count', 0)),
        Column('description', type=str, getter=lambda c, r: r.get('description', '')),
        Column('example', type=str, getter=lambda c, r: r.get('example', '')),
    ]
    vs.rows = []
    for issue in dqsheet.rows:
        vs.addRow({
            'severity': issue.severity,
            'issue_type': issue.issue_type,
            'column': issue.colname,
            'count': issue.count,
            'description': issue.description,
            'example': str(issue.example_value) if issue.example_value else '',
        })
    return vs


TableSheet.addCommand('', 'open-data-quality', 'vd.push(DataQualitySheet(sheet.name + "_dq", source=sheet))', 'open data quality inspection panel')
DataQualitySheet.addCommand('Enter', 'open-issue', 'vs = open_issue_rows([cursorRow]); vd.push(vs) if vs else None', 'open source sheet with rows for current issue')
DataQualitySheet.addCommand('gEnter', 'open-issues', 'vs = open_issue_rows(selectedRows or rows); vd.push(vs) if vs else None', 'open source sheet with rows for all selected issues')
DataQualitySheet.addCommand('s', 'select-issue', 'select_issue_rows([cursorRow], True)', 'select source rows for current issue')
DataQualitySheet.addCommand('u', 'unselect-issue', 'select_issue_rows([cursorRow], False)', 'unselect source rows for current issue')
DataQualitySheet.addCommand('gs', 'select-issues', 'select_issue_rows(selectedRows or rows, True)', 'select source rows for all selected issues')
DataQualitySheet.addCommand('gu', 'unselect-issues', 'select_issue_rows(selectedRows or rows, False)', 'unselect source rows for all selected issues')
DataQualitySheet.addCommand('t', 'toggle-issue', 'toggle_issue_row(cursorRow)', 'toggle selection of source rows for current issue')
DataQualitySheet.addCommand('', 'goto-source', 'goto_first_issue_row(cursorRow)', 'go to first source row for current issue')
DataQualitySheet.addCommand('Ctrl+R', 'refresh-dq', 'reload()', 'refresh data quality inspection results')
DataQualitySheet.addCommand('', 'export-dq', 'vd.push(export_dq_results())', 'export data quality results as a regular table')

vd.addMenuItems('''
    Data > Quality inspection > open panel > open-data-quality
''')

vd.addGlobals(
    DataQualitySheet=DataQualitySheet,
    DQIssue=DQIssue,
    DQRule=DQRule,
    ALL_DQ_RULES=ALL_DQ_RULES,
    NullValuesRule=NullValuesRule,
    TypeErrorsRule=TypeErrorsRule,
    DateParseFailuresRule=DateParseFailuresRule,
    NumericOutliersRule=NumericOutliersRule,
    DuplicateRowsRule=DuplicateRowsRule,
    HighCardinalityRule=HighCardinalityRule,
    TypeMismatchRule=TypeMismatchRule,
    scan_data_quality=scan_data_quality,
)
