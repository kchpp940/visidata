from visidata import vd, VisiData, SequenceSheet, options
from visidata import Progress
from visidata.text_source import clean_text_line, clean_text_row, is_empty_text_row, extend_text_row, wrap_error_row
from visidata.save import clean_saved_value

vd.option('csv_dialect', 'excel', 'dialect passed to csv.reader', replay=True)
vd.option('csv_delimiter', ',', 'delimiter passed to csv.reader', replay=True)
vd.option('csv_doublequote', True, 'quote-doubling setting passed to csv.reader', replay=True)
vd.option('csv_quotechar', '"', 'quotechar passed to csv.reader', replay=True)
vd.option('csv_quoting', 0, 'quoting style passed to csv.reader and csv.writer', replay=True)
vd.option('csv_skipinitialspace', True, 'skipinitialspace passed to csv.reader', replay=True)
vd.option('csv_escapechar', None, 'escapechar passed to csv.reader', replay=True)
vd.option('csv_lineterminator', '\r\n', 'lineterminator passed to csv.writer', replay=True)
vd.option('safety_first', False, 'sanitize input/output to handle edge cases, with a performance cost', replay=True)


@VisiData.api
def guess_csv_delimiter(vd, p):
    'If csv_delimiter option has been modified from default, assume CSV format.'

    if vd.options.csv_delimiter != vd.options.getdefault('csv_delimiter'):
        return dict(filetype='csv', _likelihood=2)

@VisiData.api
def guess_csv(vd, p):
    import csv
    csv.field_size_limit(2**31-1)  #288 Windows has max 32-bit
    try:
        line = next(p.open())
    except StopIteration:
        return
    if ',' in line:
        dialect = csv.Sniffer().sniff(line)
        r = dict(filetype='csv', _likelihood=0)

        for csvopt in dir(dialect):
            if not csvopt.startswith('_'):
                v = getattr(dialect, csvopt)
                optname = 'csv_'+csvopt
                r[optname] = v
                if vd.options.get(optname) != v:
                    vd.warning(f'guessed option {optname}={v}')

        return r

@VisiData.api
def open_csv(vd, p):
    return CsvSheet(p.base_stem, source=p)

class CsvSheet(SequenceSheet):
    _rowtype = list  # rowdef: list of values

    def iterload(self):
        'Load CSV rows, reusing shared helpers for cleanup, empty detection, extension, and error wrapping.'
        import csv
        csv.field_size_limit(2**31-1)  #288 Windows has max 32-bit

        csv_opts = self.source.options.getall('csv_')
        # -d (generic delimiter) overrides csv_delimiter if csv_delimiter wasn't explicitly set  #2727
        if self.source.options.delimiter != self.source.options.getdefault('delimiter'):
            if csv_opts['delimiter'] == self.source.options.getdefault('csv_delimiter'):
                csv_opts['delimiter'] = self.source.options.delimiter

        ncols = self.nVisibleCols
        with self.open_text_source(newline='') as fp:
            rdr = csv.reader((clean_text_line(line) for line in fp), **csv_opts)
            it = iter(rdr)
            while True:
                try:
                    raw = next(it)
                except StopIteration:
                    return
                except Exception as e:
                    yield wrap_error_row(e, ncols or 1)
                    continue
                try:
                    row = clean_text_row(raw)
                    if is_empty_text_row(row):
                        continue
                    yield extend_text_row(row, ncols)
                except Exception as e:
                    yield wrap_error_row(e, ncols or 1)


@VisiData.api
def save_csv(vd, p, sheet):
    'Save as single CSV file. Reuses clean_saved_value for NUL stripping.'
    import csv
    csv.field_size_limit(2**31-1)  #288 Windows has max 32-bit

    csv_opts = p.options.getall('csv_')
    # -d (generic delimiter) overrides csv_delimiter if csv_delimiter wasn't explicitly set  #2727
    if p.options.delimiter != p.options.getdefault('delimiter'):
        if csv_opts['delimiter'] == p.options.getdefault('csv_delimiter'):
            csv_opts['delimiter'] = p.options.delimiter

    with Progress(gerund='saving', total=sheet.nRows):
        with p.open(mode='w', encoding=sheet.options.save_encoding, newline='') as fp:
            cw = csv.writer(fp, **csv_opts)
            colnames = [clean_saved_value(col.name) for col in sheet.visibleCols]
            if ''.join(colnames):
                cw.writerow(colnames)
            for dispvals in sheet.iterdispvals(format=True):
                cw.writerow([clean_saved_value(v) for v in dispvals.values()])

vd.addGlobals({
    'CsvSheet': CsvSheet
})
