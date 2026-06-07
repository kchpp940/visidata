import collections

#1179  Line Separated Values for e.g. awk

from visidata import VisiData, Sheet, ItemColumn, TypedExceptionWrapper, stacktrace
from visidata.text_source import clean_text_line
from visidata.save import clean_saved_value


@VisiData.api
def open_lsv(vd, p):
    return LsvSheet(p.base_stem, source=p)


@VisiData.api
def save_lsv(vd, p, *vsheets):
    vs = vsheets[0]

    with p.open(mode='w', encoding=vs.options.save_encoding) as fp:
        for dispvals in vs.iterdispvals(format=True):
            for col, val in dispvals.items():
                fp.write('%s: %s\n' % (clean_saved_value(col.name), clean_saved_value(val)))
            fp.write('\n')


class LsvSheet(Sheet):
    def addRow(self, row, **kwargs):
        super().addRow(row, **kwargs)
        for k in row:
            if k not in self._knownCols:
                self.addColumn(ItemColumn(k))
                self._knownCols.add(k)


    def iterload(self):
        self.columns = []
        self.rows = []
        self._knownCols = set()
        row = collections.defaultdict(str)
        k = ''

        with self.open_text_source() as fp:
            for rawline in fp:
                try:
                    line = clean_text_line(rawline).strip()
                    if not line:
                        yield row
                        row = collections.defaultdict(str)

                    if ':' in line:
                        k, line = line.split(':', maxsplit=1)
                    # else append to previous k

                    row[k.strip()] += line.strip()
                except Exception as e:
                    if not hasattr(e, 'stacktrace'):
                        e.stacktrace = stacktrace()
                    errwrap = TypedExceptionWrapper(None, exception=e)
                    row['_error'] = str(errwrap)

        if row:
            yield row
