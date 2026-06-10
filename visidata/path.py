import os
import os.path
import sys
import io
import codecs
import pathlib
import tempfile
import shutil
import contextlib
from urllib.parse import urlparse, urlunparse
from functools import wraps, lru_cache

from visidata import vd
from visidata import VisiData, BaseSheet

vd.help_encoding = '''Common Encodings:

- `utf-8`: Unicode (ASCII compatible, most common)
- `utf-8-sig`: Unicode as above, but saves/skips leading BOM
- `ascii`: 7-bit ASCII
- `latin1`: also known as `iso-8859-1`
- `cp437`: original IBM PC character set
- `shift_jis`: Japanese

See [:onclick https://docs.python.org/3/library/codecs.html#standard-encodings]https://docs.python.org/3/library/codecs.html#standard-encodings[/]
'''

vd.help_encoding_errors = '''Encoding Error Handlers:

- `strict`: raise error
- `ignore`: discard
- `replace`: replacement marker
- `backslashreplace`: use "\\uxxxxxx"
- `surrogateescape`: use surrogate characters

See [:onclick https://docs.python.org/3/library/codecs.html#error-handlers]https://docs.python.org/3/library/codecs.html#error-handlers[/]
'''

vd.option('encoding', 'utf-8-sig', 'encoding passed to codecs.open when reading a file', replay=True, help=vd.help_encoding)
vd.option('encoding_errors', 'surrogateescape', 'encoding_errors passed to codecs.open', replay=True, help=vd.help_encoding_errors)

@VisiData.api
def pkg_resources_files(vd, package):
    '''
    Returns a Traversable object (Path-like), based on the location of the package.
    importlib.resources.files exists in Python >= 3.9; use importlib_resources for the rest.
    '''
    try:
        from importlib.resources import files
    except ImportError: #1968
        from importlib_resources import files
    return files(package)

@lru_cache()
def vstat(path, force=False):
    try:
        return os.stat(path)
    except Exception as e:
        return None

def filesize(path):
    if hasattr(path, 'filesize') and path.filesize is not None:
        return path.filesize
    if hasattr(path, 'is_url') and path.is_url():
        return 0
    if hasattr(path, 'has_fp') and path.has_fp():
        return 0
    st = path.stat() # vstat(path)
    return st and st.st_size

def modtime(path):
    st = path.stat()
    return st and st.st_mtime


# from https://stackoverflow.com/questions/55889474/convert-io-stringio-to-io-bytesio
class BytesIOWrapper(io.BufferedReader):
    """Wrap a buffered bytes stream over TextIOBase string stream."""

    def __init__(self, text_io_buffer, encoding=None, errors=None, **kwargs):
        super(BytesIOWrapper, self).__init__(text_io_buffer, **kwargs)
        self.encoding = encoding or text_io_buffer.encoding or vd.options.encoding
        self.errors = errors or text_io_buffer.errors or vd.options.encoding_errors

    def _encoding_call(self, method_name, *args, **kwargs):
        raw_method = getattr(self.raw, method_name)
        val = raw_method(*args, **kwargs)
        return val.encode(self.encoding, errors=self.errors)

    def read(self, size=-1):
        return self._encoding_call('read', size)

    def read1(self, size=-1):
        return self._encoding_call('read1', size)

    def peek(self, size=-1):
        return self._encoding_call('peek', size)


class FileProgress:
    'Open file in binary mode and track read() progress.'
    def __init__(self, path, fp, mode='r', **kwargs):
        from visidata import Progress
        self.path = path
        self.fp = fp
        self.prog = None
        if 'r' in mode:
            gerund = 'reading'
            self.prog = Progress(gerund=gerund, total=filesize(path))
        elif 'w' in mode:
            gerund = 'writing'
            self.prog = Progress(gerund=gerund)
        else:
            gerund = 'nothing'

        # track Progress on original fp
        self.fp_orig_read = self.fp.read
        self.fp_orig_readline = self.fp.readline
        self.fp_orig_close = self.fp.close

        self.fp.read = self.read
        self.fp.close = self.close

        if self.prog:
            self.prog.__enter__()

    def close(self, *args, **kwargs):
        if self.prog:
            self.prog.__exit__(None, None, None)
            self.prog = None
        return self.fp_orig_close(*args, **kwargs)

    def read(self, size=-1):
        r = self.fp_orig_read(size)
        if self.prog:
            if r:
                self.prog.addProgress(len(r))
        return r

    def readline(self, size=-1):
        r = self.fp_orig_readline(size)
        if self.prog:
            self.prog.addProgress(len(r))
        return r

    def __getattr__(self, k):
        return getattr(self.fp, k)

    def __enter__(self):
        self.fp.__enter__()
        return self

    def __next__(self):
        r = next(self.fp)
        self.prog.addProgress(len(r))
        return r

    def __iter__(self):
        if not self.prog:
            yield from self.fp
        else:
            for line in self.fp:
                self.prog.addProgress(len(line))
                yield line

    def __exit__(self, type, value, tb):
        return self.fp.__exit__(type, value, tb)


