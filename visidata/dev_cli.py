#!/usr/bin/env python3
"""
vd-dev: Unified VisiData development command-line interface.

All core development actions are implemented directly in this module in Python;
the legacy shell scripts in dev/ and tests/ are thin compatibility wrappers
that delegate here.

Usage:
    vd-dev <command> [options] [args...]

Commands:
    install     Install package with optional extras (--check to verify)
    test        Run tests (all, golden, unit, vgit, vdsql, smoke, perf, individual)
    build       Build resources (man, zsh, docker, all)
    lint        Run ruff linter
    setup       Setup dev environment (hooks, vscode, all)
    diff-test   Run git-diff-based tests
    clean       Remove generated files
    check       Run comprehensive check (lint + test)
    preflight   Release readiness checks (check, smoke)
    package     Build and verify distribution artifacts (build, verify, clean, all)

Run `vd-dev <command> --help` for command-specific help.
"""

from __future__ import annotations

import argparse
import ast
import configparser
import difflib
import email.parser
import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, NoReturn, Optional

ROOT = Path(__file__).resolve().parent.parent

RESET = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BOLD = "\033[1m"

EXIT_OK = 0
EXIT_ERR = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 124


# ── output helpers ───────────────────────────────────────────────────

def _print_err(msg: str) -> None:
    print(f"{RED}{BOLD}error:{RESET} {msg}", file=sys.stderr)


def _print_warn(msg: str) -> None:
    print(f"{YELLOW}{BOLD}warn:{RESET} {msg}", file=sys.stderr)


def _print_info(msg: str) -> None:
    print(f"{GREEN}{BOLD}info:{RESET} {msg}")


def _colorize(color_spec: str, text: str) -> str:
    """Use git's color config for portability (like run-tests-individually.sh)."""
    try:
        prefix = subprocess.run(
            ["git", "config", "--get-color", "", color_spec],
            capture_output=True, text=True, check=False,
        ).stdout
        reset = subprocess.run(
            ["git", "config", "--get-color", "", "reset"],
            capture_output=True, text=True, check=False,
        ).stdout
        return f"{prefix}{text}{reset}"
    except Exception:
        return text


def _die(msg: str, code: int = EXIT_ERR) -> NoReturn:
    _print_err(msg)
    sys.exit(code)


# ── subprocess helpers ───────────────────────────────────────────────

def _run(cmd, **kwargs) -> int:
    """Run a command, returning exit code. Output is passed through.

    cmd may be a list[str] (preferred) or a str (when shell=True).
    """
    cwd = kwargs.pop("cwd", ROOT)
    echo = kwargs.pop("echo", True)
    if isinstance(cmd, list):
        display = " ".join(cmd)
    else:
        display = cmd
    if echo:
        _print_info(f"$ {display}")
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), **kwargs)
        return proc.returncode
    except FileNotFoundError as e:
        _print_err(f"command not found: {e.filename}")
        return EXIT_ERR
    except KeyboardInterrupt:
        _print_err("interrupted")
        return EXIT_ERR


def _run_capture(cmd, **kwargs) -> tuple[int, str, str]:
    """Run a command, capturing stdout/stderr."""
    kwargs["stdout"] = subprocess.PIPE
    kwargs["stderr"] = subprocess.PIPE
    kwargs["text"] = True
    cwd = kwargs.pop("cwd", ROOT)
    echo = kwargs.pop("echo", True)
    if isinstance(cmd, list):
        display = " ".join(cmd)
    else:
        display = cmd
    if echo:
        _print_info(f"$ {display}")
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), **kwargs)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as e:
        return 127, "", f"command not found: {e.filename}"
    except KeyboardInterrupt:
        return 130, "", "interrupted"


def _nproc() -> int:
    """Return number of available processors (portable)."""
    try:
        if sys.platform == "darwin":
            rc, out, _ = _run_capture(["sysctl", "-n", "hw.ncpu"], echo=False)
            if rc == 0 and out.strip():
                return int(out.strip())
        rc, out, _ = _run_capture(["nproc"], echo=False)
        if rc == 0 and out.strip():
            return int(out.strip())
    except Exception:
        pass
    return os.cpu_count() or 1


def _elapsed(t_start: float) -> str:
    return f"{time.time() - t_start:.1f}s"


# ── test environment ─────────────────────────────────────────────────

class TestEnv:
    """Mirror of tests/testenv.sh — common env for test scripts."""

    def __init__(self, root: Path = ROOT):
        self.root = root
        self.python = os.environ.get("PYTHON", sys.executable)
        self.visidata_dir = root / "tests" / ".visidata"
        self.config_file = root / "tests" / ".visidatarc"
        self.vd = (
            f'{self.python} -m visidata'
            f' --config "{self.config_file}"'
            f' --visidata-dir "{self.visidata_dir}"'
        )
        self.nprocs = int(os.environ.get("NPROCS", _nproc()))
        self.outdir = root / "tests" / "output"
        self.t_start = time.time()

    def as_env(self) -> dict:
        env = os.environ.copy()
        env["PYTHON"] = self.python
        env["VD"] = self.vd
        env["NPROCS"] = str(self.nprocs)
        env["OUTDIR"] = str(self.outdir)
        env["NO_COLOR"] = "1"
        env["PYTHONFAULTHANDLER"] = "1"
        return env

    def elapsed(self) -> str:
        return _elapsed(self.t_start)


# ═══════════════════════════════════════════════════════════════════════
# install
# ═══════════════════════════════════════════════════════════════════════

def _parse_requirements(path: Path) -> list[str]:
    """Parse a requirements.txt-style file, stripping comments and blanks."""
    if not path.exists():
        return []
    reqs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if line and not line.startswith(("-e git+", "git+")):
            reqs.append(line)
    return reqs


def _pip_freeze_versions() -> dict[str, str]:
    """Return {pkg_name_lower: version} from pip freeze."""
    rc, out, _ = _run_capture([sys.executable, "-m", "pip", "freeze"], echo=False)
    versions: dict[str, str] = {}
    if rc != 0:
        return versions
    for line in out.splitlines():
        line = line.strip()
        if "==" in line and not line.startswith("-e"):
            name, ver = line.split("==", 1)
            versions[name.lower().replace("-", "_")] = ver
    return versions


def _check_install_consistency(mode: str) -> tuple[list[str], list[str]]:
    """Check that the current environment matches the requested install mode.

    Returns (issues, warnings) — issues are hard failures, warnings are advisory.
    """
    issues: list[str] = []
    warnings: list[str] = []

    # 1. Check visidata is installed (editable for dev/test/all)
    rc, loc_out, _ = _run_capture(
        [sys.executable, "-c", "import visidata, os; print(os.path.dirname(visidata.__file__))"],
        echo=False,
    )
    if rc != 0:
        issues.append("visidata package not importable — run `vd-dev install` first")
    else:
        installed_dir = Path(loc_out.strip())
        source_dir = ROOT / "visidata"
        if mode in ("dev", "test", "all"):
            if installed_dir.resolve() != source_dir.resolve():
                issues.append(
                    f"visidata not in editable mode: installed at {installed_dir}, "
                    f"expected {source_dir}. Re-run `vd-dev install {mode}`."
                )
        elif mode == "prod":
            if installed_dir.resolve() == source_dir.resolve():
                issues.append(
                    "visidata is in editable mode but prod mode requested. "
                    "Re-run `vd-dev install prod`."
                )

    # 2. Check test deps for test/all mode
    if mode in ("test", "all"):
        required_test = ["pytest"]
        optional_test = ["pandas", "pyarrow"]
        installed = _pip_freeze_versions()
        missing_req = [p for p in required_test if p.lower().replace("-", "_") not in installed]
        missing_opt = [p for p in optional_test if p.lower().replace("-", "_") not in installed]
        if missing_req:
            issues.append(
                f"missing required test dependencies: {', '.join(missing_req)}. "
                f"Run `vd-dev install {mode}`."
            )
        if missing_opt:
            warnings.append(
                f"missing optional test dependencies: {', '.join(missing_opt)} "
                f"(some golden tests will skip)"
            )

    # 3. Check dev deps for dev/all mode
    if mode in ("dev", "all"):
        required_dev = ["pytest"]
        optional_dev = ["ruff"]
        installed = _pip_freeze_versions()
        missing_req = [p for p in required_dev if p.lower().replace("-", "_") not in installed]
        missing_opt = [p for p in optional_dev if p.lower().replace("-", "_") not in installed]
        if missing_req:
            issues.append(
                f"missing required dev dependencies: {', '.join(missing_req)}. "
                f"Run `vd-dev install {mode}`."
            )
        if missing_opt:
            warnings.append(
                f"missing optional dev dependencies: {', '.join(missing_opt)}"
            )

    return issues, warnings


