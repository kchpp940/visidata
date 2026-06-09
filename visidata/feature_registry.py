import ast
import importlib
import importlib.util
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
FEATURE_STATUS_CONFLICT = 'conflict'
FEATURE_STATUS_INCOMPLETE = 'incomplete'


@dataclass
class FeatureSpec:
    name: str
    module_path: str
    source_file: str = ''
    description: str = ''
    dependencies: List[str] = field(default_factory=list)
    optional_dependencies: List[str] = field(default_factory=list)
    declared_commands: List[str] = field(default_factory=list)
    declared_menus: List[str] = field(default_factory=list)
    enabled: bool = True
    status: str = FEATURE_STATUS_PENDING
    error: Optional[str] = None
    commands_registered: List[Tuple[str, str]] = field(default_factory=list)
    menus_registered: List[str] = field(default_factory=list)
    load_time_ms: float = 0.0

    @property
    def incomplete_reasons(self) -> List[str]:
        if self.status in (FEATURE_STATUS_MISSING_DEPS, FEATURE_STATUS_DISABLED, FEATURE_STATUS_PENDING):
            return []

        reasons = []
        BAD_DESCRIPTIONS = {'', '# Usage', 'Usage', '#', 'TODO', 'todo', 'placeholder', 'Placeholder', 'TBD', 'tbd'}

        desc = self.description.strip()
        if not desc:
            reasons.append('description is empty')
        elif desc in BAD_DESCRIPTIONS:
            reasons.append(f'description is placeholder: {repr(desc)}')
        elif desc.startswith('#'):
            reasons.append(f'description starts with #: {repr(desc)}')
        elif len(desc) < 5:
            reasons.append(f'description too short: {repr(desc)}')

        registered_cmds = {c for _, c in self.commands_registered}
        declared_cmds = set(self.declared_commands)
        missing_in_declared = sorted(registered_cmds - declared_cmds)
        extra_in_declared = sorted(declared_cmds - registered_cmds)
        if missing_in_declared:
            reasons.append(f'__commands__ missing {len(missing_in_declared)}: {missing_in_declared}')
        if extra_in_declared:
            reasons.append(f'__commands__ has {len(extra_in_declared)} unregistered: {extra_in_declared}')

        registered_menus = set(self.menus_registered)
        declared_menus = set(self.declared_menus)
        missing_in_menus_decl = sorted(registered_menus - declared_menus)
        extra_in_menus_decl = sorted(declared_menus - registered_menus)
        if missing_in_menus_decl:
            reasons.append(f'__menus__ missing {len(missing_in_menus_decl)}: {missing_in_menus_decl}')
        if extra_in_menus_decl:
            reasons.append(f'__menus__ has {len(extra_in_menus_decl)} unregistered: {extra_in_menus_decl}')

        return reasons

    @property
    def is_complete(self) -> bool:
        return len(self.incomplete_reasons) == 0

    @property
    def display_status(self) -> str:
        if self.status == FEATURE_STATUS_LOADED and not self.is_complete:
            return FEATURE_STATUS_INCOMPLETE
        return self.status


def _ast_get_constant(node: ast.expr) -> Optional[object]:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_ast_get_constant(e) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_ast_get_constant(e) for e in node.elts)
    return None


def _extract_feature_declarations(source_file: str) -> Dict:
    result = {
        'description': '',
        'dependencies': [],
        'optional_dependencies': [],
        'commands': [],
        'menus': [],
    }
    try:
        with open(source_file, 'r', encoding='utf-8') as f:
            source = f.read()
        tree = ast.parse(source, filename=source_file)
    except (OSError, SyntaxError):
        return result

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                name = target.id
                if name == '__description__':
                    val = _ast_get_constant(node.value)
                    if isinstance(val, str):
                        result['description'] = val
                elif name == '__dependencies__':
                    val = _ast_get_constant(node.value)
                    if isinstance(val, list):
                        result['dependencies'] = [str(v) for v in val if isinstance(v, str)]
                elif name == '__optional_dependencies__':
                    val = _ast_get_constant(node.value)
                    if isinstance(val, list):
                        result['optional_dependencies'] = [str(v) for v in val if isinstance(v, str)]
                elif name == '__commands__':
                    val = _ast_get_constant(node.value)
                    if isinstance(val, list):
                        result['commands'] = [str(v) for v in val if isinstance(v, str)]
                elif name == '__menus__':
                    val = _ast_get_constant(node.value)
                    if isinstance(val, list):
                        result['menus'] = [str(v) for v in val if isinstance(v, str)]

        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            if not result['description']:
                doc = node.value.value.strip()
                if doc:
                    result['description'] = doc.splitlines()[0]

    return result


