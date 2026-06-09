import importlib
import time
import pkgutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from visidata import vd, VisiData, BaseSheet, Sheet, ItemColumn, Column, CellColorizer, AttrDict


FEATURE_STATUS_PENDING = 'pending'
FEATURE_STATUS_LOADED = 'loaded'
FEATURE_STATUS_FAILED = 'failed'
FEATURE_STATUS_DISABLED = 'disabled'
FEATURE_STATUS_MISSING_DEPS = 'missing_deps'


@dataclass
class FeatureSpec:
    name: str
    module_path: str
    description: str = ''
    dependencies: List[str] = field(default_factory=list)
    optional_dependencies: List[str] = field(default_factory=list)
    enabled: bool = True
    status: str = FEATURE_STATUS_PENDING
    error: Optional[str] = None
    commands_registered: List[Tuple[str, str]] = field(default_factory=list)
    menus_registered: List[str] = field(default_factory=list)
    load_time_ms: float = 0.0


class FeatureRegistry:
    def __init__(self):
        self._features: Dict[str, FeatureSpec] = {}
        self._cmd_tracking_enabled: bool = False
        self._cmd_tracking_buffer: List[Tuple[str, str]] = []
        self._menu_tracking_buffer: List[str] = []

    def register(self, name: str, module_path: str, **kwargs) -> FeatureSpec:
        spec = FeatureSpec(name=name, module_path=module_path, **kwargs)
        self._features[name] = spec
        return spec

    def get(self, name: str) -> Optional[FeatureSpec]:
        return self._features.get(name)

    def __iter__(self):
        return iter(self._features.values())

    def __len__(self):
        return len(self._features)

    def items(self):
        return self._features.items()

    def discover(self, pkgname: str) -> List[FeatureSpec]:
        discovered = []
        try:
            pkg = importlib.import_module(pkgname)
        except ImportError:
            return discovered

        for module_info in pkgutil.iter_modules(pkg.__path__):
            if module_info.name.startswith('_'):
                continue
            modpath = f'{pkgname}.{module_info.name}'
            if module_info.name not in self._features:
                spec = self.register(module_info.name, modpath)
                discovered.append(spec)
        return discovered

    def check_dependencies(self, spec: FeatureSpec) -> Tuple[bool, List[str]]:
        missing = []
        for dep in spec.dependencies:
            try:
                importlib.import_module(dep)
            except ImportError:
                missing.append(dep)
        return (len(missing) == 0, missing)

    def _start_tracking(self):
        self._cmd_tracking_enabled = True
        self._cmd_tracking_buffer = []
        self._menu_tracking_buffer = []

    def _stop_tracking(self, spec: FeatureSpec):
        self._cmd_tracking_enabled = False
        spec.commands_registered = list(self._cmd_tracking_buffer)
        spec.menus_registered = list(self._menu_tracking_buffer)
        self._cmd_tracking_buffer = []
        self._menu_tracking_buffer = []

    def track_command(self, sheet_class_name: str, longname: str):
        if self._cmd_tracking_enabled:
            self._cmd_tracking_buffer.append((sheet_class_name, longname))

    def track_menu(self, menupath: str):
        if self._cmd_tracking_enabled:
            self._menu_tracking_buffer.append(menupath)

    def load_feature(self, name: str) -> FeatureSpec:
        spec = self._features.get(name)
        if not spec:
            raise KeyError(f'no feature named `{name}`')

        if not spec.enabled:
            spec.status = FEATURE_STATUS_DISABLED
            return spec

        if spec.status == FEATURE_STATUS_LOADED:
            return spec

        ok, missing = self.check_dependencies(spec)
        if not ok:
            spec.status = FEATURE_STATUS_MISSING_DEPS
            spec.error = f'missing dependencies: {", ".join(missing)}'
            vd.warning(f'feature `{name}` not loaded; {spec.error}')
            return spec

        t0 = time.time()
        self._start_tracking()
        old_importing = vd.importingModule
        try:
            vd.importingModule = name
            mod = importlib.import_module(spec.module_path)
            vd.importedModules.append(mod)
            vd.addGlobals({spec.module_path: mod})

            if hasattr(mod, '__description__'):
                spec.description = mod.__description__
            elif mod.__doc__:
                spec.description = mod.__doc__.strip().splitlines()[0] if mod.__doc__.strip() else ''

            spec.status = FEATURE_STATUS_LOADED
        except Exception as e:
            spec.status = FEATURE_STATUS_FAILED
            spec.error = str(e)
            vd.warning(f'feature `{name}` failed to load: {e}')
            vd.exceptionCaught(e)
        finally:
            vd.importingModule = old_importing
            self._stop_tracking(spec)
            spec.load_time_ms = (time.time() - t0) * 1000.0

        return spec

    def load_all(self, pkgname: str = None) -> List[FeatureSpec]:
        if pkgname:
            self.discover(pkgname)
        results = []
        for name in list(self._features.keys()):
            results.append(self.load_feature(name))
        return results

    def find_feature_by_command(self, longname: str) -> Optional[FeatureSpec]:
        for spec in self._features.values():
            for _, cmd_longname in spec.commands_registered:
                if cmd_longname == longname:
                    return spec
        return None