def cmd_install(args: argparse.Namespace) -> int:
    mode = args.mode
    check_only = getattr(args, "check", False)

    if check_only:
        issues, warnings = _check_install_consistency(mode)
        for w in warnings:
            _print_warn(w)
        if issues:
            for msg in issues:
                _print_err(msg)
            _print_err(
                f"installation does not match '{mode}' mode "
                f"({len(issues)} issue(s), {len(warnings)} warning(s))"
            )
            return EXIT_ERR
        _print_info(
            f"installation matches '{mode}' mode "
            f"(0 issues, {len(warnings)} warning(s))"
        )
        return EXIT_OK

    if mode == "dev":
        rc = _run([sys.executable, "-m", "pip", "install", "-r", "dev/requirements-dev.txt"])
        if rc != 0:
            return rc
        return _run([sys.executable, "-m", "pip", "install", "-e", "."])
    elif mode == "test":
        return _run([sys.executable, "-m", "pip", "install", ".[test]"])
    elif mode == "all":
        return _run([sys.executable, "-m", "pip", "install", ".[all]"])
    else:
        return _run([sys.executable, "-m", "pip", "install", "."])


def setup_install(sub) -> None:
    p = sub.add_parser("install", help="install package with optional extras")
    inst_sub = p.add_subparsers(dest="mode", metavar="<mode>")
    inst_sub.required = False

    for name, help_text in [
        ("dev", "editable install with dev dependencies (default)"),
        ("test", "install with test dependencies"),
        ("all", "install with all optional dependencies"),
        ("prod", "non-editable production install"),
    ]:
        sp = inst_sub.add_parser(name, help=help_text)
        sp.add_argument("--check", action="store_true",
                        help="only verify installation matches mode, do not install")
        sp.set_defaults(func=cmd_install)

    p.add_argument("--check", action="store_true",
                   help="only verify installation matches mode, do not install")
    p.set_defaults(func=cmd_install, mode="dev")


# ═══════════════════════════════════════════════════════════════════════
# test-all (top-level scheduler)
# ═══════════════════════════════════════════════════════════════════════

def _discover_test_scripts(root: Path, explicit: list[str]) -> list[tuple[Path, str]]:
    """Return list of (script_path, test_name).

    The caller decides whether to invoke Python impl or shell script.
    """
    migrated = {
        "test-smoke",
        "test-pytest",
        "test-vdx",
    }
    if explicit:
        result = []
        for p in explicit:
            path = root / p
            result.append((path, path.stem))
        return result

    result = []
    for path in sorted(root.glob("tests/test-*.sh")):
        result.append((path, path.stem))
    return result


def _run_labeled(cmd: list[str], name: str, env: dict, timeout_sec: int) -> tuple[int, str]:
    """Run a test command, prefix every output line with the test name."""
    t_start = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except FileNotFoundError:
        return 127, name

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(f"{name}: {line}", end="")
        proc.wait(timeout=timeout_sec)
        return proc.returncode, name
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return EXIT_TIMEOUT, name
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        return 130, name


_MIGRATED = {"test-smoke": "smoke", "test-pytest": "unit", "test-vdx": "golden"}


def cmd_test_all(args: argparse.Namespace, scripts: Optional[list[str]] = None) -> int:
    """Core implementation of dev/test-all.sh."""
    env = TestEnv()
    test_timeout = int(os.environ.get("TEST_TIMEOUT", "120"))
    extra = scripts or args.extra_args or []

    test_scripts = _discover_test_scripts(ROOT, extra)
    if not test_scripts:
        _print_warn("no test scripts found")
        return EXIT_OK

    failed: list[str] = []
    n_total = 0

    for script_path, name in test_scripts:
        if name in _MIGRATED:
            # Migrated: invoke via vd-dev so shell wrapper is bypassed
            mode = _MIGRATED[name]
            cmd = [sys.executable, "-m", "visidata.dev_cli", "test", mode]
        else:
            # Unmigrated: run the shell script directly
            if not script_path.exists():
                _print_warn(f"test script not found: {script_path}")
                continue
            cmd = ["bash", str(script_path)]
        n_total += 1
        rc, tname = _run_labeled(cmd, name, env.as_env(), test_timeout)
        if rc == EXIT_TIMEOUT or rc == 137:
            print(f"FAIL: {tname} (timed out after {test_timeout}s)")
            failed.append(tname)
        elif rc != 0:
            print(f"FAIL: {tname}")
            failed.append(tname)

    n_failed = len(failed)
    n_passed = n_total - n_failed
    if failed:
        print()
        print(f"FAILED: {' '.join(failed)}")
        print(f"FAIL: {n_passed}/{n_total} tests passed")
        return EXIT_ERR
    else:
        print(f"PASS: {n_passed}/{n_total} tests passed")
        return EXIT_OK


# ═══════════════════════════════════════════════════════════════════════
# test-golden (cmdlog / vdx tests)
# ═══════════════════════════════════════════════════════════════════════

SKIP_SUFFIXES = {
    "-broken": "broken",
    "-manual": "manual",
    "-perf": "perf",
}


def _should_skip(test_path: Path, py311: bool) -> Optional[str]:
    """Return skip reason or None. Mirrors should_skip() in tests/test-vdx.sh."""
    stem = test_path.stem if test_path.suffix.startswith(".vd") else test_path.name
    # strip .vd/.vdj/.vdx
    base = stem
    for ext in (".vdx", ".vdj", ".vd"):
        if base.endswith(ext):
            base = base[: -len(ext)]
            break

    for suffix, reason in SKIP_SUFFIXES.items():
        if base.endswith(suffix):
            return reason

    if base.endswith("-nosave") or base.endswith("-flaky"):
        return None  # not skipped, special handling

    if base.endswith("-n311") and py311:
        return "n311"
    if base.endswith("-311") and not py311:
        return "311"

    return None


def _is_nosave(test_path: Path) -> bool:
    name = test_path.name
    for ext in (".vdx", ".vdj", ".vd"):
        if name.endswith(f"-nosave{ext}"):
            return True
    return False


def _resolve_tests(root: Path, patterns: list[str]) -> list[Path]:
    """Resolve test args to file list, mirroring tests/test-vdx.sh logic."""
    if not patterns:
        files = list(root.glob("tests/*.vd*"))
        return sorted(f for f in files if f.suffix in (".vd", ".vdj", ".vdx"))

    result: list[Path] = []
    for arg in patterns:
        p = root / arg
        if p.is_file():
            result.append(p)
            continue
        # try adding tests/ prefix and .vd* extensions
        if not arg.startswith("tests/"):
            arg = f"tests/{arg}"
        for ext in (".vdx", ".vdj", ".vd"):
            matched = list(root.glob(f"{arg}{ext}"))
            result.extend(matched)
    return sorted(set(result))


def _chunk(items: list, n_chunks: int) -> list[list]:
    """Split items into n_chunks roughly equal sequential chunks."""
    if not items:
        return []
    n_chunks = max(1, min(n_chunks, len(items)))
    chunk_size = (len(items) + n_chunks - 1) // n_chunks
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]


def _build_vdx_batch(
    tests: list[Path], golden_dir: Path, output_dir: Path, debug: bool
) -> tuple[str, list[Path]]:
    """Build a vdx batch string and list of expected output paths.

    Mirrors the golden batch construction in tests/test-vdx.sh.
    """
    batch_parts: list[str] = []
    expected: list[Path] = []

    for test_path in tests:
        testname = test_path.stem
        # strip .vd/.vdj/.vdx if present in stem
        for ext in (".vdx", ".vdj", ".vd"):
            if testname.endswith(ext):
                testname = testname[: -len(ext)]
                break

        # find golden files for this test
        golden_glob = list(golden_dir.glob(f"{testname}.*"))
        if not golden_glob:
            continue

        for goldfn in golden_glob:
            outfn = output_dir / goldfn.name
            batch_parts.append(f"replay-reset {outfn}")
            if not debug:
                batch_parts.append("option global replay_ignore_errors True")
            batch_parts.append(test_path.read_text())
            batch_parts.append("replay-output")
            expected.append(outfn)

    if batch_parts:
        batch_parts.append("replay-exit")
    return "\n".join(batch_parts) + "\n", expected


def _build_nosave_batch(tests: list[Path]) -> str:
    """Build the nosave test batch (no golden comparison, assert-expr enabled)."""
    parts: list[str] = []
    for test_path in tests:
        testname = test_path.stem
        for ext in (".vdx", ".vdj", ".vd"):
            if testname.endswith(ext):
                testname = testname[: -len(ext)]
                break
        parts.append(f"replay-reset {testname}")
        parts.append(test_path.read_text())
        parts.append("replay-end")
    if parts:
        parts.append("replay-exit")
    return "\n".join(parts) + "\n"


def _run_vd_batch(batch: str, env: TestEnv, log_path: Optional[Path] = None) -> int:
    """Feed a vdx batch to `vd --play -` via stdin. Returns exit code."""
    vd_cmd = [
        env.python, "-m", "visidata",
        "--play", "-",
        "--batch",
        "--config", str(env.config_file),
        "--visidata-dir", str(env.visidata_dir),
    ]
    run_env = env.as_env()
    run_env["PYTHONPATH"] = str(ROOT) + ":" + run_env.get("PYTHONPATH", "")
    run_env["LC_NUMERIC"] = "en_US.UTF-8"
    run_env["LC_TIME"] = "en_US.UTF-8"
    run_env["XDG_DATA_HOME"] = str(ROOT / "tests" / "xdg" / "data")

    stdout = open(log_path, "w") if log_path else None
    try:
        proc = subprocess.run(
            vd_cmd, cwd=str(ROOT), input=batch, text=True,
            stdout=stdout, stderr=subprocess.STDOUT if log_path else None,
            env=run_env,
        )
        return proc.returncode
    finally:
        if log_path:
            stdout.close()  # type: ignore[union-attr]


