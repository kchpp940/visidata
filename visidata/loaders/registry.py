from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Callable

from visidata import vd, VisiData


@dataclass
class LoaderDependency:
    '''描述一个 loader 的可选依赖。'''
    modname: str
    pipmodname: str = ''
    required: bool = True
    reason: str = ''

    def __post_init__(self):
        if not self.pipmodname:
            self.pipmodname = self.modname

    @property
    def is_available(self) -> bool:
        '''检查该依赖是否可用。'''
        try:
            import importlib
            importlib.import_module(self.modname)
            return True
        except ModuleNotFoundError:
            return False

    @property
    def install_hint(self) -> str:
        '''返回安装建议。'''
        if self.reason:
            return f'{self.reason}; run: `pip install {self.pipmodname}`'
        return f'run: `pip install {self.pipmodname}`'


@dataclass
class LoaderCapability:
    '''描述一个 loader 的完整能力信息。'''
    filetype: str
    extensions: List[str] = field(default_factory=list)
    can_open: bool = False
    can_save: bool = False
    dependencies: List[LoaderDependency] = field(default_factory=list)
    description: str = ''
    openfunc: Optional[Callable] = None
    savefunc: Optional[Callable] = None
    guessfunc: Optional[Callable] = None
    openurl_schemes: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.extensions:
            self.extensions = [self.filetype]

    @property
    def is_available(self) -> bool:
        '''检查所有必需依赖是否都可用。'''
        return all(dep.is_available for dep in self.dependencies if dep.required)

    @property
    def missing_dependencies(self) -> List[LoaderDependency]:
        '''返回所有缺失的必需依赖。'''
        return [dep for dep in self.dependencies if dep.required and not dep.is_available]

    @property
    def unavailable_reason(self) -> Optional[str]:
        '''返回 loader 不可用的原因，可用时返回 None。'''
        missing = self.missing_dependencies
        if not missing:
            return None
        reasons = []
        for dep in missing:
            reasons.append(f'package `{dep.modname}` not installed; {dep.install_hint}')
        return '; '.join(reasons)

    @property
    def status(self) -> str:
        '''返回状态字符串：available/unavailable 及原因。'''
        if self.is_available:
            return 'available'
        return f'unavailable: {self.unavailable_reason}'


class LoaderRegistry:
    '''集中管理所有文件格式 loader 的能力注册表。'''

    def __init__(self):
        self._loaders: OrderedDict[str, LoaderCapability] = OrderedDict()
        self._ext_to_filetype: Dict[str, str] = {}
        self._scheme_to_loader: Dict[str, str] = {}

    def register(self, capability: LoaderCapability) -> LoaderCapability:
        '''注册一个 loader 的能力信息。'''
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

        return capability

    def get(self, filetype: str) -> Optional[LoaderCapability]:
        '''根据文件类型获取能力信息。'''
        return self._loaders.get(filetype.lower())

    def get_by_extension(self, ext: str) -> Optional[LoaderCapability]:
        '''根据文件扩展名获取能力信息。'''
        ft = self._ext_to_filetype.get(ext.lower().lstrip('.'))
        if ft:
            return self.get(ft)
        return None

    def get_by_scheme(self, scheme: str) -> Optional[LoaderCapability]:
        '''根据 URL scheme 获取能力信息。'''
        ft = self._scheme_to_loader.get(scheme.lower())
        if ft:
            return self.get(ft)
        return None

    def all(self) -> List[LoaderCapability]:
        '''返回所有已注册的 loader。'''
        return list(self._loaders.values())

    def available(self) -> List[LoaderCapability]:
        '''返回所有可用的 loader。'''
        return [cap for cap in self._loaders.values() if cap.is_available]

    def unavailable(self) -> List[LoaderCapability]:
        '''返回所有不可用的 loader。'''
        return [cap for cap in self._loaders.values() if not cap.is_available]

    def can_open(self, filetype: str) -> bool:
        '''检查指定文件类型是否可以打开。'''
        cap = self.get(filetype)
        return bool(cap and cap.can_open and cap.is_available)

    def can_save(self, filetype: str) -> bool:
        '''检查指定文件类型是否可以保存。'''
        cap = self.get(filetype)
        return bool(cap and cap.can_save and cap.is_available)

    def openers(self) -> List[LoaderCapability]:
        '''返回所有可以打开的可用 loader。'''
        return [cap for cap in self.available() if cap.can_open]

    def savers(self) -> List[LoaderCapability]:
        '''返回所有可以保存的可用 loader。'''
        return [cap for cap in self.available() if cap.can_save]

    def check_open(self, filetype: str) -> None:
        '''检查是否可以打开指定类型，不可以则抛出异常提示。'''
        cap = self.get(filetype)
        if not cap:
            vd.fail(f'unknown filetype: {filetype}')
        if not cap.can_open:
            vd.fail(f'{filetype} loader does not support opening')
        if not cap.is_available:
            vd.fail(f'{filetype} loader unavailable: {cap.unavailable_reason}')

    def check_save(self, filetype: str) -> None:
        '''检查是否可以保存指定类型，不可以则抛出异常提示。'''
        cap = self.get(filetype)
        if not cap:
            vd.fail(f'unknown filetype: {filetype}')
        if not cap.can_save:
            vd.fail(f'{filetype} loader does not support saving')
        if not cap.is_available:
            vd.fail(f'{filetype} loader unavailable: {cap.unavailable_reason}')


