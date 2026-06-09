# Release process for the next `stable` version

## Preflight: Auto-Fix + Consistency Checks (Run First)

The preflight toolchains version synchronization, manpage generation, and
consistency validation into a single engineering entry point. Always run it
before manual release steps.

### Quick Start

```bash
# Recommended: auto-fix what can be auto-fixed, then validate
make preflight-fix

# Or: just rebuild docs and run checks (no file mutations beyond manpage generation)
make preflight
```

### What It Does

**Auto-Fixers** (run by `make preflight-fix` or `python3 dev/preflight_check.py --fix`):

| Fixer | What it does |
|---|---|
| `--fix-version` | Syncs version numbers from the **canonical source** `visidata/__init__.py`. Display version (e.g. `3.4dev`) goes to `visidata/main.py` and `README.md`; PEP 440 mapped version (e.g. `3.4.dev0`) goes to `setup.py`. See "Version Mapping" below. |
| `--fix-date`    | Updates the `.Dd` date line in `visidata/man/vd.inc` to today |
| `--fix-docs`    | Rebuilds manpages (`vd.1`, `visidata.1`, `vd.txt`, `docs/man.md`) via `dev/mkman.sh` (requires `soelim`, `preconv` from groff; optionally `man`, `aha`). Missing tools produce a **WARN** (non-fatal) so version/date fixes still apply; the docs check will flag missing artifacts explicitly. |

**Version Mapping Rules** (display version ↔ PEP 440 package version):

| Display (for UI, README, manpage) | PEP 440 (for setup.py, wheel/sdist metadata) |
|---|---|
| `3.4dev`  | `3.4.dev0` |
| `3.4`     | `3.4`       (identical) |
| `3.4.1`   | `3.4.1`     (identical) |

Only `visidata/__init__.py` is the canonical source of truth. Never edit `setup.py`'s `__version__` by hand; run `--fix-version` instead.

**Fixer status semantics**:
- `[OK]`   — no changes needed, already correct
- `[DONE]` — file(s) updated successfully
- `[WARN]` — skipped (e.g. missing tools); non-fatal so other fixers still run. The matching checker (e.g. `docs`, `metadata`) will flag the underlying problem as `[FAIL]` at the check stage.
- `[FAIL]` — hard error (e.g. canonical version file missing); aborts the fix phase.

**Checkers** (always run after fixes):

| Check | What it validates |
|---|---|
| `version`   | Display-version files agree, PEP 440 files agree, and the two sides map correctly |
| `imports`   | All submodules under `features/`, `loaders/`, `themes/` have valid Python syntax and are declared in `setup.py packages` |
| `cli`       | `console_scripts` entry points resolve to real callables; `bin/vd` and `visidata/__main__.py` reference `visidata.main:vd_cli` |
| `docs`      | Manpage artifacts (`vd.1`, `visidata.1`, `vd.txt`, `docs/man.md`) exist on disk |
| `formats`   | Every internal format in `docs/internal_formats.md` has a matching `open_<ext>()` function somewhere in the package |
| `metadata`  | All paths referenced by `MANIFEST.in`, `setup.py package_data`, and `setup.py data_files` exist |
| `changelog` | `CHANGELOG.md` contains a `# vX.Y` heading for the current version |

### Diagnostics

```bash
# List all available checks and fixers
make preflight-checks
# or:
python3 dev/preflight_check.py --list

# Run only specific checks
python3 dev/preflight_check.py version imports metadata

# Run only a specific fixer
python3 dev/preflight_check.py --fix-version       # just sync version numbers
python3 dev/preflight_check.py --fix-date          # just update manpage date
python3 dev/preflight_check.py --fix-docs          # just rebuild manpages
```

### Manpage Build Dependencies

If `--fix-docs` complains about missing tools:

```bash
# macOS
brew install groff    # provides soelim, preconv
brew install aha      # provides aha (ANSI->HTML for man.txt)
```

---

1. Merge `stable` to `develop` (if necessary)

2. Verify that documentation/docstrings are up-to-date on features and functionality

    a. CHANGELOG;

     git log --pretty=format:"%s :%ae" $(git tag | tail -1)..HEAD

    b. manpage;

    c. visidata.org; (formats?)
        - remember to check the tables with prettier: https://github.com/saulpw/visidata/pull/2056

3. Ensure `develop` automated tests run correctly with dev/test.sh

4. Go through the manual tests checklist

5. Verify that setup.py is up-to-date with requirements.

    a. Review current dependency versions and if a new version of Python has been released.

    b. Update any new plugins or extras in setup.py

