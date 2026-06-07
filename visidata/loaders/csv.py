from visidata import vd, VisiData, SequenceSheet, options
from visidata import Progress
from visidata.text_source import iter_clean_records, clean_text_line, clean_text_row
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
        'Convert from CSV, going through the shared iter_clean_records pipeline.'
        import csv
        csv.field_size_limit(2**31-1)  #288 Windows has max 32-bit

        csv_opts = self.source.options.getall('csv_')
        # -d (generic delimiter) overrides csv_delimiter if csv_delimiter wasn't explicitly set  #2727
        if self.source.options.delimiter != self.source.options.getdefault('delimiter'):
            if csv_opts['delimiter'] == self.source.options.getdefault('csv_delimiter'):
                csv_opts['delimiter'] = self.source.options.delimiter

        with self.open_text_source(newline='') as fp:
            # NUL-clean lines before csv.reader sees them, then pass parsed rows through pipeline
            rdr = csv.reader((clean_text_line(line) for line in fp), **csv_opts)
            yield from iter_clean_records(rdr, lambda r: r, ncols=self.nVisibleCols, record_cleaner=clean_text_row)


@VisiData.api
def save_csv(vd, p, sheet):
    'Save as single CSV file via the shared save_text_table pipeline.'
    import csv
    csv.field_size_limit(2**31-1)  #288 Windows has max 32-bit

    csv_opts = p.options.getall('csv_')
    # -d (generic delimiter) overrides csv_delimiter if csv_delimiter wasn't explicitly set  #2727
    if p.options.delimiter != p.options.getdefault('delimiter'):
        if csv_opts['delimiter'] == p.options.getdefault('csv_delimiter'):
            csv_opts['delimiter'] = p.options.delimiter

    cw_holder = {}

    def _write_header(fp, cols, clean):
        cw_holder['cw'] = csv.writer(fp, **csv_opts)
        colnames = [clean(col.name) for col in cols]
        if ''.join(colnames):
            cw_holder['cw'].writerow(colnames)

    def _write_row(fp, dispvals, clean):
        cw_holder['cw'].writerow([clean(v) for v in dispvals.values()])

    with Progress(gerund='saving', total=sheet.nRows):
        sheet.save_text_table(p, write_header=_write_header, write_row=_write_row, newline='')

vd.addGlobals({
    'CsvSheet': CsvSheet
})