@VisiData.lazy_property
def featureRegistry(vd):
    return FeatureRegistry()


@VisiData.api
def getFeatureForCommand(vd, longname: str) -> Optional[FeatureSpec]:
    return vd.featureRegistry.find_feature_by_command(longname) if vd.featureRegistry else None


class FeaturesSheet(Sheet):
    rowtype = 'features'
    colorizers = [
        CellColorizer(2, 'color_warning', lambda s,c,r,v: r and r.status in (FEATURE_STATUS_FAILED, FEATURE_STATUS_MISSING_DEPS)),
        CellColorizer(2, 'color_working', lambda s,c,r,v: r and r.status == FEATURE_STATUS_LOADED),
    ]
    columns = [
        ItemColumn('name', width=25),
        ItemColumn('status', width=14),
        ItemColumn('description', width=50),
        Column('dependencies', getter=lambda c,r: ', '.join(r.dependencies) if r.dependencies else ''),
        Column('commands', width=6, getter=lambda c,r: len(r.commands_registered)),
        Column('menus', width=6, getter=lambda c,r: len(r.menus_registered)),
        Column('load_time_ms', width=10, type=float, fmtstr='%.1f', getter=lambda c,r: r.load_time_ms),
        ItemColumn('error', width=40),
    ]
    _ordering = [('name', True)]
    nKeys = 1

    def iterload(self):
        for spec in vd.featureRegistry:
            yield AttrDict(
                name=spec.name,
                module_path=spec.module_path,
                description=spec.description,
                dependencies=spec.dependencies,
                status=spec.status,
                error=spec.error,
                commands_registered=spec.commands_registered,
                menus_registered=spec.menus_registered,
                load_time_ms=spec.load_time_ms,
            )

    def reload(self):
        self.rows = list(self.iterload())


FeaturesSheet.addCommand('Enter', 'open-feature-commands', 'vd.push(FeatureCommandsSheet(cursorRow.name + "_commands", source=cursorRow))', 'open list of commands registered by this feature')
FeaturesSheet.addCommand('r', 'reload-feature', 'vd.featureRegistry.load_feature(cursorRow.name); reload()', 'reload selected feature')


class FeatureCommandsSheet(Sheet):
    rowtype = 'commands'
    columns = [
        ItemColumn('sheet', width=20),
        ItemColumn('longname', width=30),
    ]
    nKeys = 2

    def iterload(self):
        spec = vd.featureRegistry.get(self.source.name)
        if spec:
            for sheet_class, longname in spec.commands_registered:
                yield AttrDict(sheet=sheet_class, longname=longname)

    def reload(self):
        self.rows = list(self.iterload())


BaseSheet.addCommand(None, 'open-features', 'vd.push(FeaturesSheet("features"))', 'open Features Sheet to view and manage features')

vd.addMenuItems('''
    System > Features Sheet > open-features
''')

vd.addGlobals(FeatureRegistry=FeatureRegistry, FeatureSpec=FeatureSpec, FeaturesSheet=FeaturesSheet, FeatureCommandsSheet=FeatureCommandsSheet)
