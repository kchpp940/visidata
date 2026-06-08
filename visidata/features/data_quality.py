from copy import copy
from collections import Counter
import statistics

from visidata import vd, Sheet, TableSheet, Column
from visidata import Progress, TypedExceptionWrapper, TypedWrapper
from visidata import RowColorizer, vlen, anytype, date


vd.option('dq_high_cardinality_threshold', 0.9, 'ratio threshold for flagging high-cardinality string columns')
vd.option('dq_iqr_multiplier', 1.5, 'IQR multiplier for outlier detection')
vd.option('dq_min_unique_for_cardinality', 10, 'minimum unique values before considering cardinality ratio')


class DataQualityIssue:
    def __init__(self, issue_type, column, severity, description, rows=None, example_value=None):
        self.issue_type = issue_type
        self.column = column
        self.severity = severity
        self.description = description
        self.rows = rows or []
        self.example_value = example_value

    @property
    def colname(self):
        return self.column.name if self.column else ''

    @property
    def count(self):
        return len(self.rows)

    @property
    def sheet(self):
        return self.column.sheet if self.column else None


def _scan_nulls(col, rows, isNull):
    null_rows = []
    for r in rows:
        try:
            v = col.getValue(r)
            if isNull(v):
                null_rows.append(r)
        except Exception:
            pass
    if null_rows:
        return DataQualityIssue(
            issue_type='null_values',
            column=col,
            severity='warning' if len(null_rows) < len(rows) * 0.5 else 'error',
            description=f'{len(null_rows)} null value(s) in {len(rows)} row(s)',
            rows=null_rows,
        )
    return None


def _scan_errors(col, rows, isNull):
    error_rows = []
    for r in rows:
        try:
            v = col.getValue(r)
            if isinstance(v, TypedExceptionWrapper):
                error_rows.append(r)
            elif not isNull(v):
                col.type(v)
        except Exception:
            error_rows.append(r)
    if error_rows:
        example = None
        try:
            example = col.getValue(error_rows[0])
        except Exception:
            example = '<error>'
        return DataQualityIssue(
            issue_type='type_errors',
            column=col,
            severity='error',
            description=f'{len(error_rows)} type/parsing error(s)',
            rows=error_rows,
            example_value=example,
        )
    return None


def _scan_date_parse_failures(col, rows, isNull):
    if col.type is not date:
        return None
    parse_fail_rows = []
    for r in rows:
        try:
            v = col.getValue(r)
            if isNull(v):
                continue
            typed = col.type(v)
            if isinstance(typed, TypedExceptionWrapper):
                parse_fail_rows.append(r)
        except Exception:
            parse_fail_rows.append(r)
    if parse_fail_rows:
        return DataQualityIssue(
            issue_type='date_parse_failures',
            column=col,
            severity='error',
            description=f'{len(parse_fail_rows)} date parse failure(s)',
            rows=parse_fail_rows,
        )
    return None


