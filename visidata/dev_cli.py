#!/usr/bin/env python3
"""
vd-dev: Unified VisiData development command-line interface.

Usage:
    vd-dev <command> [options] [args...]

Commands:
    install     Install package with optional extras
    test        Run tests (golden, unit, vgit, vdsql, all, smoke, perf, individual)
    build       Build resources (man, zsh, docker, all)
    lint        Run ruff linter
    setup       Setup dev environment (hooks, vscode, all)
    diff-test   Run diff-based tests
    clean       Remove generated files
    check       Run comprehensive check (lint + test)

Run `vd-dev <command> --help` for command-specific help.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, NoReturn

ROOT = Path(__file__).resolve().parent.parent

RESET = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BOLD = "\033[1m"

EXIT_OK = 0
EXIT_ERR = 1
EXIT_USAGE = 2


def _print_err(msg: str) -> None:
    print(f"{RED}{BOLD}error:{RESET} {msg}", file=sys.stderr)


def _print_warn(msg: str) -> None:
    print(f"{YELLOW}{BOLD}warn:{RESET} {msg}", file=sys.stderr)


def _print_info(msg: str) -> None:
    print(f"{GREEN}{BOLD}info:{RESET} {msg}")


def _run(cmd, **kwargs) -> int:
    """Run a command, returning exit code. Output is passed through.

    cmd may be a list[str] (preferred) or a str (when shell=True).
    """
    cwd = kwargs.pop("cwd", ROOT)
    shell = kwargs.get("shell", False)
    if isinstance(cmd, list):
        display = " ".join(cmd)
    else:
        display = cmd
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


def _die(msg: str, code: int = EXIT_ERR) -> NoReturn:
    _print_err(msg)
    sys.exit(code)


# ── install ──────────────────────────────────────────────────────────

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


def setup_install(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("install", help="install package with optional extras")
    p.add_argument("mode", nargs="?", default="dev",
                   choices=["dev", "test", "all", "prod"],
                   help="installation mode (default: dev)")
    p.set_defaults(func=cmd_install)


# ── test ─────────────────────────────────────────────────────────────

def cmd_test(args: argparse.Namespace) -> int:
    mode = args.mode
    extra = args.extra_args

    if mode == "all":
        return _run(["bash", "dev/test-all.sh", *extra])

    if mode == "golden":
        return _run(["bash", "tests/test-vdx.sh", *extra])

    if mode == "unit":
        if extra:
            return _run([sys.executable, "-m", "pytest", *extra])
        return _run([sys.executable, "-m", "pytest", "visidata/tests/"])

    if mode == "vgit":
        cmd = f"{sys.executable} -m visidata --config tests/.visidatarc -p visidata/apps/vgit/tests/*.vdx --batch"
        if extra:
            cmd += " " + " ".join(extra)
        return _run(cmd, shell=True)

    if mode == "vdsql":
        return _run(["bash", "./test.sh", *extra], cwd=ROOT / "visidata/apps/vdsql")

    if mode == "smoke":
        return _run(["bash", "tests/test-smoke.sh", *extra])

    if mode == "perf":
        return _run(["bash", "tests/test-perf.sh", *extra])

    if mode == "individual":
        return _run(["bash", "dev/run-tests-individually.sh", *extra])

    _die(f"unknown test mode: {mode}")


def setup_test(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("test", help="run tests")
    p.add_argument("mode", nargs="?", default="all",
                   choices=["all", "golden", "unit", "vgit", "vdsql",
                            "smoke", "perf", "individual"],
                   help="which test suite to run (default: all)")
    p.add_argument("extra_args", nargs=argparse.REMAINDER,
                   help="extra arguments passed to underlying test runner")
    p.set_defaults(func=cmd_test)


# ── build ────────────────────────────────────────────────────────────

def cmd_build(args: argparse.Namespace) -> int:
    mode = args.mode
    if mode == "all":
        rc = _run(["bash", "dev/mkman.sh"])
        if rc != 0:
            return rc
        return _run([sys.executable, "dev/zsh-completion.py", "_visidata"])
    if mode == "man":
        return _run(["bash", "dev/mkman.sh"])
    if mode == "zsh":
        return _run([sys.executable, "dev/zsh-completion.py", "_visidata"])
    if mode == "docker":
        return _run(["bash", "dev/build-container"])
    _die(f"unknown build mode: {mode}")


def setup_build(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("build", help="build resources (man pages, completions, docker)")
    p.add_argument("mode", nargs="?", default="all",
                   choices=["all", "man", "zsh", "docker"],
                   help="what to build (default: all)")
    p.set_defaults(func=cmd_build)


# ── lint ─────────────────────────────────────────────────────────────

def cmd_lint(args: argparse.Namespace) -> int:
    if args.extra_args:
        return _run([sys.executable, "-m", "ruff", "check", *args.extra_args])
    return _run([sys.executable, "-m", "ruff", "check", "."])


def setup_lint(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("lint", help="run ruff linter")
    p.add_argument("extra_args", nargs=argparse.REMAINDER,
                   help="extra arguments passed to ruff")
    p.set_defaults(func=cmd_lint)


# ── setup ────────────────────────────────────────────────────────────

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


def setup_setup(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("setup", help="setup development environment")
    p.add_argument("mode", nargs="?", default="all",
                   choices=["all", "hooks", "vscode"],
                   help="what to setup (default: all)")
    p.set_defaults(func=cmd_setup)


# ── diff-test ────────────────────────────────────────────────────────

def cmd_diff_test(args: argparse.Namespace) -> int:
    return _run(["bash", "dev/diff-test.sh"])


def setup_diff_test(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("diff-test", help="run git-diff-based tests")
    p.set_defaults(func=cmd_diff_test)


# ── clean ────────────────────────────────────────────────────────────

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


def setup_clean(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("clean", help="remove generated files")
    p.set_defaults(func=cmd_clean)


# ── check ────────────────────────────────────────────────────────────

def cmd_check(args: argparse.Namespace) -> int:
    _print_info("=== lint ===")
    rc = _run([sys.executable, "-m", "ruff", "check", "."])
    if rc != 0:
        _print_err("lint failed; fix issues before proceeding")
        return rc
    _print_info("=== tests ===")
    return _run(["bash", "dev/test-all.sh"])


def setup_check(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("check", help="comprehensive check (lint + test)")
    p.set_defaults(func=cmd_check)


# ── main ─────────────────────────────────────────────────────────────

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
