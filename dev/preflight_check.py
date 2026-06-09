#!/usr/bin/env python3
"""
VisiData Preflight Check & Fix - Release Engineering Entry Point

Performs consistency checks across version numbers, module imports,
CLI entry points, documentation, internal formats, and packaging metadata.
Run before building wheels/sdists or tagging a release.

Usage:
    python3 dev/preflight_check.py              # run all checks
    python3 dev/preflight_check.py --list     # list available check names
    python3 dev/preflight_check.py version      # run specific check(s)

Auto-fix mode (modifies files in-place:
    python3 dev/preflight_check.py --fix                 # run all auto-fix steps, then check
    python3 dev/preflight_check.py --fix-version       # sync version numbers (canonical source: visidata/__init__.py)
    python3 dev/preflight_check.py --fix-date          # update manpage date in visidata/man/vd.inc
    python3 dev/preflight_check.py --fix-docs          # rebuild manpages via dev/mkman.sh
"""

import argparse
import ast
import datetime
import importlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent.parent
VD = ROOT / "visidata"
MAN_DIR = VD / "man"


# ---------------------------------------------------------------------------
# Canonical version source: visidata/__init__.py
# ---------------------------------------------------------------------------

VERSION_SOURCES: Dict[str, Tuple[Path, str]] = {
    "visidata/__init__.py": (
        VD / "__init__.py",
        r"__version__\s*=\s*['\"]([^'\"]+)['\"]"
    ),
    "visidata/main.py": (
        VD / "main.py",
        r"__version__\s*=\s*['\"]([^'\"]+)['\"]"
    ),
    "setup.py": (
        ROOT / "setup.py",
        r'__version__\s*=\s*["\']([^"\']+)["\']'
    ),
    "README.md": (
        ROOT / "README.md",
        r"# VisiData v([\w.]+)"
    ),
}

CANONICAL_VERSION_KEY = "visidata/__init__.py"


class FixResult:
    def __init__(self, name: str, ok: bool, message: str = "",
                 changed: List[str] = None, skipped: List[str] = None):
        self.name = name
        self.ok = ok
        self.message = message
        self.changed = changed or []
        self.skipped = skipped or []

    def __str__(self) -> str:
        status = "OK" if self.ok else "FAIL" if not self.changed else "DONE"
        lines = [f"[{status}] {self.name}"]
        if self.message:
            lines.append(f"       {self.message}")
        for c in self.changed:
            lines.append(f"       + {c}")
        for s in self.skipped:
            lines.append(f"       ~ {s}")
        return "\n".join(lines)


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


# ===========================================================================
# Fixers
# ===========================================================================

def fix_version_numbers() -> FixResult:
    """Sync version numbers across all source files from canonical source (visidata/__init__.py)."""
    changed: List[str] = []
    skipped: List[str] = []

    canon_path, canon_pattern = VERSION_SOURCES[CANONICAL_VERSION_KEY]
    canon_version = _read_version_from_file(canon_path, canon_pattern)
    if not canon_version:
        return FixResult("fix-version", False,
                        f"Could not read canonical version from {CANONICAL_VERSION_KEY}")

    for name, (path, pattern) in VERSION_SOURCES.items():
        if name == CANONICAL_VERSION_KEY:
            continue
        if not path.exists():
            skipped.append(f"{name}: {path} does not exist")
            continue
        current = _read_version_from_file(path, pattern)
        if current and (name == "setup.py" or _normalize_version(current) == _normalize_version(canon_version)):
            if current == canon_version:
                skipped.append(f"{name}: already at {canon_version}")
                continue
        content = path.read_text(encoding="utf-8")
        if name == "setup.py":
            new_content = re.sub(
                r'(__version__\s*=\s*["\'][^"\']+["\'])',
                f'__version__ = "{canon_version}"',
                content
            )
        elif name == "README.md":
            new_content = re.sub(
                r"# VisiData v[\w.]+",
                f"# VisiData v{canon_version}",
                content
            )
        else:
            new_content = re.sub(
                r"(__version__\s*=\s*['\"])[^'\"]+(['\"])",
                rf"\g<1>{canon_version}\g<2>",
                content
            )
        if new_content != content:
            path.write_text(new_content, encoding="utf-8")
            changed.append(f"{name}: updated to {canon_version}")
        else:
            skipped.append(f"{name}: already at {canon_version}")

    if changed:
        return FixResult("fix-version", True,
                       f"Synced {len(changed)} file(s) to v{canon_version}",
                       changed=changed, skipped=skipped)
    return FixResult("fix-version", True,
                   f"All files already at v{canon_version}",
                   skipped=skipped)


