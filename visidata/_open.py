import os
import os.path
import sys

from visidata import VisiData, vd, Path, BaseSheet, TableSheet, TextSheet, SettableColumn


vd.option('filetype', '', 'specify file type', replay=True)
vd.option('load_profile', '', 'apply named loading profile when opening files (per-source, use --load-profile on CLI)', replay=False)


@VisiData.api
def inputFilename(vd, prompt, *args, **kwargs):
    completer= _completeFilename
    if not vd.couldOverwrite():  #1805 don't suggest an existing file
        completer = None
        v = kwargs.get('value', '')
        if v and Path(v).exists():
            kwargs['value'] = ''
    return vd.input(prompt, type="filename", *args, completer=completer, **kwargs).strip()


@VisiData.api
def inputPath(vd, *args, filetype='', **kwargs):
    'Input a path with filetype field. Sets filetype on the returned Path if given.'
    prompt = args[0] if args else kwargs.pop('prompt', 'path: ')
    completer = _completeFilename
    if not vd.couldOverwrite():  #1805
        completer = None
        v = kwargs.get('value', '')
        if v and Path(v).exists():
            kwargs['value'] = ''
    r = vd.inputMultiple(
        path=dict(prompt=prompt, type='filename', completer=completer, **kwargs),
        filetype=dict(prompt='as filetype: ', type='filetype', value=filetype),
    )
    p = Path(r['path'].strip())
    if r['filetype']:
        p.options.filetype = r['filetype']
    return p


def _completeFilename(val, state):
    i = val.rfind('/')
    if i < 0:  # no /
        base = ''
        partial = val
    elif i == 0: # root /
        base = '/'
        partial = val[1:]
    else:
        base = val[:i]
        partial = val[i+1:]

    files = []
    for f in os.listdir(Path(base or '.')):
        if f.startswith(partial):
            files.append(os.path.join(base, f))

    files.sort()
    return files[state%len(files)]


@VisiData.api
def guessFiletype(vd, p, *args, funcprefix='guess_'):
    '''Call all vd.guess_<filetype>(p) functions and return best candidate sheet based on file contents.'''

    guessfuncs = [getattr(vd, x) for x in dir(vd) if x.startswith(funcprefix)]
    filetypes = []
    for f in guessfuncs:
        try:
            filetype = f(p, *args)
            if filetype:
                filetype['_guesser'] = f.__name__
                filetypes.append(filetype)
        except FileNotFoundError:
            pass
        except Exception as e:
            vd.debug(f'{f.__name__}: {e}')

    if filetypes:
        return sorted(filetypes, key=lambda r: -r.get('_likelihood', 1))[0]

    return {}


@VisiData.api
def guess_extension(vd, path):
    # try auto-detect from extension
    ext = path.suffix[1:].lower()
    openfunc = getattr(vd, f'open_{ext}', vd.getGlobals().get(f'open_{ext}'))
    if openfunc:
        return dict(filetype=ext, _likelihood=3)


def _attach_profile(vs, profile_name):
    if vs and profile_name:
        vs._applied_profile = profile_name
    return vs


@VisiData.api
def openPath(vd, p, filetype=None, create=False, profile=None):
    '''Call ``open_<filetype>(p)`` or ``openurl_<p.scheme>(p, filetype)``.  Return constructed but unloaded sheet of appropriate type.
    If True, *create* will return a new, blank **Sheet** if file does not exist.'''

    profile_name, was_interactive = vd.resolveProfileForPath(p, explicit_profile=profile)
    applied_profile = None
    if profile_name:
        applied_profile = vd.applyProfile(profile_name, p)
        p._applied_profile = profile_name

    filetype = filetype or p.options.filetype  # resolve from path instance, Path class, global  #1710

    if p.scheme and not p.has_fp():
        schemes = p.scheme.split('+')
        openfuncname = 'openurl_' + schemes[-1]

        openfunc = getattr(vd, openfuncname, None) or vd.getGlobals().get(openfuncname, None)
        if not openfunc:
            vd.fail(f'no loader for url scheme: {p.scheme}')

        return _attach_profile(openfunc(p, filetype=filetype), profile_name)

    if not p.exists() and not create:
        return None

    # assign filetype from extension, but only for files, not directories
    if not p.is_dir():  #2547
        filetype = filetype or p.ext

    filetype = filetype.lower()

    # store resolved filetype on path for downstream access (e.g. self.source.options.filetype)
    p.options.set('filetype', filetype, p, cmdlog=False)

    if not p.exists():
        newfunc = getattr(vd, 'new_' + filetype, vd.getGlobals().get('new_' + filetype))
        if not newfunc:
            vd.warning('%s does not exist, creating new sheet' % p)
            return _attach_profile(vd.newSheet(p.base_stem, 1, source=p), profile_name)

        vd.status('creating blank %s' % (p.given))
        return _attach_profile(newfunc(p), profile_name)

    if p.is_fifo():
        # read the file as text, into a RepeatFile that can be opened multiple times
        p = Path(p.given, fp=p.open(mode='rb'))

    openfuncname = 'open_' + filetype
    openfunc = getattr(vd, openfuncname, vd.getGlobals().get(openfuncname))
    if not openfunc:
        opts = vd.guessFiletype(p)
        if opts and 'filetype' in opts:
            filetype = opts['filetype']
            openfuncname = 'open_' + filetype
            openfunc = getattr(vd, openfuncname, vd.getGlobals().get(openfuncname))
            if not openfunc:
                vd.error(f'guessed {filetype} but no {openfuncname}')

            vs = openfunc(p)
            for k, v in opts.items():
                if k != 'filetype' and not k.startswith('_'):
                    setattr(vs.options, k, v)
            vd.status(f'guessed `{opts["filetype"]}` filetype based on contents')
            return _attach_profile(vs, profile_name)

        vd.warning(f'unknown `{filetype}` filetype')

        filetype = 'txt'
        openfunc = vd.open_txt

    vd.status('opening %s as %s' % (p.given, filetype))

    return _attach_profile(openfunc(p), profile_name)

