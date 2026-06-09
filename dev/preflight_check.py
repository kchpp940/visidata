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
# Version mapping: display version vs PEP 440 package version
#
# Display version (canonical source: visidata/__init__.py):  e.g. "3.4dev"
#   - used in: visidata/__init__.py, visidata/main.py, README.md, manpage
#
# PEP 440 package version (for wheel/sdist metadata):          e.g. "3.4.dev0"
#   - used in: setup.py
#
# Mapping rules:
#   display "X.Ydev"    <->  PEP 440 "X.Y.dev0"
#   display "X.Y"       <->  PEP 440 "X.Y"        (no dev suffix, same string)
#   display "X.Y.Z"     <->  PEP 440 "X.Y.Z"      (same)
# ---------------------------------------------------------------------------

DISPLAY_VERSION_SOURCES: Dict[str, Tuple[Path, str]] = {
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

PEP440_VERSION_SOURCES: Dict[str, Tuple[Path, str]] = {
    "setup.py": (
        ROOT / "setup.py",
        r'__version__\s*=\s*["\']([^"\']+)["\']'
    ),
}

CANONICAL_VERSION_KEY = "visidata/__init__.py"


def display_to_pep440(display_ver: str) -> str:
    """Convert a display version like '3.4dev' to PEP 440 '3.4.dev0'."""
    if display_ver.endswith("dev"):
        return display_ver[:-3] + ".dev0"
    return display_ver


def pep440_to_display(pep440_ver: str) -> str:
    """Convert a PEP 440 version like '3.4.dev0' to display '3.4dev'."""
    if pep440_ver.endswith(".dev0"):
        return pep440_ver[:-5] + "dev"
    return pep440_ver


def _versions_equivalent(v1: str, v2: str) -> bool:
    """Return True if two version strings are equivalent under display/PEP440 mapping."""
    # Normalize both to display form for comparison
    def to_display(v: str) -> str:
        if v.endswith(".dev0"):
            return v[:-5] + "dev"
        return v
    return to_display(v1) == to_display(v2)


class FixResult:
    OK = "ok"
    DONE = "done"
    WARN = "warn"
    FAIL = "fail"

    def __init__(self, name: str, status: str, message: str = "",
                 changed: List[str] = None, skipped: List[str] = None):
        self.name = name
        self.status = status
        self.message = message
        self.changed = changed or []
        self.skipped = skipped or []

    @property
    def ok(self) -> bool:
        """Treat WARN as non-fatal so preflight-fix can continue with other fixers."""
        return self.status in (self.OK, self.DONE, self.WARN)

    def __str__(self) -> str:
        label = self.status.upper()
        lines = [f"[{label}] {self.name}"]
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


# ===========================================================================
# Fixers
# ===========================================================================

def fix_version_numbers() -> FixResult:
    """Sync version numbers: display versions from canonical source, PEP 440 for setup.py.

    Canonical source: visidata/__init__.py (display version like "3.4dev")
      -> visidata/main.py, README.md get the same display version
      -> setup.py gets PEP 440 mapped version like "3.4.dev0"
    """
    changed: List[str] = []
    skipped: List[str] = []

    canon_path, canon_pattern = DISPLAY_VERSION_SOURCES[CANONICAL_VERSION_KEY]
    canon_display = _read_version_from_file(canon_path, canon_pattern)
    if not canon_display:
        return FixResult("fix-version", FixResult.FAIL,
                        f"Could not read canonical version from {CANONICAL_VERSION_KEY}")

    canon_pep440 = display_to_pep440(canon_display)

    # Sync display-version files
    for name, (path, pattern) in DISPLAY_VERSION_SOURCES.items():
        if name == CANONICAL_VERSION_KEY:
            continue
        if not path.exists():
            skipped.append(f"{name}: {path} does not exist")
            continue
        current = _read_version_from_file(path, pattern)
        if current == canon_display:
            skipped.append(f"{name}: already at display version {canon_display}")
            continue
        content = path.read_text(encoding="utf-8")
        if name == "README.md":
            new_content = re.sub(
                r"# VisiData v[\w.]+",
                f"# VisiData v{canon_display}",
                content
            )
        else:
            new_content = re.sub(
                r"(__version__\s*=\s*['\"])[^'\"]+(['\"])",
                rf"\g<1>{canon_display}\g<2>",
                content
            )
        if new_content != content:
            path.write_text(new_content, encoding="utf-8")
            changed.append(f"{name}: display version -> {canon_display}")
        else:
            skipped.append(f"{name}: already at display version {canon_display}")

    # Sync PEP 440 version files (setup.py)
    for name, (path, pattern) in PEP440_VERSION_SOURCES.items():
        if not path.exists():
            skipped.append(f"{name}: {path} does not exist")
            continue
        current = _read_version_from_file(path, pattern)
        if current == canon_pep440:
            skipped.append(f"{name}: already at PEP 440 version {canon_pep440}")
            continue
        content = path.read_text(encoding="utf-8")
        new_content = re.sub(
            r'(__version__\s*=\s*["\'][^"\']+["\'])',
            f'__version__ = "{canon_pep440}"',
            content
        )
        if new_content != content:
            path.write_text(new_content, encoding="utf-8")
            changed.append(f"{name}: PEP 440 version -> {canon_pep440}")
        else:
            skipped.append(f"{name}: already at PEP 440 version {canon_pep440}")

    if changed:
        return FixResult("fix-version", FixResult.DONE,
                       f"Synced {len(changed)} file(s) (display={canon_display}, PEP 440={canon_pep440})",
                       changed=changed, skipped=skipped)
    return FixResult("fix-version", FixResult.OK,
                   f"All files in sync (display={canon_display}, PEP 440={canon_pep440})",
                   skipped=skipped)


def fix_manpage_date() -> FixResult:
    """Update the .Dd date line in visidata/man/vd.inc to today."""
    changed: List[str] = []
    vd_inc = MAN_DIR / "vd.inc"
    if not vd_inc.exists():
        return FixResult("fix-date", FixResult.FAIL, f"{vd_inc} does not exist")

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
        return FixResult("fix-date", FixResult.DONE,
                       f"Updated manpage date to {today}",
                       changed=[f"visidata/man/vd.inc: .Dd {today}"])
    return FixResult("fix-date", FixResult.OK, "Manpage date already current")


def _find_system_tool(name: str) -> Optional[str]:
    """Return full path to tool if available on PATH, else None."""
    return shutil.which(name)


def fix_docs() -> FixResult:
    """Rebuild manpages via dev/mkman.sh if system tools are available.

    Missing tools produce a WARN (non-fatal) so other fixers can still run;
    the check phase will fail explicitly if manpage artifacts are absent.
    """
    changed: List[str] = []
    skipped: List[str] = []

    mkman = ROOT / "dev" / "mkman.sh"
    if not mkman.exists():
        return FixResult("fix-docs", FixResult.FAIL, f"{mkman} does not exist")

    required_tools = ["soelim", "preconv"]
    optional_tools = ["man", "aha"]

    missing_req = [t for t in required_tools if not _find_system_tool(t)]
    missing_opt = [t for t in optional_tools if not _find_system_tool(t)]

    if missing_req:
        hints = []
        if missing_opt:
            hints.append(f"Optional tools also missing: {', '.join(missing_opt)}")
        if "preconv" in missing_req:
            hints.append("Install groff:  brew install groff")
        if "aha" in missing_opt:
            hints.append("Install aha:    brew install aha")
        hints.append("Manpage generation skipped (non-fatal); docs check will flag missing artifacts.")
        # WARN = non-fatal: allow preflight-fix to continue so version/date updates still apply
        return FixResult(
            "fix-docs", FixResult.WARN,
            f"Skipped: missing required tools {', '.join(missing_req)}",
            skipped=hints
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
            return FixResult("fix-docs", FixResult.WARN,
                           f"mkman.sh exited {result.returncode} (non-fatal; docs check will flag)",
                           skipped=err.splitlines()[-5:] if err else [])
        changed.append("Ran dev/mkman.sh successfully")
        for f in ["vd.1", "visidata.1", "vd.txt"]:
            p = MAN_DIR / f
            if p.exists():
                changed.append(f"Generated visidata/man/{f} ({p.stat().st_size} bytes)")
        docs_man = ROOT / "docs" / "man.md"
        if docs_man.exists():
            changed.append(f"Generated docs/man.md ({docs_man.stat().st_size} bytes)")
        return FixResult("fix-docs", FixResult.DONE,
                       "Manpages rebuilt",
                       changed=changed, skipped=skipped)
    except Exception as e:
        return FixResult("fix-docs", FixResult.WARN, str(e),
                        skipped=["Manpage build raised exception (non-fatal); docs check will flag missing artifacts"])


def fix_build() -> FixResult:
    """Build wheel and sdist into dist/ via `python3 -m build`.

    Clears any pre-existing dist/ artifacts first.
    Requires the `build` package (`pip install build`).
    """
    changed: List[str] = []
    skipped: List[str] = []

    dist_dir = ROOT / "dist"
    if dist_dir.exists():
        import shutil as _shutil
        for old in dist_dir.iterdir():
            if old.is_file():
                old.unlink()
        skipped.append("Cleared pre-existing dist/ contents")

    if not _find_system_tool("python3"):
        return FixResult("fix-build", FixResult.FAIL, "python3 not found on PATH")

    try:
        import build  # noqa: F401
    except ImportError:
        return FixResult(
            "fix-build", FixResult.WARN,
            "Package `build` not installed; skipping dist build",
            skipped=["Install with:  pip install build",
                     "Smoke/package checks will be skipped if dist/ is empty"]
        )

    try:
        result = subprocess.run(
            ["python3", "-m", "build", "--outdir", str(dist_dir)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            last_lines = err.splitlines()[-10:] if err else []
            return FixResult("fix-build", FixResult.FAIL,
                           f"`python3 -m build` exited {result.returncode}",
                           skipped=last_lines)
        for artifact in sorted(dist_dir.glob("*.whl")) + sorted(dist_dir.glob("*.tar.gz")):
            changed.append(f"Built {artifact.name} ({artifact.stat().st_size} bytes)")
        if not changed:
            return FixResult("fix-build", FixResult.FAIL,
                           "Build succeeded but no .whl / .tar.gz found in dist/")
        return FixResult("fix-build", FixResult.DONE,
                       f"Built {len(changed)} artifact(s) in dist/",
                       changed=changed, skipped=skipped)
    except Exception as e:
        return FixResult("fix-build", FixResult.FAIL, str(e))


FIXERS: Dict[str, Callable[[], FixResult]] = {
    "fix-version": fix_version_numbers,
    "fix-date": fix_manpage_date,
    "fix-docs": fix_docs,
    "fix-build": fix_build,
}


# ===========================================================================
# Checkers
# ===========================================================================

def check_version_consistency() -> CheckResult:
    """Verify version numbers are consistent, respecting display vs PEP 440 mapping.

    All display-version files should share the same string;
    all PEP 440 files should share the same string;
    and the two strings should map to each other via display<->PEP440 rules.
    """
    display_versions: Dict[str, str] = {}
    pep440_versions: Dict[str, str] = {}
    missing: List[str] = []

    for name, (path, pattern) in DISPLAY_VERSION_SOURCES.items():
        v = _read_version_from_file(path, pattern)
        if v is None:
            missing.append(f"{name}: could not extract version from {path}")
        else:
            display_versions[name] = v

    for name, (path, pattern) in PEP440_VERSION_SOURCES.items():
        v = _read_version_from_file(path, pattern)
        if v is None:
            missing.append(f"{name}: could not extract version from {path}")
        else:
            pep440_versions[name] = v

    if missing:
        return CheckResult("version", False,
                           "Could not read all version sources", missing)

    errors: List[str] = []
    details: List[str] = []

    unique_display = set(display_versions.values())
    if len(unique_display) != 1:
        errors.append(f"Display-version files disagree: {unique_display}")
    else:
        details.append(f"Display version: {list(unique_display)[0]} (in {', '.join(sorted(display_versions))})")

    unique_pep440 = set(pep440_versions.values())
    if len(unique_pep440) != 1:
        errors.append(f"PEP 440-version files disagree: {unique_pep440}")
    else:
        details.append(f"PEP 440 version: {list(unique_pep440)[0]} (in {', '.join(sorted(pep440_versions))})")

    if not errors:
        display_v = list(unique_display)[0]
        pep440_v = list(unique_pep440)[0]
        if not _versions_equivalent(display_v, pep440_v):
            errors.append(
                f"Display '{display_v}' does not map to PEP 440 '{pep440_v}' "
                f"(expected PEP 440 '{display_to_pep440(display_v)}' or display '{pep440_to_display(pep440_v)}')"
            )

    if errors:
        all_versions = {**display_versions, **pep440_versions}
        details = [f"{k}: {v}" for k, v in sorted(all_versions.items())] + details
        return CheckResult(
            "version", False,
            f"Version inconsistency; run --fix-version to sync from canonical {CANONICAL_VERSION_KEY}",
            errors + details
        )

    return CheckResult(
        "version", True,
        f"Display={list(unique_display)[0]}  PEP 440={list(unique_pep440)[0]} (mapping verified)",
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


def _find_latest_artifact(pattern: str) -> Optional[Path]:
    """Return the most recently modified file matching pattern in dist/."""
    dist = ROOT / "dist"
    if not dist.exists():
        return None
    candidates = sorted(dist.glob(pattern))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


_WHEEL_WHITELIST: List[str] = [
    "visidata/__init__.py",
    "visidata/main.py",
    "visidata/features/__init__.py",
    "visidata/loaders/__init__.py",
    "visidata/themes/__init__.py",
    "visidata/ddw/input.ddw",
    "visidata/ddw/regex.ddw",
    "visidata/desktop/visidata.desktop",
    "visidata/desktop/org.visidata.VisiData.metainfo.xml",
    "visidata/desktop/icons/32x32/visidata.png",
    "visidata/desktop/icons/48x48/visidata.png",
]


def check_package_contents() -> CheckResult:
    """Inspect the built wheel and sdist for expected files and metadata.

    Verifies:
      - wheel METADATA Name/Version correct
      - wheel entry_points.txt references vd=visidata.main:vd_cli
      - core modules + package_data are present in the wheel
      - sdist contains expected top-level files (setup.py, README.md, visidata/)
    """
    details: List[str] = []
    errors: List[str] = []

    import zipfile
    import tarfile

    dist_dir = ROOT / "dist"
    if not dist_dir.exists() or not any(dist_dir.glob("*.whl")):
        return CheckResult(
            "package", False,
            "dist/ has no .whl artifacts — run --build first"
        )

    # --- Wheel inspection ---
    wheel = _find_latest_artifact("*.whl")
    if not wheel:
        return CheckResult(
            "package", False,
            "No .whl artifacts in dist/ — run --build first",
        )

    try:
        with zipfile.ZipFile(wheel) as zf:
            names = set(zf.namelist())

        # Check core modules / package_data
        for rel in _WHEEL_WHITELIST:
            matched = any(n.endswith(rel) or n.endswith("/" + rel) for n in names)
            if not matched:
                errors.append(f"Wheel missing: {rel}")

        # Check METADATA
        metadata_files = [n for n in names if n.endswith(".dist-info/METADATA")]
        if not metadata_files:
            errors.append("Wheel missing .dist-info/METADATA")
        else:
            with zipfile.ZipFile(wheel) as zf:
                meta_raw = zf.read(metadata_files[0]).decode("utf-8", errors="replace")
            for line in meta_raw.splitlines():
                if line.startswith("Name:"):
                    details.append(f"Wheel METADATA Name: {line.split(':', 1)[1].strip()}")
                if line.startswith("Version:"):
                    details.append(f"Wheel METADATA Version: {line.split(':', 1)[1].strip()}")

        # Check entry_points.txt
        ep_files = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
        if not ep_files:
            errors.append("Wheel missing .dist-info/entry_points.txt")
        else:
            with zipfile.ZipFile(wheel) as zf:
                ep_raw = zf.read(ep_files[0]).decode("utf-8", errors="replace")
            ep_flat = ep_raw.replace(" ", "")
            if "vd=visidata.main:vd_cli" not in ep_flat:
                errors.append("entry_points.txt missing vd=visidata.main:vd_cli")
            if "visidata=visidata.main:vd_cli" not in ep_flat:
                errors.append("entry_points.txt missing visidata=visidata.main:vd_cli")
            details.append("Wheel entry_points.txt verified")
    except Exception as e:
        errors.append(f"Failed to inspect wheel {wheel.name}: {e}")

    # --- sdist inspection ---
    sdist = _find_latest_artifact("*.tar.gz")
    if not sdist:
        details.append("No .tar.gz sdist found — wheel only")
    else:
        try:
            with tarfile.open(sdist) as tf:
                sdist_names = set(tf.getnames())
            for top in ["setup.py", "README.md", "CHANGELOG.md",
                         "requirements.txt", "visidata/__init__.py"]:
                found = any(n.endswith(top) for n in sdist_names)
                if not found:
                    errors.append(f"Sdist missing top-level: {top}")
            details.append(f"Sdist contains {len(sdist_names)} files")
        except Exception as e:
            errors.append(f"Failed to inspect sdist {sdist.name}: {e}")

    if errors:
        return CheckResult("package", False,
                           f"{len(errors)} package content issue(s)",
                           errors + details)
    return CheckResult("package", True,
                       "Wheel + sdist contents verified",
                       details)


_SMOKE_IMPORTS: List[str] = [
    "visidata",
    "visidata.main",
    "visidata.features.describe",
    "visidata.features.slide",
    "visidata.loaders.vdx",
    "visidata.loaders.tsv",
]


def check_install_smoke() -> CheckResult:
    """Install the built wheel in a temporary venv and smoke-test CLI + imports.

    Creates a throwaway venv under /tmp, pip install -q <wheel>, then:
      - `vd --version` returns the expected version string
      - each module in _SMOKE_IMPORTS imports cleanly
    """
    details: List[str] = []
    errors: List[str] = []

    import tempfile
    import venv as _venv

    wheel = _find_latest_artifact("*.whl")
    if not wheel:
        return CheckResult(
            "smoke", False,
            "No .whl in dist/ — run --build first"
        )

    expected_display = _read_version_from_file(
        VD / "__init__.py",
        r"__version__\s*=\s*['\"]([^'\"]+)['\"]"
    )

    tmpdir = Path(tempfile.mkdtemp(prefix="vd_preflight_smoke_"))
    venv_dir = tmpdir / "venv"

    try:
        _venv.create(venv_dir, with_pip=True, clear=True)
        details.append(f"Created temp venv at {venv_dir}")

        py = venv_dir / "bin" / "python3"
        if not py.exists():
            py = venv_dir / "Scripts" / "python.exe"

        # Install the wheel
        result = subprocess.run(
            [str(py), "-m", "pip", "install", "--quiet", str(wheel)],
            capture_output=True, text=True)
        if result.returncode != 0:
            err = ((result.stderr or result.stdout) or "").strip()[-300:]
            errors.append(f"pip install failed: {err}")
            return CheckResult("smoke", False,
                               "Failed to install wheel in venv",
                               errors + details)
        details.append(f"Installed {wheel.name} in venv")

        # --- Run `vd --version` ---
        vd_bin = venv_dir / "bin" / "vd"
        if not vd_bin.exists():
            vd_bin = venv_dir / "Scripts" / "vd.exe"
        result = subprocess.run(
            [str(vd_bin), "--version"],
            capture_output=True, text=True)
        out = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            errors.append(f"`vd --version` exited {result.returncode}: {out[:300]}")
        else:
            details.append(f"`vd --version` output: {out}")
            if expected_display and expected_display not in out:
                errors.append(
                    f"`vd --version` output does not contain expected version '{expected_display}'"
                )

        # --- Feature imports ---
        for mod in _SMOKE_IMPORTS:
            result = subprocess.run(
                [str(py), "-c", f"import {mod}; print(repr({mod}) + ' imported OK')"],
                capture_output=True, text=True)
            if result.returncode != 0:
                errors.append(f"Import failed: {mod}")
            else:
                details.append(f"import {mod}: OK")

        # --- vd object import ---
        result = subprocess.run(
            [str(py), "-c",
             "from visidata import vd; print('vd.version=' + str(vd.version))"],
            capture_output=True, text=True)
        if result.returncode != 0:
            errors.append("from visidata import vd failed")
        else:
            details.append(f"vd.version = {result.stdout.strip()}")

    finally:
        import shutil as _shutil
        _shutil.rmtree(tmpdir, ignore_errors=True)

    if errors:
        return CheckResult("smoke", False,
                           f"{len(errors)} smoke test failure(s)",
                           errors + details)
    return CheckResult("smoke", True,
                       "Install + CLI + feature imports all OK",
                       details)


CHECKS: Dict[str, Callable[[], CheckResult]] = {
    "version": check_version_consistency,
    "imports": check_module_imports,
    "cli": check_cli_entrypoints,
    "docs": check_documentation,
    "formats": check_internal_formats,
    "metadata": check_packaging_metadata,
    "changelog": check_changelog,
    "package": check_package_contents,
    "smoke": check_install_smoke,
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
  %(prog)s                           run all checks (source-level only)
  %(prog)s --package                 build wheel/sdist, then run ALL checks
  %(prog)s package smoke             run only package + smoke checks (needs dist/)
  %(prog)s --list                    list available check names and fixers
  %(prog)s --fix                     auto-fix version/date/docs, then check
  %(prog)s --fix --build             auto-fix everything AND build dist, then check all
  %(prog)s --fix-version             sync version numbers only
  %(prog)s --fix-date                update manpage date only
  %(prog)s --fix-docs                rebuild manpages only
  %(prog)s --build                   build wheel + sdist into dist/ (slow)
""",
    )
    parser.add_argument("--list", action="store_true",
                        help="list available check names and fixers, then exit")
    parser.add_argument("--fix", action="store_true",
                        help="run fast auto-fix steps (version, date, docs), then run checks")
    parser.add_argument("--build", action="store_true",
                        help="build wheel and sdist into dist/ (runs fix-build)")
    parser.add_argument("--package", action="store_true",
                        help="build dist and run package-level checks (package + smoke + all others)")
    parser.add_argument("--fix-version", action="store_true",
                        help="sync version numbers from canonical visidata/__init__.py")
    parser.add_argument("--fix-date", action="store_true",
                        help="update manpage date in visidata/man/vd.inc to today")
    parser.add_argument("--fix-docs", action="store_true",
                        help="rebuild manpages via dev/mkman.sh")
    parser.add_argument("checks", nargs="*",
                        help="specific check(s) to run (default: all, or all+package/smoke with --package)")

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
    any_fix = (args.fix or args.build or args.fix_version or args.fix_date
               or args.fix_docs or args.package)
    fixers_to_run: List[str] = []

    # --fix runs fast fixers only; --build adds the slow dist builder
    if args.fix:
        fixers_to_run.extend(["fix-version", "fix-date", "fix-docs"])
    else:
        if args.fix_version:
            fixers_to_run.append("fix-version")
        if args.fix_date:
            fixers_to_run.append("fix-date")
        if args.fix_docs:
            fixers_to_run.append("fix-docs")

    # --build or --package triggers the wheel/sdist build
    if args.build or args.package:
        fixers_to_run.append("fix-build")

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

        # Only hard FAIL (not WARN) should abort the fix phase.
        # WARN (e.g. missing manpage tools) allows checks to proceed so the
        # underlying problem is surfaced explicitly by the check stage.
        hard_fails = [r for r in fix_results if r.status == FixResult.FAIL]
        if hard_fails:
            print("=" * 60)
            print(f"{len(hard_fails)} fix step(s) failed hard. Aborting.")
            print("=" * 60)
            return 1

    # --- Check phase ---
    # --package or explicit "package"/"smoke" implies running the dist-based checks.
    if args.package and not args.checks:
        selected = list(CHECKS.keys())
    else:
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
            print("For dist-level validation:  python3 dev/preflight_check.py --package")
            print("Or run individual fixers:")
            print("  --fix-version   sync version numbers")
            print("  --fix-date      update manpage date")
            print("  --fix-docs      rebuild manpages")
            print("  --build         build wheel + sdist into dist/")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
