# Repository Guidelines

## Start Here (Progressive Disclosure)
- Read `CLAUDE.md` first for the primary contributor workflow and architecture context.
- Use focused deep dives only when needed:
  - `dev/STYLE.md` for coding patterns and API conventions.
  - `dev/GIT.md` for commit and branch practices.
  - `dev/DOCS.md` for documentation syntax and style.
  - `dev/PERFORMANCE.md` for profiling and optimization work.
- Human gate is required: all AI-assisted code must be reviewed, approved, and tested by a human before merge or PR.

## Project Structure
- `visidata/`: main package (`features/`, `loaders/`, `apps/`, `themes/`).
- `tests/`: functional replay tests (`.vd/.vdj/.vdx`) and `tests/golden/` outputs.
- `visidata/tests/`: Python unit tests.
- `docs/` and `dev/`: user and developer documentation.

## Build, Test, and Development Commands
- `vd-dev install dev`: editable install with dev dependencies
- `vd-dev install test`: install with test dependencies
- `vd-dev install all`: install with all optional dependencies
- `vd --version`: startup sanity check
- `vd-dev test unit`: run unit tests (pytest)
- `vd-dev test golden`: run functional cmdlog tests
- `vd-dev test all`: run all test suites
- `vd-dev lint`: run ruff linter
- `vd-dev check`: comprehensive check (lint + all tests)
- `vd-dev build`: build man pages and zsh completions
- `vd . --batch`: quick load smoke test

Or equivalently via `make`: `make install-dev`, `make test`, `make lint`, `make check`, etc.

## Coding and Command Conventions
- Follow `dev/STYLE.md` for naming, quoting, decorators, and sheet/column patterns.
- Use the canonical command pattern shown in `CLAUDE.md`:
  - `BaseSheet.addCommand('', 'command-name', 'code', 'help text')`
- For class placement decisions and exceptions, defer to `dev/STYLE.md`.

## Testing Guidelines
- Update `tests/*.vd*` for workflow-visible behavior changes.
- Update `visidata/tests/` for isolated Python logic.
- Before PR: run `vd-dev check` (or equivalently `vd-dev lint` and `vd-dev test all`).

## Commit & Pull Request Guidelines
- Base PRs on `develop` (not `stable`).
- Follow existing short, imperative subjects; optional scope prefixes like `[test]`, `[docs]`, `[vdsql]`.
- Keep commits focused and include tests/docs for behavior changes.
- PRs should include problem, approach, risks, and linked issues; add screenshots/gifs and reproducible `.vd` scripts for UI changes.
- Follow CAA and copyright assignment requirements in `CONTRIBUTING.md`.
