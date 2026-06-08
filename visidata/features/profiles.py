"""Data source loading profiles.

Save named sets of loading options (filetype, encoding, delimiter, header,
compression, etc.) and apply them when opening matching files or URLs.

Profile Storage Format (profiles.json):
----------------------------------------
Profiles are persisted as JSON, by default at ``$VD_DIR/profiles.json``
(or ``$XDG_DATA_HOME/visidata/profiles.json``).  The file is a dict mapping
profile names to profile objects:

    {
      "semicolon-csv": {
        "name": "semicolon-csv",
        "description": "European CSV with ; separator",
        "path_pattern": "*.csv",
        "options": {
          "csv_delimiter": ";",
          "encoding": "latin-1",
          "header": 1
        },
        "created_at": "2025-01-15T10:30:00",
        "updated_at": "2025-01-15T10:30:00"
      }
    }

Each profile has:
  * ``name``         – unique identifier (string)
  * ``description``  – free-text description (string, optional)
  * ``path_pattern`` – fnmatch glob or substring matched against the given
                       path (string, optional).  Example: ``"*.csv"`` or
                       ``"financial_reports"``.
  * ``options``      – dict of option-name → value.  Only non-default
                       loading-format options are stored.  Recognised keys
                       are listed in ``REPLAYABLE_LOAD_OPTS``.
  * ``created_at`` / ``updated_at`` – ISO-8601 timestamps (auto-managed).

Ways to Use Profiles (all per-source, no global residue):
----------------------------------------------------------
1. **Command line** – apply a profile when opening files.  The profile is
   attached to each input source independently; opening a second file
   without ``--load-profile`` will NOT inherit the first file's profile::

       vd --load-profile=semicolon-csv data.csv
       vd --load-profile=utf16-tsv report.txt --batch -o cleaned.tsv

2. **Interactive** – save, apply, list, delete via long commands or menu:

       save-profile        – save current sheet's loading options as a profile
       apply-profile       – apply a saved profile to the current sheet
       open-file-with-profile  – pick a file and then pick a profile to apply
       open-profile        – open the profile browser sheet
       delete-profile      – remove a saved profile

   Menu path:  ``File > Options > profiles > …``
               ``File > Open > with profile > …``

3. **Auto-prompt** – when ``options.profiles_auto_prompt`` is True (the
   default) and you open a file whose path matches any saved
   ``path_pattern``, VisiData prompts you to choose one of the matching
   profiles.  This is automatically disabled in batch and replay modes.

4. **Macros / VDX replay** – every file opened with a profile records the
   profile name directly in the ``open-file`` cmdlog row (in the ``col``
   field).  On replay, the profile is extracted from that row and applied
   **only to that specific open** – it does not leak to subsequent opens.
   Minimal VDX format:

         col semicolon
         open-file data.csv

   The VDX loader also supports these standalone commands (recorded via
   the respective interactive commands):

         apply-profile myprofile
         save-profile newprof
         delete-profile oldprof

5. **Config file** – ``~/.visidatarc`` supports ``option`` statements for
   one-off configuration, but for reproducible loads prefer putting the
   profile JSON directly into ``$VD_DIR/profiles.json`` and referencing it
   per-source.

Unified Resolution Order
------------------------
Every call to ``openSource`` / ``openPath`` resolves the profile through
``vd.resolveProfileForPath``.  The profile is always attached per-source –
the cmdlog ``openHook`` never writes a sticky global option, so no residue
leaks between recorded opens:

  1. Explicit ``profile=`` keyword argument passed by the caller
     (used by ``open-file`` replay, ``open-file-with-profile``, and the
     CLI ``--load-profile`` handler in ``openSource``).
  2. ``path.options.load_profile`` (set per-path before opening; used by
     VDX ``option scope load_profile name`` when scoped to a specific
     source).
  3. ``vd.options.load_profile`` (fallback for explicitly-set global
     options via VDX ``option global load_profile X`` or
     ``~/.visidatarc``).  Crucially, this global is NEVER written
     automatically by ``openHook`` – it only takes effect when the user
     sets it deliberately.
  4. (interactive only) Auto-prompt if matching profiles exist and
     ``profiles_auto_prompt`` is enabled and neither batch nor replay is
     active.
"""

