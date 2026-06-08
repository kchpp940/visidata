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
        "filetype": "csv",
        "sheet_name": "",
        "match_rules": {
          "source_kind": "local",
          "filetype": "csv",
          "compression": null,
          "innerpath": null,
          "url_host": null,
          "url_scheme": null,
          "schema_digest": "sha256:abcdef1234..."
        },
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
  * ``filetype``     – explicit filetype override (string, optional).
                       If set, forces the loader to use this filetype.
  * ``sheet_name``   – sheet/table selector for multi-sheet sources like
                       Excel workbooks (string, optional).
  * ``match_rules``  – dict of fine-grained compatibility checks.  When
                       opening a source, every non-null field in
                       ``match_rules`` must agree with the source; if any
                       field explicitly contradicts, the profile is treated
                       as ``mismatch`` and is never auto-applied.  Fields:
                         - ``source_kind`` – one of ``local``, ``url``,
                           ``stdin``, ``archive_member``, ``memory``
                         - ``filetype``    – detected or explicit filetype
                         - ``compression`` – ``gz``/``bz2``/``xz``/``lzma``/
                           ``zst`` or ``null``
                         - ``innerpath``   – member path inside an archive
                         - ``url_host``    – hostname for URL sources
                         - ``url_scheme``  – ``http``/``https``/``s3``/…
                         - ``schema_digest`` – SHA-256 of sorted column
                           names (after a successful load).  When present
                           the profile will only auto-match sources whose
                           post-load column set matches.
  * ``options``      – dict of option-name → value.  Only non-default
                       loading-format options are stored.  Recognised keys
                       are listed in ``REPLAYABLE_LOAD_OPTS``.
  * ``created_at`` / ``updated_at`` – ISO-8601 timestamps (auto-managed).

Compatibility / Match Levels
----------------------------
``vd.profileMatchLevel(profile, source)`` returns one of four levels:

  * ``exact``       – all non-null ``match_rules`` fields agree, and at
                      least ``schema_digest`` or ``innerpath``+``url_host``
                      matches.  Safe to apply silently.
  * ``strong``      – ``path_pattern`` + ``filetype`` + ``compression`` all
                      agree, and no ``match_rules`` field contradicts.
                      Safe to apply silently when auto-prompt is disabled
                      or only one strong match exists.
  * ``weak``        – only ``path_pattern`` or a subset of fields match.
                      The user is always asked; the profile is *never*
                      applied silently.
  * ``mismatch``    – at least one non-null ``match_rules`` field
                      contradicts the source.  Not offered as a match.

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
   default) and you open a file with ``strong`` or ``exact`` matching
   profiles, VisiData prompts you to choose one.  Only ``exact`` matches
   are ever applied silently (and only when a single exact match exists
   and the user has confirmed once).

4. **Macros / VDX replay** – every file opened with a profile records the
   profile name directly in the ``open-file`` cmdlog row in the dedicated
   ``profile`` field.  On replay, the profile is extracted from that row
   and applied **only to that specific open** – it does not leak to
   subsequent opens.  Minimal VDX format:

         profile semicolon
         open-file data.csv

   The VDX loader also supports these standalone commands (recorded via
   the respective interactive commands):

         apply-profile myprofile
         save-profile newprof
         delete-profile oldprof

   Backward compatibility: older cmdlogs/VDX files that stored the profile
   name in the ``col`` field are still recognised during replay.

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
  4. (interactive only) Auto-prompt if ``exact`` or ``strong`` matching
     profiles exist and ``profiles_auto_prompt`` is enabled and neither
     batch nor replay is active.  ``weak`` matches only appear in the
     menu, never as a default.