vd.loaders = LoaderRegistry()


@VisiData.api
def registerLoader(vd, filetype, *, extensions=None, can_open=False, can_save=False,
                   dependencies=None, description='', openfunc=None, savefunc=None,
                   guessfunc=None, openurl_schemes=None, extra=None):
    '''注册一个 loader 的能力信息到全局 registry。

    Args:
        filetype: 文件类型标识，如 'csv', 'xlsx'
        extensions: 关联的文件扩展名列表，如 ['xlsx', 'xlsm']
        can_open: 是否支持打开
        can_save: 是否支持保存
        dependencies: 依赖列表，每个元素可以是 LoaderDependency、字符串或 (modname, pipmodname) 元组
        description: 描述文本
        openfunc: 打开函数 (vd, path) -> sheet
        savefunc: 保存函数 (vd, path, *sheets) -> None
        guessfunc: 文件类型猜测函数
        openurl_schemes: URL scheme 列表，如 ['s3', 'postgres']
        extra: 额外信息字典
    '''
    deps = []
    if dependencies:
        for dep in dependencies:
            if isinstance(dep, LoaderDependency):
                deps.append(dep)
            elif isinstance(dep, str):
                deps.append(LoaderDependency(modname=dep))
            elif isinstance(dep, (tuple, list)) and len(dep) >= 2:
                deps.append(LoaderDependency(modname=dep[0], pipmodname=dep[1]))
            elif isinstance(dep, dict):
                deps.append(LoaderDependency(**dep))

    cap = LoaderCapability(
        filetype=filetype,
        extensions=extensions or [],
        can_open=can_open,
        can_save=can_save,
        dependencies=deps,
        description=description,
        openfunc=openfunc,
        savefunc=savefunc,
        guessfunc=guessfunc,
        openurl_schemes=openurl_schemes or [],
        extra=extra or {},
    )
    return vd.loaders.register(cap)


@VisiData.api
def listLoaders(vd, include_unavailable=True):
    '''列出所有已注册的 loader。'''
    if include_unavailable:
        return vd.loaders.all()
    return vd.loaders.available()


@VisiData.api
def getLoaderInfo(vd, filetype):
    '''获取指定文件类型的 loader 信息。'''
    return vd.loaders.get(filetype)


from visidata import Sheet, Column, ItemColumn, BaseSheet


class LoaderSheet(Sheet):
    '''显示所有已注册 loader 及其能力、依赖状态的表格。'''
    rowtype = 'loaders'
    columns = [
        Column('filetype', type=str, getter=lambda c, r: r.filetype),
        Column('extensions', type=str, getter=lambda c, r: ', '.join(r.extensions)),
        Column('can_open', type=bool, getter=lambda c, r: r.can_open),
        Column('can_save', type=bool, getter=lambda c, r: r.can_save),
        Column('available', type=bool, getter=lambda c, r: r.is_available),
        Column('status', type=str, getter=lambda c, r: r.status),
        Column('description', type=str, getter=lambda c, r: r.description),
        Column('dependencies', type=str,
               getter=lambda c, r: ', '.join(f'{d.modname}{"✓" if d.is_available else "✗"}' for d in r.dependencies) if r.dependencies else ''),
        Column('schemes', type=str, getter=lambda c, r: ', '.join(r.openurl_schemes) if r.openurl_schemes else ''),
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
