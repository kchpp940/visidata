import importlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

from visidata import vd, VisiData, Sheet, Column, BaseSheet


@dataclass
class LoaderDependency:
    modname: str
    pipmodname: str = ''
    required: bool = True
    reason: str = ''

    def __post_init__(self):
        if not self.pipmodname:
            self.pipmodname = self.modname

    @property
    def is_available(self) -> bool:
        try:
            importlib.import_module(self.modname)
            return True
        except ModuleNotFoundError:
            return False

    @property
    def install_hint(self) -> str:
        if self.reason:
            return f'{self.reason}; run: `pip install {self.pipmodname}`'
        return f'run: `pip install {self.pipmodname}`'

    def import_module(self):
        '''Import and return this module. Fail with unified error if missing.'''
        try:
            return importlib.import_module(self.modname)
        except ModuleNotFoundError:
            vd.fail(f'package `{self.modname}` not installed; {self.install_hint}')


@dataclass
class LoaderCapability:
    filetype: str
    module: str = ''
    extensions: List[str] = field(default_factory=list)
    can_open: bool = False
    can_save: bool = False
    dependencies: List[LoaderDependency] = field(default_factory=list)
    description: str = ''
    openurl_schemes: List[str] = field(default_factory=list)
    openfunc_name: str = ''
    savefunc_name: str = ''
    openurl_func_name: str = ''
    guess_func_name: str = ''
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.extensions:
            self.extensions = [self.filetype]
        if not self.openfunc_name and self.can_open and not self.openurl_schemes:
            self.openfunc_name = f'open_{self.filetype}'
        if not self.savefunc_name and self.can_save:
            self.savefunc_name = f'save_{self.filetype}'
        if not self.openurl_func_name and self.openurl_schemes:
            self.openurl_func_name = f'openurl_{self.openurl_schemes[-1]}'

    @property
    def is_available(self) -> bool:
        return all(dep.is_available for dep in self.dependencies if dep.required)

    @property
    def missing_dependencies(self) -> List[LoaderDependency]:
        return [dep for dep in self.dependencies if dep.required and not dep.is_available]

    @property
    def unavailable_reason(self) -> Optional[str]:
        missing = self.missing_dependencies
        if not missing:
            return None
        return '; '.join(
            f'package `{dep.modname}` not installed; {dep.install_hint}'
            for dep in missing
        )

    @property
    def status(self) -> str:
        if self.is_available:
            return 'available'
        return f'unavailable: {self.unavailable_reason}'

    def resolve_openfunc(self):
        if self.openfunc_name:
            return getattr(vd, self.openfunc_name, None) or vd.getGlobals().get(self.openfunc_name)
        return None

    def resolve_savefunc(self):
        if self.savefunc_name:
            fn = getattr(vd, self.savefunc_name, None) or vd.getGlobals().get(self.savefunc_name)
            if fn:
                return fn
            for ft in self.extensions:
                fn = getattr(vd, f'save_{ft}', None) or vd.getGlobals().get(f'save_{ft}')
                if fn:
                    return fn
        return None

    def resolve_openurlfunc(self):
        if self.openurl_func_name:
            return getattr(vd, self.openurl_func_name, None) or vd.getGlobals().get(self.openurl_func_name)
        return None

    def resolve_guessfunc(self):
        if self.guess_func_name:
            return getattr(vd, self.guess_func_name, None) or vd.getGlobals().get(self.guess_func_name)
        return None

    def require_deps(self):
        '''Check all required dependencies. Return list of imported required modules. Fail if any missing.'''
        imported = []
        for dep in self.dependencies:
            if dep.required:
                m = dep.import_module()
                vd.addGlobals({dep.modname: m})
                imported.append(m)
        return imported

    def require_dep(self, modname):
        '''Import and return a single dependency module by modname. Fail if missing or not declared.'''
        dep = self.get_dep(modname)
        if dep is None:
            vd.fail(f'dependency `{modname}` not declared for filetype `{self.filetype}`')
        return dep.import_module()

    def get_dep(self, modname):
        '''Return the LoaderDependency for the given modname, or None if not declared.'''
        for dep in self.dependencies:
            if dep.modname == modname:
                return dep
        return None