5. Set version number to next most reasonable number (v#.#.#)

   a. add to front of CHANGELOG, along with the release date and bullet points of major changes;

   b. **(auto)** `python3 dev/preflight_check.py --fix-date` updates the date in the manpage;

   c. **(auto)** `python3 dev/preflight_check.py --fix-version` updates version number on README and setup.py and visidata/main.py;

   d. bump version in `__version__` in the canonical source `visidata/__init__.py` (this is what the auto-fixer syncs from);

6. **(auto)** `python3 dev/preflight_check.py --fix-docs` runs dev/mkman.sh to build the manpage and updated website
    - Run ./mkmanhtml.sh, and move that to visidata.org:site/docs/man, and to visidata:docs/man.md

7. Run `make preflight` one final time to confirm everything is green before merging.

8. Merge `develop` to stable

(... rest of the existing release steps continue unchanged ...)

14. motd
    a. Upload new motd for new version.
    b. Test that VisiData downloads motd.


8. Merge `stable` back into other branches

    a. if the branch works with minimal conflicts, keep the branch

    b. otherwise, clean out the branch


9. Push code to stable

10. Push `stable` to pypi

    a. set up a ~/.pypirc

    ```
    [distutils]
    index-servers=
        pypi
        testpypi
    [pypi]
    repository:https://upload.pypi.org/legacy/
    username:
    password:

    [testpypi]
    repository: https://test.pypi.org/legacy
    username:
    password:
    ```


  Push to pypi
    ```
    rm -rf dist/
    rm -rf build/
    python3 setup.py sdist bdist_wheel
    chmod -R a+rX dist
    ls dist/
    twine upload dist/*
    ```

11. Test install/upgrade from pypi

  a. Build and deploy the website

   b. Ask someone else to test install

12. Create a tag `v#.#.#` for that commit

```
git tag v#.#.#
git push --tags
```

13. Write up the release notes and add it to `www/releases.md`. Add it to index.md.


15. Update the website by pushing to master. Update with new manpage. Update redirect to point to new manpage.
    - release notes

16. Comb through issues and close the ones that have been solved, referencing the version number

17. Post github release notes on patreon.

18. Update the other distributions.

# conda

Registry: https://github.com/conda-forge/visidata-feedstock/

1. Fork https://github.com/conda-forge/visidata-feedstock. Open the recipe/meta.yaml.

2. Update the VisiData version and sha256.

3. Make any necessary removals, additions or modifications to the dependencies -> note that a dependency must be part of conda.

4. Make a PR.

5. Comment `@conda-forge-admin, please rerender` in the PR.

6. Merge the PR, when everything is green.


# Homebrew

Registry: https://github.com/saulpw/homebrew-vd

1. Open the Formula/visidata.rb file. Update the link in url to the new visidata tar.gz file.
2. Update the sha256 and version in visidata.rb. Update the version in README.md.
3. For major version ships, check each dependency and see if it has been updated. If so, update the url and sha256 for the newest version.
4. On a mac, test the formula with `brew install --build-from-source visidata`. Fix as needed.
5. Audit the formula with `brew audit --new-formula visidata`
6. Add and commit the formula.

## Debian
1. Download the visidata tar.gz file from pypi
2. tar -xzmf visidata.tar.gz
3. cp visidata.ver.tar.gz visidata_ver.orig.tar.gz
4. cd visidata-ver/
5. Place there the contents of the debian directory from git@salsa.debian.org:anjakefala/visidata.git
6. Update changelog
```
dch -v new_version
```

where new_version = "$version"-1

Edit as necessary. I usually set stability to unstable, and urgency to low, and add a small changelog.
7. Run debuild. Fix errors as they come up.
8. If a package fails to import a module, it must be added to the build dependencies as python3-modules
9. cd ..
10. If unsigned by debuild, sign the changes files
```
debsign -k keycode visidata_ver.changes
```

anjakefala has the key and password.
11. Upload to debian mentors and contact the mentor, [Martin](https://qa.debian.org/developer.php?email=debacle%40debian.org).
```
dput mentors visidata_ver.changes
```

12. Copy the fresh contents of the debian folder back into visidata/dev/debian and in debian-visidata (https://salsa.debian.org/anjakefala/visidata).

## deb-vd
Private registry: https://github.com/saulpw/deb-vd
1. Enter saulpw/deb-vd.
2. Run the command reprepro includedeb sid new-vd.deb
3. Update the README.md with the new version, commit *all* the changes and push to master.