class Path(os.PathLike):
    'File and path-handling class, modeled on `pathlib.Path`.'
    def __init__(self, given, fp=None, fptext=None, lines=None, filesize=None):
        # Resolve pathname shell variables and ~userdir
        self.given = os.path.expandvars(os.path.expanduser(str(given)))
        self.fptext = fptext
        self.fp = fp
        self.lines = lines or []  # shared among all RepeatFile instances
        self.filesize = filesize
        self.rfile = None

    @property
    def name(self):
        'Full filename including extensions. Same as pathlib.Path.name.'
        if self._given == '.':
            return self._path.absolute().name
        return self._path.name

    @property
    def given(self):
        'The path as given to the constructor.'
        return self._given

    @given.setter
    def given(self, given):
        self._given = given
        if isinstance(given, os.PathLike):
            self._path = given
        else:
            self._path = pathlib.Path(given)

        self.ext = self.suffix[1:]
        if self.suffix and self.suffix != '.':  #1450  don't make this a oneliner; [:-0] doesn't work  #2887
            self.base_stem = self._path.name[:-len(self.suffix)]
        elif self._given == '.':  #1768
            self.base_stem = self._path.absolute().name
        else:
            self.base_stem = self._path.name

        # check if file is compressed
        if self.suffix in ['.gz', '.bz2', '.xz', '.lzma', '.zst']:
            self.compression = self.ext
            uncompressedpath = Path(self.given[:-len(self.suffix)])  # strip suffix
            self.base_stem = uncompressedpath.base_stem
            self.ext = uncompressedpath.ext
        else:
            self.compression = None

    @property
    def options(self):
        return vd.OptionsObject(vd._options, obj=self)

    def __getattr__(self, k):
        if hasattr(self.__dict__, k):
            r = getattr(self.__dict__, k)
        else:
            if self.__dict__.get('_path', None) is not None:
                r = getattr(self._path, k)
            else:
                raise AttributeError(k)
        if isinstance(r, pathlib.Path):
            return Path(r)
        return r

    def __fspath__(self):
        return self._path.__fspath__()

    def __lt__(self, a):
        if isinstance(a, Path):
            return self._path.__lt__(a._path)
        return self._path.__lt__(a)

    def __truediv__(self, a):
        return Path(self._path.__truediv__(a))

    def has_fp(self):
        'Return True if this is a virtual Path to an already open file.'
        return bool(self.fp or self.fptext)

    def open(self, mode='rt', encoding=None, encoding_errors=None, newline=None):
        if 'b' in mode:
            return self.open_bytes(mode)

        return self.open_text(mode=mode, encoding=encoding, encoding_errors=encoding_errors, newline=newline)

    def open_bytes(self, mode='rb'):
        'Open the file pointed by this path and return a file object in binary mode.'
        if self.rfile:
            raise ValueError('a RepeatFile holds text and cannot be reopened in binary mode')

        if 'b' not in mode:
            mode += 'b'

        if self.given == '-':
            if 'r' in mode:
                return os.fdopen(vd._stdin.fileno(), 'rb')
            elif 'w' in mode or 'a' in mode:
                # convert 'a' to 'w' for stdout: https://bugs.python.org/issue27805
                return os.dup(vd._stdout.fileno())
            else:
                vd.error(f'invalid mode `{mode}` for Path.open()')
                return sys.stderr

        return self._open(mode=mode)

    def open_text(self, mode='rt', encoding=None, encoding_errors=None, newline=None):
        'Open path in text mode, using options.encoding and options.encoding_errors.  Return open file-pointer or file-pointer-like.'
        # rfile makes a single-access fp reusable

        if 't' not in mode:
            mode += 't'

        if self.rfile:
            return self.rfile.reopen()

        if self.fp and not self.fptext:
            self.fptext = codecs.iterdecode(self.fp,
                                            encoding=encoding or vd.options.encoding,
                                            errors=encoding_errors or vd.options.encoding_errors)

        if self.fptext:
            self.rfile = RepeatFile(self.fptext)
            return self.rfile

        if self.given == '-':
            if 'r' in mode:
                return vd._stdin
            elif 'w' in mode or 'a' in mode:
                # convert 'a' to 'w' for stdout: https://bugs.python.org/issue27805
                return open(os.dup(vd._stdout.fileno()), 'wt')
            else:
                vd.error(f'invalid mode `{mode}` for Path.open()')
                return sys.stderr

        return self._open(mode=mode, encoding=encoding or vd.options.encoding, errors=vd.options.encoding_errors, newline=newline)

    @wraps(pathlib.Path.read_text)
    def read_text(self, *args, **kwargs):
        'Open the file in text mode and return its entire decoded contents.'
        if 'encoding' not in kwargs:
            kwargs['encoding'] = vd.options.encoding
        if 'errors' not in kwargs:
            kwargs['errors'] = kwargs.get('encoding_errors', vd.options.encoding_errors)

        if self.lines:
            return RepeatFile(self.lines).read()
        elif self.fp:
            return self.fp.read()
        elif self.fptext:
            return self.fptext.read()
        else:
            return self._path.read_text(*args, **kwargs)

    @wraps(pathlib.Path.open)
    def _open(self, *args, **kwargs):
        if self.fp:
            return FileProgress(self, fp=self.fp, **kwargs)

        if self.fptext:
            return FileProgress(self, fp=BytesIOWrapper(self.fptext), **kwargs)

        path = self

        if self.compression == 'gz':
            import gzip
            zopen = gzip.open
        elif self.compression == 'bz2':
            import bz2
            zopen = bz2.open
        elif self.compression in ['xz', 'lzma']:
            import lzma
            zopen = lzma.open
        elif self.compression == 'zst':
            zstandard = vd.importExternal('zstandard')
            zopen = zstandard.open
        else:
            return FileProgress(path, fp=self._path.open(*args, **kwargs), **kwargs)

        if 'w' in kwargs.get('mode', ''):
            #1159 FileProgress on the outside to close properly when writing
            return FileProgress(path, fp=zopen(path, **kwargs), **kwargs)

        #1255 FileProgress on the inside to track uncompressed bytes when reading
        return zopen(FileProgress(path, fp=open(path, mode='rb'), **kwargs), **kwargs)

    def __iter__(self):
        with self.open(encoding=vd.options.encoding) as fd:
            for line in fd:
                yield line.rstrip('\n')

    def read_bytes(self):
        'Return the entire binary contents of the pointed-to file as a bytes object.'
        with self.open(mode='rb') as fp:
            return fp.read()

    @wraps(pathlib.Path.is_fifo)
    def is_fifo(self):
        'Return True if the path is a fifo.'
        return self._path.is_fifo()

    def is_local(self):
        'Return True if self.filename refers to a file on the local disk.'
        return not bool(self.is_url()) and not bool(self.fp) and not bool(self.fptext)

    def is_url(self):
        'Return True if the given path appears to be a URL.'
        return '://' in self.given

    def __str__(self):
        if self.is_url():
            return self.given
        return str(self._path)

    @wraps(pathlib.Path.stat)
    @lru_cache()
    def stat(self, force=False):
        'Return Path.stat() if relevant.'
        try:
            if not self.is_url():
                return self._path.stat()
        except Exception as e:
            return None

    @wraps(pathlib.Path.exists)
    def exists(self):
        'Return True if the path can be opened.'
        if self.has_fp() or self.is_url():
            return True
        return self._path.exists()

    @property
    def scheme(self):
        'The URL scheme component, if path is a URL.'
        if self.is_url():
            return urlparse(self.given).scheme

    def iterdir(self):  #2188
        'Yield Path objects of the directory contents.'
        return (Path(p) for p in self._path.iterdir())

    def with_name(self, name):
        'Return a sibling Path with *name* as a filename in the same directory.'
        if self.is_url():
            urlparts = list(urlparse(self.given))
            urlparts[2] = '/'.join(list(Path(urlparts[2]).parts[1:-1]) + [name])
            return Path(urlunparse(urlparts))
        else:
            return Path(self._from_parsed_parts(self._drv, self._root, list(self.parts[:-1]) + [name]))


