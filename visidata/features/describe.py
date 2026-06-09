"""
Describe sheet — backwards-compatible wrapper.

Delegates execution to the shared ``vd.diagnosticRunner`` (see
``visidata.features.diagnostics``) while preserving the original
``describeData`` dict, ``reloadColumn`` helper, and the ``I`` / ``gI`` key
bindings expected by tests and plugins.
"""

from copy import copy
from statistics import mode, median, mean, stdev

from visidata import vd, Column, ColumnAttr, vlen, RowColorizer, asyncthread, Progress, wrapply
from visidata import BaseSheet, TableSheet, ColumnsSheet, IndexSheet

from visidata.features.diagnostics import (
    DiagnosticsSheet,
    DiagnosticResult,
    DiagnosticColumn,
    DiagnosticRule,
    vd as _diag_vd,
)


@Column.api
def isError(col, row):
    'Return True if the computed or typed value for *row* in this column is an error.'
    try:
        v = col.getValue(row)
        if v is not None:
            col.type(v)
        return False
    except Exception as e:
        return True


class DescribeColumn(Column):
    def __init__(self, name, **kwargs):
        kwargs.setdefault('width', 10)
        super().__init__(name, getter=lambda col, srccol: col.sheet.describeData[srccol].get(col.expr, ''), expr=name, **kwargs)


# rowdef: Column from source sheet
class DescribeSheet(DiagnosticsSheet):
    """Describe Sheet — backwards-compatible façade over DiagnosticsSheet.

    Preserves the original ``describeData`` layout and ``reloadColumn`` so
    callers (tests.vd, plugins, …) that introspect ``sheet.describeData`` keep
    working.
    """
    guide = '''
        # Describe Sheet
        This `Describe Sheet` shows a few basic metrics over data in {sheet.displaySource}, with each column represented by a row.

        For example, row {sheet.cursorRowIndex} describes the _{sheet.cursorRow.name}_ column, showing its minimum value, maximum value, mean, median, and other measures.
    '''
    precious = True
    columns = [
        ColumnAttr('sheet', 'sheet', width=0),
        ColumnAttr('column', 'name'),
        ColumnAttr('type', 'typestr', width=0),
        DescribeColumn('errors', type=vlen),
        DescribeColumn('nulls', type=vlen),
        DescribeColumn('distinct', type=vlen),
        DescribeColumn('mode', type=str),
        DescribeColumn('min', type=str),
        DescribeColumn('max', type=str),
        DescribeColumn('sum'),
        DescribeColumn('median', type=str),
    ]
    colorizers = [
        RowColorizer(7, 'color_key_col', lambda s, c, r, v: r and r in r.sheet.keyCols),
    ]
    nKeys = 2

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.describeData = {}

    def loader(self):
        ColumnsSheet.loader(self)
        self.rows = [c for c in self.rows if not c.hidden]
        self.describeData = {col: {} for col in self.rows}
        self.resetCols()

        extra_aggrs = tuple(vd.options.describe_aggrs.split())
        for aggrname in extra_aggrs:
            self.addColumn(DescribeColumn(aggrname, type=float))

        for srcsheet in self._sourceSheets():
            vd.diagnosticRunner.ensure(srcsheet, extra_aggrs=extra_aggrs)

        for srccol in self.rows:
            self.reloadColumn(srccol)

    def reloadColumn(self, srccol):
        """Populate ``self.describeData[srccol]`` from the runner cache.

        Retained for backwards compatibility; callers can still invoke it to
        (re)build the legacy dict layout for a single column.
        """
        d = self.describeData.setdefault(srccol, {})
        runner_results = vd.diagnosticRunner.all_rules_for(srccol)

        for rulename, result in runner_results.items():
            if rulename == 'errors':
                d['errors'] = result.rows
            elif rulename == 'nulls':
                d['nulls'] = result.rows
            elif rulename == 'distinct':
                d['distinct'] = set() if result.value is None else result.value
            else:
                d[rulename] = result.value

        # Legacy compat – ensure errors/nulls lists and distinct set are always present
        d.setdefault('errors', [])
        d.setdefault('nulls', [])
        d.setdefault('distinct', set())

    def openCell(self, col, row):
        'open copy of source sheet with rows described in current cell'
        val = col.getValue(row)
        if isinstance(val, list):
            vs = copy(row.sheet)
            vs.rows = val
            vs.name += '_%s_%s' % (row.name, col.name)
            return vs
        vd.warning(val)


TableSheet.addCommand('I', 'describe-sheet', 'vd.push(DescribeSheet(sheet.name+"_describe", source=[sheet]))', 'open Describe Sheet with descriptive statistics for all visible columns')
BaseSheet.addCommand('gI', 'describe-all', 'vd.push(DescribeSheet("describe_all", source=vd.stackedSheets))', 'open Describe Sheet with description statistics for all visible columns from all sheets')
IndexSheet.addCommand('gI', 'describe-selected', 'vd.push(DescribeSheet("describe_all", source=selectedRows))', 'open Describe Sheet with all visible columns from selected sheets')

DescribeSheet.addCommand('zs', 'select-cell', 'cursorRow.sheet.select(cursorValue)', 'select rows on source sheet which are being described in current cell')
DescribeSheet.addCommand('zu', 'unselect-cell', 'cursorRow.sheet.unselect(cursorValue)', 'unselect rows on source sheet which are being described in current cell')

vd.addMenuItems('Data > Statistics > describe-sheet')

vd.addGlobals({'DescribeSheet': DescribeSheet})