def _diff_files(goldfn: Path, outfn: Path) -> str:
    """Return unified diff between two text files."""
    try:
        gold = goldfn.read_text().splitlines(keepends=True)
        out = outfn.read_text().splitlines(keepends=True)
    except Exception:
        return ""
    return "".join(difflib.unified_diff(gold, out, fromfile=str(goldfn), tofile=str(outfn)))


def cmd_test_golden(args: argparse.Namespace) -> int:
    """Core implementation of tests/test-vdx.sh (golden/cmdlog tests)."""
    env = TestEnv()

    # parse -d / -j from leading flags mixed with test names
    debug = False
    nprocs = env.nprocs
    positional: list[str] = []
    i = 0
    extra = args.extra_args or []
    while i < len(extra):
        a = extra[i]
        if a == "-d":
            debug = True
        elif a == "-j" and i + 1 < len(extra):
            nprocs = int(extra[i + 1])
            i += 1
        elif a.startswith("-j"):
            nprocs = int(a[2:])
        else:
            positional.append(a)
        i += 1

    py311 = sys.version_info[:2] >= (3, 11)

    test_paths = _resolve_tests(ROOT, positional)
    output_dir = ROOT / "tests" / "output"
    golden_dir = ROOT / "tests" / "golden"
    output_dir.mkdir(parents=True, exist_ok=True)

    # clean output dir
    for f in output_dir.glob("*"):
        if f.name != ".gitignore":
            try:
                f.unlink()
            except IsADirectoryError:
                pass

    n_tests = 0
    n_skipped = 0
    failed_tests: set[str] = set()
    flaky_tests: set[str] = set()
    expected_outputs: list[Path] = []

    golden_tests: list[Path] = []
    nosave_tests: list[Path] = []

    for tp in test_paths:
        reason = _should_skip(tp, py311)
        testname = tp.stem
        for ext in (".vdx", ".vdj", ".vd"):
            if testname.endswith(ext):
                testname = testname[: -len(ext)]
                break
        if reason:
            n_skipped += 1
            if debug:
                print(f"SKIP: {testname} ({reason})")
            continue
        n_tests += 1
        if _is_nosave(tp):
            nosave_tests.append(tp)
        else:
            golden_tests.append(tp)

    # build batches
    batches = _chunk(golden_tests, nprocs)
    batch_vdx: list[tuple[str, list[Path]]] = []
    for chunk in batches:
        if chunk:
            batch_vdx.append(_build_vdx_batch(chunk, golden_dir, output_dir, debug))
            expected_outputs.extend(batch_vdx[-1][1])

    nosave_vdx = _build_nosave_batch(nosave_tests) if nosave_tests else ""

    # launch nosave in background, golden batches in parallel
    nosave_log = Path(tempfile.gettempdir()) / "vd-nosave-output.txt"
    nosave_proc = None
    if nosave_vdx:
        nosave_proc = subprocess.Popen(
            ["python3", "-c", (
                "import sys; sys.path.insert(0, sys.argv[1]);\n"
                "from visidata.dev_cli import TestEnv, _run_vd_batch;\n"
                "import pathlib;\n"
                "env = TestEnv(pathlib.Path(sys.argv[1]));\n"
                "batch = sys.stdin.read();\n"
                "rc = _run_vd_batch(batch, env, pathlib.Path(sys.argv[2]));\n"
                "sys.exit(rc);\n"
            ), str(ROOT), str(nosave_log)],
            cwd=str(ROOT), stdin=subprocess.PIPE, text=True,
        )
        try:
            assert nosave_proc.stdin is not None
            nosave_proc.stdin.write(nosave_vdx)
            nosave_proc.stdin.close()
        except Exception:
            pass

    # run golden batches in parallel
    golden_procs: list[subprocess.Popen] = []
    for batch_str, _ in batch_vdx:
        if batch_str:
            proc = subprocess.Popen(
                ["python3", "-c", (
                    "import sys; sys.path.insert(0, sys.argv[1]);\n"
                    "from visidata.dev_cli import TestEnv, _run_vd_batch;\n"
                    "import pathlib;\n"
                    "env = TestEnv(pathlib.Path(sys.argv[1]));\n"
                    "batch = sys.stdin.read();\n"
                    "rc = _run_vd_batch(batch, env);\n"
                    "sys.exit(rc);\n"
                ), str(ROOT)],
                cwd=str(ROOT), stdin=subprocess.PIPE, text=True,
            )
            try:
                assert proc.stdin is not None
                proc.stdin.write(batch_str)
                proc.stdin.close()
            except Exception:
                pass
            golden_procs.append(proc)

    # wait for golden batches
    for p in golden_procs:
        p.wait()

    # wait for nosave
    if nosave_proc:
        nosave_proc.wait()
        if nosave_proc.returncode != 0:
            if nosave_log.exists():
                sys.stderr.write(nosave_log.read_text())
            import re
            failing_test = None
            if nosave_log.exists():
                for line in nosave_log.read_text().splitlines():
                    m = re.match(r"^(\S+-nosave)", line)
                    if m:
                        failing_test = m.group(1)
                        break
            failed_tests.add(failing_test or "nosave-batch")

    # check for missing expected outputs
    for outfn in expected_outputs:
        if not outfn.exists():
            testname = outfn.stem
            if testname.endswith("-flaky"):
                print(f"FLAKY: {testname} (no output: {outfn})")
                flaky_tests.add(testname)
            else:
                print(f"DIFF: {testname} (no output: {outfn})")
                failed_tests.add(testname)

    # diff all outputs against golden
    for outfn in sorted(output_dir.glob("*")):
        if outfn.name == ".gitignore" or not outfn.is_file():
            continue
        goldfn = golden_dir / outfn.name
        testname = outfn.stem
        if not goldfn.exists():
            if testname.endswith("-flaky"):
                print(f"FLAKY: {testname} (no golden file: {goldfn})")
                flaky_tests.add(testname)
            else:
                print(f"DIFF: {testname} (no golden file: {goldfn})")
                failed_tests.add(testname)
            continue
        if goldfn.read_bytes() != outfn.read_bytes():
            if testname.endswith("-flaky"):
                print(f"FLAKY: {testname} ({outfn})")
                flaky_tests.add(testname)
            else:
                print(f"DIFF: {testname} ({outfn})")
                failed_tests.add(testname)
                if debug:
                    print(_diff_files(goldfn, outfn))
                    break

    n_failed = len(failed_tests)
    n_flaky = len(flaky_tests)
    if n_failed:
        print()
        print(f"{n_failed} tests failed: {' '.join(sorted(failed_tests))}")

    n_passed = n_tests - n_failed - n_flaky
    parts = [f"{n_passed} passed in {env.elapsed()}"]
    if n_flaky:
        parts.append(f"{n_flaky} flaky")
    if n_skipped:
        parts.append(f"{n_skipped} skipped")
    summary = ", ".join(parts)

    if n_failed:
        print(f"FAIL: {summary}")
        return EXIT_ERR
    print(summary)
    return EXIT_OK


# ═══════════════════════════════════════════════════════════════════════
# test-individual (each test in own process)
# ═══════════════════════════════════════════════════════════════════════