import fnmatch
import json
import os
from datetime import datetime

from visidata import vd, VisiData, BaseSheet, Sheet, Column, ColumnAttr, ItemColumn, AttrDict, Path, TableSheet, CompleteKey


vd.option('profiles_auto_prompt', True, 'prompt to apply matching profiles when opening files', replay=True)


def _choose_name(prompt, names):
    '''Prompt user to choose one name from a list of strings; return None on cancel.'''
    if vd.cmdlog:
        v = vd.getLastArgs()
        if v is not None:
            vd.setLastArgs(v)
            return v
    choice = vd.input(prompt, completer=CompleteKey(names))
    if choice and choice in names:
        return choice
    return None


REPLAYABLE_LOAD_OPTS = [
    'filetype',
    'encoding',
    'encoding_errors',
    'delimiter',
    'header',
    'skip',
    'compression',
    'csv_dialect',
    'csv_delimiter',
    'csv_doublequote',
    'csv_quotechar',
    'csv_quoting',
    'csv_skipinitialspace',
    'csv_escapechar',
    'csv_lineterminator',
    'json_indent',
    'json_sort_keys',
    'json_ensure_ascii',
    'default_colname',
    'xlsx_meta_columns',
    'xlsx_color_cells',
    'xlsx_sheet',
]


def _now_iso():
    return datetime.now().isoformat(timespec='seconds')


vd.option('profiles_file', '', 'path to profiles.json file (default: $VD_DIR/profiles.json or $XDG_DATA_HOME/visidata/profiles.json)', sheettype=None)


@VisiData.property
def profiles_path(vd):
    if vd.options.profiles_file:
        return Path(vd.options.profiles_file)
    if vd.options.visidata_dir:
        p = Path(vd.options.visidata_dir) / 'profiles.json'
        return p
    return vd.data_dir / 'profiles.json'


@VisiData.api
def loadProfiles(vd):
    p = vd.profiles_path
    if not p.exists():
        return {}
    try:
        with p.open(encoding='utf-8') as fp:
            data = json.load(fp)
            if isinstance(data, list):
                return {pr['name']: AttrDict(pr) for pr in data}
            elif isinstance(data, dict):
                return {k: AttrDict(v) for k, v in data.items()}
    except Exception as e:
        vd.warning(f'failed to load profiles: {e}')
    return {}


@VisiData.api
def saveProfiles(vd, profiles):
    p = vd.profiles_path
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open(mode='w', encoding='utf-8') as fp:
            json.dump({k: dict(v) for k, v in profiles.items()}, fp, indent=2, sort_keys=True)
    except Exception as e:
        vd.fail(f'failed to save profiles: {e}')


@VisiData.cached_property
def _profiles_store(vd):
    return vd.loadProfiles()


@VisiData.api
def getProfiles(vd):
    return vd._profiles_store


@VisiData.api
def saveProfile(vd, name, source=None, description='', path_pattern=None):
    profiles = vd.getProfiles()
    if not name:
        vd.fail('profile name is required')

    opts = {}
    obj = None
    if isinstance(source, Path):
        obj = source
    elif isinstance(source, BaseSheet) and source.source:
        if isinstance(source.source, Path):
            obj = source.source
        else:
            obj = source

    if obj:
        for optname in REPLAYABLE_LOAD_OPTS:
            if vd.options._get(optname):
                val = obj.options.getonly(optname, obj, None)
                if val is not None and val != vd.options.getdefault(optname):
                    opts[optname] = val

    if source is None:
        for optname in REPLAYABLE_LOAD_OPTS:
            if vd.options._get(optname):
                val = vd.options.getonly(optname, 'global', None)
                if val is not None and val != vd.options.getdefault(optname):
                    opts[optname] = val

    if not opts:
        vd.warning('no non-default loading options detected to save')

    if not path_pattern and isinstance(source, Path):
        path_pattern = source.given
    elif not path_pattern and isinstance(source, BaseSheet) and isinstance(source.source, Path):
        path_pattern = source.source.given

    profile = AttrDict(
        name=name,
        description=description,
        path_pattern=path_pattern or '',
        options=opts,
        created_at=profiles.get(name, {}).get('created_at', _now_iso()),
        updated_at=_now_iso(),
    )
    profiles[name] = profile
    vd.saveProfiles(profiles)
    vd.status(f'saved profile `{name}` with {len(opts)} option(s)')
    return profile


