# dev/

Resources for VisiData contributors and maintainers.

## Contributor Guides

| File | Description |
|------|-------------|
| `STYLE.md` | Code style guide |
| `GIT.md` | Git workflow and branching conventions |
| `DOCS.md` | Documentation standards |
| `PERFORMANCE.md` | Performance guidelines |
| `TESTING.md` | Test framework guide (golden tests, pytest, writing new tests) |

## Checklists

| File | Description |
|------|-------------|
| `checklists/release.md` | Release process checklist |
| `checklists/feature.md` | New feature checklist (docs, tests, marketing, sample data) |
| `checklists/manual-tests.md` | Manual testing checklist (cmdlog, replay, options, split window, etc.) |
| `checklists/add-command.md` | Adding a new command |
| `checklists/add-aggregator.md` | Adding a new aggregator |

## Design Docs

See [design/README.md](design/README.md) for full index.

## Scripts

All development actions are unified under the `vd-dev` CLI (installed with the package, or run via `python3 -m visidata.dev_cli`).

| Command | Description |
|---------|-------------|
| `vd-dev install [dev|test|all|prod] [--check]` | Install package; `--check` verifies mode without installing |
| `vd-dev test golden` | Run test suite (batched, fast; `-d` for debug; see `TESTING.md`) |
| `vd-dev test individual` | Run each test in its own process (isolated, slower; see `TESTING.md`) |
| `vd-dev test all` | Run all test suites |
| `vd-dev build man` | Generate manpage |
| `vd-dev build zsh` | Generate zsh completions |
| `vd-dev lint` | Run ruff linter |
| `vd-dev check` | Comprehensive check (lint + all tests) |
| `vd-dev preflight check` | Release readiness checks (version, files, install) |
| `vd-dev preflight smoke` | Fast import + version smoke test |
| `vd-dev package build` | Build sdist + wheel into dist/ |
| `vd-dev package verify` | Verify built packages can be installed and imported |
| `vd-dev package all` | Build + verify (default) |
| `vd-dev package clean` | Remove dist/ and build/ |
| `vd-dev setup hooks` | Configure git hooks |
| `vd-dev setup vscode` | Configure VS Code settings |
| `vd-dev diff-test` | Diff-based test runner |
| `vd-dev clean` | Remove generated files |

Run `vd-dev --help` and `vd-dev <command> --help` for full details.

The underlying shell/Python scripts in this directory (`mkman.sh`, `zsh-completion.py`, etc.) are implementation details and are invoked by `vd-dev` as needed.

## Data Files

| File | Description |
|------|-------------|
| `formats.jsonl` | Format metadata (builds visidata.org/formats) |
| `types.jsonl` | Type system structure |

## Test Fixtures

| File | Description |
|------|-------------|
| `stdin.vdj` | Used in CI (`.github/workflows/main.yml`) |
| `quit.vdx` | Quit test fixture |
| `formats.vd` | Format test fixture |
| `vduplot.vdx` | Unicode plot test |

## Packaging

| File | Description |
|------|-------------|
| `debian/` | Debian packaging files |
| `build-container` | Container build script |
| `visidata-brew.rb` | Homebrew formula (stale — pins v1.2) |
| `requirements-dev.txt` | Dev dependencies |

## Media

| File | Description |
|------|-------------|
| `kb-blank.svg` | Blank keyboard layout template |
| `kb-layout.svg` | VisiData keyboard layout |
| `vdlogo.svg` | VisiData logo |

## Other

| File | Description |
|------|-------------|
| `zsh-completion.in` | Zsh completion template |
| `workshop-outline.md` | Workshop outline |