def cmd_test_individual(args: argparse.Namespace) -> int:
    """Core implementation of dev/run-tests-individually.sh."""
    extra = args.extra_args or []
    if extra:
        test_files = [ROOT / p for p in extra]
    else:
        test_files = sorted(ROOT.glob("tests/*.vd"))

    log_dir = ROOT / "tests" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    for lf in log_dir.glob("*.log"):
        lf.unlink()

    failures: list[str] = []

    try:
        for tf in test_files:
            if not tf.exists():
                continue
            test_name = tf.stem
            log_file = log_dir / f"{test_name}.log"
            rc = cmd_test_golden(argparse.Namespace(extra_args=[test_name]))
            # actually we need to capture output; run via subprocess to isolate
            rc, out, err = _run_capture(
                [sys.executable, "-m", "visidata.dev_cli", "test", "golden", test_name],
                echo=False,
            )
            log_file.write_text(out + err)
            if rc == 0:
                print(_colorize("green bold", "    ok"), test_name)
            else:
                print(_colorize("red   bold", "not ok"), test_name)
                failures.append(str(tf))
    except KeyboardInterrupt:
        return 130

    if failures:
        print(file=sys.stderr)
        print(f"{len(failures)} failed tests:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return EXIT_ERR
    return EXIT_OK


# ═══════════════════════════════════════════════════════════════════════
# test-smoke
# ═══════════════════════════════════════════════════════════════════════

def cmd_test_smoke(args: argparse.Namespace) -> int:
    """Core implementation of tests/test-smoke.sh."""
    env = TestEnv()
    rc, version_out, _ = _run_capture(
        env.python + " -m visidata --version",
        shell=True, echo=False, env=env.as_env(),
    )
    if rc != 0:
        return rc
    version = version_out.strip()
    rc = _run(
        f"{env.vd} -f dir . --batch -o /dev/null",
        shell=True, echo=False, env=env.as_env(),
    )
    if rc != 0:
        return rc
    print(f"{version}; 1 passed in {env.elapsed()}")
    return EXIT_OK


# ═══════════════════════════════════════════════════════════════════════
# test dispatch
# ═══════════════════════════════════════════════════════════════════════

def _cmd_test_unit(args: argparse.Namespace) -> int:
    extra = args.extra_args or []
    if extra:
        return _run([sys.executable, "-m", "pytest", *extra])
    return _run([sys.executable, "-m", "pytest", "visidata/tests/"])


def _cmd_test_vgit(args: argparse.Namespace) -> int:
    extra = args.extra_args or []
    cmd = (
        f"{sys.executable} -m visidata --config tests/.visidatarc"
        f" -p visidata/apps/vgit/tests/*.vdx --batch"
    )
    if extra:
        cmd += " " + " ".join(extra)
    return _run(cmd, shell=True)


def _cmd_test_vdsql(args: argparse.Namespace) -> int:
    extra = args.extra_args or []
    return _run(["bash", "./test.sh", *extra], cwd=ROOT / "visidata/apps/vdsql")


def _cmd_test_perf(args: argparse.Namespace) -> int:
    return _run(["bash", "tests/test-perf.sh", *(args.extra_args or [])])


def _add_extra_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("extra_args", nargs=argparse.REMAINDER,
                   help="extra arguments passed to underlying test runner")


def setup_test(sub) -> None:
    p = sub.add_parser("test", help="run tests")
    test_sub = p.add_subparsers(dest="mode", metavar="<suite>")
    test_sub.required = False

    for name, func, help_text in [
        ("all", cmd_test_all, "run all test suites (default)"),
        ("golden", cmd_test_golden, "run functional cmdlog tests"),
        ("unit", _cmd_test_unit, "run pytest unit tests"),
        ("vgit", _cmd_test_vgit, "run vgit tests"),
        ("vdsql", _cmd_test_vdsql, "run vdsql tests"),
        ("smoke", cmd_test_smoke, "run quick import/version smoke test"),
        ("perf", _cmd_test_perf, "run performance tests"),
        ("individual", cmd_test_individual, "run each test in isolation"),
    ]:
        sp = test_sub.add_parser(name, help=help_text)
        if name in ("unit", "vgit", "vdsql", "perf"):
            _add_extra_args(sp)
        sp.set_defaults(func=func)

    p.set_defaults(func=cmd_test_all, mode="all")


# ═══════════════════════════════════════════════════════════════════════
# build-man (man page generation)
# ═══════════════════════════════════════════════════════════════════════

def cmd_build_man(args: argparse.Namespace) -> int:
    """Core implementation of dev/mkman.sh.

    Builds:
      - visidata/man/vd.1, visidata/man/visidata.1  (groff man pages)
      - visidata/man/vd.txt                          (plain text man page)
      - docs/man.md                                   (HTML man page for docs site)
    """
    man_dir = ROOT / "visidata" / "man"
    build_dir = Path(tempfile.gettempdir()) / "visidata_manpages"
    docs_man = ROOT / "docs" / "man.md"

    _print_info(f"Cleaning up {build_dir}")
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    run_env = os.environ.copy()
    run_env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'visidata'}:" + run_env.get("PYTHONPATH", "")
    run_env["PATH"] = f"{ROOT / 'bin'}:" + run_env.get("PATH", "")

    # copy man templates
    for f in man_dir.iterdir():
        if f.is_file():
            shutil.copy2(f, build_dir / f.name)

    # parse options
    parse_opts = man_dir / "parse_options.py"
    rc = _run(
        [sys.executable, str(parse_opts),
         str(build_dir / "vd-cli.inc"), str(build_dir / "vd-opts.inc")],
        env=run_env, echo=True,
    )
    if rc != 0:
        return rc

    # soelim -> preconv produces groff man page
    rc = _run(
        f"soelim -rt -I {build_dir} {build_dir / 'vd.inc'} > {build_dir / 'vd-pre.1'}",
        shell=True, env=run_env,
    )
    if rc != 0:
        return rc

    rc = _run(
        f"preconv -r -e utf8 {build_dir / 'vd-pre.1'} > {man_dir / 'vd.1'}",
        shell=True, env=run_env,
    )
    if rc != 0:
        return rc
    shutil.copy2(man_dir / "vd.1", man_dir / "visidata.1")

    # plain text version
    rc = _run(
        f"MANWIDTH=80 man {man_dir / 'vd.1'} > {man_dir / 'vd.txt'}",
        shell=True, env=run_env,
    )
    if rc != 0:
        _print_warn(f"could not generate vd.txt (rc={rc}); continuing")

    # docs/man.md with HTML-rendered man page
    try:
        manhtml_env = run_env.copy()
        manhtml_env["MAN_KEEP_FORMATTING"] = "1"
        manhtml_env["COLUMNS"] = "1000"
        rc, html_out, _ = _run_capture(
            f'man "{man_dir / "vd.1"}" | ul | aha --no-header',
            shell=True, env=manhtml_env, echo=False,
        )
        if rc == 0:
            docs_man.write_text(
                "---\n"
                "eleventyNavigation:\n"
                "  key: Quick Reference Guide\n"
                "  order: 2\n"
                "permalink: /man/\n"
                "---\n"
                '<section><pre id="manpage" class="whitespace-pre-wrap text-xs">\n'
                + html_out
                + "</pre></section>\n"
            )
        else:
            _print_warn("could not generate docs/man.md (aha/man may be missing)")
    except Exception as e:
        _print_warn(f"could not generate docs/man.md: {e}")

    _print_info(f"Files are written to {build_dir}")
    return EXIT_OK


def cmd_build(args: argparse.Namespace) -> int:
    mode = args.mode
    if mode in ("all", "man"):
        rc = cmd_build_man(args)
        if mode == "man" or rc != 0:
            return rc
    if mode in ("all", "zsh"):
        rc = cmd_build_zsh(args)
        if mode == "zsh" or rc != 0:
            return rc
    if mode == "docker":
        return cmd_build_docker(args)
    if mode == "all":
        return EXIT_OK
    _die(f"unknown build mode: {mode}")


def cmd_build_zsh(args: argparse.Namespace) -> int:
    return _run([sys.executable, "dev/zsh-completion.py", "_visidata"])


def cmd_build_docker(args: argparse.Namespace) -> int:
    return _run(["bash", "dev/build-container"])


def setup_build(sub) -> None:
    p = sub.add_parser("build", help="build resources (man pages, completions, docker)")
    b_sub = p.add_subparsers(dest="mode", metavar="<target>")
    b_sub.required = False

    for name, func, help_text in [
        ("all", cmd_build, "build everything (default)"),
        ("man", cmd_build_man, "build man pages"),
        ("zsh", cmd_build_zsh, "build zsh completions"),
        ("docker", cmd_build_docker, "build docker container"),
    ]:
        sp = b_sub.add_parser(name, help=help_text)
        sp.set_defaults(func=func)

    p.set_defaults(func=cmd_build, mode="all")


# ═══════════════════════════════════════════════════════════════════════
# lint
# ═══════════════════════════════════════════════════════════════════════

def cmd_lint(args: argparse.Namespace) -> int:
    extra = args.extra_args or []
    if "-h" in extra or "--help" in extra:
        return _run([sys.executable, "-m", "ruff", "check", "--help"])
    if extra:
        return _run([sys.executable, "-m", "ruff", "check", *extra])
    return _run([sys.executable, "-m", "ruff", "check", "."])


def setup_lint(sub) -> None:
    p = sub.add_parser("lint", help="run ruff linter")
    p.add_argument("extra_args", nargs=argparse.REMAINDER,
                   help="extra arguments passed to ruff")
    p.set_defaults(func=cmd_lint)


# ═══════════════════════════════════════════════════════════════════════
# setup
# ═══════════════════════════════════════════════════════════════════════

def cmd_setup(args: argparse.Namespace) -> int:
    mode = args.mode

    if mode in ("all", "hooks"):
        rc = _run(["git", "config", "core.hooksPath", "dev/hooks"])
        if rc != 0 and mode == "hooks":
            return rc
        if rc != 0:
            _print_warn("git hooks setup failed; continuing")

    if mode in ("all", "vscode"):
        vscode = ROOT / ".vscode"
        vscode.mkdir(exist_ok=True)
        for name in ("launch.json", "settings.json"):
            src = ROOT / ".devcontainer" / name
            dst = vscode / name
            _print_info(f"copy {src.relative_to(ROOT)} → {dst.relative_to(ROOT)}")
            dst.write_text(src.read_text())
        return EXIT_OK

    if mode == "hooks":
        return EXIT_OK

    _die(f"unknown setup mode: {mode}")


