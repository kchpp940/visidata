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


@Sheet.after
def clearSelected(sheet):
    _bump_dq_version(sheet)


class DQSourceSnapshot:
    '''Complete immutable fingerprint of a source sheet's data-quality-relevant state.

    Captures:
      - rowid ordering (rows identity)
      - selected row ids
      - per-column signature: identity, name, typestr, visibility, key status, and TypedExceptionWrapper count
      - key column identities
      - row count
    '''

    __slots__ = (
        'rowids_tuple',
        'selected_rowids',
        'col_signatures',
        'keycol_ids',
        'nRows',
        'nSelectedRows',
    )

    def __init__(self, sheet):
        self.rowids_tuple = tuple(sheet.rowid(r) for r in sheet.rows)
        self.selected_rowids = frozenset(sheet._selectedRows.keys())
        self.col_signatures = tuple(
            (
                id(c),
                c.name,
                c.typestr,
                c.hidden,
                c.keycol,
                self._count_errors(sheet, c),
            )
            for c in sheet.columns
        )
        self.keycol_ids = tuple(id(c) for c in sheet.keyCols)
        self.nRows = sheet.nRows
        self.nSelectedRows = sheet.nSelectedRows

    @staticmethod
    def _count_errors(sheet, col):
        'Count TypedExceptionWrapper values in *col* across sheet rows. Cheap if col caches.'
        n = 0
        for r in sheet.rows:
            try:
                v = col.getTypedValue(r)
                if isinstance(v, TypedExceptionWrapper):
                    n += 1
            except Exception:
                n += 1
        return n

    @property
    def col_id_to_sig(self):
        return {sig[0]: sig for sig in self.col_signatures}

    def rows_changed(self, other):
        'True iff the row identity set or ordering differs between snapshots.'
        return self.rowids_tuple != other.rowids_tuple

    def selection_changed(self, other):
        return self.selected_rowids != other.selected_rowids

    def keycols_changed(self, other):
        return self.keycol_ids != other.keycol_ids

    def changed_col_ids(self, other):
        'Return set of column ids whose signatures differ between snapshots.'
        mine = self.col_id_to_sig
        theirs = other.col_id_to_sig
        changed = set()
        for cid, sig in mine.items():
            if cid not in theirs or theirs[cid] != sig:
                changed.add(cid)
        for cid in theirs:
            if cid not in mine:
                changed.add(cid)
        return changed

    def all_col_ids(self):
        return frozenset(sig[0] for sig in self.col_signatures)

    def scan_deps_changed(self, other):
        '''True iff the rule-output-affecting state differs between snapshots.

        This covers row identity/ordering, column signatures, and key columns —
        everything that can change the result of a DQ rule scan.  Selection state
        is deliberately excluded: selecting/unselecting rows on the source sheet
        does not change which rows have nulls, type errors, duplicates, etc.
        '''
        if not isinstance(other, DQSourceSnapshot):
            return True
        return (self.rowids_tuple != other.rowids_tuple or
                self.col_signatures != other.col_signatures or
                self.keycol_ids != other.keycol_ids)

    def __eq__(self, other):
        if not isinstance(other, DQSourceSnapshot):
            return NotImplemented
        return (self.rowids_tuple == other.rowids_tuple and
                self.selected_rowids == other.selected_rowids and
                self.col_signatures == other.col_signatures and
                self.keycol_ids == other.keycol_ids)

    def __hash__(self):
        return hash((self.rowids_tuple, self.selected_rowids,
                     self.col_signatures, self.keycol_ids))