@VisiData.api
def openSource(vd, p, filetype=None, create=False, profile=None, **kwargs):
    '''Return unloaded sheet object for *p* opened as the given *filetype* and with *kwargs* as option overrides. *p* can be a Path or a string (filename, url, or "-" for stdin).
    when true, *create* will return a blank sheet, if file does not exist.'''

    if isinstance(p, BaseSheet):
        return p

    load_profile = kwargs.pop('load_profile', None)
    if load_profile and not profile:
        profile = load_profile

    vs = None
    if isinstance(p, str):
        if '://' in p:
            vs = vd.openPath(Path(p), filetype=filetype, profile=profile)  # convert to Path and recurse
        elif p == '-':
            if vd.stdinSource.fptext.isatty():
                vd.fail('cannot open stdin when it is a tty')
            vs = vd.openPath(vd.stdinSource, filetype=filetype, profile=profile)
        else:
            vs = vd.openPath(Path(p), filetype=filetype, create=create, profile=profile)  # convert to Path and recurse
    else:
        vs = vd.openPath(p, filetype=filetype, create=create, profile=profile)

    for optname, optval in kwargs.items():
        vs.options[optname] = optval
        # Path is authoritative for format options  #2727
        if isinstance(vs.source, Path):
            vs.source.options.set(optname, optval, vs.source, cmdlog=False)

    return vs


#### enable external addons
@VisiData.api
def open_txt(vd, p):
    'Create sheet from `.txt` file at Path `p`, checking whether it is TSV.'
    if p.exists(): #1611
        with p.open(encoding=vd.options.encoding) as fp:
            delimiter = vd.options.delimiter
            try:
                if delimiter and delimiter in next(fp):    # peek at the first line
                    return vd.open_tsv(p)  # TSV often have .txt extension
            except StopIteration:
                return vd.newSheet(p.base_stem, 1, source=p)
    return TextSheet(p.base_stem, source=p)


@VisiData.api
def _get_replay_profile(vd):
    '''Return the profile name from the current replay row, or None.

    Reads from the dedicated ``profile`` field first (new format).
    Falls back to the ``col`` field for backward compatibility with
    older cmdlogs/VDX files that stored the profile name in the col field.
    '''
    r = getattr(vd, 'currentReplayRow', None)
    if r and getattr(r, 'longname', None) in ('open-file', 'open-file-with-profile'):
        prof = getattr(r, 'profile', None)
        if prof:
            return prof
        colprof = getattr(r, 'col', None)
        if colprof:
            return colprof
    return None


BaseSheet.addCommand('o', 'open-file', '''
p = inputFilename("open: ")
prof = vd._get_replay_profile()
vd.push(openSource(p, create=True, profile=prof))
''', 'Open file or URL')
TableSheet.addCommand('zo', 'open-cell-file', 'cd=cursorDisplay; (vd.push(openSource(cd) if cd else fail("no path given")) or fail(f"file {cd} does not exist"))', 'Open file or URL from path in current cell')
BaseSheet.addCommand('gU', 'undo-last-quit', 'push(allSheets[-1])', 'reopen most recently closed sheet')

vd.addMenuItems('''
    File > Open > input file/url > open-file
    File > Reopen last closed > undo-last-quit
''')
