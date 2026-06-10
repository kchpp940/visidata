#!/usr/bin/env python3
"""
Pre-release consistency checker for VisiData.

Verifies that version numbers, module imports, CLI entry points,
documentation, and packaging metadata are all in sync.

Usage:
    python3 dev/pre_release_check.py [--verbose] [--strict]
    dev/pre_release_check.py [--verbose] [--strict]

Exit codes:
    0 - all checks passed
    1 - one or more checks failed
"""

import os
import sys
import ast
import re
import importlib
import importlib.util
import pkgutil
from pathlib import Path
from typing import List, Tuple, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERBOSE = False
STRICT = False


class CheckResult:
    def __init__(self, name: str):
        self.name = name
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.passed = True

    def error(self, msg: str):
        self.errors.append(msg)
        self.passed = False

    def warn(self, msg: str):
        self.warnings.append(msg)

    def ok(self, msg: str):
        if VERBOSE:
            print(f"  ✓ {msg}")

    def print_summary(self):
        status = "PASS" if self.passed else "FAIL"
        icon = "✓" if self.passed else "✗"
        print(f"\n{icon} [{status}] {self.name}")

        if self.errors:
            for e in self.errors:
                print(f"    ERROR: {e}")

        if self.warnings:
            for w in self.warnings:
                print(f"    WARN:  {w}")

        if not self.errors and not self.warnings and VERBOSE:
            print("    All checks passed")


def read_file(path: Path) -> str:
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def extract_version_from_file(path: Path) -> Optional[str]:
    """Extract __version__ string from a Python file using AST."""
    try:
        source = read_file(path)
        tree = ast.parse(source, filename=str(path))

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == '__version__':
                        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                            return node.value.value
    except (SyntaxError, FileNotFoundError):
        pass
    return None


def check_versions() -> CheckResult:
    """Check that version numbers are consistent across all source files."""
    result = CheckResult("Version consistency")

    version_sources = {
        'visidata/__init__.py': PROJECT_ROOT / 'visidata' / '__init__.py',
        'visidata/main.py': PROJECT_ROOT / 'visidata' / 'main.py',
        'setup.py': PROJECT_ROOT / 'setup.py',
    }

    versions = {}
    for name, path in version_sources.items():
        ver = extract_version_from_file(path)
        if ver:
            versions[name] = ver
            result.ok(f"{name}: {ver}")
        else:
            result.error(f"Could not extract __version__ from {name}")

    if len(set(versions.values())) > 1:
        result.error(
            f"Version mismatch detected: " +
            ", ".join(f"{k}={v}" for k, v in versions.items())
        )
    elif versions:
        canonical = list(versions.values())[0]
        result.ok(f"All sources agree: {canonical}")

    return result


def discover_python_packages(base_path: Path, base_pkg: str = '') -> List[str]:
    """Discover all Python packages under base_path."""
    packages = []
    if base_pkg:
        packages.append(base_pkg)

    if not base_path.is_dir():
        return packages

    for item in sorted(base_path.iterdir()):
        if item.is_dir():
            init_file = item / '__init__.py'
            if init_file.exists():
                sub_pkg = f"{base_pkg}.{item.name}" if base_pkg else item.name
                packages.extend(discover_python_packages(item, sub_pkg))

    return packages


def discover_feature_modules() -> List[str]:
    """Discover all feature modules in visidata/features/."""
    features_dir = PROJECT_ROOT / 'visidata' / 'features'
    modules = []
    if features_dir.is_dir():
        for f in sorted(features_dir.iterdir()):
            if f.suffix == '.py' and f.name != '__init__.py':
                modules.append(f"visidata.features.{f.stem}")
    return modules


def discover_loader_modules() -> List[str]:
    """Discover all loader modules in visidata/loaders/."""
    loaders_dir = PROJECT_ROOT / 'visidata' / 'loaders'
    modules = []
    if loaders_dir.is_dir():
        for f in sorted(loaders_dir.iterdir()):
            if f.suffix == '.py' and f.name != '__init__.py':
                modules.append(f"visidata.loaders.{f.stem}")
    return modules