class RepeatFile:
    '''Lazy file-like object that can be read and line-seeked more than once from memory.'''

    def __init__(self, iter_lines, lines=None):
        self.iter_lines = iter_lines
        self.lines = lines if lines is not None else []
        self.iter = RepeatFileIter(self)
        self.encoding = None  #2829
        self.errors = None

    def __enter__(self):
        '''Returns a new independent file-like object, sharing the same line cache.'''
        return self.reopen()

    def __exit__(self, a,b,c):
        pass

    def reopen(self):
        'Return copy of file-like with internal iterator reset.'
        return RepeatFile(self.iter_lines, lines=self.lines)

    def read(self, n=None):
        '''Returns a string or bytes object. Unlike the standard read() function, when *n* is given, more than *n* characters/bytes can be returned, and often will.'''
        if n is None:
            n = 10**12  # some too huge number
        r = []
        size = 0
        output_type = str; eol = '\n'; joiner = ''
        while not r or size < n:
            try:
                s = next(self.iter)
                if not r and isinstance(s, bytes):
                    output_type = bytes; eol = b'\n'; joiner = b''
                assert isinstance(s, output_type), (s, output_type)
                r.append(s)
                r.append(eol)
                size += len(s) + len(eol)
            except StopIteration:
                break  # end of file
        return joiner.join(r)

    def write(self, s):
        return self.iter_lines.write(s)

    def tell(self):
        '''Tells the current position as an opaque line marker.'''
        return self.iter.nextIndex

    def seek(self, offset, whence=io.SEEK_SET):
        '''Seek to an already seen opaque line position marker only.'''
        if whence != io.SEEK_SET and offset != 0:
            if whence == io.SEEK_CUR:
                raise io.UnsupportedOperation("can't do nonzero cur-relative seeks")
            elif whence == io.SEEK_END:
                raise io.UnsupportedOperation("can't do nonzero end-relative seeks")
            else:
                raise ValueError('invalid whence (%s, should be %s, %s or %s)' % (whence, io.SEEK_SET, io.SEEK_CUR, io.SEEK_END))
        self.iter.nextIndex = offset

    def readline(self, size=-1):
        if size != -1:
            vd.error('RepeatFile does not support limited line length')
        try:
            return next(self.iter)
        except StopIteration:
            return ''

    def __iter__(self):
        return RepeatFileIter(self)

    def __next__(self):
        return next(self.iter)

    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return True

    def read1(self, n=-1):
        return self.read(n)

    def peek(self, n=-1):
        pos = self.tell()
        data = self.read(n)
        self.seek(pos)
        return data

    def exists(self):
        return True