class DQIssue:
    '''Represents a single data quality issue found on a source sheet.

    Stores row identity (rowids) rather than row object references,
    and remembers which rule and which columns it depends on so the
    inspector can decide whether to recalculate it incrementally.
    '''

    __slots__ = (
        'issue_type',
        '_column',
        'severity',
        'description',
        '_source_sheet',
        '_rowids',
        'example_value',
        '_dep_col_ids',
        '_rule_name',
    )

    def __init__(self, issue_type, column, severity, description, *,
                 source_sheet=None, rowids=None, example_value=None,
                 dep_col_ids=None, rule_name=None):
        self.issue_type = issue_type
        self._column = column
        self.severity = severity
        self.description = description
        self._source_sheet = source_sheet or (column.sheet if column else None)
        self._rowids = frozenset(rowids) if rowids is not None else frozenset()
        self.example_value = example_value
        if dep_col_ids is None and column is not None and source_sheet is not None:
            dep_col_ids = frozenset([id(column)])
        elif dep_col_ids is None:
            dep_col_ids = frozenset()
        self._dep_col_ids = frozenset(dep_col_ids)
        self._rule_name = rule_name or issue_type

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

    @property
    def rule_name(self):
        return self._rule_name

    @property
    def dep_col_ids(self):
        return self._dep_col_ids

    def resolve_rows(self):
        '''Return (valid_rows, missing_count) tuple.

        *valid_rows* are the actual row objects still present on the source sheet.
        *missing_count* is how many stored rowids no longer resolve.
        '''
        if not self._source_sheet:
            return [], 0
        id_to_row = {self._source_sheet.rowid(r): r for r in self._source_sheet.rows}
        valid = []
        missing = 0
        for rid in self._rowids:
            if rid in id_to_row:
                valid.append(id_to_row[rid])
            else:
                missing += 1
        return valid, missing

    def resolve_first_row(self):
        'Return (row, missing_count_since_scan). row is None if no rows resolvable.'
        valid, missing = self.resolve_rows()
        return (valid[0], missing) if valid else (None, missing)