def _scan_numeric_outliers(col, rows, isNull):
    if not vd.isNumeric(col):
        return None
    vals = []
    val_rows = {}
    for r in rows:
        try:
            v = col.getTypedValue(r)
            if not isNull(v) and isinstance(v, (int, float)):
                vals.append(v)
                val_rows.setdefault(v, []).append(r)
        except Exception:
            pass
    if len(vals) < 4:
        return None
    try:
        vals_sorted = sorted(vals)
        q1 = statistics.median(vals_sorted[:len(vals_sorted)//2])
        q3 = statistics.median(vals_sorted[(len(vals_sorted)+1)//2:])
        iqr = q3 - q1
        multiplier = vd.options.dq_iqr_multiplier
        lower_bound = q1 - multiplier * iqr
        upper_bound = q3 + multiplier * iqr
        outlier_rows = []
        for v in vals:
            if v < lower_bound or v > upper_bound:
                outlier_rows.extend(val_rows.get(v, []))
        unique_outlier_rows = list({id(r): r for r in outlier_rows}.values())
        if unique_outlier_rows:
            return DataQualityIssue(
                issue_type='numeric_outliers',
                column=col,
                severity='warning',
                description=f'{len(unique_outlier_rows)} outlier(s) outside [{lower_bound:.4g}, {upper_bound:.4g}] (IQR method)',
                rows=unique_outlier_rows,
                example_value=f'min={min(vals):.4g}, max={max(vals):.4g}',
            )
    except Exception:
        pass
    return None


def _scan_duplicates(sheet, cols, rows):
    keyCols = sheet.keyCols
    cols_to_check = keyCols if len(keyCols) > 0 else [c for c in cols if not c.hidden]
    if not cols_to_check:
        return None

    seen = {}
    dup_rows = []
    for r in rows:
        try:
            vals = tuple(c.getTypedValue(r) for c in cols_to_check)
            if any(isinstance(v, TypedExceptionWrapper) for v in vals):
                continue
            if vals in seen:
                dup_rows.append(r)
                if seen[vals] not in dup_rows:
                    dup_rows.insert(0, seen[vals])
            else:
                seen[vals] = r
        except Exception:
            pass
    if dup_rows:
        colname = '+'.join(c.name for c in cols_to_check)
        return DataQualityIssue(
            issue_type='duplicate_rows',
            column=cols_to_check[0] if cols_to_check else None,
            severity='warning',
            description=f'{len(dup_rows)} duplicate row(s) based on {colname}',
            rows=dup_rows,
        )
    return None


def _scan_high_cardinality(col, rows, isNull):
    if col.type not in (str, anytype):
        return None
    vals = []
    for r in rows:
        try:
            v = col.getValue(r)
            if not isNull(v) and v is not None:
                vals.append(str(v))
        except Exception:
            pass
    if len(vals) < vd.options.dq_min_unique_for_cardinality:
        return None
    unique_count = len(set(vals))
    ratio = unique_count / len(vals) if vals else 0
    if ratio >= vd.options.dq_high_cardinality_threshold:
        return DataQualityIssue(
            issue_type='high_cardinality',
            column=col,
            severity='info',
            description=f'high cardinality: {unique_count}/{len(vals)} unique ({ratio*100:.1f}%)',
            rows=[],
            example_value=f'top: {Counter(vals).most_common(3)}',
        )
    return None


def _scan_column_type_mismatch(col, rows, isNull):
    if col.type is anytype or col.type is str:
        return None
    mismatched = []
    str_count = 0
    non_null_count = 0
    for r in rows:
        try:
            v = col.getValue(r)
            if isNull(v) or v is None:
                continue
            if isinstance(v, TypedWrapper):
                continue
            non_null_count += 1
            if isinstance(v, str) and v.strip() != '':
                str_count += 1
                if len(mismatched) < 5:
                    mismatched.append(r)
        except Exception:
            pass
    if non_null_count > 0 and str_count / non_null_count > 0.5:
        return DataQualityIssue(
            issue_type='type_mismatch',
            column=col,
            severity='warning',
            description=f'{str_count}/{non_null_count} non-null values are strings but column type is {col.typestr or "anytype"}; consider changing column type',
            rows=mismatched,
        )
    return None


class IssueColumn(Column):
    def __init__(self, name, **kwargs):
        kwargs.setdefault('width', 12)
        super().__init__(name, **kwargs)


# rowdef: DataQualityIssue
class DataQualitySheet(Sheet):
    'Data quality inspection results panel.'
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

    def loader(self):
        if not isinstance(self.source, Sheet):
            self.rows = []
            return
        self.rows = list(scan_data_quality(self.source))


@Sheet.api
def scan_data_quality(sheet):
    'Scan sheet for data quality issues. Yields DataQualityIssue objects.'
    if sheet.nRows == 0:
        return

    isNull = sheet.isNullFunc()
    visible_cols = [c for c in sheet.visibleCols if not c.hidden]
    rows = sheet.rows

    issue = _scan_duplicates(sheet, visible_cols, rows)
    if issue:
        yield issue

    for col in Progress(visible_cols, gerund='scanning columns'):
        for scanner in [_scan_nulls, _scan_errors, _scan_date_parse_failures,
                        _scan_numeric_outliers, _scan_high_cardinality,
                        _scan_column_type_mismatch]:
            try:
                issue = scanner(col, rows, isNull)
                if issue:
                    yield issue
            except Exception as e:
                vd.exceptionCaught(e)


@DataQualitySheet.api
def open_issue_rows(dqsheet, issue_rows):
    'Open source sheet filtered to rows referenced in the selected issue(s).'
    all_rows = []
    src_sheet = None
    for issue in issue_rows:
        if issue.rows:
            all_rows.extend(issue.rows)
            if issue.sheet:
                src_sheet = issue.sheet
    if not all_rows or not src_sheet:
        vd.warning('no rows to open')
        return None
    vs = copy(src_sheet)
    vs.names = vs.names + ['dq_filtered']
    seen = set()
    unique_rows = []
    for r in all_rows:
        if id(r) not in seen:
            seen.add(id(r))
            unique_rows.append(r)
    vs.rows = unique_rows
    return vs


@DataQualitySheet.api
def select_issue_rows(dqsheet, issue_rows, status=True):
    'Select/unselect rows on source sheet for the given issue(s).'
    count = 0
    for issue in issue_rows:
        if issue.sheet and issue.rows:
            for r in issue.rows:
                if status:
                    issue.sheet.selectRow(r)
                else:
                    issue.sheet.unselectRow(r)
                count += 1
    vd.status(f'{"selected" if status else "unselected"} {count} row(s) on source sheet')


@DataQualitySheet.api
def toggle_issue_row(dqsheet, issue):
    'Toggle selection of source rows for the given issue.'
    if not issue or not issue.rows or not issue.sheet:
        return
    first = issue.rows[0]
    currently_selected = first in issue.sheet.selectedRows
    select_issue_rows(dqsheet, [issue], not currently_selected)


@DataQualitySheet.api
def goto_first_issue_row(dqsheet, issue):
    'Jump to the first row of the issue on the source sheet.'
    if not issue or not issue.rows or not issue.sheet:
        vd.warning('no source row to go to')
        return
    src = issue.sheet
    col = issue.column
    vd.push(src)
    try:
        idx = src.rows.index(issue.rows[0])
        src.cursorRowIndex = idx
        if col and col in src.columns:
            src.cursorColIndex = src.columns.index(col)
    except (ValueError, IndexError):
        pass


@DataQualitySheet.api
def export_dq_results(dqsheet):
    'Export data quality results as a regular table with plain dict rows.'
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
    DataQualityIssue=DataQualityIssue,
    scan_data_quality=scan_data_quality,
)