class RepeatFileIter:
    def __init__(self, rf):
        self.rf = rf
        self.nextIndex = 0

    def __iter__(self):
        return RepeatFileIter(self.rf)

    def __next__(self):
        if self.nextIndex < len(self.rf.lines):
            r = self.rf.lines[self.nextIndex]
        elif self.rf.iter_lines:
            try:
                r = next(self.rf.iter_lines)
                self.rf.lines.append(r)
            except StopIteration:
                self.rf.iter_lines = None
                raise
        else:
            raise StopIteration()


        self.nextIndex += 1
        return r


class RuntimePaths:
    '''Unified runtime paths manager for VisiData.

    Centralizes all path resolution, directory creation, permission handling,
    and diagnostics for state files, caches, configs, and temporary files.
    All modules should go through this instead of constructing paths manually.
    '''

    def __init__(self, vd):
        self._vd = vd
        self._dirs = {}
        self._writable_cache = {}
        self._migration_log = []
        self._permission_log = []

    def _resolve_base(self, category):
        '''Resolve the base directory for a given category.'''
        from visidata.vendor.appdirs import (user_config_dir, user_data_dir,
                                             user_cache_dir, user_state_dir)

        overrides = {
            'config': lambda: self._vd.options.visidata_dir if self._vd.options.visidata_dir else None,
        }

        override = overrides.get(category, lambda: None)()
        if override:
            return Path(os.path.expanduser(os.path.expandvars(str(override))))

        resolvers = {
            'config': lambda: user_config_dir('visidata'),
            'data': lambda: user_data_dir('visidata'),
            'cache': lambda: user_cache_dir('visidata'),
            'state': lambda: user_state_dir('visidata'),
            'temp': lambda: tempfile.gettempdir(),
        }

        if category not in resolvers:
            raise ValueError(f'unknown path category: {category}')

        return Path(resolvers[category]())

    def get_dir(self, category, *subpaths, ensure=False, writable=False):
        '''Get a Path for a category directory, optionally with subpaths.

        Args:
            category: one of 'config', 'data', 'cache', 'state', 'temp'
            *subpaths: path components to append
            ensure: if True, create the directory if it doesn't exist
            writable: if True, verify the directory is writable
        '''
        if category == 'temp':
            base = Path(tempfile.mkdtemp(prefix='visidata-'))
        else:
            cache_key = (category, subpaths)
            if cache_key not in self._dirs:
                base = self._resolve_base(category)
                for sub in subpaths:
                    base = base / sub
                self._dirs[cache_key] = base
            base = self._dirs[cache_key]

        if ensure:
            self.ensure_dir(base, writable=writable)

        return base

    def get_file(self, category, filename, *subpaths, ensure_dir=False, writable=False):
        '''Get a Path for a file within a category directory.

        Args:
            category: one of 'config', 'data', 'cache', 'state', 'temp'
            filename: the file name
            *subpaths: intermediate subdirectories
            ensure_dir: if True, create the parent directory
            writable: if True, verify parent directory is writable
        '''
        d = self.get_dir(category, *subpaths, ensure=ensure_dir, writable=writable)
        return d / filename

    def ensure_dir(self, path, writable=False):
        '''Create directory and optionally verify writability.

        Handles permission errors gracefully with diagnostics.
        Returns True if the directory exists and is accessible.
        '''
        if self._vd.options.nothing:
            return False

        p = Path(path) if not isinstance(path, Path) else path

        try:
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            self._permission_log.append((str(p), f'permission denied: {e}'))
            self._vd.warning(f'permission denied creating {p}: {e}')
            return False
        except OSError as e:
            self._permission_log.append((str(p), f'cannot create: {e}'))
            self._vd.warning(f'cannot create directory {p}: {e}')
            return False

        if writable:
            return self._check_writable(p)

        return True

    def _check_writable(self, path):
        '''Check if a directory is writable, caching the result.'''
        cache_key = str(path)
        if cache_key in self._writable_cache:
            return self._writable_cache[cache_key]

        try:
            testfile = os.path.join(str(path), '.visidata-write-test')
            with open(testfile, 'w') as f:
                f.write('')
            os.unlink(testfile)
            result = True
        except (PermissionError, OSError) as e:
            self._permission_log.append((str(path), f'not writable: {e}'))
            self._vd.warning(f'{path} is not writable: {e}')
            result = False

        self._writable_cache[cache_key] = result
        return result

    def resolve_relative(self, relpath, base_category='config'):
        '''Resolve a potentially relative path against a category base.

        If relpath is absolute, return it as-is. Otherwise resolve against
        the base directory for base_category.
        '''
        p = Path(relpath) if not isinstance(relpath, Path) else relpath
        if p.is_absolute():
            return p
        try:
            base = self.get_dir(base_category)
        except Exception as e:
            self._permission_log.append((str(relpath), f'cannot resolve base for {base_category}: {e}'))
            self._vd.warning(f'cannot resolve relative path {relpath}: {e}')
            return p
        return base / p

    def migrate_file(self, old_path, new_category, *new_subpaths, filename=None):
        '''Migrate a file from an old location to the new managed path.

        Returns the new Path if migration happened or file exists, None on failure.
        '''
        old = Path(old_path) if not isinstance(old_path, Path) else old_path
        if not old.exists():
            return None

        fname = filename or old.name
        new = self.get_file(new_category, fname, *new_subpaths, ensure_dir=True, writable=True)

        if new.exists():
            return new

        try:
            self.ensure_dir(new.parent, writable=True)
            shutil.copy2(str(old), str(new))
            self._migration_log.append((str(old), str(new), True, ''))
            self._vd.status(f'migrated {old} to {new}')
            return new
        except (OSError, IOError) as e:
            self._migration_log.append((str(old), str(new), False, str(e)))
            self._vd.warning(f'failed to migrate {old} to {new}: {e}')
            return None

    def diagnose(self):
        '''Return a list of (path, category, ok, note) tuples for all managed paths.'''
        results = []
        for category in ['config', 'data', 'cache', 'state']:
            try:
                p = self.get_dir(category)
                exists = p.exists()
                writable = self._check_writable(p) if exists else False
                note = ''
                if not exists:
                    note = 'directory does not exist'
                elif not writable:
                    note = 'directory not writable'
                results.append((str(p), category, exists and writable, note))
            except Exception as e:
                results.append(('', category, False, str(e)))
        for old, new, ok, note in self._migration_log:
            status = 'migrated' if ok else 'migration failed'
            note = f'{old} -> {new}' + (f': {note}' if note else '')
            results.append((new, 'migration', ok, note))
        for path, note in self._permission_log:
            results.append((path, 'permission', False, note))
        return results

    def config_file(self, name='config.py'):
        '''Get path to a config file, checking standard locations.'''
        xdg_path = self.get_file('config', name)
        if xdg_path.exists():
            return xdg_path

        legacy = Path(os.path.expanduser('~/.visidatarc'))
        if legacy.exists() and name == 'config.py':
            migrated = self.migrate_file(str(legacy), 'config', filename='config.py')
            if migrated:
                self._vd.status(f'migrated legacy ~/.visidatarc to {migrated}')
                return migrated
            return legacy

        return xdg_path

    def temp_file(self, suffix='', prefix='vd-'):
        '''Create and return a Path to a temporary file.'''
        fd, tmppath = tempfile.mkstemp(suffix=suffix, prefix=prefix)
        os.close(fd)
        return Path(tmppath)

    def temp_dir(self, suffix='', prefix='vd-'):
        '''Create and return a Path to a temporary directory.'''
        return Path(tempfile.mkdtemp(suffix=suffix, prefix=prefix))

    @contextlib.contextmanager
    def temp_file_ctx(self, suffix='', prefix='vd-', delete=True):
        '''Context manager yielding a Path to a temporary file, deleted on exit by default.'''
        p = self.temp_file(suffix=suffix, prefix=prefix)
        try:
            yield p
        finally:
            if delete and p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    @contextlib.contextmanager
    def temp_dir_ctx(self, suffix='', prefix='vd-', delete=True):
        '''Context manager yielding a Path to a temporary directory, deleted on exit by default.'''
        p = self.temp_dir(suffix=suffix, prefix=prefix)
        try:
            yield p
        finally:
            if delete and p.exists():
                try:
                    shutil.rmtree(str(p))
                except OSError:
                    pass

    def session_file(self, name='session', ensure_dir=False, writable=False):
        '''Get path to a session state file in the state directory.'''
        return self.get_file('state', f'{name}.json', ensure_dir=ensure_dir, writable=writable)

    def graph_file(self, name='graph', ensure_dir=False, writable=False):
        '''Get path to a graph state file in the state directory.'''
        return self.get_file('state', f'{name}.graph.json', ensure_dir=ensure_dir, writable=writable)


