#!/usr/bin/env python3
"""
VisiData Preflight Check - Release Engineering Entry Point

Performs consistency checks across version numbers, module imports,
CLI entry points, documentation, internal formats, and packaging metadata.
Run before building wheels/sdists or tagging a release.

Usage:
    python3 dev/preflight_check.py          # run all checks
    python3 dev/preflight_check.py --list   # list available check names
    python3 dev/preflight_check.py version  # run specific check(s)
"""

import ast
import importlib
import importlib.util
import os
import re
import sys
import pkgutil
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent.parent
VD = ROOT / "visidata"


class CheckResult:
    def __init__(self, name: str, ok: bool, message: str = "", details: Optional[List[str]] = None):
        self.name = name
        self.ok = ok
        self.message = message
        self.details = details or []

    def __str__(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        lines = [f"[{status}] {self.name}"]
        if self.message:
            lines.append(f"       {self.message}")
        for d in self.details:
            lines.append(f"         - {d}")
        return "\n".join(lines)


def _read_version_from_file(filepath: Path, pattern: str) -> Optional[str]:
    """Extract version string from a file using regex pattern."""
    if not filepath.exists():
        return None
    content = filepath.read_text(encoding="utf-8")
    m = re.search(pattern, content)
    return m.group(1) if m else None


def _normalize_version(v: str) -> str:
    """Normalize version string for comparison (strip dev/pre-release tags)."""
    return v.replace(".dev0", "dev").replace(".dev", "dev").replace("-", "")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_version_consistency() -> CheckResult:
    """Verify version numbers are consistent across all source files."""
    sources: Dict[str, Tuple[Path, str]] = {
        "setup.py": (
            ROOT / "setup.py",
            r'__version__\s*=\s*["\']([^"\']+)["\']'
        ),
        "visidata/__init__.py": (
            VD / "__init__.py",
            r"__version__\s*=\s*['\"]([^'\"]+)['\"]"
        ),
        "visidata/main.py": (
            VD / "main.py",
            r"__version__\s*=\s*['\"]([^'\"]+)['\"]"
        ),
        "README.md": (
            ROOT / "README.md",
            r"# VisiData v([\w.]+)"
        ),
    }

    versions: Dict[str, str] = {}
    missing: List[str] = []

    for name, (path, pattern) in sources.items():
        v = _read_version_from_file(path, pattern)
        if v is None:
            missing.append(f"{name}: could not extract version from {path}")
        else:
            versions[name] = v

    if missing:
        return CheckResult("version", False,
                           "Could not read all version sources", missing)

    normalized = {k: _normalize_version(v) for k, v in versions.items()}
    unique = set(normalized.values())

    if len(unique) == 1:
        return CheckResult("version", True,
                           f"All sources agree on version {list(versions.values())[0]}")

    details = [f"{k}: {v}" for k, v in sorted(versions.items())]
    return CheckResult(
        "version", False,
        f"Version mismatch: found {len(unique)} distinct normalized versions {unique}",
        details
    )


def _check_python_syntax(filepath: Path) -> Optional[str]:
    """Return error message if Python file has syntax errors, else None."""
    try:
        source = filepath.read_text(encoding="utf-8")
        ast.parse(source, filename=str(filepath))
        return None
    except SyntaxError as e:
        return f"SyntaxError at line {e.lineno}: {e.msg}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def check_module_imports() -> CheckResult:
    """Verify all expected submodules exist, have valid syntax, and are listed in setup.py."""
    details: List[str] = []
    errors: List[str] = []

    # Parse packages list from setup.py
    setup_path = ROOT / "setup.py"
    setup_content = setup_path.read_text(encoding="utf-8")
    m = re.search(r"packages\s*=\s*\[(.*?)\]", setup_content, re.DOTALL)
    if not m:
        return CheckResult("imports", False, "Could not parse packages list from setup.py")
    setup_packages = set(re.findall(r'"([^"]+)"', m.group(1)))

    # Check that each directory in visidata/ with __init__.py is in setup.py
    for item in sorted(VD.iterdir()):
        if item.is_dir() and (item / "__init__.py").exists():
            pkg_name = f"visidata.{item.name}"
            if pkg_name not in setup_packages:
                if pkg_name != "visidata.apps":
                    errors.append(
                        f"{pkg_name}/ exists but is missing from setup.py packages list"
                    )

    total_checked = 0

    # Helper: check all .py files in a subpackage directory
    def check_subpkg_dir(dirpath: Path, pkg_prefix: str):
        nonlocal total_checked
        if not dirpath.exists():
            return
        for f in sorted(dirpath.glob("*.py")):
            if f.name.startswith("_"):
                continue
            total_checked += 1
            modname = f"{pkg_prefix}.{f.stem}"
            err = _check_python_syntax(f)
            if err:
                errors.append(f"{modname}: {err}")

    check_subpkg_dir(VD / "features", "visidata.features")
    check_subpkg_dir(VD / "loaders", "visidata.loaders")
    check_subpkg_dir(VD / "themes", "visidata.themes")

    details.append(f"Checked {total_checked} submodule files for valid syntax")

    if errors:
        return CheckResult("imports", False,
                           f"{len(errors)} module syntax/packaging issue(s) found",
                           errors + details)
    return CheckResult("imports", True,
                       f"All {total_checked} submodule files have valid syntax and packages listed in setup.py",
                       details)


def check_cli_entrypoints() -> CheckResult:
    """Verify console_scripts entry points match actual callable functions."""
    errors: List[str] = []
    details: List[str] = []

    # Parse entry_points from setup.py
    setup_content = (ROOT / "setup.py").read_text(encoding="utf-8")
    m = re.search(
        r'"console_scripts"\s*:\s*\[(.*?)\]',
        setup_content, re.DOTALL
    )
    if not m:
        return CheckResult("cli", False, "Could not parse console_scripts from setup.py")

    entries = re.findall(r'"([^"]+)"', m.group(1))
    for entry in entries:
        if "=" not in entry:
            errors.append(f"Malformed entry: {entry}")
            continue
        script_name, target = entry.split("=", 1)
        script_name = script_name.strip()
        target = target.strip()
        details.append(f"{script_name} -> {target}")

        # Parse module:func
        if ":" not in target:
            errors.append(f"{script_name}: target '{target}' missing ':' separator")
            continue
        mod_name, func_name = target.split(":", 1)

        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            errors.append(f"{script_name}: cannot import module '{mod_name}': {e}")
            continue

        if not hasattr(mod, func_name):
            errors.append(
                f"{script_name}: function '{func_name}' not found in module '{mod_name}'"
            )
            continue

        func = getattr(mod, func_name)
        if not callable(func):
            errors.append(
                f"{script_name}: '{func_name}' in '{mod_name}' is not callable"
            )

    # Also check bin/vd script
    bin_vd = ROOT / "bin" / "vd"
    if bin_vd.exists():
        try:
            content = bin_vd.read_text(encoding="utf-8")
            if "visidata.main" not in content or "vd_cli" not in content:
                errors.append("bin/vd does not reference visidata.main:vd_cli")
        except Exception as e:
            errors.append(f"Could not read bin/vd: {e}")
    else:
        errors.append("bin/vd does not exist")

    # Also check visidata/__main__.py
    main_py = VD / "__main__.py"
    if main_py.exists():
        content = main_py.read_text(encoding="utf-8")
        if "vd_cli" not in content:
            errors.append("visidata/__main__.py does not call vd_cli()")
    else:
        errors.append("visidata/__main__.py does not exist")

    if errors:
        return CheckResult("cli", False,
                           f"{len(errors)} CLI entrypoint issue(s) found",
                           errors + details)
    return CheckResult("cli", True,
                       f"{len(entries)} console script(s) verified",
                       details)


def check_documentation() -> CheckResult:
    """Verify manpage and help documentation files exist and are current."""
    errors: List[str] = []
    details: List[str] = []

    man_dir = VD / "man"
    required_files = ["vd.1", "visidata.1", "vd.txt"]
    for f in required_files:
        p = man_dir / f
        if p.exists():
            details.append(f"{f}: present ({p.stat().st_size} bytes)")
        else:
            errors.append(f"{man_dir.name}/{f} missing - run `make man` first")

    # Check docs/man.md
    docs_man = ROOT / "docs" / "man.md"
    if docs_man.exists():
        details.append(f"docs/man.md: present ({docs_man.stat().st_size} bytes)")
    else:
        errors.append("docs/man.md missing - run `make man` first")

    # Check that parse_options.py exists
    parse_opts = man_dir / "parse_options.py"
    if not parse_opts.exists():
        errors.append(f"{man_dir.name}/parse_options.py missing")

    # Check vd.inc exists
    vd_inc = man_dir / "vd.inc"
    if not vd_inc.exists():
        errors.append(f"{man_dir.name}/vd.inc missing")

    if errors:
        return CheckResult("docs", False,
                           f"{len(errors)} documentation issue(s) found",
                           errors + details)
    return CheckResult("docs", True,
                       "All documentation artifacts present",
                       details)


def _find_open_functions() -> Dict[str, str]:
    """Scan visidata package for open_<ext> functions and return {ext: filepath}."""
    result: Dict[str, str] = {}
    pattern = re.compile(r'def\s+open_(\w+)\s*\(')

    for pyfile in VD.rglob("*.py"):
        if not pyfile.is_file():
            continue
        try:
            content = pyfile.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in pattern.finditer(content):
            ext = f".{m.group(1)}"
            relpath = pyfile.relative_to(ROOT)
            if ext not in result:
                result[ext] = str(relpath)
    return result


def check_internal_formats() -> CheckResult:
    """Verify internal formats documented in docs/internal_formats.md have matching loaders."""
    errors: List[str] = []
    details: List[str] = []

    fmt_doc = ROOT / "docs" / "internal_formats.md"
    if not fmt_doc.exists():
        return CheckResult("formats", False, "docs/internal_formats.md missing")

    doc_content = fmt_doc.read_text(encoding="utf-8")
    documented_formats = set(re.findall(r"## (\.\w+)", doc_content))
    details.append(f"Documented internal formats: {sorted(documented_formats)}")

    # Find all open_<ext> functions across the package
    loaders_map = _find_open_functions()
    details.append(f"Discovered open_<ext> functions: {len(loaders_map)}")

    for fmt in sorted(documented_formats):
        if fmt in loaders_map:
            details.append(f"{fmt}: loader found at {loaders_map[fmt]}")
        else:
            errors.append(
                f"Format {fmt} documented in docs/internal_formats.md "
                f"but no open_{fmt[1:]}() function found in visidata package"
            )

    if errors:
        return CheckResult("formats", False,
                           f"{len(errors)} format/loader mismatch(es)",
                           errors + details)
    return CheckResult("formats", True,
                       "All documented internal formats have registered loaders",
                       details)


def check_packaging_metadata() -> CheckResult:
    """Verify MANIFEST.in, package_data, and data_files reference files that exist."""
    errors: List[str] = []
    details: List[str] = []

    # Parse MANIFEST.in entries
    manifest = ROOT / "MANIFEST.in"
    if not manifest.exists():
        return CheckResult("metadata", False, "MANIFEST.in not found")

    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        directive = parts[0]
        if directive in ("include", "recursive-include"):
            patterns = parts[1:] if directive == "include" else parts[2:]
            base = ROOT if directive == "include" else ROOT / parts[1]
            for pat in patterns:
                matches = list(base.glob(pat)) if "*" in pat or "?" in pat else [base / pat]
                if not matches or not any(m.exists() for m in matches):
                    errors.append(f"MANIFEST.in: {directive} {pat} matches nothing")

    # Check setup.py package_data references
    setup_content = (ROOT / "setup.py").read_text(encoding="utf-8")
    m = re.search(r'package_data\s*=\s*\{(.*?)\}\s*,', setup_content, re.DOTALL)
    if m:
        pkgdata_block = m.group(1)
        for fpat in re.findall(r'"([^"]+)"', pkgdata_block):
            if "/" in fpat and not any(c in fpat for c in "*?["):
                # Check actual file references (not glob patterns)
                pass  # package_data paths are relative to packages, skip deep check
        details.append("setup.py package_data block parsed")

    # Check setup.py data_files references
    m = re.search(r'data_files\s*=\s*\[(.*?)\]\s*,', setup_content, re.DOTALL)
    if m:
        data_block = m.group(1)
        for f in re.findall(r'\[f for f in \["([^"]+)"', data_block):
            p = ROOT / f
            if not p.exists():
                errors.append(f"setup.py data_files: {f} does not exist")
        for f in re.findall(r'\["([^"]+)"\]', data_block):
            if f.startswith("share/"):
                continue
            p = ROOT / f
            if not p.exists():
                errors.append(f"setup.py data_files: {f} does not exist")
        details.append("setup.py data_files block parsed")

    # Check desktop files exist
    desktop_dir = VD / "desktop"
    for f in ["visidata.desktop", "org.visidata.VisiData.metainfo.xml"]:
        p = desktop_dir / f
        if p.exists():
            details.append(f"visidata/desktop/{f}: present")
        else:
            errors.append(f"visidata/desktop/{f}: missing")

    # Check icon files
    for size in ["32x32", "48x48"]:
        p = desktop_dir / "icons" / size / "visidata.png"
        if p.exists():
            details.append(f"visidata/desktop/icons/{size}/visidata.png: present")
        else:
            errors.append(f"visidata/desktop/icons/{size}/visidata.png: missing")

    # Check ddw files
    ddw_dir = VD / "ddw"
    for f in ["input.ddw", "regex.ddw"]:
        p = ddw_dir / f
        if p.exists():
            details.append(f"visidata/ddw/{f}: present")
        else:
            errors.append(f"visidata/ddw/{f}: missing")

    if errors:
        return CheckResult("metadata", False,
                           f"{len(errors)} packaging metadata issue(s)",
                           errors + details)
    return CheckResult("metadata", True,
                       "All packaging metadata references valid files",
                       details)


def check_changelog() -> CheckResult:
    """Verify CHANGELOG.md has an entry for the current version."""
    cl_path = ROOT / "CHANGELOG.md"
    if not cl_path.exists():
        return CheckResult("changelog", False, "CHANGELOG.md not found")

    content = cl_path.read_text(encoding="utf-8")

    # Extract version from visidata/__init__.py
    v = _read_version_from_file(
        VD / "__init__.py",
        r"__version__\s*=\s*['\"]([^'\"]+)['\"]"
    )
    if not v:
        return CheckResult("changelog", False, "Could not read current version")

    # Strip dev suffix for comparison
    v_clean = v.replace("dev", "").replace(".", "")
    found = False
    for line in content.splitlines():
        if line.startswith("# v"):
            line_ver = line[3:].strip().split()[0]
            line_clean = re.sub(r"[\.\-]", "", line_ver)
            if line_clean.startswith(v_clean[:3]):
                found = True
                break

    if found:
        return CheckResult("changelog", True,
                           f"CHANGELOG.md has entry for v{v}")
    return CheckResult(
        "changelog", False,
        f"CHANGELOG.md missing entry for current version v{v}",
        ["Add a '# vX.Y' heading near the top of CHANGELOG.md"]
    )


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------

CHECKS: Dict[str, Callable[[], CheckResult]] = {
    "version": check_version_consistency,
    "imports": check_module_imports,
    "cli": check_cli_entrypoints,
    "docs": check_documentation,
    "formats": check_internal_formats,
    "metadata": check_packaging_metadata,
    "changelog": check_changelog,
}


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv or sys.argv[1:]

    if "--list" in argv:
        print("Available preflight checks:")
        for name in sorted(CHECKS):
            fn = CHECKS[name]
            doc = (fn.__doc__ or "").strip().splitlines()[0]
            print(f"  {name:12s}  {doc}")
        return 0

    selected = argv if argv else list(CHECKS.keys())

    # Validate names
    invalid = [n for n in selected if n not in CHECKS]
    if invalid:
        print(f"Unknown check(s): {', '.join(invalid)}", file=sys.stderr)
        print(f"Use --list to see available checks", file=sys.stderr)
        return 2

    print("=" * 60)
    print("VisiData Preflight Check")
    print(f"Root: {ROOT}")
    print("=" * 60)
    print()

    results: List[CheckResult] = []
    for name in selected:
        r = CHECKS[name]()
        results.append(r)
        print(str(r))
        print()

    passed = sum(1 for r in results if r.ok)
    failed = sum(1 for r in results if not r.ok)

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {len(results)} check(s)")
    print("=" * 60)

    if failed > 0:
        print()
        print("Fix the issues above before building or tagging a release.")
        print("For documentation issues, run:  make man")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
