import re

from visidata import vd, BaseSheet, TypedExceptionWrapper, stacktrace

vd.option('regex_skip', '', 'regex of lines to skip in text sources', help='regex', replay=True)
vd.option('regex_flags', 'I', 'flags to pass to re.compile() [AILMSUX]', replay=True)


def clean_text_line(line):
    'Shared line-level cleanup for all text-table loaders: strip NUL characters.'
    if isinstance(line, str):
        return line.replace('\0', '')
    return line


def clean_text_row(row):
    'Shared row-level cleanup for pre-parsed rows (e.g. csv.reader output): strip NUL from each string field in-place.'
    if isinstance(row, list):
        for i, v in enumerate(row):
            if isinstance(v, str):
                row[i] = v.replace('\0', '')
    return row


def is_empty_text_row(row):
    'Return True if row is empty (all empty string or None).'
    if row is None:
        return True
    if isinstance(row, list):
        return len(row) == 0 or (len(row) == 1 and (row[0] == '' or row[0] is None))
    if isinstance(row, str):
        return row.strip() == ''
    return False


def extend_text_row(row, ncols):
    'Extend row to ncols by appending None. Returns the same row list (mutated).'
    if ncols and len(row) < ncols:
        row.extend([None] * (ncols - len(row)))
    return row


def wrap_error_row(exc, ncols):
    'Wrap an exception into an error row of size ncols with TypedExceptionWrapper in first cell, rest None.'
    if not hasattr(exc, 'stacktrace'):
        exc.stacktrace = stacktrace()
    errrow = [None] * ncols
    errrow[0] = TypedExceptionWrapper(None, exception=exc)
    return errrow


def iter_clean_text_rows(fp, parse_line, ncols=0):
    '''Generator that yields parsed rows from a text file fp using parse_line(line)->row.

    Shared for line-per-record text-table loaders (CSV, TSV, USV, PSV).
    Not for multi-line-per-record formats like LSV (those should implement their own record producer).

    Pipeline per line: clean NUL → parse → empty check → extend → error wrap
    '''
    yield from iter_clean_records(fp, parse_line, ncols=ncols, record_cleaner=clean_text_line)


def iter_clean_records(records_iter, parse_record, ncols=0, record_cleaner=clean_text_line):
    '''Unified pipeline for line-per-record text-table formats (CSV, TSV, USV, PSV).

    Pipeline per record:
      1. Get next from records_iter (catches iterator exceptions like csv.Error → error row)
      2. record_cleaner (NUL cleanup per line or per field)
      3. parse_record (format-specific parse, must return list row)
      4. is_empty_text_row check → skip
      5. extend_text_row to ncols → yield

    Exceptions from both the record iterator and parse_record are caught, wrapped as error
    rows with TypedExceptionWrapper in the first column.

    For multi-line-per-record formats (e.g. LSV), implement your own record producer and
    reuse the individual helpers: clean_text_line, open_text_source, clean_saved_value, etc.

    Args:
        records_iter: iterable yielding raw records (strings for line-based, list for pre-parsed like CSV)
        parse_record: callable(cleaned_record) -> list row (must not return None)
        ncols: target column count for extending short rows and sizing error rows
        record_cleaner: callable(raw_record) -> cleaned_record, default: clean_text_line (strip NUL from strings).
            Use clean_text_row for pre-parsed list records (e.g. csv.reader output).
    '''
    it = iter(records_iter)
    while True:
        try:
            raw = next(it)
        except StopIteration:
            return
        except Exception as e:
            yield wrap_error_row(e, ncols or 1)
            continue
        try:
            cleaned = record_cleaner(raw)
            row = parse_record(cleaned)
            if is_empty_text_row(row):
                continue
            yield extend_text_row(row, ncols)
        except Exception as e:
            yield wrap_error_row(e, ncols or 1)


@BaseSheet.api
def iter_text_rows(sheet, parse_record, ncols=None, record_cleaner=clean_text_line, **open_kwargs):
    '''Sheet-level unified loading API for line-per-record text formats.

    Opens sheet source via open_text_source (encoding, encoding_errors, regex_skip all handled uniformly),
    then runs iter_clean_records pipeline.

    Not for multi-line-per-record formats (e.g. LSV) — those should call open_text_source
    directly and implement their own record assembly.

    Args:
        parse_record: callable(cleaned_record) -> list row
        ncols: column count (default sheet.nVisibleCols)
        record_cleaner: clean_text_line for string records, clean_text_row for pre-parsed list rows
        **open_kwargs: extra kwargs for open_text_source (e.g. newline='' for CSV)
    '''
    ncols = ncols if ncols is not None else sheet.nVisibleCols
    with sheet.open_text_source(**open_kwargs) as fp:
        yield from iter_clean_records(fp, parse_record, ncols=ncols, record_cleaner=record_cleaner)

@BaseSheet.api
def regex_flags(sheet):
    'Return flags to pass to regex functions from options'
    return sum(getattr(re, f.upper()) for f in sheet.options.regex_flags)


class FilterFile:
    def __init__(self, fp, regex:str, regex_flags:int=0):
        import re
        self._fp = fp
        self._regex_skip = re.compile(regex, regex_flags)

    def readline(self) -> str:
        while True:
            line = self._fp.readline()
            if self._regex_skip.match(line):
                continue
            return line

    def __getattr__(self, k):
        return getattr(self._fp, k)

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        return self._fp.__exit__(*args, **kwargs)


@BaseSheet.api
def open_text_source(sheet, **kwargs):
    'Open sheet source as text, passing **kwargs to .open() (default to sheet options for encoding and regex_skip).'
    openkwargs = dict(encoding=sheet.options.encoding,
                      encoding_errors=sheet.options.encoding_errors)
    openkwargs.update(kwargs)
    fp = sheet.source.open(**openkwargs)
    regex_skip = sheet.options.regex_skip
    if regex_skip:
        return FilterFile(fp, regex_skip, sheet.regex_flags())
    return fp

vd.addGlobals({
    'clean_text_line': clean_text_line,
    'clean_text_row': clean_text_row,
    'is_empty_text_row': is_empty_text_row,
    'extend_text_row': extend_text_row,
    'wrap_error_row': wrap_error_row,
    'iter_clean_text_rows': iter_clean_text_rows,
    'iter_clean_records': iter_clean_records,
})