"""

import fnmatch
import hashlib
import json
import os
from datetime import datetime
from urllib.parse import urlparse

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


MATCH_RULE_FIELDS = [
    'source_kind', 'filetype', 'compression',
    'innerpath', 'url_host', 'url_scheme', 'schema_digest',
]

SOURCE_KINDS = ('local', 'url', 'stdin', 'archive_member', 'memory')


def _empty_match_rules():
    return {k: None for k in MATCH_RULE_FIELDS}


@VisiData.api
def detectSourceKind(vd, source):
    if isinstance(source, BaseSheet) and isinstance(source.source, Path):
        p = source.source
    elif isinstance(source, Path):
        p = source
    else:
        return 'memory'

    if p.given == '-':
        return 'stdin'
    if p.has_fp():
        return 'archive_member'
    if p.is_url():
        return 'url'
    return 'local'


@VisiData.api
def extractSourceAttrs(vd, source):
    attrs = _empty_match_rules()

    if isinstance(source, BaseSheet):
        srcsheet = source
        if isinstance(source.source, Path):
            p = source.source
        else:
            p = None
    elif isinstance(source, Path):
        p = source
        srcsheet = None
    else:
        p = None
        srcsheet = None

    attrs['source_kind'] = vd.detectSourceKind(source)

    if p is not None:
        if p.compression:
            attrs['compression'] = p.compression
        if p.is_url():
            parsed = urlparse(p.given)
            attrs['url_host'] = parsed.hostname or None
            attrs['url_scheme'] = parsed.scheme or None

        ft = p.options.getonly('filetype', p, None)
        if ft:
            attrs['filetype'] = ft
        elif p.ext:
            attrs['filetype'] = p.ext

    elif srcsheet is not None:
        ft = srcsheet.options.getonly('filetype', srcsheet, None)
        if ft:
            attrs['filetype'] = ft

    if isinstance(source, BaseSheet) and source.source is not None:
        if isinstance(source.source, Path) and source.source.has_fp():
            attrs['innerpath'] = source.source.given

    if isinstance(source, BaseSheet):
        try:
            if hasattr(source, 'columns') and source.columns:
                attrs['schema_digest'] = vd.computeSchemaDigest(source)
        except Exception:
            pass

    return attrs


@VisiData.api
def computeSchemaDigest(vd, sheet):
    colnames = []
    for c in getattr(sheet, 'columns', []) or []:
        n = getattr(c, 'name', None)
        if n is not None:
            colnames.append(str(n))
    if not colnames:
        return None
    colnames.sort()
    h = hashlib.sha256()
    for n in colnames:
        h.update(n.encode('utf-8'))
        h.update(b'\x00')
    return 'sha256:' + h.hexdigest()[:16]


def _norm(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _rule_agrees(profile_val, source_val):
    pv = _norm(profile_val)
    sv = _norm(source_val)
    if pv is None:
        return True
    if sv is None:
        return False
    return pv == sv


@VisiData.api
def profileMatchLevel(vd, profile, source):
    rules = profile.get('match_rules') or {}
    if not rules:
        rules = _empty_match_rules()

    source_attrs = vd.extractSourceAttrs(source)

    for field in MATCH_RULE_FIELDS:
        if not _rule_agrees(rules.get(field), source_attrs.get(field)):
            return 'mismatch'

    has_nonnull_rules = any(_norm(rules.get(f)) is not None for f in MATCH_RULE_FIELDS)

    exact_markers = 0
    if _norm(rules.get('schema_digest')) and _rule_agrees(rules.get('schema_digest'), source_attrs.get('schema_digest')):
        exact_markers += 1
    if _norm(rules.get('innerpath')) and _rule_agrees(rules.get('innerpath'), source_attrs.get('innerpath')):
        exact_markers += 1
    if _norm(rules.get('url_host')) and _rule_agrees(rules.get('url_host'), source_attrs.get('url_host')):
        exact_markers += 1

    if exact_markers >= 1 and has_nonnull_rules:
        return 'exact'

    pattern = profile.get('path_pattern', '')
    path_str = ''
    p = None
    if isinstance(source, Path):
        p = source
    elif isinstance(source, BaseSheet) and isinstance(source.source, Path):
        p = source.source
    if p is not None:
        path_str = str(p.given)

    pattern_ok = bool(pattern) and (fnmatch.fnmatch(path_str, pattern) or pattern in path_str)
    ft_ok = _rule_agrees(rules.get('filetype'), source_attrs.get('filetype'))
    comp_ok = _rule_agrees(rules.get('compression'), source_attrs.get('compression'))

    if pattern_ok and ft_ok and comp_ok:
        return 'strong'

    if pattern_ok:
        return 'weak'

    return 'weak' if has_nonnull_rules else 'mismatch'


MATCH_LEVEL_SCORE = {'exact': 4, 'strong': 3, 'weak': 2, 'mismatch': 0}


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
            out = {}
            if isinstance(data, list):
                items = [(pr.get('name', ''), pr) for pr in data]
            elif isinstance(data, dict):
                items = list(data.items())
            else:
                return {}
            for k, v in items:
                pr = AttrDict(v)
                if 'match_rules' not in pr:
                    pr['match_rules'] = _empty_match_rules()
                else:
                    mr = _empty_match_rules()
                    for field in MATCH_RULE_FIELDS:
                        if field in pr['match_rules']:
                            mr[field] = pr['match_rules'][field]
                    pr['match_rules'] = mr
                out[k] = pr
            return out
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
    captured_filetype = ''
    captured_sheet_name = ''
    match_rules = _empty_match_rules()

    if isinstance(source, Path):
        obj = source
    elif isinstance(source, BaseSheet) and source.source:
        if isinstance(source.source, Path):
            obj = source.source
        else:
            obj = source

    if isinstance(source, BaseSheet):
        captured_sheet_name = getattr(source, 'name', '') or ''
        if obj:
            sheet_opt = obj.options.getonly('xlsx_sheet', obj, None)
            if sheet_opt:
                captured_sheet_name = str(sheet_opt)

    if obj:
        for optname in REPLAYABLE_LOAD_OPTS:
            if vd.options._get(optname):
                val = obj.options.getonly(optname, obj, None)
                if val is not None and val != vd.options.getdefault(optname):
                    opts[optname] = val

        ft = obj.options.getonly('filetype', obj, None)
        if ft and ft != vd.options.getdefault('filetype'):
            captured_filetype = str(ft)

    if source is None:
        for optname in REPLAYABLE_LOAD_OPTS:
            if vd.options._get(optname):
                val = vd.options.getonly(optname, 'global', None)
                if val is not None and val != vd.options.getdefault(optname):
                    opts[optname] = val

    if not opts:
        vd.warning('no non-default loading options detected to save')

    if source is not None:
        extracted = vd.extractSourceAttrs(source)
        for k in MATCH_RULE_FIELDS:
            v = extracted.get(k)
            if v not in (None, ''):
                match_rules[k] = v

    if not path_pattern:
        if isinstance(source, Path):
            ext = source.suffix
            if ext:
                path_pattern = '*' + ext
            else:
                path_pattern = source.given
        elif isinstance(source, BaseSheet) and isinstance(source.source, Path):
            ext = source.source.suffix
            if ext:
                path_pattern = '*' + ext
            else:
                path_pattern = source.source.given

    old = profiles.get(name, {})
    if isinstance(old, dict) and old.get('match_rules'):
        for k, v in old['match_rules'].items():
            if k in match_rules and match_rules[k] in (None, '') and v not in (None, ''):
                match_rules[k] = v

    profile = AttrDict(
        name=name,
        description=description,
        path_pattern=path_pattern or '',
        filetype=captured_filetype,
        sheet_name=captured_sheet_name,
        match_rules=match_rules,
        options=opts,
        created_at=old.get('created_at', _now_iso()),
        updated_at=_now_iso(),
    )
    profiles[name] = profile
    vd.saveProfiles(profiles)
    parts = [f'{len(opts)} option(s)']
    if captured_filetype:
        parts.append(f'filetype={captured_filetype}')
    if captured_sheet_name:
        parts.append(f'sheet={captured_sheet_name}')
    mr_info = [f'{k}={v}' for k, v in match_rules.items() if v not in (None, '')]
    if mr_info:
        parts.append('match={' + ','.join(mr_info) + '}')
    vd.status(f'saved profile `{name}` with {", ".join(parts)}')
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
    4. (interactive only) auto-prompt if exact/strong matching profiles
       exist and profiles_auto_prompt=True, batch=False, no replay active.
       Weak matches are offered as choices but never applied silently.

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
            matches = vd.getMatchingProfiles(p, include_weak=True)
            exact_strong = [(n, pr, lvl) for n, pr, lvl in matches if lvl in ('exact', 'strong')]
            weak = [(n, pr, lvl) for n, pr, lvl in matches if lvl == 'weak']

            if exact_strong or weak:
                names = [n for n, _, _ in exact_strong] + [n for n, _, _ in weak]
                if len(exact_strong) == 1 and not weak and False:
                    profile_name = exact_strong[0][0]
                    was_interactive = False
                else:
                    names.append('(none)')
                    if exact_strong:
                        prompt = f'{len(exact_strong)} strong/exact + {len(weak)} weak matching profile(s); apply which? '
                    else:
                        prompt = f'{len(weak)} weak matching profile(s) (may be incompatible); apply which? '
                    choice = _choose_name(prompt, names)
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
def getMatchingProfiles(vd, source, include_weak=True):
    '''Return list of (name, profile, match_level) sorted by match strength
    (exact > strong > weak).  *mismatch* profiles are never returned.

    When *include_weak* is False only exact/strong matches are returned.'''
    profiles = vd.getProfiles()
    scored = []
    for name, profile in profiles.items():
        lvl = vd.profileMatchLevel(profile, source)
        if lvl == 'mismatch':
            continue
        if not include_weak and lvl == 'weak':
            continue
        scored.append((MATCH_LEVEL_SCORE[lvl], name, profile, lvl))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [(n, pr, lvl) for _, n, pr, lvl in scored]


@VisiData.api
def chooseProfile(vd, path=None, prompt='choose profile: '):
    profiles = vd.getProfiles()
    if not profiles:
        vd.fail('no profiles saved')
    names = sorted(profiles.keys())
    if path:
        matching = [n for n, _, _ in vd.getMatchingProfiles(path, include_weak=True)]
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

    @staticmethod
    def _match_summary(row):
        mr = row.get('match_rules') or {}
        parts = []
        for k in MATCH_RULE_FIELDS:
            v = mr.get(k)
            if v not in (None, ''):
                if k == 'schema_digest' and isinstance(v, str) and len(v) > 20:
                    parts.append(f'schema={v[:14]}…')
                else:
                    parts.append(f'{k}={v}')
        return ','.join(parts) if parts else ''

    columns = [
        ColumnAttr('name'),
        ColumnAttr('description'),
        ColumnAttr('path_pattern', width=30),
        ColumnAttr('filetype'),
        ColumnAttr('sheet_name'),
        Column('match_rules', width=40, getter=lambda c, r: ProfilesSheet._match_summary(r)),
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
    '''
name = input("save profile as: ")
desc = input("description (optional): ", value="")
p = sheet._source_path_or_sheet()
default_pattern = ''
if isinstance(p, Path):
    default_pattern = '*' + p.suffix if p.suffix else p.given
elif isinstance(p, BaseSheet) and isinstance(p.source, Path):
    default_pattern = '*' + p.source.suffix if p.source.suffix else p.source.given
pat = input(f"path pattern (glob, optional): ", value=default_pattern)
sheet.save_profile_cmd(name, desc, pat)
''',
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