def check_module_imports() -> CheckResult:
    """Check that all submodules can be imported and are listed in setup.py."""
    result = CheckResult("Module import & packaging")

    sys.path.insert(0, str(PROJECT_ROOT))

    features_dir = PROJECT_ROOT / 'visidata' / 'features'
    loaders_dir = PROJECT_ROOT / 'visidata' / 'loaders'

    feature_modules = discover_feature_modules()
    loader_modules = discover_loader_modules()

    result.ok(f"Found {len(feature_modules)} feature modules")
    result.ok(f"Found {len(loader_modules)} loader modules")

    import_failures = []
    for mod_name in feature_modules + loader_modules:
        try:
            spec = importlib.util.find_spec(mod_name)
            if spec is None:
                import_failures.append(f"{mod_name}: module not found")
                continue
        except Exception as e:
            import_failures.append(f"{mod_name}: {e}")

    if import_failures:
        for fail in import_failures:
            result.error(f"Import failure: {fail}")
    else:
        result.ok("All feature/loader modules discoverable")

    setup_path = PROJECT_ROOT / 'setup.py'
    setup_source = read_file(setup_path)

    packages_match = re.search(r'packages=\[(.*?)\]', setup_source, re.DOTALL)
    if packages_match:
        packages_str = packages_match.group(1)
        setup_packages = re.findall(r'"([^"]+)"', packages_str)

        actual_packages = discover_python_packages(PROJECT_ROOT / 'visidata', 'visidata')

        pkg_data_match = re.search(
            r'package_data=\{(.*?)\}',
            setup_source,
            re.DOTALL
        )
        pkg_data_pkgs = set()
        if pkg_data_match:
            pkg_data_str = pkg_data_match.group(1)
            pkg_data_pkgs = set(re.findall(r'"([^"]+)"\s*:', pkg_data_str))

        missing_from_setup = [p for p in actual_packages if p not in setup_packages]
        extra_in_setup = [p for p in setup_packages if p not in actual_packages]

        data_packages = [p for p in extra_in_setup if p in pkg_data_pkgs]
        truly_extra = [p for p in extra_in_setup if p not in pkg_data_pkgs]

        if missing_from_setup:
            result.error(
                f"Packages missing from setup.py packages list: {', '.join(missing_from_setup)}"
            )
        if data_packages:
            result.ok(
                f"Data packages in setup.py (no __init__.py but have package_data): {', '.join(data_packages)}"
            )
        if truly_extra:
            result.warn(
                f"Extra packages in setup.py without package_data: {', '.join(truly_extra)}"
            )

        if not missing_from_setup and not truly_extra:
            result.ok(f"setup.py packages consistent: {len(actual_packages)} real packages, {len(data_packages)} data packages")
    else:
        result.error("Could not parse packages list from setup.py")

    return result


def check_cli_entry_points() -> CheckResult:
    """Check that CLI entry points are consistent."""
    result = CheckResult("CLI entry points")

    setup_path = PROJECT_ROOT / 'setup.py'
    setup_source = read_file(setup_path)

    entry_points_match = re.search(
        r'"console_scripts":\s*\[(.*?)\]',
        setup_source,
        re.DOTALL
    )

    if entry_points_match:
        eps_str = entry_points_match.group(1)
        entry_points = re.findall(r'"([^"]+)"', eps_str)
        result.ok(f"setup.py entry_points: {', '.join(entry_points)}")

        for ep in entry_points:
            if '=' in ep:
                name, target = ep.split('=', 1)
                name = name.strip()
                target = target.strip()

                module_path, func_name = target.rsplit(':', 1)
                try:
                    mod = importlib.import_module(module_path)
                    if hasattr(mod, func_name):
                        result.ok(f"Entry point '{name}' -> {target} resolves")
                    else:
                        result.error(f"Entry point '{name}' target {target}: function not found")
                except ImportError as e:
                    result.error(f"Entry point '{name}' target {target}: import error: {e}")
    else:
        result.error("Could not parse console_scripts from setup.py")

    bin_vd = PROJECT_ROOT / 'bin' / 'vd'
    if bin_vd.exists():
        if os.access(bin_vd, os.X_OK):
            result.ok("bin/vd exists and is executable")
        else:
            result.warn("bin/vd exists but is not executable")
    else:
        result.warn("bin/vd not found")

    main_py = PROJECT_ROOT / 'visidata' / 'main.py'
    main_source = read_file(main_py)
    if 'def vd_cli()' in main_source:
        result.ok("vd_cli() function found in visidata.main")
    else:
        result.error("vd_cli() function not found in visidata.main")

    if '__main__.py' in os.listdir(PROJECT_ROOT / 'visidata'):
        main_module = PROJECT_ROOT / 'visidata' / '__main__.py'
        main_module_source = read_file(main_module)
        if 'vd_cli' in main_module_source or 'main' in main_module_source:
            result.ok("visidata.__main__ has entry point")
        else:
            result.warn("visidata/__main__.py may not have proper entry point")

    return result