class LoaderRegistry:
    def __init__(self):
        self._loaders: OrderedDict[str, LoaderCapability] = OrderedDict()
        self._ext_to_filetype: Dict[str, str] = {}
        self._scheme_to_loader: Dict[str, str] = {}
        self._skip_reasons: Dict[str, str] = {}

    def register(self, capability: LoaderCapability) -> LoaderCapability:
        ft = capability.filetype.lower()
        self._loaders[ft] = capability

        for ext in capability.extensions:
            ext_lower = ext.lower().lstrip('.')
            if ext_lower and ext_lower not in self._ext_to_filetype:
                self._ext_to_filetype[ext_lower] = ft

        for scheme in capability.openurl_schemes:
            scheme_lower = scheme.lower()
            if scheme_lower not in self._scheme_to_loader:
                self._scheme_to_loader[scheme_lower] = ft

        modkey = capability.module or ft
        if modkey:
            any_available = any(
                cap.is_available
                for cap in self._loaders.values()
                if (cap.module or cap.filetype) == modkey
            )
            if any_available:
                self._skip_reasons.pop(modkey, None)
            else:
                unavailable_caps = [
                    cap for cap in self._loaders.values()
                    if (cap.module or cap.filetype) == modkey and not cap.is_available
                ]
                if unavailable_caps:
                    self._skip_reasons[modkey] = unavailable_caps[0].unavailable_reason

        return capability

    def get(self, filetype: str) -> Optional[LoaderCapability]:
        return self._loaders.get(filetype.lower())

    def get_by_extension(self, ext: str) -> Optional[LoaderCapability]:
        ft = self._ext_to_filetype.get(ext.lower().lstrip('.'))
        if ft:
            return self.get(ft)
        return None

    def get_by_scheme(self, scheme: str) -> Optional[LoaderCapability]:
        ft = self._scheme_to_loader.get(scheme.lower())
        if ft:
            return self.get(ft)
        return None

    def should_skip_module(self, modname: str) -> Optional[str]:
        return self._skip_reasons.get(modname)

    def all(self) -> List[LoaderCapability]:
        return list(self._loaders.values())

    def available(self) -> List[LoaderCapability]:
        return [cap for cap in self._loaders.values() if cap.is_available]

    def unavailable(self) -> List[LoaderCapability]:
        return [cap for cap in self._loaders.values() if not cap.is_available]

    def can_open(self, filetype: str) -> bool:
        cap = self.get(filetype)
        return bool(cap and cap.can_open and cap.is_available)

    def can_save(self, filetype: str) -> bool:
        cap = self.get(filetype)
        return bool(cap and cap.can_save and cap.is_available)

    def openers(self) -> List[LoaderCapability]:
        return [cap for cap in self.available() if cap.can_open]

    def savers(self) -> List[LoaderCapability]:
        return [cap for cap in self.available() if cap.can_save]

    def check_open(self, filetype: str) -> None:
        cap = self.get(filetype)
        if not cap:
            vd.fail(f'unknown filetype: {filetype}')
        if not cap.is_available:
            vd.fail(f'{filetype} loader unavailable: {cap.unavailable_reason}')
        if not cap.can_open:
            vd.fail(f'{filetype} loader does not support opening')

    def check_save(self, filetype: str) -> None:
        cap = self.get(filetype)
        if not cap:
            vd.fail(f'unknown filetype: {filetype}')
        if not cap.is_available:
            vd.fail(f'{filetype} loader unavailable: {cap.unavailable_reason}')
        if not cap.can_save:
            vd.fail(f'{filetype} loader does not support saving')

    def find_openfunc(self, filetype: str):
        cap = self.get(filetype)
        if not cap:
            return None
        if not cap.is_available:
            vd.fail(f'{filetype} loader unavailable: {cap.unavailable_reason}')
        if not cap.can_open:
            return None
        return cap.resolve_openfunc()

    def find_savefunc(self, filetype: str, sheet=None):
        cap = self.get(filetype)
        if not cap:
            if sheet:
                fn = getattr(sheet, f'save_{filetype}', None)
                if fn:
                    return fn
            return None
        if not cap.is_available:
            vd.fail(f'{filetype} loader unavailable: {cap.unavailable_reason}')
        if not cap.can_save:
            return None
        fn = cap.resolve_savefunc()
        if fn:
            return fn
        if sheet:
            fn = getattr(sheet, f'save_{filetype}', None)
            if fn:
                return fn
        return None

    def find_openurlfunc(self, scheme: str):
        cap = self.get_by_scheme(scheme)
        if not cap:
            return None
        if not cap.is_available:
            vd.fail(f'{scheme} loader unavailable: {cap.unavailable_reason}')
        return cap.resolve_openurlfunc()