@VisiData.cached_property
def runtime_paths(vd):
    '''Unified RuntimePaths instance.'''
    return RuntimePaths(vd)


@VisiData.api
def getPath(vd, category, *args, **kwargs):
    '''Legacy-compatible path getter; delegates to runtime_paths.'''
    return vd.runtime_paths.get_dir(category, *args, **kwargs)


@VisiData.api
def getFilePath(vd, category, filename, *args, **kwargs):
    '''Legacy-compatible file path getter; delegates to runtime_paths.'''
    return vd.runtime_paths.get_file(category, filename, *args, **kwargs)


@VisiData.api
def ensureDir(vd, path, writable=False):
    '''Legacy-compatible directory creator; delegates to runtime_paths.'''
    return vd.runtime_paths.ensure_dir(path, writable=writable)


@VisiData.api
def diagnosePaths(vd):
    '''Print diagnostics for all managed runtime paths.'''
    for path, category, ok, note in vd.runtime_paths.diagnose():
        status = 'OK' if ok else 'FAIL'
        vd.status(f'{category}: {status} {path} {note}')


@VisiData.api
def sessionStatePath(vd, name='session', writable=False):
    '''Get path to a session state file, optionally ensuring writability.'''
    return vd.runtime_paths.session_file(name, ensure_dir=writable, writable=writable)


