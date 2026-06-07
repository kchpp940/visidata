import collections
from copy import copy
from visidata import ScopedSetattr, Column, Sheet, asyncthread, Progress, forward, wrapply, INPROGRESS
from visidata import vlen, vd, date, setitem, anytype, TypedWrapper, TypedExceptionWrapper
import visidata


class GroupingNull:
    def __bool__(self):
        return False
    def __repr__(self):
        return 'GroupingNull'

class GroupingError:
    def __bool__(self):
        return False
    def __repr__(self):
        return 'GroupingError'

GROUPING_NULL = GroupingNull()
GROUPING_ERROR = GroupingError()


def normalizeGroupValue(v):
    if isinstance(v, TypedExceptionWrapper):
        return GROUPING_ERROR
    if isinstance(v, TypedWrapper):
        return GROUPING_NULL
    if v is None:
        return GROUPING_NULL
    return v


def isGroupingExceptional(v):
    return v is GROUPING_NULL or v is GROUPING_ERROR or v is None or isinstance(v, TypedWrapper)


def formatGroupValue(col, v):
    if v is GROUPING_ERROR or isinstance(v, TypedExceptionWrapper):
        return '#ERR'
    if v is GROUPING_NULL or v is None or (isinstance(v, TypedWrapper) and v.val is None):
        return ''
    if isinstance(v, TypedWrapper):
        return ''
    if isinstance(v, tuple) and len(v) == 2:
        a, b = v
        if a is GROUPING_ERROR and b is GROUPING_ERROR:
            return '#ERR'
        if a is GROUPING_NULL and b is GROUPING_NULL:
            return ''
        if a is GROUPING_ERROR or a is GROUPING_NULL:
            return formatGroupValue(col, a)
        if b is GROUPING_ERROR or b is GROUPING_NULL:
            return formatGroupValue(col, a)
        if a == b:
            return col.format(a) if col is not None else str(a)
        return ' - '.join(col.format(x) if col is not None else str(x) for x in v)
    if col is not None:
        return col.format(v)
    return str(v)


def formatRange(col, numeric_key):
    return formatGroupValue(col, numeric_key)


PivotGroupRow = collections.namedtuple('PivotGroupRow', 'discrete_keys numeric_key sourcerows pivotrows'.split())


def makePivot(source, groupByCols, pivotCols):
    return PivotSheet('',
            groupByCols=groupByCols,
            pivotCols=pivotCols,
            source=source)


vd.addGlobals({
    'normalizeGroupValue': normalizeGroupValue,
    'isGroupingExceptional': isGroupingExceptional,
    'formatGroupValue': formatGroupValue,
    'GROUPING_NULL': GROUPING_NULL,
    'GROUPING_ERROR': GROUPING_ERROR,
})


class RangeColumn(Column):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.formatter = 'range'

    def formatter_range(self, fmtstr):
        return self._format

    def _format(self, typedval, *args, **kwargs):
        if typedval is None:
            return None
        return formatRange(self.origcol, typedval)


class AggrColumn(Column):
    def calcValue(col, row):
        if col.sheet.loading:
            return visidata.INPROGRESS
        return col.aggregator.aggregate(col.origCol, row.sourcerows)


def makeAggrColumn(aggcol, aggregator):
    aggname = '%s_%s' % (aggcol.name, aggregator.name)

    return AggrColumn(aggname,
                  type=aggregator.type or aggcol.type,
                  fmtstr=aggcol.fmtstr,
                  formatter=aggcol.formatter,
                  displayer=aggcol.displayer,
                  origCol=aggcol,
                  aggregator=aggregator)