_D = LoaderDependency


LOADER_MANIFEST = [
    LoaderCapability('tsv', module='tsv', extensions=['tsv'],
        can_open=True, can_save=True, dependencies=[],
        description='Tab-separated values'),
    LoaderCapability('csv', module='csv', extensions=['csv'],
        can_open=True, can_save=True, dependencies=[],
        description='Comma-separated values',
        guess_func_name='guess_csv'),
    LoaderCapability('json', module='json', extensions=['json'],
        can_open=True, can_save=True, dependencies=[],
        description='JavaScript Object Notation',
        guess_func_name='guess_json'),
    LoaderCapability('jsonl', module='json', extensions=['jsonl', 'ndjson', 'ldjson'],
        can_open=True, can_save=True, dependencies=[],
        openfunc_name='open_jsonl', savefunc_name='save_jsonl',
        description='JSON Lines (one object per line)'),
    LoaderCapability('jsonla', module='jsonla', extensions=['jsonla'],
        can_open=True, can_save=True, dependencies=[],
        description='JSON Lines with async wrapper',
        guess_func_name='guess_jsonla'),
    LoaderCapability('fixed', module='fixed_width', extensions=['fixed'],
        can_open=True, can_save=True, dependencies=[],
        description='Fixed-width text'),
    LoaderCapability('psv', module='psv', extensions=['psv'],
        can_open=True, can_save=True, dependencies=[],
        description='Pipe-separated values'),
    LoaderCapability('usv', module='usv', extensions=['usv'],
        can_open=True, can_save=True, dependencies=[],
        description='Unit-separated values'),
    LoaderCapability('rec', module='rec', extensions=['rec'],
        can_open=True, can_save=True, dependencies=[],
        description='GNU Recutils database'),
    LoaderCapability('lsv', module='lsv', extensions=['lsv'],
        can_open=True, can_save=True, dependencies=[],
        description='Line-separated values'),
    LoaderCapability('jrnl', module='jrnl', extensions=['jrnl'],
        can_open=True, can_save=True, dependencies=[],
        description='jrnl journal format'),
    LoaderCapability('grep', module='grep', extensions=['grep'],
        can_open=True, can_save=True, dependencies=[],
        description='Grep results'),
    LoaderCapability('vds', module='vds', extensions=['vds'],
        can_open=True, can_save=True, dependencies=[],
        description='VisiData save format'),
    LoaderCapability('vdx', module='vdx', extensions=['vdx'],
        can_open=True, can_save=True, dependencies=[],
        description='VisiData command log XML'),
    LoaderCapability('org', module='orgmode', extensions=['org', 'forg', 'orgdir'],
        can_open=True, can_save=True, dependencies=[],
        description='Emacs Org-mode'),
    LoaderCapability('geojson', module='geojson', extensions=['geojson'],
        can_open=True, can_save=True, dependencies=[],
        description='GeoJSON geographic data'),
    LoaderCapability('xml', module='xml', extensions=['xml', 'svg'],
        can_open=True, can_save=True,
        dependencies=[_D('lxml')],
        description='XML / SVG'),
    LoaderCapability('html', module='html', extensions=['html', 'htm'],
        can_open=True, can_save=True,
        dependencies=[_D('lxml')],
        description='HTML table',
        guess_func_name='guess_html'),
    LoaderCapability('markdown', module='markdown', extensions=['md'],
        can_save=True, dependencies=[],
        savefunc_name='save_md',
        description='Markdown table'),
    LoaderCapability('jira', module='markdown', extensions=['jira'],
        can_save=True, dependencies=[],
        description='Jira table'),
    LoaderCapability('eml', module='eml', extensions=['eml', 'mhtml'],
        can_open=True, dependencies=[],
        description='Email message (EML / MHTML)'),
    LoaderCapability('mbox', module='mailbox', extensions=['mbox', 'maildir', 'mmdf', 'babyl', 'mh'],
        can_open=True, dependencies=[],
        openfunc_name='open_mbox',
        description='Mailbox formats'),
    LoaderCapability('sqlite', module='sqlite', extensions=['sqlite', 'sqlite3', 'db'],
        can_open=True, can_save=True, dependencies=[],
        description='SQLite database',
        openurl_schemes=['sqlite'],
        guess_func_name='guess_sqlite'),
    LoaderCapability('dot', module='graphviz', extensions=['dot'],
        can_save=True, dependencies=[],
        description='Graphviz DOT graph'),
    LoaderCapability('yaml', module='yaml', extensions=['yml', 'yaml'],
        can_open=True,
        dependencies=[_D('yaml', 'PyYAML')],
        openfunc_name='open_yml',
        description='YAML'),
    LoaderCapability('toml', module='toml', extensions=['toml'],
        can_open=True,
        dependencies=[_D('tomli', required=False)],
        description='TOML configuration file'),
    LoaderCapability('http', module='http',
        can_open=True, dependencies=[],
        description='HTTP/HTTPS URL',
        openurl_schemes=['http', 'https'],
        openurl_func_name='openurl_http'),
    LoaderCapability('zip', module='archive', extensions=['zip', 'whl'],
        can_open=True,
        dependencies=[_D('urllib3', required=False)],
        openfunc_name='open_zip', guess_func_name='guess_zip',
        description='ZIP archive'),
    LoaderCapability('tar', module='archive', extensions=['tar', 'tgz', 'txz', 'tbz2'],
        can_open=True, dependencies=[],
        openfunc_name='open_tar', guess_func_name='guess_tar',
        description='TAR archive'),
    LoaderCapability('xlsx', module='xlsx', extensions=['xlsx'],
        can_open=True, can_save=True,
        dependencies=[_D('openpyxl')],
        description='Excel 2007+ XLSX'),
    LoaderCapability('xls', module='xlsx', extensions=['xls'],
        can_open=True, can_save=True,
        dependencies=[_D('xlrd'), _D('xlwt')],
        description='Legacy Excel XLS'),
    LoaderCapability('xlsb', module='xlsb', extensions=['xlsb'],
        can_open=True,
        dependencies=[_D('pyxlsb', 'git+https://github.com/saulpw/pyxlsb.git@visidata#egg=pyxlsb')],
        guess_func_name='guess_xls',
        description='Excel XLSB binary'),
    LoaderCapability('ods', module='odf', extensions=['ods'],
        can_open=True,
        dependencies=[_D('odf', 'odfpy')],
        description='OpenDocument Spreadsheet'),
    LoaderCapability('pandas', module='_pandas',
        can_open=True, can_save=True,
        dependencies=[_D('pandas'), _D('numpy')],
        description='Pandas DataFrame (feather/gbq/orc/pickle/sas/stata/dta)'),
    LoaderCapability('arrow', module='arrow', extensions=['arrow', 'arrows'],
        can_open=True, can_save=True,
        dependencies=[_D('pyarrow'), _D('numpy', required=False)],
        description='Apache Arrow IPC'),
    LoaderCapability('parquet', module='parquet', extensions=['parquet'],
        can_open=True, can_save=True,
        dependencies=[_D('pyarrow'), _D('shapely', required=False)],
        description='Apache Parquet'),
    LoaderCapability('hdf5', module='hdf5', extensions=['h5', 'hdf5', 'hdf'],
        can_open=True,
        dependencies=[_D('h5py'), _D('numpy', required=False)],
        openfunc_name='open_h5',
        description='HDF5 hierarchical data'),
    LoaderCapability('npy', module='npy', extensions=['npy', 'npz'],
        can_open=True, can_save=True,
        dependencies=[_D('numpy')],
        description='NumPy array'),
    LoaderCapability('s3', module='s3',
        can_open=True,
        dependencies=[_D('s3fs.core', 's3fs')],
        description='Amazon S3',
        openurl_schemes=['s3'],
        openurl_func_name='openurl_s3'),
    LoaderCapability('postgres', module='postgres',
        can_open=True,
        dependencies=[_D('psycopg2', 'psycopg2-binary'), _D('boto3', required=False)],
        description='PostgreSQL',
        openurl_schemes=['postgres', 'postgresql', 'rds'],
        openurl_func_name='openurl_postgres'),
    LoaderCapability('mysql', module='mysql',
        can_open=True,
        dependencies=[_D('MySQLdb', 'mysqlclient')],
        description='MySQL',
        openurl_schemes=['mysql'],
        openurl_func_name='openurl_mysql'),
    LoaderCapability('imap', module='imap',
        can_open=True,
        dependencies=[_D('google.auth.transport.requests', 'google-auth'),
                      _D('google_auth_oauthlib.flow', 'google-auth-oauthlib')],
        description='IMAP email',
        openurl_schemes=['imap'],
        openurl_func_name='openurl_imap'),
    LoaderCapability('pdf', module='pdf', extensions=['pdf'],
        can_open=True,
        dependencies=[_D('pdfminer.high_level', 'pdfminer.six'), _D('tabula', required=False)],
        description='PDF document'),
    LoaderCapability('png', module='png', extensions=['png'],
        can_open=True, can_save=True,
        dependencies=[_D('png', 'pypng')],
        description='PNG image'),
    LoaderCapability('pcap', module='pcap', extensions=['pcap', 'cap', 'pcapng', 'ntar'],
        can_open=True,
        dependencies=[_D('dpkt'), _D('dnslib', required=False)],
        openfunc_name='open_pcap',
        description='Packet capture'),
    LoaderCapability('vcf', module='vcf', extensions=['vcf'],
        can_open=True,
        dependencies=[_D('vobject')],
        description='vCard address book'),
    LoaderCapability('shp', module='shp', extensions=['shp', 'dbf'],
        can_open=True,
        dependencies=[_D('shapefile', 'pyshp')],
        description='ESRI Shapefile'),
    LoaderCapability('mbtiles', module='mbtiles', extensions=['mbtiles', 'pbf'],
        can_open=True,
        dependencies=[_D('mapbox_vector_tile', 'mapbox-vector-tile')],
        description='Mapbox MBTiles / PBF'),
    LoaderCapability('ttf', module='ttf', extensions=['ttf', 'otf'],
        can_open=True,
        dependencies=[_D('fontTools.ttLib', 'fonttools')],
        description='TrueType / OpenType font'),
    LoaderCapability('msgpack', module='msgpack', extensions=['msgpack', 'msgpackz'],
        can_open=True,
        dependencies=[_D('msgpack'), _D('brotli', required=False)],
        description='MessagePack / MessagePackZ binary'),
    LoaderCapability('sas', module='sas', extensions=['sas7bdat'],
        can_open=True,
        dependencies=[_D('sas7bdat'), _D('xport', required=False)],
        description='SAS dataset'),
    LoaderCapability('xpt', module='sas', extensions=['xpt'],
        can_open=True,
        dependencies=[_D('xport.v56', 'xport>=3')],
        description='SAS XPORT transport'),
    LoaderCapability('spss', module='spss', extensions=['spss', 'sav'],
        can_open=True,
        dependencies=[_D('savReaderWriter')],
        openfunc_name='open_spss',
        description='SPSS dataset'),
    LoaderCapability('fec', module='fec', extensions=['fec'],
        can_open=True,
        dependencies=[_D('fecfile')],
        description='FEC filing'),
    LoaderCapability('f5log', module='f5log', extensions=['f5log'],
        can_open=True, dependencies=[],
        description='F5 Networks log'),
    LoaderCapability('frictionless', module='frictionless', extensions=['frictionless'],
        can_open=True,
        dependencies=[_D('datapackage')],
        description='Frictionless Data Package'),
    LoaderCapability('conll', module='conll', extensions=['conll', 'conllu'],
        can_open=True,
        dependencies=[_D('pyconll')],
        openfunc_name='open_conll',
        description='CoNLL / CoNLL-U annotation'),
    LoaderCapability('scrape', module='scrape',
        can_open=True,
        dependencies=[_D('bs4', 'beautifulsoup4'), _D('requests')],
        description='Web scraper (BeautifulSoup + requests)',
        openurl_schemes=['http+scrape', 'scrape'],
        openurl_func_name='openhttp_scrape'),
    LoaderCapability('airtable', module='api_airtable',
        can_open=True,
        dependencies=[_D('pyairtable')],
        description='Airtable API'),
    LoaderCapability('matrix', module='api_matrix',
        can_open=True,
        dependencies=[_D('matrix_client')],
        description='Matrix protocol',
        openurl_schemes=['http+matrix', 'matrix'],
        openurl_func_name='openhttp_matrix'),
    LoaderCapability('reddit', module='api_reddit', extensions=['reddit'],
        can_open=True,
        dependencies=[_D('praw')],
        description='Reddit API'),
    LoaderCapability('zulip', module='api_zulip',
        can_open=True,
        dependencies=[_D('zulip')],
        description='Zulip chat API',
        openurl_schemes=['http+zulip', 'zulip'],
        openurl_func_name='openhttp_zulip'),
    LoaderCapability('claude', module='claude', extensions=['claude'],
        can_open=True, dependencies=[],
        description='Claude AI conversation'),
    LoaderCapability('xword', module='xword', extensions=['puz', 'xd'],
        can_open=True, can_save=True,
        dependencies=[_D('xdfile', required=False)],
        description='Crossword puzzle'),
    LoaderCapability('pandas_freqtbl', module='pandas_freqtbl',
        can_open=True,
        dependencies=[_D('pandas'), _D('numpy')],
        description='Pandas frequency table'),
    LoaderCapability('google', module='google',
        dependencies=[_D('google.auth.transport.requests', 'google-auth'),
                      _D('google_auth_oauthlib.flow', 'google-auth-oauthlib')],
        description='Google OAuth helper'),
    LoaderCapability('texttables', module='texttables',
        can_save=True,
        dependencies=[_D('tabulate')],
        description='Tabulate text table formats (grid/pipe/simple/rst/etc)'),
]