def check_manpage() -> CheckResult:
    """Check manpage generation and consistency."""
    result = CheckResult("Manpage & help documentation")

    man_dir = PROJECT_ROOT / 'visidata' / 'man'

    required_sources = ['vd.inc', 'parse_options.py']
    for f in required_sources:
        path = man_dir / f
        if path.exists():
            result.ok(f"{f} exists")
        else:
            result.error(f"Manpage source missing: visidata/man/{f}")

    generated_files = ['vd.1', 'visidata.1', 'vd.txt']
    generated_count = 0
    for f in generated_files:
        path = man_dir / f
        if path.exists():
            generated_count += 1
            result.ok(f"{f} exists (generated)")
        else:
            result.warn(f"Generated manpage file missing: visidata/man/{f} (run `make man`)")

    if generated_count == 0:
        result.warn("No generated manpage files found. Run `make man` before release.")

    docs_man_md = PROJECT_ROOT / 'docs' / 'man.md'
    if docs_man_md.exists():
        result.ok("docs/man.md exists")
    else:
        result.warn("docs/man.md not found (generated by mkman.sh)")

    help_py = PROJECT_ROOT / 'visidata' / 'help.py'
    if help_py.exists():
        help_source = read_file(help_py)
        if 'openManPage' in help_source:
            result.ok("help.py has man page integration")
        if 'HelpSheet' in help_source:
            result.ok("help.py has HelpSheet class")
    else:
        result.error("visidata/help.py not found")

    main_path = PROJECT_ROOT / 'visidata' / 'main.py'
    main_source = read_file(main_path)
    if 'man/vd.txt' in main_source or 'manpath' in main_source:
        result.ok("main.py references manpage for --help")
    else:
        result.warn("main.py may not use manpage for --help output")

    return result


def check_manifest_and_package_data() -> CheckResult:
    """Check MANIFEST.in and package_data consistency."""
    result = CheckResult("MANIFEST.in & package_data")

    manifest_path = PROJECT_ROOT / 'MANIFEST.in'
    if not manifest_path.exists():
        result.error("MANIFEST.in not found")
        return result

    manifest_source = read_file(manifest_path)
    manifest_entries = []
    for line in manifest_source.strip().split('\n'):
        line = line.strip()
        if line and not line.startswith('#'):
            manifest_entries.append(line)

    result.ok(f"MANIFEST.in has {len(manifest_entries)} entries")

    setup_path = PROJECT_ROOT / 'setup.py'
    setup_source = read_file(setup_path)

    pkg_data_match = re.search(
        r'package_data=\{(.*?)\}',
        setup_source,
        re.DOTALL
    )

    if pkg_data_match:
        pkg_data_str = pkg_data_match.group(1)
        result.ok("setup.py has package_data")
    else:
        result.warn("Could not parse package_data from setup.py")

    data_files_match = re.search(
        r'data_files=\[(.*?)\]',
        setup_source,
        re.DOTALL
    )

    if data_files_match:
        result.ok("setup.py has data_files")
    else:
        result.warn("Could not parse data_files from setup.py")

    man_files = ['vd.1', 'visidata.1', 'vd.txt']
    for f in man_files:
        man_path = f'visidata/man/{f}'
        in_manifest = f'include {man_path}' in manifest_source or man_path in manifest_source
        in_pkg_data = f'"{f}"' in pkg_data_match.group(1) if pkg_data_match else False

        if in_manifest:
            result.ok(f"{man_path} in MANIFEST.in")
        else:
            result.warn(f"{man_path} not found in MANIFEST.in")

        if in_pkg_data:
            result.ok(f"visidata.man includes {f} in package_data")
        else:
            result.warn(f"{f} not in package_data for visidata.man")

    guides_dir = PROJECT_ROOT / 'visidata' / 'guides'
    if guides_dir.is_dir():
        guide_files = list(guides_dir.glob('*.md'))
        if guide_files:
            result.ok(f"Found {len(guide_files)} guide files in visidata/guides/")
            if '"visidata": ["guides/*.md"]' in setup_source or "'visidata': ['guides/*.md']" in setup_source:
                result.ok("guides/*.md included in package_data")
            else:
                result.warn("guides/*.md may not be in package_data")
        else:
            result.warn("No guide files found in visidata/guides/")

    return result