class FeatureRegistry:
    def __init__(self):
        self._features: Dict[str, FeatureSpec] = {}
        self._cmd_tracking_enabled: bool = False
        self._cmd_tracking_buffer: List[Tuple[str, str]] = []
        self._menu_tracking_buffer: List[str] = []
        self._registered_menu_paths: Dict[str, str] = {}

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
                self._populate_from_source(spec)
                discovered.append(spec)
            else:
                spec = self._features[module_info.name]
                self._populate_from_source(spec)
        return discovered

    def _populate_from_source(self, spec: FeatureSpec):
        try:
            spec_info = importlib.util.find_spec(spec.module_path)
            if spec_info and spec_info.origin:
                spec.source_file = spec_info.origin
                decls = _extract_feature_declarations(spec.source_file)
                if decls['description'] and not spec.description:
                    spec.description = decls['description']
                if decls['dependencies'] and not spec.dependencies:
                    spec.dependencies = decls['dependencies']
                if decls['optional_dependencies'] and not spec.optional_dependencies:
                    spec.optional_dependencies = decls['optional_dependencies']
                if decls['commands'] and not spec.declared_commands:
                    spec.declared_commands = decls['commands']
                if decls['menus'] and not spec.declared_menus:
                    spec.declared_menus = decls['menus']
        except (ImportError, ValueError):
            pass

    def check_dependencies(self, spec: FeatureSpec) -> Tuple[bool, List[str]]:
        missing = []
        for dep in spec.dependencies:
            try:
                importlib.import_module(dep)
            except ImportError:
                missing.append(dep)
        return (len(missing) == 0, missing)

    def check_command_conflicts(self, spec: FeatureSpec) -> Tuple[bool, List[str]]:
        conflicts = []
        for cmd_longname in spec.declared_commands:
            if cmd_longname in vd.commands:
                for objname in vd.commands[cmd_longname]:
                    existing = vd.commands[cmd_longname][objname]
                    if hasattr(existing, 'module') and existing.module and existing.module != spec.name:
                        if existing.module in self._features:
                            conflicts.append(f'{cmd_longname} (registered by feature `{existing.module}`)')
                            break
        return (len(conflicts) == 0, conflicts)

    def check_menu_conflicts(self, spec: FeatureSpec) -> Tuple[bool, List[str]]:
        conflicts = []
        for menupath in spec.declared_menus:
            if menupath in self._registered_menu_paths:
                owner = self._registered_menu_paths[menupath]
                if owner != spec.name and owner in self._features:
                    conflicts.append(f'{menupath} (registered by feature `{owner}`)')
        return (len(conflicts) == 0, conflicts)

    def _start_tracking(self):
        self._cmd_tracking_enabled = True
        self._cmd_tracking_buffer = []
        self._menu_tracking_buffer = []

    def _stop_tracking(self, spec: FeatureSpec):
        self._cmd_tracking_enabled = False
        spec.commands_registered = list(self._cmd_tracking_buffer)
        spec.menus_registered = list(self._menu_tracking_buffer)
        for menupath in spec.menus_registered:
            self._registered_menu_paths[menupath] = spec.name
        self._cmd_tracking_buffer = []
        self._menu_tracking_buffer = []

    def track_command(self, sheet_class_name: str, longname: str):
        if self._cmd_tracking_enabled:
            for existing_sheet, existing_longname in self._cmd_tracking_buffer:
                if existing_longname == longname and existing_sheet == sheet_class_name:
                    return
            self._cmd_tracking_buffer.append((sheet_class_name, longname))

    def track_menu(self, menupath: str):
        if self._cmd_tracking_enabled:
            if menupath not in self._menu_tracking_buffer:
                self._menu_tracking_buffer.append(menupath)

    def find_menu_owner(self, menupath: str) -> Optional[str]:
        return self._registered_menu_paths.get(menupath)

    def _rollback_feature(self, spec: FeatureSpec):
        for sheet_class_name, cmd_longname in list(self._cmd_tracking_buffer):
            try:
                if cmd_longname in vd.commands:
                    for objname in list(vd.commands[cmd_longname].keys()):
                        if objname == sheet_class_name:
                            existing = vd.commands[cmd_longname][objname]
                            if hasattr(existing, 'module') and existing.module == spec.name:
                                del vd.commands[cmd_longname][objname]
                    if not vd.commands[cmd_longname]:
                        del vd.commands[cmd_longname]
            except Exception:
                pass

        for sheet_class_name, cmd_longname in spec.commands_registered:
            try:
                if cmd_longname in vd.commands:
                    for objname in list(vd.commands[cmd_longname].keys()):
                        if objname == sheet_class_name:
                            existing = vd.commands[cmd_longname][objname]
                            if hasattr(existing, 'module') and existing.module == spec.name:
                                del vd.commands[cmd_longname][objname]
                    if not vd.commands[cmd_longname]:
                        del vd.commands[cmd_longname]
            except Exception:
                pass

        for menupath in list(self._menu_tracking_buffer):
            self._rollback_menu(menupath, spec.name)
        for menupath in spec.menus_registered:
            self._rollback_menu(menupath, spec.name)

    def _rollback_menu(self, menupath: str, owner: str):
        if self._registered_menu_paths.get(menupath) != owner:
            return
        try:
            del self._registered_menu_paths[menupath]
            parts = menupath.split(' > ')
            if len(parts) < 2:
                return
            parent_menu = vd
            for p in parts[:-1]:
                found = None
                for m in getattr(parent_menu, 'menus', []):
                    if getattr(m, 'title', None) == p:
                        found = m
                        break
                if not found:
                    return
                parent_menu = found
            leaf_name = parts[-1]
            if hasattr(parent_menu, 'menus'):
                for i, m in enumerate(parent_menu.menus):
                    if getattr(m, 'title', None) == leaf_name or getattr(m, 'longname', None) == leaf_name:
                        del parent_menu.menus[i]
                        break
        except Exception:
            pass

    def load_feature(self, name: str) -> FeatureSpec:
        spec = self._features.get(name)
        if not spec:
            raise KeyError(f'no feature named `{name}`')

        if not spec.enabled:
            spec.status = FEATURE_STATUS_DISABLED
            return spec

        if spec.status == FEATURE_STATUS_LOADED:
            return spec

        if not spec.source_file:
            self._populate_from_source(spec)

        ok, missing = self.check_dependencies(spec)
        if not ok:
            spec.status = FEATURE_STATUS_MISSING_DEPS
            spec.error = f'missing dependencies: {", ".join(missing)}'
            vd.warning(f'feature `{name}` not loaded; {spec.error}')
            return spec

        if spec.declared_commands:
            ok, conflicts = self.check_command_conflicts(spec)
            if not ok:
                spec.status = FEATURE_STATUS_CONFLICT
                spec.error = f'command conflicts: {", ".join(conflicts)}'
                vd.warning(f'feature `{name}` not loaded; {spec.error}')
                return spec

        if spec.declared_menus:
            ok, conflicts = self.check_menu_conflicts(spec)
            if not ok:
                spec.status = FEATURE_STATUS_CONFLICT
                spec.error = f'menu conflicts: {", ".join(conflicts)}'
                vd.warning(f'feature `{name}` not loaded; {spec.error}')
                return spec

        t0 = time.time()
        self._start_tracking()
        old_importing = vd.importingModule
        success = False
        try:
            vd.importingModule = name
            mod = importlib.import_module(spec.module_path)
            vd.importedModules.append(mod)
            vd.addGlobals({spec.module_path: mod})

            if hasattr(mod, '__description__'):
                spec.description = mod.__description__
            elif mod.__doc__ and not spec.description:
                spec.description = mod.__doc__.strip().splitlines()[0] if mod.__doc__.strip() else ''

            spec.status = FEATURE_STATUS_LOADED
            success = True
        except Exception as e:
            self._rollback_feature(spec)
            error_msg = str(e)
            if 'already registered by feature' in error_msg or 'core command, cannot be overridden' in error_msg or 'already in menu' in error_msg:
                spec.status = FEATURE_STATUS_CONFLICT
                spec.error = error_msg
            else:
                spec.status = FEATURE_STATUS_FAILED
                spec.error = error_msg
            vd.warning(f'feature `{name}` not loaded; {spec.error}')
            vd.exceptionCaught(e)
        finally:
            vd.importingModule = old_importing
            self._stop_tracking(spec)
            if not success:
                spec.commands_registered = []
                spec.menus_registered = []
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

    def find_feature_by_menu(self, menupath: str) -> Optional[FeatureSpec]:
        owner = self._registered_menu_paths.get(menupath)
        if owner:
            return self._features.get(owner)
        return None


