import re

from visidata import vd, BaseSheet, TypedExceptionWrapper, stacktrace

vd.option('regex_skip', '', 'regex of lines to skip in text sources', help='regex', replay=True)
vd.option('regex_flags', 'I', 'flags to pass to re.compile() [AILMSUX]', replay=True)


def clean_text_line(line):
    'Shared line-level cleanup for all text-table loaders: strip NUL characters.'
    if isinstance(line, str):
        return line.replace('\0', '')
    return line


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

    Shared for all text-table loaders:
    - Clean NUL from each line
    - Catch exceptions per row (non-destructively continues to next row)
    - Extend short rows
    - Yields parsed rows
    '''
    for rawline in fp:
        try:
            line = clean_text_line(rawline)
            row = parse_line(line)
            if is_empty_text_row(row):
                continue
            yield extend_text_row(row, ncols)
        except Exception as e:
            yield wrap_error_row(e, ncols or 1)

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
    'is_empty_text_row': is_empty_text_row,
    'extend_text_row': extend_text_row,
    'wrap_error_row': wrap_error_row,
    'iter_clean_text_rows': iter_clean_text_rows,
})
