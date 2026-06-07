from visidata import vd, Sheet, options, Column, asyncthread, Progress, PivotGroupRow, HistogramColumn, TypedWrapper, TypedExceptionWrapper, wrapply

from visidata.loaders._pandas import PandasSheet
from visidata.pivot import PivotSheet, normalizeGroupValue, GROUPING_NULL, GROUPING_ERROR

class DataFrameRowSliceAdapter:
    """Tracks original dataframe and a boolean row mask

    This is a workaround to (1) save memory (2) keep id(row)
    consistent when iterating, as id() is used significantly
    by visidata's selectRow implementation.
    """
    def __init__(self, df, mask):
        pd = vd.importExternal('pandas')
        np = vd.importExternal('numpy')
        if not isinstance(df, pd.DataFrame):
            vd.fail('%s is not a dataframe' % type(df).__name__)
        if not isinstance(mask, pd.Series):
            vd.fail('mask %s is not a Series' % type(mask).__name__)
        if df.shape[0] != mask.shape[0]:
            vd.fail('dataframe and mask have different shapes (%s vs %s)' % (df.shape[0], mask.shape[0]))

        self.df = df
        self.mask_bool = mask  # boolean mask
        self.mask_iloc = np.where(mask.values)[0]  # integer indexes corresponding to mask
        self.mask_count = mask.sum()

    def __len__(self):
        return self.mask_count

    def __getitem__(self, k):
        if isinstance(k, slice):
            import pandas as pd
            new_mask = pd.Series(False, index=self.df.index)
            new_mask.iloc[self.mask_iloc[k]] = True
            return DataFrameRowSliceAdapter(self.df, new_mask)
        return self.df.iloc[self.mask_iloc[k]]

    def __iter__(self):
        # With the internal selection API used by PandasSheet,
        # this should no longer be needed and can be replaced by
        # DataFrameAdapter(self.df[self.mask_iloc])
        return DataFrameRowSliceIter(self.df, self.mask_iloc)

    def __getattr__(self, k):
        # This is trouble ..
        return getattr(self.df[self.mask_bool], k)

class DataFrameRowSliceIter:
    def __init__(self, df, mask_iloc, index=0):
        self.df = df
        self.mask_iloc = mask_iloc
        self.index = index

    def __next__(self):
        # Accessing row of original dataframe, to ensure
        # that no copies are made and id() of selected rows
        # will match original dataframe's rows
        if self.index >= self.mask_iloc.shape[0]:
            raise StopIteration()
        row = self.df.iloc[self.mask_iloc[self.index]]
        self.index += 1
        return row

def makePandasFreqTable(sheet, *groupByCols):
    fqcolname = '%s_freq' % '-'.join(col.name for col in groupByCols)
    return PandasFreqTableSheet(sheet.name, fqcolname, groupByCols=groupByCols, source=sheet)


def _pandasIsNA(v):
    if v is None:
        return True
    pd = vd.importExternal('pandas')
    return bool(pd.isna(v))


def _pandasNormalizeGroupValue(v):
    if _pandasIsNA(v):
        return GROUPING_NULL
    return normalizeGroupValue(v)


class PandasFreqTableSheet(PivotSheet):
    'Generate frequency-table sheet on currently selected column.'
    rowtype = 'bins'  # rowdef FreqRow(keys, sourcerows)

    def selectRow(self, row):
        # Select all entries in the bin on the source sheet.
        # Use the internally defined _selectByLoc to avoid
        # looping which causes a significant performance hit.
        self.source._selectByILoc(row.sourcerows.mask_iloc, selected=True)
        # then select the bin itself on this sheet
        return super().selectRow(row)

    def unselectRow(self, row):
        self.source._selectByILoc(row.sourcerows.mask_iloc, selected=False)
        return super().unselectRow(row)

    def addUndoSelection(self):
        self.source.addUndoSelection()
        super().addUndoSelection()

    def updateLargest(self, grouprow):
        self.largest = max(self.largest, len(grouprow.sourcerows))

    def loader(self):
        'Generate frequency table then reverse-sort by length.'
        import pandas as pd

        df = self.source.df

        if len(self.groupByCols) < 1:
            vd.fail("no columns to group on")

        ncols = len(self.groupByCols)

        for c in [
                    Column('count', type=int,
                           getter=lambda col,row: len(row.sourcerows)),
                    Column('percent', type=float,
                           getter=lambda col,row: len(row.sourcerows)*100/df.shape[0]),
                    HistogramColumn('histogram', type=str, width=self.options.default_width*2)
                    ]:
            self.addColumn(c)

        buckets = {}

        for i, sourcerow in enumerate(Progress(self.source.rows)):
            typed_vals = []
            norm_vals = []
            for col in self.groupByCols:
                raw = col.getValue(sourcerow)
                typed = wrapply(col.type, raw)
                norm = _pandasNormalizeGroupValue(typed)
                typed_vals.append(typed)
                norm_vals.append(norm)

            norm_key = tuple(norm_vals)

            if norm_key not in buckets:
                display_keys = []
                for j, col in enumerate(self.groupByCols):
                    nk = norm_vals[j]
                    if nk is GROUPING_NULL:
                        display_keys.append(TypedWrapper(col.type, None))
                    elif nk is GROUPING_ERROR:
                        display_keys.append(TypedExceptionWrapper(col.type, exception=ValueError('type conversion error')))
                    else:
                        display_keys.append(typed_vals[j])

                buckets[norm_key] = {
                    'ilocs': [],
                    'display_keys': tuple(display_keys),
                }

            buckets[norm_key]['ilocs'].append(i)

        sorted_buckets = sorted(buckets.items(), key=lambda kv: len(kv[1]['ilocs']), reverse=True)

        for norm_key, info in sorted_buckets:
            mask = pd.Series(False, index=df.index)
            mask.iloc[info['ilocs']] = True

            self.addRow(PivotGroupRow(
                info['display_keys'],
                None,
                DataFrameRowSliceAdapter(df, mask),
                {}
            ))

    def openRow(self, row):
        return self.source.expand_source_rows(row)

@Sheet.api
def expand_source_rows(sheet, row):
    """Support for expanding a row of frequency table to underlying rows"""
    if row.sourcerows is None:
        vd.fail("no source rows")
    return PandasSheet(sheet.name, vd.valueNames(row.discrete_keys, row.numeric_key), source=row.sourcerows)

PandasSheet.addCommand('F', 'freq-col', 'vd.push(makePandasFreqTable(sheet, cursorCol))', 'open Frequency Table grouped on current column, with aggregations of other columns')
PandasSheet.addCommand('gF', 'freq-keys', 'vd.push(makePandasFreqTable(sheet, *keyCols))', 'open Frequency Table grouped by all key columns on source sheet, with aggregations of other columns')

PandasFreqTableSheet.init('largest', lambda: 1)
PandasFreqTableSheet.options.numeric_binning = False

vd.addGlobals(makePandasFreqTable=makePandasFreqTable)