@VisiData.lazy_property
def featureRegistry(vd):
    return FeatureRegistry()


@VisiData.api
def getFeatureForCommand(vd, longname: str) -> Optional[FeatureSpec]:
    return vd.featureRegistry.find_feature_by_command(longname) if vd.featureRegistry else None


@VisiData.api
def getFeatureForMenu(vd, menupath: str) -> Optional[FeatureSpec]:
    return vd.featureRegistry.find_feature_by_menu(menupath) if vd.featureRegistry else None


class FeaturesSheet(Sheet):
    rowtype = 'features'
    colorizers = [
        CellColorizer(2, 'color_warning', lambda s,c,r,v: r and r.status in (FEATURE_STATUS_FAILED, FEATURE_STATUS_MISSING_DEPS, FEATURE_STATUS_CONFLICT)),
        CellColorizer(2, 'color_working', lambda s,c,r,v: r and r.status == FEATURE_STATUS_LOADED),
        CellColorizer(3, 'color_pending', lambda s,c,r,v: r and r.display_status == FEATURE_STATUS_INCOMPLETE),
    ]
    columns = [
        ItemColumn('name', width=25),
        Column('status', width=14, getter=lambda c,r: r.display_status),
        ItemColumn('description', width=50),
        Column('dependencies', getter=lambda c,r: ', '.join(r.dependencies) if r.dependencies else ''),
        Column('declared_commands', width=8, getter=lambda c,r: len(r.declared_commands)),
        Column('declared_menus', width=8, getter=lambda c,r: len(r.declared_menus)),
        Column('commands', width=6, getter=lambda c,r: len(r.commands_registered)),
        Column('menus', width=6, getter=lambda c,r: len(r.menus_registered)),
        Column('load_time_ms', width=10, type=float, fmtstr='%.1f', getter=lambda c,r: r.load_time_ms),
        ItemColumn('error', width=40),
        Column('incomplete_reasons', width=60, getter=lambda c,r: '; '.join(r.incomplete_reasons) if r.incomplete_reasons else ''),
    ]
    _ordering = [('name', True)]
    nKeys = 1

    def iterload(self):
        for spec in vd.featureRegistry:
            yield AttrDict(
                name=spec.name,
                module_path=spec.module_path,
                source_file=spec.source_file,
                description=spec.description,
                dependencies=spec.dependencies,
                optional_dependencies=spec.optional_dependencies,
                declared_commands=spec.declared_commands,
                declared_menus=spec.declared_menus,
                status=spec.status,
                display_status=spec.display_status,
                is_complete=spec.is_complete,
                incomplete_reasons=spec.incomplete_reasons,
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


class FeatureMenusSheet(Sheet):
    rowtype = 'menus'
    columns = [
        ItemColumn('menupath', width=60),
    ]
    nKeys = 1

    def iterload(self):
        spec = vd.featureRegistry.get(self.source.name)
        if spec:
            for menupath in spec.menus_registered:
                yield AttrDict(menupath=menupath)

    def reload(self):
        self.rows = list(self.iterload())


FeaturesSheet.addCommand('zEnter', 'open-feature-menus', 'vd.push(FeatureMenusSheet(cursorRow.name + "_menus", source=cursorRow))', 'open list of menus registered by this feature')

BaseSheet.addCommand(None, 'open-features', 'vd.push(FeaturesSheet("features"))', 'open Features Sheet to view and manage features')

vd.addMenuItems('''
    System > Features Sheet > open-features
''')

vd.addGlobals(FeatureRegistry=FeatureRegistry, FeatureSpec=FeatureSpec, FeaturesSheet=FeaturesSheet, FeatureCommandsSheet=FeatureCommandsSheet, FeatureMenusSheet=FeatureMenusSheet)