def fix_manpage_date() -> FixResult:
    """Update the .Dd date line in visidata/man/vd.inc to today."""
    changed: List[str] = []
    vd_inc = MAN_DIR / "vd.inc"
    if not vd_inc.exists():
        return FixResult("fix-date", False, f"{vd_inc} does not exist")

    today = datetime.date.today().strftime("%B %d, %Y")
    content = vd_inc.read_text(encoding="utf-8")

    new_content = re.sub(
        r"^\.Dd .*$",
        f".Dd {today}",
        content,
        count=1,
        flags=re.MULTILINE,
    )

    if new_content != content:
        vd_inc.write_text(new_content, encoding="utf-8")
        return FixResult("fix-date", True,
                       f"Updated manpage date to {today}",
                       changed=[f"visidata/man/vd.inc: .Dd {today}"])
    return FixResult("fix-date", True, "Manpage date already current")


def _find_system_tool(name: str) -> Optional[str]:
    """Return full path to tool if available on PATH, else None."""
    return shutil.which(name)


def fix_docs() -> FixResult:
    """Rebuild manpages via dev/mkman.sh if system tools are available."""
    changed: List[str] = []
    skipped: List[str] = []

    mkman = ROOT / "dev" / "mkman.sh"
    if not mkman.exists():
        return FixResult("fix-docs", False, f"{mkman} does not exist")

    required_tools = ["soelim", "preconv"]
    optional_tools = ["man", "aha"]

    missing_req = [t for t in required_tools if not _find_system_tool(t)]
    missing_opt = [t for t in optional_tools if not _find_system_tool(t)]

    if missing_req:
        hints = []
        if "preconv" in missing_req:
            hints.append("Install groff:  brew install groff")
        if "aha" in missing_opt:
            hints.append("Install aha:    brew install aha")
        if missing_opt:
            hints.insert(0, f"Optional tools missing: {', '.join(missing_opt)}")
        return FixResult(
            "fix-docs", False,
            f"Missing required tools: {', '.join(missing_req)}",
            skipped=hints or []
        )

    if missing_opt:
        skipped.append(f"Optional tools unavailable: {', '.join(missing_opt)}")

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'visidata'}"
    env["PATH"] = f"{ROOT / 'bin'}:{env.get('PATH', '')}"

    try:
        result = subprocess.run(
            ["bash", str(mkman)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            return FixResult("fix-docs", False,
                           f"mkman.sh exited {result.returncode}",
                           skipped=err.splitlines()[-5:] if err else [])
        changed.append("Ran dev/mkman.sh successfully")
        for f in ["vd.1", "visidata.1", "vd.txt"]:
            p = MAN_DIR / f
            if p.exists():
                changed.append(f"Generated visidata/man/{f} ({p.stat().st_size} bytes)")
        docs_man = ROOT / "docs" / "man.md"
        if docs_man.exists():
            changed.append(f"Generated docs/man.md ({docs_man.stat().st_size} bytes)")
        return FixResult("fix-docs", True,
                       "Manpages rebuilt",
                       changed=changed, skipped=skipped)
    except Exception as e:
        return FixResult("fix-docs", False, str(e))


FIXERS: Dict[str, Callable[[], FixResult]] = {
    "fix-version": fix_version_numbers,
    "fix-date": fix_manpage_date,
    "fix-docs": fix_docs,
}


# ===========================================================================
# Checkers
# ===========================================================================

def check_version_consistency() -> CheckResult:
    """Verify version numbers are consistent across all source files."""
    versions: Dict[str, str] = {}
    missing: List[str] = []

    for name, (path, pattern) in VERSION_SOURCES.items():
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
    canon = versions.get(CANONICAL_VERSION_KEY, "?")
    return CheckResult(
        "version", False,
        f"Version mismatch (canonical source {CANONICAL_VERSION_KEY}={canon}); run --fix-version to sync",
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

    setup_path = ROOT / "setup.py"
    setup_content = setup_path.read_text(encoding="utf-8")
    m = re.search(r"packages\s*=\s*\[(.*?)\]", setup_content, re.DOTALL)
    if not m:
        return CheckResult("imports", False, "Could not parse packages list from setup.py")
    setup_packages = set(re.findall(r'"([^"]+)"', m.group(1)))

    for item in sorted(VD.iterdir()):
        if item.is_dir() and (item / "__init__.py").exists():
            pkg_name = f"visidata.{item.name}"
            if pkg_name not in setup_packages:
                if pkg_name != "visidata.apps":
                    errors.append(
                        f"{pkg_name}/ exists but is missing from setup.py packages list"
                    )

    total_checked = 0

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

    required_files = ["vd.1", "visidata.1", "vd.txt"]
    for f in required_files:
        p = MAN_DIR / f
        if p.exists():
            details.append(f"{f}: present ({p.stat().st_size} bytes)")
        else:
            errors.append(f"visidata/man/{f} missing - run --fix-docs (or `make man`)")

    docs_man = ROOT / "docs" / "man.md"
    if docs_man.exists():
        details.append(f"docs/man.md: present ({docs_man.stat().st_size} bytes)")
    else:
        errors.append("docs/man.md missing - run --fix-docs (or `make man`)")

    parse_opts = MAN_DIR / "parse_options.py"
    if not parse_opts.exists():
        errors.append("visidata/man/parse_options.py missing")

    vd_inc = MAN_DIR / "vd.inc"
    if vd_inc.exists():
        m = re.search(r"^\.Dd (.*)$", vd_inc.read_text(encoding="utf-8"), re.MULTILINE)
        if m:
            details.append(f"visidata/man/vd.inc: dated {m.group(1).strip()}")
    else:
        errors.append("visidata/man/vd.inc missing")

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
                    errors.append(f"MANIFEST.in: {directive} {pat} matches nothing - run --fix-docs")

    setup_content = (ROOT / "setup.py").read_text(encoding="utf-8")
    m = re.search(r'package_data\s*=\s*\{(.*?)\}\s*,', setup_content, re.DOTALL)
    if m:
        details.append("setup.py package_data block parsed")

    m = re.search(r'data_files\s*=\s*\[(.*?)\]\s*,', setup_content, re.DOTALL)
    if m:
        data_block = m.group(1)
        for f in re.findall(r'\[f for f in \["([^"]+)"', data_block):
            p = ROOT / f
            if not p.exists():
                errors.append(f"setup.py data_files: {f} does not exist - run --fix-docs")
        for f in re.findall(r'\["([^"]+)"\]', data_block):
            if f.startswith("share/"):
                continue
            p = ROOT / f
            if not p.exists():
                errors.append(f"setup.py data_files: {f} does not exist")
        details.append("setup.py data_files block parsed")

    desktop_dir = VD / "desktop"
    for f in ["visidata.desktop", "org.visidata.VisiData.metainfo.xml"]:
        p = desktop_dir / f
        if p.exists():
            details.append(f"visidata/desktop/{f}: present")
        else:
            errors.append(f"visidata/desktop/{f}: missing")

    for size in ["32x32", "48x48"]:
        p = desktop_dir / "icons" / size / "visidata.png"
        if p.exists():
            details.append(f"visidata/desktop/icons/{size}/visidata.png: present")
        else:
            errors.append(f"visidata/desktop/icons/{size}/visidata.png: missing")

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

    v = _read_version_from_file(
        VD / "__init__.py",
        r"__version__\s*=\s*['\"]([^'\"]+)['\"]"
    )
    if not v:
        return CheckResult("changelog", False, "Could not read current version")

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


CHECKS: Dict[str, Callable[[], CheckResult]] = {
    "version": check_version_consistency,
    "imports": check_module_imports,
    "cli": check_cli_entrypoints,
    "docs": check_documentation,
    "formats": check_internal_formats,
    "metadata": check_packaging_metadata,
    "changelog": check_changelog,
}


# ===========================================================================
# Main
# ===========================================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="VisiData Preflight Check & Fix",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s                     run all checks
  %(prog)s --list              list available check names
  %(prog)s version imports      run only version and imports checks
  %(prog)s --fix              run all auto-fix steps then check
  %(prog)s --fix-version      sync version numbers only
  %(prog)s --fix-date         update manpage date only
  %(prog)s --fix-docs         rebuild manpages only
""",
    )
    parser.add_argument("--list", action="store_true",
                        help="list available check names and exit")
    parser.add_argument("--fix", action="store_true",
                        help="run all auto-fix steps, then run checks")
    parser.add_argument("--fix-version", action="store_true",
                        help=f"sync version numbers across files from canonical source")
    parser.add_argument("--fix-date", action="store_true",
                        help="update manpage date in visidata/man/vd.inc to today")
    parser.add_argument("--fix-docs", action="store_true",
                        help="rebuild manpages via dev/mkman.sh")
    parser.add_argument("checks", nargs="*",
                        help="specific check(s) to run (default: all)")

    args = parser.parse_args(argv)

    if args.list:
        print("Available preflight checks:")
        for name in sorted(CHECKS):
            fn = CHECKS[name]
            doc = (fn.__doc__ or "").strip().splitlines()[0]
            print(f"  {name:12s}  {doc}")
        print()
        print("Available auto-fixers:")
        for name in sorted(FIXERS):
            fn = FIXERS[name]
            doc = (fn.__doc__ or "").strip().splitlines()[0]
            print(f"  {name:15s}  {doc}")
        return 0

    # --- Fix phase ---
    any_fix = args.fix or args.fix_version or args.fix_date or args.fix_docs
    fixers_to_run: List[str] = []

    if args.fix:
        fixers_to_run = list(FIXERS.keys())
    else:
        if args.fix_version:
            fixers_to_run.append("fix-version")
        if args.fix_date:
            fixers_to_run.append("fix-date")
        if args.fix_docs:
            fixers_to_run.append("fix-docs")

    if fixers_to_run:
        print("=" * 60)
        print("VisiData Preflight Auto-Fix")
        print(f"Root: {ROOT}")
        print("=" * 60)
        print()

        fix_results: List[FixResult] = []
        for fname in fixers_to_run:
            if fname not in FIXERS:
                continue
            fr = FIXERS[fname]()
            fix_results.append(fr)
            print(str(fr))
            print()

        fix_ok = all(r.ok for r in fix_results)
        if not fix_ok:
            print("=" * 60)
            print("Some fix steps failed. See details above.")
            print("=" * 60)
            return 1

    # --- Check phase ---
    selected = args.checks or list(CHECKS.keys())

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
        if any_fix:
            print("Remaining issues after auto-fix:")
        else:
            print("Issues found. Try:  python3 dev/preflight_check.py --fix")
            print("Or run individual fixers:")
            print("  --fix-version   sync version numbers")
            print("  --fix-date      update manpage date")
            print("  --fix-docs      rebuild manpages")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