class DQRule:
    '''Base class for reusable data quality rules.

    Subclasses must implement:
      - rule_name (class attr): short stable identifier
      - default_severity (class attr): 'error' / 'warning' / 'info'
      - per_column (class attr): True if rule runs once per column; False if once per sheet
      - applies_to(cls, sheet, col_or_None): whether rule applies to this sheet/column
      - scan(cls, sheet, col_or_None, rows, isNull): returns DQIssue or None

    Implementations must:
      - Use ``col.getTypedValue(row)`` for typed values / error detection (TypedExceptionWrapper)
      - Use ``sheet.isNullFunc()`` for null semantics
      - Use ``sheet.rowid(row)`` to build rowid sets (never store row objects)
      - Use ``vd.isNumeric(col)`` for numeric column detection
    '''

    rule_name = 'base'
    default_severity = 'info'
    per_column = True

    @classmethod
    def applies_to(cls, sheet, col):
        return True

    @classmethod
    def _make_issue(cls, sheet, col, severity, description, *,
                    rowids, example_value=None, dep_col_ids=None):
        return DQIssue(
            cls.rule_name, col, severity, description,
            source_sheet=sheet, rowids=rowids,
            example_value=example_value,
            dep_col_ids=dep_col_ids,
            rule_name=cls.rule_name,
        )

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
        return cls._make_issue(
            sheet, col, severity,
            f'{len(null_rowids)} null value(s) in {total} row(s)',
            rowids=null_rowids,
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
        return cls._make_issue(
            sheet, col, 'error',
            f'{len(error_rowids)} type/parsing error(s)',
            rowids=error_rowids,
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
        return cls._make_issue(
            sheet, col, 'error',
            f'{len(fail_rowids)} date parse failure(s)',
            rowids=fail_rowids,
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
            return cls._make_issue(
                sheet, col, 'warning',
                f'{len(outlier_rowids)} outlier(s) outside [{lower_bound:.4g}, {upper_bound:.4g}] (IQR method)',
                rowids=outlier_rowids,
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
        dep_col_ids = frozenset(id(c) for c in cols_to_check)
        return cls._make_issue(
            sheet, cols_to_check[0], 'warning',
            f'{len(dup_rowids)} duplicate row(s) based on {colname}',
            rowids=dup_rowids,
            dep_col_ids=dep_col_ids,
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
        return cls._make_issue(
            sheet, col, 'info',
            f'high cardinality: {unique_count}/{len(vals)} unique ({ratio*100:.1f}%)',
            rowids=set(),
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
        return cls._make_issue(
            sheet, col, 'warning',
            f'{str_count}/{non_null_count} non-null values are strings but column type is {col.typestr or "anytype"}; consider changing column type',
            rowids=set(str_rowids),
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


def _scan_columns_for_issues(sheet, cols, rules, isNull, rows, progress_label=None):
    'Run per-column *rules* on specific *cols* across *rows*; yield DQIssue objects.'
    for col in (Progress(cols, gerund=progress_label or 'scanning columns') if progress_label else cols):
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


def _scan_sheet_rules(sheet, rules, isNull, rows):
    'Run per-sheet *rules* across *rows*; yield DQIssue objects.'
    for rule in rules:
        if rule.per_column:
            continue
        try:
            if rule.applies_to(sheet, None):
                issue = rule.scan(sheet, None, rows, isNull)
                if issue:
                    yield issue
        except Exception as e:
            vd.exceptionCaught(e)


@Sheet.api
def scan_data_quality(sheet, rules=None):
    '''Scan *sheet* for data quality issues using *rules* (default: ALL_DQ_RULES).

    Scans the rows currently present on *sheet* (``sheet.rows``) — which reflects
    any filtering via duplicate-sheet, row selection subsets, etc. — and only the
    visible, non-hidden columns.
    Yields ``DQIssue`` objects.
    '''
    rows = sheet.rows
    if not rows:
        return

    rules = rules or ALL_DQ_RULES
    isNull = sheet.isNullFunc()
    visible_cols = [c for c in sheet.visibleCols if not c.hidden]

    yield from _scan_sheet_rules(sheet, rules, isNull, rows)
    yield from _scan_columns_for_issues(sheet, visible_cols, rules, isNull, rows, progress_label='scanning columns')


class IssueColumn(Column):
    def __init__(self, name, **kwargs):
        kwargs.setdefault('width', 12)
        super().__init__(name, **kwargs)


# rowdef: DQIssue
class DataQualitySheet(Sheet):
    'Data quality inspection panel with source snapshot tracking and incremental refresh.'
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

## Source state tracking
The panel captures a full snapshot of the source sheet at scan time:
row identity ordering, selected rows, per-column signatures (name/type/visibility/key/error count),
and key columns.  Any change on the source sheet marks the DQ results stale, and
the next command will trigger a refresh — or refresh only affected columns when possible.

When rows referenced by an issue no longer exist on the source sheet, you will see
an explicit warning rather than a silent skip.
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
        self._dq_snapshot = None
        self._dq_dirty = True
        if isinstance(source, Sheet):
            if self not in source._dq_dependents:
                source._dq_dependents.append(self)

    # ---------- source change / staleness ----------

    def _dq_source_changed(self):
        'Called by source sheet when its version counter increments.'
        self._dq_dirty = True

    def _dq_is_stale(self):
        '''True iff rule outputs need recomputation.

        Only scan dependencies (rows, columns, keycols) trigger staleness.
        Selection-only changes update the snapshot in place without rescanning.
        '''
        src = self.source
        if not isinstance(src, Sheet):
            return False
        if not self._dq_dirty:
            return False
        if self._dq_snapshot is None:
            return True
        new_snap = DQSourceSnapshot(src)
        if self._dq_snapshot.scan_deps_changed(new_snap):
            return True
        self._dq_snapshot = new_snap
        self._dq_dirty = False
        return False

    def _dq_incremental_refresh(self):
        '''Refresh only the rules/columns affected by the source change.

        Falls back to full reload when the row identity set itself has changed.
        Selection changes on the source sheet do NOT trigger any rule rescan —
        they only update the cached snapshot for accurate联动 warnings.
        '''
        src = self.source
        if not isinstance(src, Sheet):
            self.rows = []
            self._dq_snapshot = None
            self._dq_dirty = False
            return

        old_snap = self._dq_snapshot
        new_snap = DQSourceSnapshot(src)

        if old_snap is None or old_snap.rows_changed(new_snap):
            vd.debug(f'{self.name}: rows changed; full DQ rescan')
            self.rows = list(scan_data_quality(src))
            self._dq_snapshot = new_snap
            self._dq_dirty = False
            return

        isNull = src.isNullFunc()
        changed_col_ids = old_snap.changed_col_ids(new_snap)
        keycols_changed = old_snap.keycols_changed(new_snap)

        if not changed_col_ids and not keycols_changed:
            self._dq_snapshot = new_snap
            self._dq_dirty = False
            return

        rules = ALL_DQ_RULES
        rules_to_rescan_sheet = []
        rules_to_rescan_cols = []
        for rule in rules:
            if rule.per_column:
                rules_to_rescan_cols.append(rule)
            else:
                if keycols_changed:
                    rules_to_rescan_sheet.append(rule)
                elif rule is DuplicateRowsRule and (
                    changed_col_ids & new_snap.all_col_ids()
                ):
                    rules_to_rescan_sheet.append(rule)
                elif any(dep in changed_col_ids for dep in
                         (new_snap.all_col_ids() if rule is DuplicateRowsRule else set())):
                    rules_to_rescan_sheet.append(rule)

        # Determine which columns to rescan for per-column rules
        # A column must be rescanned if (a) its own signature changed or
        # (b) it was a dependency of a removed/changed issue.
        old_issue_dep_cols = set()
        for issue in self.rows:
            old_issue_dep_cols.update(issue._dep_col_ids)
        col_ids_to_rescan = changed_col_ids | (old_issue_dep_cols & new_snap.all_col_ids())

        # Build id -> Column map for the current source sheet
        col_by_id = {id(c): c for c in src.columns}
        cols_to_rescan = [col_by_id[cid] for cid in col_ids_to_rescan if cid in col_by_id]

        vd.debug(f'{self.name}: incremental DQ refresh; '
                 f'sheet_rules={[r.rule_name for r in rules_to_rescan_sheet]}, '
                 f'cols_to_rescan={len(cols_to_rescan)}')

        # Drop all issues that depend on changed columns or come from rescanned sheet rules
        resheet_rule_names = {r.rule_name for r in rules_to_rescan_sheet}
        new_rows = []
        for issue in self.rows:
            if issue._rule_name in resheet_rule_names:
                continue
            if issue._dep_col_ids & changed_col_ids:
                continue
            new_rows.append(issue)

        # Re-scan sheet-level rules
        if rules_to_rescan_sheet:
            for issue in _scan_sheet_rules(src, rules_to_rescan_sheet, isNull, src.rows):
                new_rows.append(issue)

        # Re-scan affected columns
        if cols_to_rescan and rules_to_rescan_cols:
            for issue in _scan_columns_for_issues(src, cols_to_rescan, rules_to_rescan_cols, isNull, src.rows):
                new_rows.append(issue)

        self.rows = new_rows
        self._dq_snapshot = new_snap
        self._dq_dirty = False

    def _dq_refresh_if_stale(self):
        if self._dq_is_stale():
            self._dq_incremental_refresh()

    def loader(self):
        if not isinstance(self.source, Sheet):
            self.rows = []
            self._dq_snapshot = None
            self._dq_dirty = False
            return
        self.rows = list(scan_data_quality(self.source))
        self._dq_snapshot = DQSourceSnapshot(self.source)
        self._dq_dirty = False

    def ensureLoaded(self):
        self._dq_refresh_if_stale()
        return super().ensureLoaded()


# ---------- helpers: row-resolution with explicit staleness warnings ----------

@DataQualitySheet.api
def _warn_if_rows_missing(dqsheet, stored_count, resolved_count):
    missing = stored_count - resolved_count
    if missing > 0:
        vd.warning(f'{missing}/{stored_count} row(s) referenced by this issue no longer exist on the source sheet; consider refreshing the DQ panel with Ctrl+R')
    return missing


@DataQualitySheet.api
def _collect_resolved_rows(dqsheet, issues):
    '''Given a list of DQIssue, return (source_sheet, unique_rows).

    Emits an explicit warning if any stored rowids no longer resolve on the
    source sheet, instead of silently skipping them.
    '''
    all_rowids = set()
    src_sheet = None
    for issue in issues:
        all_rowids.update(issue._rowids)
        if issue.source_sheet:
            src_sheet = issue.source_sheet
    if not all_rowids or not src_sheet:
        return None, []
    id_to_row = {src_sheet.rowid(r): r for r in src_sheet.rows}
    unique_rows = []
    for rid in all_rowids:
        if rid in id_to_row:
            unique_rows.append(id_to_row[rid])
    _warn_if_rows_missing(dqsheet, len(all_rowids), len(unique_rows))
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
    if not src_sheet:
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
    valid, missing = issue.resolve_rows()
    if missing:
        _warn_if_rows_missing(dqsheet, issue.count, len(valid))
    if not valid:
        vd.warning('no resolvable rows for this issue on the source sheet; consider refreshing with Ctrl+R')
        return
    currently_selected = issue.source_sheet.isSelected(valid[0])
    select_issue_rows(dqsheet, [issue], not currently_selected)


@DataQualitySheet.api
def goto_first_issue_row(dqsheet, issue):
    'Jump to the first still-existing row of *issue* on the source sheet.'
    dqsheet._dq_refresh_if_stale()
    if not issue or not issue._rowids or not issue.source_sheet:
        vd.warning('no source row to go to')
        return
    src = issue.source_sheet
    first, missing = issue.resolve_first_row()
    if missing:
        _warn_if_rows_missing(dqsheet, issue.count, 1 if first else 0)
    if first is None:
        vd.warning(f'none of the {issue.count} row(s) in this issue still exist on the source sheet; refresh with Ctrl+R')
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
    DQSourceSnapshot=DQSourceSnapshot,
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