@VisiData.api
def deleteProfile(vd, name):
    profiles = vd.getProfiles()
    if name not in profiles:
        vd.fail(f'no profile named `{name}`')
    del profiles[name]
    vd.saveProfiles(profiles)
    vd.status(f'deleted profile `{name}`')


@VisiData.api
def resolveProfileForPath(vd, p, explicit_profile=None):
    '''Unified per-source profile resolution.

    Resolution order:
    1. explicit_profile kwarg (from openSource/openPath call site – used by
       replay, open-file-with-profile, and CLI --load-profile)
    2. path.options.load_profile (set per-path before opening)
    3. vd.options.load_profile (fallback for explicitly-set global options
       via VDX ``option global load_profile X`` or ``.visidatarc`` – note
       this is NEVER recorded automatically by openHook, so no residue
       leaks between cmdlog-recorded opens)
    4. (interactive only) auto-prompt if matching profiles exist and
       profiles_auto_prompt=True, batch=False, no replay active

    Returns (profile_name_or_None, was_interactive).
    '''
    profile_name = explicit_profile or ''

    if not profile_name and isinstance(p, Path):
        profile_name = p.options.getonly('load_profile', p, '') or ''

    if not profile_name:
        profile_name = vd.options.getonly('load_profile', 'global', '') or ''

    was_interactive = False
    if not profile_name:
        is_interactive = (not vd.options.batch
                          and vd.currentReplay is None
                          and getattr(vd, '_cmdlogReplaying', None) is None
                          and vd.options.get('profiles_auto_prompt', True)
                          and p.given not in ('', '-'))

        if is_interactive:
            matches = vd.getMatchingProfiles(p)
            if matches:
                names = [n for n, _ in matches]
                names.append('(none)')
                choice = _choose_name(f'{len(matches)} matching profile(s) found; apply which? ', names)
                if choice and choice != '(none)':
                    profile_name = choice
                    was_interactive = True

    return (profile_name if profile_name else None, was_interactive)


@VisiData.api
def applyProfile(vd, name, target=None):
    '''Apply the named profile's options to *target* (Path, BaseSheet, or vd.activeSheet).
    Sets ``target._applied_profile`` so the profile name is recorded by the cmdlog
    ``openHook`` when the sheet is pushed.'''
    profiles = vd.getProfiles()
    if name not in profiles:
        vd.fail(f'no profile named `{name}`')

    profile = profiles[name]
    opts = profile.get('options', {})

    if target is None:
        target = vd.activeSheet

    for optname, optval in opts.items():
        if vd.options._get(optname):
            if isinstance(target, Path):
                target.options.set(optname, optval, target, cmdlog=False)
            elif isinstance(target, BaseSheet):
                target.options.set(optname, optval, target)
                if isinstance(target.source, Path):
                    target.source.options.set(optname, optval, target.source, cmdlog=False)

    if isinstance(target, (Path, BaseSheet)):
        target._applied_profile = name

    if isinstance(target, BaseSheet) and opts:
        vd.status(f'applied profile `{name}` ({len(opts)} option(s)); reload to take effect')
    elif opts:
        vd.status(f'applied profile `{name}` ({len(opts)} option(s))')

    return profile


@VisiData.api
def getMatchingProfiles(vd, path):
    profiles = vd.getProfiles()
    matches = []
    path_str = str(path.given) if isinstance(path, Path) else str(path)
    for name, profile in profiles.items():
        pattern = profile.get('path_pattern', '')
        if not pattern:
            continue
        if fnmatch.fnmatch(path_str, pattern) or pattern in path_str:
            matches.append((name, profile))
    return matches