@VisiData.api
def graphStatePath(vd, name='graph', writable=False):
    '''Get path to a graph state file, optionally ensuring writability.'''
    return vd.runtime_paths.graph_file(name, ensure_dir=writable, writable=writable)


@VisiData.api
def saveSessionState(vd, state=None, name='session'):
    '''Save session state to the state directory. If state is None, auto-collect
    current sheets and sources. Returns True on success.'''
    import json
    if state is None:
        sources = []
        for s in vd.sheets:
            src = getattr(s, 'source', None)
            if src is None:
                sources.append(None)
            elif isinstance(src, str):
                sources.append(src)
            elif hasattr(src, 'given'):
                sources.append(str(src.given) if src.given else str(src))
            else:
                sources.append(str(src))
        state = {
            'sheets': [s.name for s in vd.sheets],
            'sources': sources,
            'version': getattr(vd, 'version_info', ''),
        }
    p = vd.runtime_paths.session_file(name, ensure_dir=True, writable=True)
    try:
        with open(str(p), 'w') as fp:
            json.dump(state, fp)
        vd.status(f'saved session state to {p}')
        return True
    except (OSError, PermissionError) as e:
        vd.warning(f'failed to save session state to {p}: {e}')
        return False


@VisiData.api
def restoreSessionState(vd, name='session', open_sources=True):
    '''Restore session from state directory. If open_sources=True, attempts to
    reopen saved sources. Returns state dict or None.'''
    import json
    p = vd.runtime_paths.session_file(name)
    if not p.exists():
        return None
    try:
        with open(str(p)) as fp:
            state = json.load(fp)
        if open_sources and isinstance(state, dict) and state.get('sources'):
            opened = 0
            for src in state['sources']:
                if not src:
                    continue
                try:
                    vs = vd.openSource(src)
                    if vs:
                        vd.sync(vs.reload())
                        vd.push(vs)
                        opened += 1
                except Exception as e:
                    vd.warning(f'failed to restore source {src}: {e}')
            if opened:
                vd.status(f'restored {opened}/{len(state["sources"])} sheets from session')
        return state
    except (OSError, PermissionError, json.JSONDecodeError) as e:
        vd.warning(f'failed to restore session state from {p}: {e}')
        return None