class PivotSheet(Sheet):
    'Summarize key columns in pivot table and display as new sheet.'
    rowtype = 'grouped rows'  # rowdef: PivotGroupRow

    def __init__(self, *names, groupByCols=[], pivotCols=[], **kwargs):
        super().__init__(*names,
                pivotCols=pivotCols, # whose values become columns
                groupByCols=groupByCols,  # whose values become rows
                **kwargs)

    def isNumericRange(self, col):
        return vd.isNumeric(col) and self.source.options.numeric_binning

    def resetCols(self):
        super().resetCols()

        # add key columns (grouped by)
        for colnum, c in enumerate(self.groupByCols):
            if c in self.pivotCols:
                continue

            if self.isNumericRange(c):
                newcol = RangeColumn(c.name, origcol=c, width=c.width and c.width*2, getter=lambda c,r: r.numeric_key)
            else:
                newcol = Column(c.name, width=c.width, fmtstr=c.fmtstr,
                                  type=c.type if c.type in vd.typemap else anytype,
                                  origcol=c,
                                  getter=lambda col,row,i=colnum: row.discrete_keys[i],
                                  setter=lambda col,row,val,i=colnum: setitem(row.discrete_keys, i, val) and col.origcol.setValues(row.sourcerows, val))

            self.addColumn(newcol)

        self.setKeys(self.columns)

    def openRow(self, row):
        'open sheet of source rows aggregated in current pivot row'
        vs = copy(self.source)
        vs.name += "_%s"%vd.valueNames(row.discrete_keys, row.numeric_key)
        vs.rows = sum(row.pivotrows.values(), [])
        return vs

    def openCell(self, col, row):
        'open sheet of source rows aggregated in current pivot cell'
        vs = copy(self.source)
        vs.name += "_%s"%str(col.aggvalue)
        vs.rows = row.pivotrows.get(col.aggvalue, [])
        return vs

    def loader(self):
        # two different threads for better interactive display
        vd.sync(self.addAggregateCols(),
                self.groupRows())

    @asyncthread
    def addAggregateCols(self):
        # add aggregated columns
        aggcols = {  # [Column] -> list(aggregators)
            sourcecol: sourcecol.aggregators
                for sourcecol in self.source.visibleCols
                    if sourcecol.aggregators
        } or {  # if pivot given but no aggregators specified
            sourcecol: [vd.aggregators["count"]]
                for sourcecol in self.pivotCols
        }

        if not aggcols:
#            self.addColumn(ColumnAttr('count', 'sourcerows', type=vlen))
            return

        # aggregators without pivot
        if not self.pivotCols:
            for aggcol, aggregatorlist in aggcols.items():
                for aggregator in aggregatorlist:
                    c = makeAggrColumn(aggcol, aggregator)
                    self.addColumn(c)

        # add pivoted columns
        for pivotcol in self.pivotCols:
            allValues = set()
            for value in Progress(pivotcol.getValues(self.source.rows), 'pivoting', total=len(self.source.rows)):
                normValue = normalizeGroupValue(value)
                if normValue in allValues:
                    continue
                allValues.add(normValue)

                if len(self.pivotCols) > 1:
                    valname = '%s_%s' % (pivotcol.name, value)
                else:
                    valname = str(value)

                for aggcol, aggregatorlist in aggcols.items():
                    for aggregator in aggregatorlist:
                        if len(aggcols) > 1: #  if more than one aggregated column, include that column name in the new column name
                            aggname = '%s_%s' % (aggcol.name, aggregator.name)
                        else:
                            aggname = aggregator.name


                        if len(aggregatorlist) > 1 or len(aggcols) > 1:
                            colname = '%s_%s' % (aggname, valname)
                            if not self.name:
                                self.name = self.source.name+'_pivot_'+''.join(c.name for c in self.pivotCols)
                        else:
                            colname = valname
                            if not self.name:
                                self.name = self.source.name+'_pivot_'+''.join(c.name for c in self.pivotCols) + '_' + aggname

                        c = Column(colname,
                                    type=aggregator.type or aggcol.type,
                                    aggvalue=normValue,
                                    getter=lambda col,row,aggcol=aggcol,agg=aggregator: agg.aggregate(aggcol, row.pivotrows.get(col.aggvalue, [])))
                        self.addColumn(c)