@VisiData.api
def chooseProfile(vd, path=None, prompt='choose profile: '):
    profiles = vd.getProfiles()
    if not profiles:
        vd.fail('no profiles saved')
    names = sorted(profiles.keys())
    if path:
        matching = [n for n, _ in vd.getMatchingProfiles(path)]
        if matching:
            names = matching + [n for n in names if n not in matching]
    name = _choose_name(prompt, names)
    if not name:
        return None
    return profiles.get(name)


# rowdef: AttrDict profile
class ProfilesSheet(Sheet):
    'List of saved loading profiles.'
    rowtype = 'profiles'
    columns = [
        ColumnAttr('name'),
        ColumnAttr('description'),
        ColumnAttr('path_pattern', width=30),
        Column('options_count', type=int, getter=lambda c,r: len(r.get('options', {}))),
        ColumnAttr('updated_at', width=19),
    ]
    nKeys = 1

    def iterload(self):
        for name, profile in sorted(vd.getProfiles().items()):
            yield profile

    def reload(self):
        self.rows = list(self.iterload())

    def openRow(self, row):
        return ProfileDetailSheet(row.name, source=row)


# rowdef: (optname, optval)
class ProfileDetailSheet(Sheet):
    'Detail view of a single loading profile.'
    rowtype = 'options'
    columns = [
        ItemColumn('option', 0),
        ItemColumn('value', 1),
    ]
    nKeys = 1

    @property
    def profile_name(self):
        return self.names[0]

    def iterload(self):
        profiles = vd.getProfiles()
        profile = profiles.get(self.profile_name)
        if not profile:
            return
        self._profile = profile
        for optname, optval in sorted(profile.get('options', {}).items()):
            yield [optname, repr(optval)]

    def reload(self):
        self.rows = list(self.iterload())


@VisiData.api
def profilesSheet(vd):
    return ProfilesSheet('profiles')


@BaseSheet.api
def _source_path_or_sheet(sheet):
    if isinstance(sheet.source, Path):
        return sheet.source
    return sheet


@Sheet.api
def save_profile_cmd(sheet, name, description='', path_pattern=''):
    vd.saveProfile(name, sheet._source_path_or_sheet(), description, path_pattern)


@Sheet.api
def apply_profile_cmd(sheet, name):
    vd.applyProfile(name, sheet)


@Sheet.api
def delete_profile_cmd(sheet, name):
    vd.deleteProfile(name)


BaseSheet.addCommand('', 'save-profile',
    'sheet.save_profile_cmd(input("save profile as: "), input("description (optional): ", value=""), input("path pattern (glob, optional): ", value=""))',
    'save current loading options as a named profile')

BaseSheet.addCommand('', 'apply-profile',
    'sheet.apply_profile_cmd(vd.getLastArgs() or vd.chooseProfile().name)',
    'apply a saved profile to the current sheet')

BaseSheet.addCommand('', 'open-profile',
    'vd.push(vd.profilesSheet())',
    'open the profiles sheet with all saved loading profiles')

BaseSheet.addCommand('', 'open-file-with-profile',
    'p=Path(inputFilename("open with profile: ")); prof=vd.chooseProfile(p, "choose profile for this file: "); vd.push(openSource(p, create=True, profile=prof.name if prof else None))',
    'open a file and apply a profile before loading')

BaseSheet.addCommand('', 'delete-profile',
    'sheet.delete_profile_cmd(vd.getLastArgs() or vd.chooseProfile().name)',
    'delete a saved profile')

vd.addMenuItems('''
    File > Open > with profile > open-file-with-profile
    File > Options > profiles > save current as profile > save-profile
    File > Options > profiles > apply to sheet > apply-profile
    File > Options > profiles > list all > open-profile
    File > Options > profiles > delete > delete-profile
''')

vd.addGlobals(
    ProfilesSheet=ProfilesSheet,
    ProfileDetailSheet=ProfileDetailSheet,
)
