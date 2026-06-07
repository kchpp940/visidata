import collections

#1179  Line Separated Values for e.g. awk

from visidata import VisiData, Sheet, ItemColumn, TypedExceptionWrapper, stacktrace
from visidata.text_source import iter_clean_records


@VisiData.api
def open_lsv(vd, p):
    return LsvSheet(p.base_stem, source=p)


@VisiData.api
def save_lsv(vd, p, *vsheets):
    vs = vsheets[0]

    def _write_row(fp, dispvals, clean):
        for col, val in dispvals.items():
            fp.write('%s: %s\n' % (clean(col.name), clean(val)))
        fp.write('\n')

    vs.save_text_table(p, write_row=_write_row)


def _lsv_wrap_error(exc, ncols):
    if not hasattr(exc, 'stacktrace'):
        exc.stacktrace = stacktrace()
    errwrap = TypedExceptionWrapper(None, exception=exc)
    return {'_error': str(errwrap)}


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

        state = {'row': collections.defaultdict(str), 'k': ''}

        def _parse_record(line):
            line = line.strip()
            if not line:
                result = state['row']
                state['row'] = collections.defaultdict(str)
                state['k'] = ''
                return result
            if ':' in line:
                state['k'], rest = line.split(':', maxsplit=1)
                state['k'] = state['k'].strip()
                line = rest
            state['row'][state['k']] += line.strip()
            return None

        with self.open_text_source() as fp:
            yield from iter_clean_records(fp, _parse_record, ncols=0, wrap_error=_lsv_wrap_error)

        if state['row']:
            yield state['row']