def _build_registry():
    reg = LoaderRegistry()
    for cap in LOADER_MANIFEST:
        reg.register(cap)
    return reg


vd.loaders = _build_registry()


@VisiData.api
def registerLoader(vd, filetype, **kwargs):
    '''Register a loader capability into the global registry.

    This is the explicit entry point for external addons to register their own
    file types.  The filetype is required; all other arguments are optional.

    Function references may be passed directly as ``openfunc`` / ``savefunc``
    / ``openurlfunc`` / ``guessfunc``, which will be auto-assigned to the
    ``openfunc_name`` / ``savefunc_name`` / ``openurl_func_name`` /
    ``guess_func_name`` slots on the resulting LoaderCapability.

    Examples::

        vd.registerLoader('mydb', can_open=True, dependencies=['mydb'],
                          openfunc=open_mydb, extensions=['mydb', 'mdb'])
    '''
    deps = []
    for dep in (kwargs.pop('dependencies', None) or []):
        if isinstance(dep, LoaderDependency):
            deps.append(dep)
        elif isinstance(dep, str):
            deps.append(LoaderDependency(modname=dep))
        elif isinstance(dep, (tuple, list)) and len(dep) >= 2:
            deps.append(LoaderDependency(modname=dep[0], pipmodname=dep[1]))
        elif isinstance(dep, dict):
            deps.append(LoaderDependency(**dep))

    cap_kwargs = dict(
        filetype=filetype,
        module=kwargs.pop('module', ''),
        extensions=kwargs.pop('extensions', []),
        can_open=kwargs.pop('can_open', False),
        can_save=kwargs.pop('can_save', False),
        dependencies=deps,
        description=kwargs.pop('description', ''),
        openurl_schemes=kwargs.pop('openurl_schemes', []),
        extra=kwargs.pop('extra', {}),
    )

    openfunc = kwargs.pop('openfunc', None)
    savefunc = kwargs.pop('savefunc', None)
    openurlfunc = kwargs.pop('openurlfunc', None)
    guessfunc = kwargs.pop('guessfunc', None)

    openfunc_name = kwargs.pop('openfunc_name', '')
    savefunc_name = kwargs.pop('savefunc_name', '')
    openurl_func_name = kwargs.pop('openurl_func_name', '')
    guess_func_name = kwargs.pop('guess_func_name', '')

    if openfunc:
        if not openfunc_name:
            openfunc_name = getattr(openfunc, '__name__', f'open_{filetype}')
        vd.addGlobals({openfunc_name: openfunc})
    if savefunc:
        if not savefunc_name:
            savefunc_name = getattr(savefunc, '__name__', f'save_{filetype}')
        vd.addGlobals({savefunc_name: savefunc})
    if openurlfunc:
        if not openurl_func_name:
            openurl_func_name = getattr(openurlfunc, '__name__', f'openurl_{filetype}')
        vd.addGlobals({openurl_func_name: openurlfunc})
    if guessfunc:
        if not guess_func_name:
            guess_func_name = getattr(guessfunc, '__name__', f'guess_{filetype}')
        vd.addGlobals({guess_func_name: guessfunc})

    cap_kwargs.update(
        openfunc_name=openfunc_name,
        savefunc_name=savefunc_name,
        openurl_func_name=openurl_func_name,
        guess_func_name=guess_func_name,
    )

    cap = LoaderCapability(**cap_kwargs)
    return vd.loaders.register(cap)