def check_internal_formats_doc() -> CheckResult:
    """Check internal formats documentation consistency."""
    result = CheckResult("Internal formats documentation")

    docs_path = PROJECT_ROOT / 'docs' / 'internal_formats.md'
    if not docs_path.exists():
        result.error("docs/internal_formats.md not found")
        return result

    doc_source = read_file(docs_path)
    result.ok("docs/internal_formats.md exists")

    formats_mentioned = []
    for fmt in ['.vd', '.vdj', '.vdx', '.vds']:
        if fmt in doc_source:
            formats_mentioned.append(fmt)

    result.ok(f"Formats documented: {', '.join(formats_mentioned)}")

    loaders_dir = PROJECT_ROOT / 'visidata' / 'loaders'
    vdx_loader = loaders_dir / 'vdx.py'
    vds_loader = loaders_dir / 'vds.py'

    if vdx_loader.exists():
        result.ok("visidata/loaders/vdx.py exists")
    else:
        result.error("visidata/loaders/vdx.py not found")

    if vds_loader.exists():
        result.ok("visidata/loaders/vds.py exists")
    else:
        result.warn("visidata/loaders/vds.py not found")

    return result


def check_requirements() -> CheckResult:
    """Check requirements consistency."""
    result = CheckResult("Requirements & dependencies")

    req_txt = PROJECT_ROOT / 'requirements.txt'
    if req_txt.exists():
        reqs = []
        with open(req_txt) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and not line.startswith('-'):
                    if '#' in line:
                        line = line.split('#')[0].strip()
                    if line:
                        reqs.append(line)
        result.ok(f"requirements.txt has {len(reqs)} dependencies")
    else:
        result.warn("requirements.txt not found")

    setup_path = PROJECT_ROOT / 'setup.py'
    setup_source = read_file(setup_path)

    if 'install_requires' in setup_source:
        result.ok("setup.py has install_requires")
    else:
        result.error("setup.py missing install_requires")

    if 'extras_require' in setup_source:
        result.ok("setup.py has extras_require")
    else:
        result.warn("setup.py missing extras_require")

    return result


def check_dev_scripts() -> CheckResult:
    """Check that development scripts are properly linked."""
    result = CheckResult("Dev scripts & documentation")

    dev_dir = PROJECT_ROOT / 'dev'

    important_scripts = [
        'test.sh',
        'mkman.sh',
        'pre_release_check.py',
    ]

    for script in important_scripts:
        path = dev_dir / script
        if path.exists():
            result.ok(f"dev/{script} exists")
        else:
            result.warn(f"dev/{script} not found")

    release_checklist = dev_dir / 'checklists' / 'release.md'
    if release_checklist.exists():
        result.ok("dev/checklists/release.md exists")
        release_source = read_file(release_checklist)
        if 'pre_release_check' in release_source or 'pre-release' in release_source.lower():
            result.ok("Release checklist references pre-release checks")
        else:
            result.warn("Release checklist may not reference pre-release checks")
    else:
        result.warn("dev/checklists/release.md not found")

    makefile = PROJECT_ROOT / 'Makefile'
    if makefile.exists():
        makefile_source = read_file(makefile)
        if 'pre-release' in makefile_source or 'release-check' in makefile_source:
            result.ok("Makefile has pre-release check target")
        else:
            result.warn("Makefile may not have pre-release check target")
    else:
        result.warn("Makefile not found")

    return result


def run_all_checks() -> Tuple[bool, List[CheckResult]]:
    """Run all pre-release checks and return overall pass/fail."""
    checks = [
        check_versions(),
        check_module_imports(),
        check_cli_entry_points(),
        check_manpage(),
        check_manifest_and_package_data(),
        check_internal_formats_doc(),
        check_requirements(),
        check_dev_scripts(),
    ]

    all_passed = all(c.passed for c in checks)
    return all_passed, checks


def main():
    global VERBOSE, STRICT

    args = sys.argv[1:]
    VERBOSE = '--verbose' in args or '-v' in args
    STRICT = '--strict' in args

    print("=" * 60)
    print("  VisiData Pre-Release Consistency Check")
    print("=" * 60)
    print(f"  Project root: {PROJECT_ROOT}")
    print(f"  Mode: {'strict' if STRICT else 'standard'}{', verbose' if VERBOSE else ''}")
    print()

    all_passed, checks = run_all_checks()

    for check in checks:
        check.print_summary()

    total_errors = sum(len(c.errors) for c in checks)
    total_warnings = sum(len(c.warnings) for c in checks)
    passed_count = sum(1 for c in checks if c.passed)

    print()
    print("=" * 60)
    print(f"  Summary: {passed_count}/{len(checks)} checks passed")
    print(f"  Errors: {total_errors}  Warnings: {total_warnings}")

    if all_passed:
        print("  ✓ All checks PASSED")
        exit_code = 0
    else:
        print("  ✗ Some checks FAILED")
        exit_code = 1

    if STRICT and total_warnings > 0:
        print(f"  Strict mode: {total_warnings} warnings treated as errors")
        exit_code = 1

    print("=" * 60)
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