#                    if aggregator.name != 'count':  # already have count above
#                        c = Column('Total_' + aggcol.name,
#                                    type=aggregator.type or aggcol.type,
#                                    getter=lambda col,row,aggcol=aggcol,agg=aggregator: agg.aggregate(aggcol, row.sourcerows))
#                        self.addColumn(c)

    @asyncthread
    def groupRows(self, rowfunc=None):
      with ScopedSetattr(self, 'loading', True):
        self.rows = []

        discreteCols = [c for c in self.groupByCols if not self.isNumericRange(c)]

        numericCols = [c for c in self.groupByCols if self.isNumericRange(c)]

        if len(numericCols) > 1:
            vd.fail('only one numeric column can be binned')

        numericBins = []
        degenerateBinning = False
        width = 0
        minval = 0
        nbins = 0
        if numericCols:
            nbins = self.source.options.histogram_bins or int(len(self.source.rows) ** (1./2))
            vals = tuple(numericCols[0].getValues(self.source.rows))
            minval = min(vals) if vals else 0
            maxval = max(vals) if vals else 0
            width = (maxval - minval)/nbins

            if width == 0:
                if vals:
                    numericBins = [(minval, maxval)]
                else:
                    numericBins = []
            elif (numericCols[0].type in (int, vlen) and nbins > (maxval - minval)) or (width == 1):
                degenerateBinning = True
                numericBins = [(val, val) for val in sorted(set(vals))]
                nbins = len(numericBins)
            else:
                numericBins = [(minval+width*i, minval+width*(i+1)) for i in range(nbins)]

        groups = {}

        for sourcerow in self.source.iterrows('grouping'):
            discreteKeys = list(forward(origcol.getTypedValue(sourcerow)) for origcol in discreteCols)

            normalizedDiscreteKeys = tuple(normalizeGroupValue(v) for v in discreteKeys)

            numericGroupRows, groupRow = groups.get(normalizedDiscreteKeys, (None, None))
            if numericGroupRows is None:
                numericGroupRows = {numRange: PivotGroupRow(discreteKeys, numRange, [], {}) for numRange in numericBins}
                groups[normalizedDiscreteKeys] = (numericGroupRows, None)
                for r in numericGroupRows.values():
                    self.addRow(r)

            if numericCols:
                try:
                    val = numericCols[0].getValue(sourcerow)
                    val = wrapply(numericCols[0].type, val)
                    if isGroupingExceptional(val):
                        normVal = normalizeGroupValue(val)
                        if normVal is GROUPING_ERROR:
                            numrange = (GROUPING_ERROR, GROUPING_ERROR)
                        else:
                            numrange = (GROUPING_NULL, GROUPING_NULL)
                        groupRow = numericGroupRows.get(numrange, None)
                        if groupRow is None:
                            groupRow = PivotGroupRow(discreteKeys, numrange, [], {})
                            numericGroupRows[numrange] = groupRow
                            self.addRow(groupRow)
                    else:
                        if not width:
                            binidx = 0
                        elif degenerateBinning:
                            binidx = numericBins.index((val, val))
                        else:
                            binidx = int((val-minval)//width)
                        groupRow = numericGroupRows[numericBins[min(binidx, nbins-1)]]
                except Exception as e:
                    vd.exceptionCaught(e)
                    numrange = (GROUPING_ERROR, GROUPING_ERROR)
                    groupRow = numericGroupRows.get(numrange, None)
                    if groupRow is None:
                        groupRow = PivotGroupRow(discreteKeys, numrange, [], {})
                        numericGroupRows[numrange] = groupRow
                        self.addRow(groupRow)

            if groupRow is None:
                if numericCols:
                    numrange = (GROUPING_ERROR, GROUPING_ERROR)
                    groupRow = PivotGroupRow(discreteKeys, numrange, [], {})
                    numericGroupRows[numrange] = groupRow
                else:
                    groupRow = PivotGroupRow(discreteKeys, None, [], {})
                    groups[normalizedDiscreteKeys] = (numericGroupRows, groupRow)
                self.addRow(groupRow)

            groupRow.sourcerows.append(sourcerow)

            for col in self.pivotCols:
                varval = col.getTypedValue(sourcerow)
                varval = normalizeGroupValue(varval)
                matchingRows = groupRow.pivotrows.get(varval)
                if matchingRows is None:
                    matchingRows = groupRow.pivotrows[varval] = []
                matchingRows.append(sourcerow)

            if rowfunc:
                rowfunc(groupRow)

    def afterLoad(self):
        super().afterLoad()

        # automatically add cache to all columns now that everything is binned
        for c in self.nonKeyVisibleCols:
            if isinstance(c, AggrColumn):
                c.setCache(True)


@PivotSheet.api
def addcol_aggr(sheet, col):
    hasattr(col, 'origCol') or vd.fail('not an aggregation column')
    for agg_choice in vd.chooseAggregators():
        agg_or_list = vd.aggregators[agg_choice]
        aggs = agg_or_list if isinstance(agg_or_list, list) else [agg_or_list]
        for agg in aggs:
            sheet.addColumnAtCursor(makeAggrColumn(col.origCol, vd.aggregators[agg]))


Sheet.addCommand('W', 'pivot', 'vd.push(makePivot(sheet, keyCols, [cursorCol]))', 'open Pivot Table: group rows by key column and summarize current column')

PivotSheet.addCommand('', 'addcol-aggr', 'addcol_aggr(cursorCol)', 'add aggregation column from source of current column')

vd.addGlobals(
    makePivot=makePivot,
    PivotSheet=PivotSheet,
    PivotGroupRow=PivotGroupRow,
)

vd.addMenuItems('''
    Column > Add column > aggregator > addcol-aggr
    Data > Pivot > pivot
''')