@VisiData.api
def listLoaders(vd, include_unavailable=True):
    if include_unavailable:
        return vd.loaders.all()
    return vd.loaders.available()


@VisiData.api
def getLoaderInfo(vd, filetype):
    return vd.loaders.get(filetype)


@VisiData.api
def requireLoaderDeps(vd, filetype):
    '''Require (import and return) all required dependencies of the given filetype.

    Fail with a unified error message from the capability registry if any
    required package is missing.  This is the registry-aware replacement for
    scattered ``vd.importExternal`` / ``vd.importModule`` calls inside loaders.

    Example inside a loader::

        pandas, numpy = vd.requireLoaderDeps('pandas')
    '''
    cap = vd.loaders.get(filetype)
    if not cap:
        vd.fail(f'unknown filetype: {filetype}')
    return cap.require_deps()


@VisiData.api
def requireLoaderDep(vd, filetype, modname):
    '''Require (import and return) a single dependency ``modname`` of the given filetype.

    Fail with a unified error from the registry if the package is missing or
    not declared in the manifest.

    Example inside a loader::

        requests = vd.requireLoaderDep('scrape', 'requests')
    '''
    cap = vd.loaders.get(filetype)
    if not cap:
        vd.fail(f'unknown filetype: {filetype}')
    return cap.require_dep(modname)