@VisiData.api
def saveGraphState(vd, state, name='graph'):
    '''Save graph state dict to the state directory. Returns True on success.'''
    import json
    p = vd.runtime_paths.graph_file(name, ensure_dir=True, writable=True)
    try:
        with open(str(p), 'w') as fp:
            json.dump(state, fp)
        return True
    except (OSError, PermissionError) as e:
        vd.warning(f'failed to save graph state to {p}: {e}')
        return False


@VisiData.api
def restoreGraphState(vd, name='graph'):
    '''Restore graph state dict from the state directory. Returns state or None.'''
    import json
    p = vd.runtime_paths.graph_file(name)
    if not p.exists():
        return None
    try:
        with open(str(p)) as fp:
            state = json.load(fp)
        return state
    except (OSError, PermissionError, json.JSONDecodeError) as e:
        vd.warning(f'failed to restore graph state from {p}: {e}')
        return None


@VisiData.api
def openDiagnosticsSheet(vd):
    '''Open a TextSheet showing all runtime path diagnostics.'''
    from visidata import TextSheet
    lines = []
    lines.append('Runtime Paths Diagnostics')
    lines.append('=' * 60)
    lines.append('')
    for path, category, ok, note in vd.runtime_paths.diagnose():
        status = 'OK' if ok else 'FAIL'
        lines.append(f'[{status == "OK" and "green" or "error"}]{category}: {status}[/{status == "OK" and "green" or "error"}]  {path}')
        if note:
            lines.append(f'  note: {note}')
        lines.append('')
    lines.append('')
    lines.append('Temporary directory:')
    import tempfile
    lines.append(f'  {tempfile.gettempdir()}')
    return TextSheet('runtime_paths_diagnostics', source=lines)


vd.addGlobals(RepeatFile=RepeatFile,
              Path=Path,
              modtime=modtime,
              filesize=filesize,
              vstat=vstat,
              RuntimePaths=RuntimePaths)

BaseSheet.addCommand('', 'diagnose-paths', 'vd.push(vd.openDiagnosticsSheet())', 'show runtime path diagnostics as a sheet')
BaseSheet.addCommand('', 'session-save', 'vd.saveSessionState()', 'save current session (open sheets, sources) to state directory')
BaseSheet.addCommand('', 'session-restore', 'vd.restoreSessionState() or vd.status("no session state found")', 'restore session from state directory, reopen saved sources')
