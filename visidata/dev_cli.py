#!/usr/bin/env python3
"""
vd-dev: Unified VisiData development command-line interface.

All core development actions are implemented directly in this module in Python;
the legacy shell scripts in dev/ and tests/ are thin compatibility wrappers
that delegate here.

Usage:
    vd-dev <command> [options] [args...]

Commands:
    install     Install package with optional extras
    test        Run tests (all, golden, unit, vgit, vdsql, smoke, perf, individual)
    build       Build resources (man, zsh, docker, all)
    lint        Run ruff linter
    setup       Setup dev environment (hooks, vscode, all)
    diff-test   Run git-diff-based tests
    clean       Remove generated files
    check       Run comprehensive check (lint + test)

Run `vd-dev <command> --help` for command-specific help.
"""

from __future__ import annotations

import argparse
import difflib
import glob
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
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

def cmd_install(args: argparse.Namespace) -> int:
    mode = args.mode
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
    p.add_argument("mode", nargs="?", default="dev",
                   choices=["dev", "test", "all", "prod"],
                   help="installation mode (default: dev)")
    p.set_defaults(func=cmd_install)


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

def cmd_test(args: argparse.Namespace) -> int:
    mode = args.mode

    if mode == "all":
        return cmd_test_all(args)
    if mode == "golden":
        return cmd_test_golden(args)
    if mode == "unit":
        extra = args.extra_args or []
        if extra:
            return _run([sys.executable, "-m", "pytest", *extra])
        return _run([sys.executable, "-m", "pytest", "visidata/tests/"])
    if mode == "vgit":
        extra = args.extra_args or []
        cmd = (
            f"{sys.executable} -m visidata --config tests/.visidatarc"
            f" -p visidata/apps/vgit/tests/*.vdx --batch"
        )
        if extra:
            cmd += " " + " ".join(extra)
        return _run(cmd, shell=True)
    if mode == "vdsql":
        extra = args.extra_args or []
        return _run(["bash", "./test.sh", *extra], cwd=ROOT / "visidata/apps/vdsql")
    if mode == "smoke":
        return cmd_test_smoke(args)
    if mode == "perf":
        return _run(["bash", "tests/test-perf.sh", *(args.extra_args or [])])
    if mode == "individual":
        return cmd_test_individual(args)

    _die(f"unknown test mode: {mode}")


def setup_test(sub) -> None:
    p = sub.add_parser("test", help="run tests")
    p.add_argument("mode", nargs="?", default="all",
                   choices=["all", "golden", "unit", "vgit", "vdsql",
                            "smoke", "perf", "individual"],
                   help="which test suite to run (default: all)")
    p.add_argument("extra_args", nargs=argparse.REMAINDER,
                   help="extra arguments passed to underlying test runner")
    p.set_defaults(func=cmd_test)


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
        rc = _run([sys.executable, "dev/zsh-completion.py", "_visidata"])
        if mode == "zsh" or rc != 0:
            return rc
    if mode == "docker":
        return _run(["bash", "dev/build-container"])
    if mode == "all":
        return EXIT_OK
    _die(f"unknown build mode: {mode}")


def setup_build(sub) -> None:
    p = sub.add_parser("build", help="build resources (man pages, completions, docker)")
    p.add_argument("mode", nargs="?", default="all",
                   choices=["all", "man", "zsh", "docker"],
                   help="what to build (default: all)")
    p.set_defaults(func=cmd_build)


# ═══════════════════════════════════════════════════════════════════════
# lint
# ═══════════════════════════════════════════════════════════════════════

def cmd_lint(args: argparse.Namespace) -> int:
    if args.extra_args:
        return _run([sys.executable, "-m", "ruff", "check", *args.extra_args])
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
    p.add_argument("mode", nargs="?", default="all",
                   choices=["all", "hooks", "vscode"],
                   help="what to setup (default: all)")
    p.set_defaults(func=cmd_setup)


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