def setup_setup(sub) -> None:
    p = sub.add_parser("setup", help="setup development environment")
    s_sub = p.add_subparsers(dest="mode", metavar="<target>")
    s_sub.required = False

    for name, help_text in [
        ("all", "setup everything (default)"),
        ("hooks", "configure git hooks"),
        ("vscode", "configure VS Code settings"),
    ]:
        sp = s_sub.add_parser(name, help=help_text)
        sp.set_defaults(func=cmd_setup)

    p.set_defaults(func=cmd_setup, mode="all")


# ═══════════════════════════════════════════════════════════════════════
# diff-test
# ═══════════════════════════════════════════════════════════════════════

def cmd_diff_test(args: argparse.Namespace) -> int:
    """Core implementation of dev/diff-test.sh.

    For each *.tsv file changed in git (vs HEAD^), run vd --diff to compare.
    Skips files whose name ends with -notest.tsv.
    """
    rc, out, _ = _run_capture(
        ["git", "diff", "--name-only", "--", "*.tsv"],
        echo=False,
    )
    if rc != 0:
        return rc

    files = [line.strip() for line in out.splitlines() if line.strip()]
    if not files:
        _print_info("no changed .tsv files")
        return EXIT_OK

    overall_rc = EXIT_OK
    for fn in files:
        path = Path(fn)
        # skip *-notest.tsv
        if path.stem.endswith("-notest"):
            continue
        if not path.exists():
            continue
        rc, prev_content, _ = _run_capture(
            ["git", "show", f"HEAD^:{fn}"],
            echo=False,
        )
        if rc != 0:
            continue
        # run bin/vd --diff
        vd_cmd = [
            sys.executable, "-m", "visidata",
            "--diff", fn,
        ]
        vd_rc = subprocess.run(
            vd_cmd, cwd=str(ROOT),
            input=prev_content, text=True,
        ).returncode
        if vd_rc != 0:
            overall_rc = EXIT_ERR

    return overall_rc


def setup_diff_test(sub) -> None:
    p = sub.add_parser("diff-test", help="run git-diff-based tests")
    p.set_defaults(func=cmd_diff_test)


# ═══════════════════════════════════════════════════════════════════════
# clean
# ═══════════════════════════════════════════════════════════════════════

def cmd_clean(args: argparse.Namespace) -> int:
    targets = [
        ROOT / "visidata/man/vd.1",
        ROOT / "visidata/man/visidata.1",
        ROOT / "visidata/man/vd.txt",
        ROOT / "docs/man.md",
    ]
    for t in targets:
        if t.exists():
            _print_info(f"remove {t.relative_to(ROOT)}")
            t.unlink()
    return EXIT_OK


def setup_clean(sub) -> None:
    p = sub.add_parser("clean", help="remove generated files")
    p.set_defaults(func=cmd_clean)


# ═══════════════════════════════════════════════════════════════════════
# check (lint + test)
# ═══════════════════════════════════════════════════════════════════════

def cmd_check(args: argparse.Namespace) -> int:
    extra = args.extra_args or []
    if "-h" in extra or "--help" in extra:
        _print_info("runs: lint + test all")
        _print_info("pass extra args to the test runner, e.g. `vd-dev check -k keyword`")
        return EXIT_OK
    _print_info("=== lint ===")
    rc = _run([sys.executable, "-m", "ruff", "check", "."])
    if rc != 0:
        _print_err("lint failed; fix issues before proceeding")
        return rc
    _print_info("=== tests ===")
    return cmd_test_all(args)


def setup_check(sub) -> None:
    p = sub.add_parser("check", help="comprehensive check (lint + test)")
    p.add_argument("extra_args", nargs=argparse.REMAINDER,
                   help="extra arguments passed to test runner")
    p.set_defaults(func=cmd_check)


# ═══════════════════════════════════════════════════════════════════════
# shared validation rules (used by both `preflight check` and `package verify`)
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class CheckResult:
    """Result of a single validation rule."""
    rule: str                     # rule identifier, e.g. "version-consistency"
    ok: bool                      # True = pass, False = fail
    severity: str                 # "error" or "warn"
    message: str                  # human-readable message
    detail: str = ""              # optional extra detail (paths, etc.)

    def format(self) -> str:
        if self.ok:
            tag = _colorize("green", "PASS")
        elif self.severity == "warn":
            tag = _colorize("yellow", "WARN")
        else:
            tag = _colorize("red", "FAIL")
        line = f"[{tag}] {self.rule}: {self.message}"
        if self.detail:
            line += f"\n       {self.detail}"
        return line

# ---------------------------------------------------------------------------
# helpers: parse metadata from built artifacts
# ---------------------------------------------------------------------------

EXPECTED_CONSOLE_SCRIPTS = {
    "vd": "visidata.main:vd_cli",
    "visidata": "visidata.main:vd_cli",
    "vd-dev": "visidata.dev_cli:vd_dev_cli",
}

EXPECTED_PACKAGES = [
    "visidata",
    "visidata.loaders",
    "visidata.vendor",
    "visidata.tests",
    "visidata.guides",
    "visidata.ddw",
    "visidata.man",
    "visidata.themes",
    "visidata.features",
    "visidata.experimental",
    "visidata.apps",
    "visidata.apps.vgit",
    "visidata.apps.vdsql",
    "visidata.desktop",
]

EXPECTED_DATA_FILES_SOURCE = [
    "visidata/man/vd.1",
    "visidata/man/visidata.1",
    "visidata/desktop/visidata.desktop",
    "visidata/desktop/org.visidata.VisiData.metainfo.xml",
    "visidata/desktop/icons/48x48/visidata.png",
    "visidata/desktop/icons/32x32/visidata.png",
]


def _read_version_setup() -> str:
    """Extract __version__ from setup.py."""
    for line in (ROOT / "setup.py").read_text().splitlines():
        if line.strip().startswith("__version__"):
            parts = line.split("=", 1)
            if len(parts) == 2:
                return parts[1].strip().strip('"').strip("'")
    return ""


def _read_version_init() -> str:
    """Extract __version__ from visidata/__init__.py."""
    for line in (ROOT / "visidata" / "__init__.py").read_text().splitlines():
        if line.strip().startswith("__version__"):
            parts = line.split("=", 1)
            if len(parts) == 2:
                return parts[1].strip().strip('"').strip("'")
    return ""


def _read_version_main() -> str:
    """Extract version from visidata/main.py if present."""
    main_py = ROOT / "visidata" / "main.py"
    if not main_py.exists():
        return ""
    for line in main_py.read_text().splitlines():
        if "__version__" in line and "=" in line:
            parts = line.split("=", 1)
            if len(parts) == 2:
                ver = parts[1].strip().strip('"').strip("'").rstrip(",")
                if ver:
                    return ver
    return ""


def _parse_wheel_metadata(wheel_path: Path) -> dict:
    """Parse METADATA and entry_points.txt from a .whl file.

    Returns dict with keys: 'name', 'version', 'console_scripts' (dict name->entry).
    """
    result: dict = {"name": "", "version": "", "console_scripts": {}}
    with zipfile.ZipFile(wheel_path) as zf:
        # find dist-info dir
        dist_info_names = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
        if not dist_info_names:
            return result
        dist_info_prefix = dist_info_names[0].rsplit("/", 1)[0] + "/"

        with zf.open(dist_info_names[0]) as mf:
            parser = email.parser.Parser()
            msg = parser.parsestr(mf.read().decode("utf-8", errors="replace"))
            result["name"] = msg.get("Name", "")
            result["version"] = msg.get("Version", "")

        ep_path = dist_info_prefix + "entry_points.txt"
        if ep_path in zf.namelist():
            with zf.open(ep_path) as epf:
                cp = configparser.ConfigParser()
                cp.read_string(epf.read().decode("utf-8", errors="replace"))
                if cp.has_section("console_scripts"):
                    for k, v in cp.items("console_scripts"):
                        result["console_scripts"][k] = v.strip()
    return result


def _wheel_file_set(wheel_path: Path) -> set[str]:
    """Return set of normalized file paths inside a wheel."""
    with zipfile.ZipFile(wheel_path) as zf:
        return set(zf.namelist())


