import collections

#1179  Line Separated Values for e.g. awk

from visidata import VisiData, Sheet, ItemColumn, TypedExceptionWrapper, stacktrace
from visidata.text_source import clean_text_line
from visidata.save import clean_saved_value


@VisiData.api
def open_lsv(vd, p):
    return LsvSheet(p.base_stem, source=p)


def _lsv_error_row(exc):
    if not hasattr(exc, 'stacktrace'):
        exc.stacktrace = stacktrace()
    return {'_error': str(TypedExceptionWrapper(None, exception=exc))}


def _lsv_records(fp):
    '''LSV record producer — multi-line accumulation specific to this format.

    Reuses clean_text_line (NUL strip) and produces dict rows per record.
    Last non-empty record without trailing blank line is flushed on StopIteration.
    Each line is wrapped individually for exceptions; record boundaries are always
    explicit blank lines so a bad line only corrupts its own record.
    '''
    row = collections.defaultdict(str)
    current_key = ''

    for raw_line in fp:
        try:
            line = clean_text_line(raw_line).strip()
            if not line:
                if row:
                    yield row
                    row = collections.defaultdict(str)
                    current_key = ''
                continue
            if ':' in line:
                current_key, rest = line.split(':', maxsplit=1)
                current_key = current_key.strip()
                line = rest
            row[current_key] += line.strip()
        except Exception as e:
            yield _lsv_error_row(e)
            row = collections.defaultdict(str)
            current_key = ''

    if row:
        yield row


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

        with self.open_text_source() as fp:
            yield from _lsv_records(fp)