class LoaderSheet(Sheet):
    rowtype = 'loaders'
    columns = [
        Column('filetype', type=str, getter=lambda c, r: r.filetype),
        Column('module', type=str, getter=lambda c, r: r.module),
        Column('extensions', type=str, getter=lambda c, r: ', '.join(r.extensions)),
        Column('can_open', type=bool, getter=lambda c, r: r.can_open),
        Column('can_save', type=bool, getter=lambda c, r: r.can_save),
        Column('available', type=bool, getter=lambda c, r: r.is_available),
        Column('status', type=str, getter=lambda c, r: r.status),
        Column('description', type=str, getter=lambda c, r: r.description),
        Column('dependencies', type=str,
               getter=lambda c, r: ', '.join(
                   f'{d.modname}{"+" if d.is_available else "-"}'
                   for d in r.dependencies) if r.dependencies else ''),
        Column('schemes', type=str,
               getter=lambda c, r: ', '.join(r.openurl_schemes) if r.openurl_schemes else ''),
        Column('openfunc', type=str, getter=lambda c, r: r.openfunc_name or r.openurl_func_name or ''),
        Column('savefunc', type=str, getter=lambda c, r: r.savefunc_name or ''),
    ]

    def iterload(self):
        for cap in vd.loaders.all():
            yield cap


@VisiData.api
def open_loaders(vd, p):
    return LoaderSheet('loaders', source=p)


BaseSheet.addCommand('', 'open-loaders', 'vd.push(LoaderSheet("loaders"))', 'show all registered loaders and their capabilities')

vd.addGlobals({
    'LoaderSheet': LoaderSheet,
    'LoaderDependency': LoaderDependency,
    'LoaderCapability': LoaderCapability,
    'LoaderRegistry': LoaderRegistry,
})

vd.addMenuItems('''
    Data > Loaders > show all loaders > open-loaders
''')