def _sdist_file_set(sdist_path: Path) -> set[str]:
    """Return set of normalized file paths inside an sdist tar.gz."""
    paths: set[str] = set()
    with tarfile.open(sdist_path, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            name = member.name
            # strip top-level directory like "visidata-3.4/"
            if "/" in name:
                name = name.split("/", 1)[1]
            paths.add(name)
    return paths


# ---------------------------------------------------------------------------
# rule: version consistency across source files
# ---------------------------------------------------------------------------

def rule_version_consistency() -> list[CheckResult]:
    results: list[CheckResult] = []
    v_setup = _read_version_setup()
    v_init = _read_version_init()
    v_main = _read_version_main()

    detail = (f"setup.py={v_setup or '(missing)'}  "
              f"__init__.py={v_init or '(missing)'}  "
              f"main.py={v_main or '(n/a)'}")
    _print_info(f"versions — {detail}")

    versions = {v for v in (v_setup, v_init, v_main) if v}
    if len(versions) > 1:
        results.append(CheckResult(
            "version-consistency", False, "error",
            "version mismatch across source files",
            detail,
        ))
    elif not versions:
        results.append(CheckResult(
            "version-consistency", False, "error",
            "could not determine version from any source file",
            detail,
        ))
    else:
        version = next(iter(versions))
        results.append(CheckResult(
            "version-consistency", True, "error",
            f"all sources agree on version {version!r}",
            detail,
        ))
        if version.endswith("dev"):
            results.append(CheckResult(
                "version-dev-suffix", False, "warn",
                f"version {version!r} still has 'dev' suffix",
                "bump version before release",
            ))
    return results


# ---------------------------------------------------------------------------
# rule: CHANGELOG mentions the current version
# ---------------------------------------------------------------------------

def rule_changelog_entry(version: str) -> list[CheckResult]:
    results: list[CheckResult] = []
    changelog = ROOT / "CHANGELOG.md"
    if not changelog.exists():
        results.append(CheckResult(
            "changelog-entry", False, "warn",
            "CHANGELOG.md not found",
            f"expected at {changelog}",
        ))
        return results
    head = changelog.read_text()[:2000]
    if version.endswith("dev"):
        results.append(CheckResult(
            "changelog-entry", True, "warn",
            "skipping CHANGELOG check (dev version)",
            f"CHANGELOG.md exists at {changelog}",
        ))
    elif version not in head:
        results.append(CheckResult(
            "changelog-entry", False, "error",
            f"CHANGELOG.md does not mention version {version} near the top",
            f"checked first 2000 chars of {changelog}",
        ))
    else:
        results.append(CheckResult(
            "changelog-entry", True, "error",
            f"CHANGELOG.md mentions version {version}",
            str(changelog),
        ))
    return results


# ---------------------------------------------------------------------------
# rule: generated / data files present in source tree
# ---------------------------------------------------------------------------

def rule_source_data_files() -> list[CheckResult]:
    results: list[CheckResult] = []
    missing: list[str] = []
    present: list[str] = []
    for rel in EXPECTED_DATA_FILES_SOURCE:
        p = ROOT / rel
        if p.exists():
            present.append(rel)
        else:
            missing.append(rel)
    if missing:
        results.append(CheckResult(
            "source-data-files", False, "warn",
            f"{len(missing)} expected data file(s) missing from source tree",
            "missing:\n  " + "\n  ".join(f"{m}  (try: vd-dev build man)" for m in missing),
        ))
    if present:
        results.append(CheckResult(
            "source-data-files", True, "error",
            f"{len(present)} data files present in source tree",
            "\n  ".join(present),
        ))
    return results


# ---------------------------------------------------------------------------
# rule: setup.py entry_points defines expected console_scripts
# ---------------------------------------------------------------------------

def _parse_setup_ast() -> tuple[dict[str, str], list[str]]:
    """Parse setup.py with AST to extract console_scripts and packages list.

    Returns (console_scripts_dict, packages_list).
    """
    console_scripts: dict[str, str] = {}
    packages: list[str] = []
    setup_path = ROOT / "setup.py"
    try:
        tree = ast.parse(setup_path.read_text())
    except Exception as e:
        _print_warn(f"could not parse {setup_path}: {e}")
        return console_scripts, packages

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "setup":
            for kw in node.keywords:
                if kw.arg == "entry_points":
                    # entry_points = {"console_scripts": ["vd=visidata.main:vd_cli", ...]}
                    if isinstance(kw.value, ast.Dict):
                        for k, v in zip(kw.value.keys, kw.value.values):
                            if isinstance(k, ast.Constant) and k.value == "console_scripts":
                                if isinstance(v, ast.List):
                                    for elt in v.elts:
                                        if isinstance(elt, ast.Constant):
                                            parts = elt.value.partition("=")
                                            if parts[2]:
                                                console_scripts[parts[0].strip()] = parts[2].strip()
                elif kw.arg == "packages":
                    if isinstance(kw.value, ast.List):
                        for elt in kw.value.elts:
                            if isinstance(elt, ast.Constant):
                                packages.append(elt.value)
    return console_scripts, packages


def rule_setup_entry_points() -> list[CheckResult]:
    results: list[CheckResult] = []
    setup_path = ROOT / "setup.py"
    found, _ = _parse_setup_ast()

    if not found:
        results.append(CheckResult(
            "setup-entry-points", False, "error",
            "could not parse setup.py entry_points",
            f"setup.py at {setup_path}",
        ))
        return results

    issues: list[str] = []
    for name, expected_entry in EXPECTED_CONSOLE_SCRIPTS.items():
        if name not in found:
            issues.append(f"missing console_scripts entry: {name}")
        elif found[name] != expected_entry:
            issues.append(
                f"console_scripts[{name}] = {found[name]!r}, expected {expected_entry!r}"
            )
    extra = [n for n in found if n not in EXPECTED_CONSOLE_SCRIPTS]
    for n in extra:
        issues.append(f"unexpected console_scripts entry: {n}={found[n]!r}")

    if issues:
        results.append(CheckResult(
            "setup-entry-points", False, "error",
            f"{len(issues)} problem(s) with setup.py console_scripts",
            "\n  ".join(issues),
        ))
    else:
        results.append(CheckResult(
            "setup-entry-points", True, "error",
            "setup.py console_scripts matches expected",
            "\n  ".join(f"{k}={v}" for k, v in EXPECTED_CONSOLE_SCRIPTS.items()),
        ))
    return results


# ---------------------------------------------------------------------------
# rule: setup.py packages list matches actual source directories
# ---------------------------------------------------------------------------

def rule_setup_packages_list() -> list[CheckResult]:
    results: list[CheckResult] = []
    setup_path = ROOT / "setup.py"
    _, declared_list = _parse_setup_ast()

    if not declared_list:
        results.append(CheckResult(
            "setup-packages", False, "warn",
            "could not parse setup.py packages list (skipping)",
            f"setup.py at {setup_path}",
        ))
        return results

    declared = set(declared_list)
    missing_decl = [p for p in EXPECTED_PACKAGES if p not in declared]
    missing_dirs: list[str] = []
    no_init: list[str] = []
    for pkg in declared:
        pkg_path = ROOT / pkg.replace(".", "/")
        if not pkg_path.exists():
            missing_dirs.append(pkg)
        elif not (pkg_path / "__init__.py").exists():
            no_init.append(pkg)

    if missing_decl:
        results.append(CheckResult(
            "setup-packages", False, "warn",
            f"{len(missing_decl)} expected package(s) not in setup.py packages list",
            "\n  ".join(missing_decl),
        ))
    if missing_dirs:
        results.append(CheckResult(
            "setup-packages", False, "error",
            f"{len(missing_dirs)} package(s) in setup.py have no source directory",
            "\n  ".join(missing_dirs),
        ))
    if no_init:
        results.append(CheckResult(
            "setup-packages", True, "warn",
            f"{len(no_init)} package(s) are data-only (no __init__.py) — valid with package_data",
            "\n  ".join(no_init),
        ))
    if not missing_decl and not missing_dirs:
        results.append(CheckResult(
            "setup-packages", True, "error",
            f"setup.py packages list and source directories agree ({len(declared)} packages)",
            "\n  ".join(sorted(declared)),
        ))
    return results


# ---------------------------------------------------------------------------
# rule: install consistency (editable + deps)
# ---------------------------------------------------------------------------

def rule_install_consistency(mode: str = "dev") -> list[CheckResult]:
    results: list[CheckResult] = []
    issues, warnings = _check_install_consistency(mode)
    for w in warnings:
        results.append(CheckResult(
            f"install-{mode}", False, "warn", w,
        ))
    for i in issues:
        results.append(CheckResult(
            f"install-{mode}", False, "error", i,
        ))
    if not issues and not warnings:
        results.append(CheckResult(
            f"install-{mode}", True, "error",
            f"installation matches '{mode}' mode",
        ))
    return results


# ---------------------------------------------------------------------------
# rule: built artifacts exist
# ---------------------------------------------------------------------------

def rule_artifact_existence(dist_dir: Path) -> tuple[list[CheckResult], Path | None, Path | None]:
    results: list[CheckResult] = []
    sdist_files = sorted(dist_dir.glob("*.tar.gz"))
    wheel_files = sorted(dist_dir.glob("*.whl"))

    sdist = sdist_files[0] if sdist_files else None
    wheel = wheel_files[0] if wheel_files else None

    if sdist:
        results.append(CheckResult(
            "artifact-existence", True, "error",
            f"sdist present: {sdist.name} ({sdist.stat().st_size:,} bytes)",
            str(sdist),
        ))
    else:
        results.append(CheckResult(
            "artifact-existence", False, "error",
            "missing sdist (.tar.gz) in dist/",
            f"expected in {dist_dir}",
        ))

    if wheel:
        results.append(CheckResult(
            "artifact-existence", True, "error",
            f"wheel present: {wheel.name} ({wheel.stat().st_size:,} bytes)",
            str(wheel),
        ))
    else:
        results.append(CheckResult(
            "artifact-existence", False, "error",
            "missing wheel (.whl) in dist/",
            f"expected in {dist_dir}",
        ))

    return results, sdist, wheel


# ---------------------------------------------------------------------------
# rule: wheel metadata (name, version, console_scripts)
# ---------------------------------------------------------------------------

def _normalize_version(v: str) -> str:
    """Normalize a version string per PEP 440 (e.g. '3.4dev' → '3.4.dev0')."""
    v = v.strip()
    # handle trailing dev without dot: '3.4dev' → '3.4.dev0'
    v = re.sub(r"(\d)dev$", r"\1.dev0", v, flags=re.IGNORECASE)
    return v


def rule_wheel_metadata(wheel_path: Path, expected_version: str) -> list[CheckResult]:
    results: list[CheckResult] = []
    meta = _parse_wheel_metadata(wheel_path)

    # name
    if meta["name"].lower() != "visidata":
        results.append(CheckResult(
            "wheel-metadata", False, "error",
            f"wheel Name = {meta['name']!r}, expected 'visidata'",
            str(wheel_path),
        ))
    else:
        results.append(CheckResult(
            "wheel-metadata", True, "error",
            "wheel Name = 'visidata'",
            str(wheel_path),
        ))

    # version (compare normalized)
    actual_norm = _normalize_version(meta["version"])
    expected_norm = _normalize_version(expected_version) if expected_version else ""
    if expected_norm and actual_norm != expected_norm:
        results.append(CheckResult(
            "wheel-version", False, "warn",
            f"wheel Version = {meta['version']!r}, expected {expected_version!r} (normalized: {actual_norm} vs {expected_norm})",
            str(wheel_path),
        ))
    elif meta["version"]:
        results.append(CheckResult(
            "wheel-version", True, "error",
            f"wheel Version = {meta['version']!r}",
            str(wheel_path),
        ))

    # console_scripts
    cs = meta["console_scripts"]
    cs_issues: list[str] = []
    for name, expected_entry in EXPECTED_CONSOLE_SCRIPTS.items():
        if name not in cs:
            cs_issues.append(f"missing: {name}")
        elif cs[name] != expected_entry:
            cs_issues.append(f"{name} = {cs[name]!r} (expected {expected_entry!r})")
    extra_cs = [n for n in cs if n not in EXPECTED_CONSOLE_SCRIPTS]
    for n in extra_cs:
        cs_issues.append(f"unexpected: {n}={cs[n]!r}")

    if cs_issues:
        results.append(CheckResult(
            "wheel-console-scripts", False, "error",
            f"{len(cs_issues)} problem(s) with wheel console_scripts",
            "\n  ".join(cs_issues),
        ))
    else:
        results.append(CheckResult(
            "wheel-console-scripts", True, "error",
            f"wheel console_scripts correct ({len(EXPECTED_CONSOLE_SCRIPTS)} entries)",
            "\n  ".join(f"{k}={v}" for k, v in EXPECTED_CONSOLE_SCRIPTS.items()),
        ))

    return results


# ---------------------------------------------------------------------------
# rule: wheel contents (packages, data files, modules)
# ---------------------------------------------------------------------------

def rule_wheel_contents(wheel_path: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    contents = _wheel_file_set(wheel_path)

    # packages with __init__.py
    code_packages = [p for p in EXPECTED_PACKAGES
                     if p not in ("visidata.guides", "visidata.ddw",
                                  "visidata.man", "visidata.desktop")]
    pkg_issues: list[str] = []
    pkg_ok: list[str] = []
    for pkg in code_packages:
        init_path = pkg.replace(".", "/") + "/__init__.py"
        found = any(p.endswith(init_path) for p in contents)
        if found:
            pkg_ok.append(pkg)
        else:
            pkg_issues.append(pkg)

    if pkg_issues:
        results.append(CheckResult(
            "wheel-contents", False, "error",
            f"{len(pkg_issues)} expected package(s) missing from wheel",
            "\n  ".join(pkg_issues),
        ))
    if pkg_ok:
        results.append(CheckResult(
            "wheel-contents", True, "error",
            f"{len(pkg_ok)} code packages present in wheel",
            "\n  ".join(pkg_ok),
        ))

    # data-only packages: check their data files exist somewhere in the wheel
    data_pkg_specs = {
        "visidata.man": ["vd.1", "visidata.1", "vd.txt"],
        "visidata.ddw": ["input.ddw", "regex.ddw"],
        "visidata.guides": [".md"],
        "visidata.desktop": ["visidata.desktop", "metainfo.xml"],
    }
    data_pkg_missing: list[str] = []
    data_pkg_ok: list[str] = []
    data_pkg_warn: list[str] = []
    for pkg, markers in data_pkg_specs.items():
        pkg_prefix = pkg.replace(".", "/") + "/"
        pkg_files = [p for p in contents if pkg_prefix in p]
        if not pkg_files:
            data_pkg_missing.append(pkg)
            continue
        # package directory exists; check for specific data markers
        any_marker = False
        for p in pkg_files:
            for marker in markers:
                if marker in p:
                    any_marker = True
                    break
            if any_marker:
                break
        if any_marker:
            data_pkg_ok.append(pkg)
        else:
            data_pkg_warn.append(f"{pkg} (has {len(pkg_files)} file(s), but no data markers: {', '.join(markers)})")

    if data_pkg_missing:
        results.append(CheckResult(
            "wheel-contents", False, "error",
            f"{len(data_pkg_missing)} data-only package(s) entirely missing from wheel",
            "\n  ".join(data_pkg_missing),
        ))
    if data_pkg_warn:
        results.append(CheckResult(
            "wheel-contents", False, "warn",
            f"{len(data_pkg_warn)} data-only package(s) missing expected data markers",
            "\n  ".join(data_pkg_warn),
        ))
    if data_pkg_ok:
        results.append(CheckResult(
            "wheel-contents", True, "error",
            f"{len(data_pkg_ok)} data-only packages present in wheel",
            "\n  ".join(data_pkg_ok),
        ))

    return results


# ---------------------------------------------------------------------------
# rule: sdist contents
# ---------------------------------------------------------------------------

def rule_sdist_contents(sdist_path: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    contents = _sdist_file_set(sdist_path)

    required = ["setup.py", "README.md", "requirements.txt", "CHANGELOG.md"]
    missing_core = [f for f in required if f not in contents]

    if missing_core:
        results.append(CheckResult(
            "sdist-contents", False, "error",
            f"{len(missing_core)} required file(s) missing from sdist",
            "\n  ".join(missing_core),
        ))
    else:
        results.append(CheckResult(
            "sdist-contents", True, "error",
            "core files present in sdist",
            "\n  ".join(required),
        ))

    # spot-check some package directories
    for pkg in ["visidata", "visidata/loaders", "visidata/features"]:
        init = f"{pkg}/__init__.py"
        if init not in contents:
            results.append(CheckResult(
                "sdist-contents", False, "error",
                f"expected package directory missing from sdist",
                f"missing {init}",
            ))

    return results


# ---------------------------------------------------------------------------
# rule: after installing wheel in a venv, commands and import work
# ---------------------------------------------------------------------------

def _clean_env() -> dict:
    """Return a clean environment dict for venv subprocess calls (no PYTHONPATH leaking)."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    return env


def rule_installed_wheel(venv_bin_dir: Path, expected_version: str, cwd: Path | None = None) -> list[CheckResult]:
    results: list[CheckResult] = []
    venv_python = venv_bin_dir / "python"
    clean_env = _clean_env()
    safe_cwd = cwd or Path.home()

    # 1. import visidata works and version matches
    rc, out, err = _run_capture(
        [str(venv_python), "-c",
         "import visidata, os; print(visidata.__version__); print(os.path.dirname(visidata.__file__))"],
        echo=False, env=clean_env, cwd=str(safe_cwd),
    )
    if rc != 0:
        results.append(CheckResult(
            "installed-import", False, "error",
            "import visidata failed in venv after wheel install",
            err.strip()[:300],
        ))
    else:
        lines = out.strip().splitlines()
        actual = lines[0].strip() if lines else ""
        actual_norm = _normalize_version(actual)
        expected_norm = _normalize_version(expected_version) if expected_version else ""
        detail = f"imported from: {lines[1].strip() if len(lines) > 1 else 'n/a'}"
        if expected_norm and actual_norm != expected_norm:
            results.append(CheckResult(
                "installed-import", False, "warn",
                f"imported version = {actual!r}, expected {expected_version!r}",
                detail,
            ))
        else:
            results.append(CheckResult(
                "installed-import", True, "error",
                f"`import visidata` works, version = {actual!r}",
                detail,
            ))

    # 2. each console_script works
    # Small delay to ensure pip install has fully written files to disk
    time.sleep(0.2)
    for script in EXPECTED_CONSOLE_SCRIPTS:
        script_path = venv_bin_dir / script
        if not script_path.exists():
            results.append(CheckResult(
                f"installed-cmd-{script}", False, "error",
                f"console_script {script!r} not found after wheel install",
                f"checked at {script_path}",
            ))
            continue
        # vd-dev is a subparser CLI and doesn't accept --version; use -h which exits 0
        test_args = ["-h"] if script == "vd-dev" else ["--version"]
        rc, out, err = _run_capture([str(script_path)] + test_args, echo=False, env=clean_env, cwd=str(safe_cwd))
        if rc != 0:
            results.append(CheckResult(
                f"installed-cmd-{script}", False, "error",
                f"`{script} {' '.join(test_args)}` failed",
                err.strip()[:300] or f"exit code {rc}",
            ))
        else:
            if script == "vd-dev":
                results.append(CheckResult(
                    f"installed-cmd-{script}", True, "error",
                    f"`{script}` is installed and invokable",
                    "help output printed successfully",
                ))
            else:
                ver_line = out.strip().splitlines()[-1] if out.strip() else "(no output)"
                results.append(CheckResult(
                    f"installed-cmd-{script}", True, "error",
                    f"`{script} --version` works",
                    ver_line,
                ))

    return results


# ---------------------------------------------------------------------------
# shared report formatter
# ---------------------------------------------------------------------------

def _report_results(results: list[CheckResult], header: str) -> tuple[int, int, int]:
    """Print formatted results. Return (passed, errors, warnings)."""
    _print_info(f"=== {header} ===")
    errors = 0
    warnings = 0
    passed = 0
    for r in results:
        print(r.format())
        if r.ok:
            passed += 1
        elif r.severity == "warn":
            warnings += 1
        else:
            errors += 1
    total = len(results)
    _print_info(
        f"summary: {passed}/{total} passed, "
        f"{errors} error(s), {warnings} warning(s)"
    )
    return passed, errors, warnings


# ═══════════════════════════════════════════════════════════════════════
# preflight (release readiness checks)
# ═══════════════════════════════════════════════════════════════════════

def cmd_preflight_check(args: argparse.Namespace) -> int:
    """Run pre-release checks using the shared validation rule set."""
    all_results: list[CheckResult] = []

    all_results += rule_version_consistency()

    version = _read_version_setup() or _read_version_init()
    if version:
        all_results += rule_changelog_entry(version)

    all_results += rule_source_data_files()
    all_results += rule_setup_entry_points()
    all_results += rule_setup_packages_list()
    all_results += rule_install_consistency("dev")

    passed, errors, warnings = _report_results(all_results, "preflight check")

    if errors > 0:
        _print_err(f"preflight check FAILED: {errors} error(s), {warnings} warning(s)")
        return EXIT_ERR
    _print_info(f"preflight check PASSED: {passed} passed, {warnings} warning(s)")
    return EXIT_OK


def cmd_preflight_smoke(args: argparse.Namespace) -> int:
    """Fast pre-release smoke test: import + version + basic load."""
    _print_info("import visidata...")
    rc, out, err = _run_capture(
        [sys.executable, "-c", "import visidata; print(visidata.__version_info__)"],
        echo=False,
    )
    if rc != 0:
        _print_err(f"import failed: {err.strip()}")
        return rc
    _print_info(f"  {out.strip()}")

    _print_info("vd --version...")
    rc = cmd_test_smoke(args)
    if rc != 0:
        return rc

    _print_info("preflight smoke OK")
    return EXIT_OK


def setup_preflight(sub) -> None:
    p = sub.add_parser("preflight", help="release readiness checks")
    pf_sub = p.add_subparsers(dest="mode", metavar="<mode>")
    pf_sub.required = False

    for name, func, help_text in [
        ("check", cmd_preflight_check, "full release readiness check (default)"),
        ("smoke", cmd_preflight_smoke, "fast import + version smoke test"),
    ]:
        sp = pf_sub.add_parser(name, help=help_text)
        sp.set_defaults(func=func)

    p.set_defaults(func=cmd_preflight_check, mode="check")


# ═══════════════════════════════════════════════════════════════════════
# package (build and verify distribution artifacts)
# ═══════════════════════════════════════════════════════════════════════

def cmd_package_build(args: argparse.Namespace) -> int:
    """Build sdist + wheel into dist/."""
    dist = ROOT / "dist"
    build = ROOT / "build"
    for d in (dist, build):
        if d.exists():
            _print_info(f"remove {d.relative_to(ROOT)}/")
            shutil.rmtree(d)

    rc = _run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "build"])
    if rc != 0:
        _print_warn("could not upgrade build; will try to use installed version")

    rc = _run([sys.executable, "-m", "build"])
    if rc != 0:
        return rc

    # chmod -R a+rX dist
    for root, dirs, files in os.walk(dist):
        for d in dirs:
            os.chmod(os.path.join(root, d), 0o755)
        for f in files:
            os.chmod(os.path.join(root, f), 0o644)

    _print_info("built artifacts:")
    for f in sorted(dist.iterdir()):
        size = f.stat().st_size
        _print_info(f"  {f.name}  ({size:,} bytes)")
    return EXIT_OK


def cmd_package_verify(args: argparse.Namespace) -> int:
    """Verify built packages using the shared validation rule set."""
    dist = ROOT / "dist"
    all_results: list[CheckResult] = []

    if not dist.exists() or not any(dist.iterdir()):
        all_results.append(CheckResult(
            "artifact-existence", False, "error",
            "no artifacts in dist/",
            "run `vd-dev package build` first",
        ))
        _report_results(all_results, "package verify")
        return EXIT_ERR

    # 1. Artifact existence
    exist_results, sdist_path, wheel_path = rule_artifact_existence(dist)
    all_results += exist_results

    expected_version = _read_version_setup() or _read_version_init()

    # 2. Wheel metadata + contents
    if wheel_path:
        all_results += rule_wheel_metadata(wheel_path, expected_version)
        all_results += rule_wheel_contents(wheel_path)

    # 3. Sdist contents
    if sdist_path:
        all_results += rule_sdist_contents(sdist_path)

    # 4. Install wheel in temp venv and verify commands + import
    if wheel_path:
        import venv as _venv
        tmpdir = Path(tempfile.mkdtemp(prefix="vd-pkgverify-"))
        try:
            venv_dir = tmpdir / "venv"
            _venv.create(venv_dir, with_pip=True)
            venv_bin_dir = venv_dir / "bin"
            venv_python = venv_bin_dir / "python"
            if not venv_python.exists():
                venv_bin_dir = venv_dir / "Scripts"
                venv_python = venv_bin_dir / "python.exe"

            rc, out, err = _run_capture(
                [str(venv_python), "-m", "pip", "install", str(wheel_path)],
                echo=False, env=_clean_env(), cwd=str(tmpdir),
            )
            if rc != 0:
                all_results.append(CheckResult(
                    "wheel-install", False, "error",
                    "pip install from wheel failed in venv",
                    err.strip()[:300] or f"exit code {rc}",
                ))
            else:
                all_results.append(CheckResult(
                    "wheel-install", True, "error",
                    "pip install from wheel succeeded in fresh venv",
                    str(wheel_path),
                ))
                all_results += rule_installed_wheel(venv_bin_dir, expected_version, cwd=tmpdir)
        except Exception as e:
            all_results.append(CheckResult(
                "wheel-install", False, "warn",
                f"could not verify wheel in venv: {e}",
            ))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    passed, errors, warnings = _report_results(all_results, "package verify")

    if errors > 0:
        _print_err(f"package verify FAILED: {errors} error(s), {warnings} warning(s)")
        return EXIT_ERR
    _print_info(f"package verify PASSED: {passed} passed, {warnings} warning(s)")
    return EXIT_OK


def cmd_package_clean(args: argparse.Namespace) -> int:
    """Remove dist/ and build/ directories."""
    for dname in ("dist", "build"):
        d = ROOT / dname
        if d.exists():
            _print_info(f"remove {dname}/")
            shutil.rmtree(d)
    return EXIT_OK


def _cmd_package_all(args: argparse.Namespace) -> int:
    rc = cmd_package_build(args)
    if rc != 0:
        return rc
    return cmd_package_verify(args)


def setup_package(sub) -> None:
    p = sub.add_parser("package", help="build and verify distribution artifacts")
    pkg_sub = p.add_subparsers(dest="mode", metavar="<mode>")
    pkg_sub.required = False

    for name, func, help_text in [
        ("all", _cmd_package_all, "build + verify (default)"),
        ("build", cmd_package_build, "build sdist + wheel into dist/"),
        ("verify", cmd_package_verify, "verify built packages can be installed"),
        ("clean", cmd_package_clean, "remove dist/ and build/"),
    ]:
        sp = pkg_sub.add_parser(name, help=help_text)
        sp.set_defaults(func=func)

    p.set_defaults(func=_cmd_package_all, mode="all")


# ═══════════════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vd-dev",
        description="Unified VisiData development CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-C", "--cwd", type=Path, default=ROOT,
                        help="project root directory (default: repo root)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    setup_install(sub)
    setup_test(sub)
    setup_build(sub)
    setup_lint(sub)
    setup_setup(sub)
    setup_diff_test(sub)
    setup_clean(sub)
    setup_check(sub)
    setup_preflight(sub)
    setup_package(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE

    if args.cwd:
        global ROOT
        ROOT = args.cwd.resolve()

    try:
        return args.func(args) or EXIT_OK
    except KeyboardInterrupt:
        _print_err("interrupted")
        return EXIT_ERR
    except Exception as e:
        _print_err(f"{type(e).__name__}: {e}")
        if os.environ.get("VD_DEV_DEBUG"):
            raise
        return EXIT_ERR


def vd_dev_cli() -> NoReturn:
    sys.exit(main())


if __name__ == "__main__":
    vd_dev_cli()
