"""Delivery Package feature for exporting reproducible VisiData workspaces.

Exports a single workspace.vdx entry point that, when replayed via `vd -p workspace.vdx`,
will:
  1. Restore all macro bindings (from bundled .vdj files in config/macros/)
  2. Set all non-default global and sheet-class options
  3. Open each data sheet (with column types preserved via .vds)
  4. Rebuild GraphSheets with full state (xcols/ycols/reflines/visibleBox)
  5. Replay the original command log (with conflicting open-file commands filtered)
"""

import datetime
import inspect
import json
import os
import shutil
import tempfile
import zipfile

from visidata import vd, VisiData, BaseSheet, GraphSheet, Path, Progress, BoundingBox
from visidata import globalCommand, Sheet, AttrDict, Option
import visidata


vd.option('delivery_data_format', 'vds', 'default data format for delivery package: vds|tsv|csv|json', replay=True)
vd.option('delivery_include_derived', True, 'include derived sheets in delivery package', replay=True)
vd.option('delivery_include_graphs', True, 'include graph sheets in delivery package', replay=True)
vd.option('delivery_include_cmdlog', True, 'include command log in delivery package', replay=True)
vd.option('delivery_include_macros', True, 'include macro files in delivery package', replay=True)
vd.option('delivery_include_config', True, 'include options/configuration in delivery package', replay=True)


def _sanitize_filename(name):
    keepcharacters = (' ', '.', '_', '-')
    return ''.join(c for c in name if c.isalnum() or c in keepcharacters).rstrip().replace(' ', '_')


def _collect_derived_sheets(sheet):
    derived = []
    seen = set()
    for vs in vd.allSheets:
        if id(vs) in seen:
            continue
        src = getattr(vs, 'source', None)
        while isinstance(src, BaseSheet):
            if src is sheet:
                derived.append(vs)
                seen.add(id(vs))
                break
            src = getattr(src, 'source', None)
    return derived


def _collect_graph_sheets(sheets):
    graphs = []
    seen = set()
    for vs in sheets:
        if isinstance(vs, GraphSheet) and id(vs) not in seen:
            graphs.append(vs)
            seen.add(id(vs))
    for vs in vd.allSheets:
        if isinstance(vs, GraphSheet) and id(vs) not in seen:
            src = getattr(vs, 'source', None)
            while isinstance(src, BaseSheet):
                if src in sheets:
                    graphs.append(vs)
                    seen.add(id(vs))
                    break
                src = getattr(src, 'source', None)
    return graphs


def _collect_scope_sheets(scope):
    if scope == 'current':
        return [vd.activeSheet]
    elif scope == 'current_derived':
        sheets = [vd.activeSheet]
        sheets.extend(_collect_derived_sheets(vd.activeSheet))
        return sheets
    elif scope == 'all':
        return list(vd.allSheets)
    elif scope == 'stacked':
        return list(vd.stackedSheets)
    else:
        return [vd.activeSheet]


def _graph_state(graph):
    state = {
        'name': graph.name,
        'sourceName': graph.source.name if isinstance(graph.source, BaseSheet) else str(graph.source),
        'xcols': [c.name for c in getattr(graph, 'xcols', [])],
        'ycols': [c.name for c in getattr(graph, 'ycols', [])],
        'reflines_x': [float(x) for x in getattr(graph, 'reflines_x', [])],
        'reflines_y': [float(y) for y in getattr(graph, 'reflines_y', [])],
        'visibleBox': None,
    }
    vb = getattr(graph, 'visibleBox', None)
    if vb:
        state['visibleBox'] = {
            'xmin': float(vb.xmin),
            'xmax': float(vb.xmax),
            'ymin': float(vb.ymin),
            'ymax': float(vb.ymax),
        }
    return state


@VisiData.api
def restore_graph(vd, json_input):
    """Replay command: restore a GraphSheet from serialized JSON state."""
    try:
        state = json.loads(json_input)
    except (json.JSONDecodeError, TypeError) as e:
        vd.fail(f'invalid graph state JSON: {e}')
        return

    src_sheet = None
    if isinstance(vd.activeSheet, BaseSheet):
        src_sheet = vd.activeSheet
    else:
        src_name = state.get('sourceName', '')
        src_sheet = vd.getSheet(src_name)

    if not src_sheet:
        vd.warning(f'could not find source sheet for graph {state.get("name")}, skipping')
        return

    src_sheet.ensureLoaded()
    vd.sync()

    all_cols = list(getattr(src_sheet, 'columns', []))
    xcols = [c for c in all_cols if c.name in state.get('xcols', [])]
    ycols = [c for c in all_cols if c.name in state.get('ycols', [])]

    if not ycols:
        ycols = vd.numericCols(getattr(src_sheet, 'visibleCols', []))

    try:
        gs = GraphSheet(
            src_sheet.name,
            'graph',
            source=src_sheet,
            sourceRows=getattr(src_sheet, 'rows', []),
            xcols=xcols,
            ycols=ycols,
        )
    except Exception as e:
        vd.warning(f'failed to create graph sheet: {e}')
        return

    gs.name = state.get('name', gs.name)

    gs.reflines_x = list(state.get('reflines_x', []))
    gs.reflines_y = list(state.get('reflines_y', []))

    vbox = state.get('visibleBox')
    if vbox:
        try:
            gs.zoomTo(BoundingBox(vbox['xmin'], vbox['ymin'], vbox['xmax'], vbox['ymax']))
        except Exception:
            pass

    vd.push(gs)
    vd.sync(gs.ensureLoaded())


globalCommand('', 'restore-graph',
    'vd.restore_graph(input("graph state JSON: "))',
    'restore a GraphSheet from serialized JSON state (internal replay command)')


@VisiData.api
def restore_macro(vd, json_input):
    """Replay command: load a macro file and register it as a command/keybinding."""
    try:
        data = json.loads(json_input)
    except (json.JSONDecodeError, TypeError) as e:
        vd.fail(f'invalid macro JSON: {e}')
        return

    binding = data.get('binding', '')
    relpath = data.get('file', '')
    helpstr = data.get('helpstr', '')
    keystroke = data.get('keystroke', '')

    if not binding or not relpath:
        vd.warning(f'skipping invalid macro entry: {json_input}')
        return

    p = Path(relpath)
    if not p.is_absolute():
        base = getattr(vd, 'currentReplay', None)
        if base and getattr(base, 'source', None):
            replay_src = base.source
            if isinstance(replay_src, Path):
                p = replay_src.parent / relpath

    cmdlog = vd.loadMacro(p)
    if not cmdlog:
        vd.warning(f'could not load macro file {p}')
        return

    vd.setMacro(binding, cmdlog, helpstr=helpstr, keystroke=keystroke)
    vd.status(f'restored macro: {binding}')


globalCommand('', 'restore-macro',
    'vd.restore_macro(input("macro spec JSON: "))',
    'restore a macro from serialized binding and file (internal replay command)')


@VisiData.api
def _collect_initial_sources(vd, data_sheets):
    """Return set of original source paths used to open the given data sheets."""
    original_sources = set()
    for vs in data_sheets:
        src = getattr(vs, 'source', None)
        if isinstance(src, BaseSheet):
            continue
        if src and hasattr(src, 'given'):
            original_sources.add(str(src.given))
        elif src:
            original_sources.add(str(src))
    return original_sources


@VisiData.api
def _collect_options(vd):
    """Collect options that differ from defaults, but only user-relevant scopes.

    Returns {scope: {name: value}} suitable for VDX 'option scope name value' lines.
    Captures:
      - 'global' scope overrides (user changed via .visidatarc, CLI, or Options sheet)
      - specific sheet instance names (user changed options on a particular sheet)
    Excludes:
      - 'default' scope (the original defaults)
      - Sheet CLASS names (GridSheet, CommandLog, etc.) — these are re-set by theme on import
    """
    options_data = {}

    try:
        mgr = vd.options._opts
        allobjs = mgr.allobjs

        for (optname, scope), opt in mgr.iterall():
            if scope == 'default':
                continue
            if not isinstance(opt, Option):
                continue

            obj = allobjs.get(scope)
            if inspect.isclass(obj) and issubclass(obj, BaseSheet):
                continue

            try:
                default_val = vd.options.getdefault(optname)
            except Exception:
                default_val = None
            current_val = opt.value
            if default_val == current_val:
                continue

            if scope not in options_data:
                options_data[scope] = {}
            options_data[scope][optname] = current_val
    except Exception as e:
        vd.warning(f'error collecting options: {e}')

    return options_data


@VisiData.api
def _write_workspace_vdx(vd, pkgdir, data_sheets, graph_sheets, manifest, data_format, original_sources, options_data, macro_data):
    """Write the unified workspace.vdx replay entry point.

    Layout of workspace.vdx (mixed VDX minimal + VDJ JSON lines, as both are
    handled by CommandLogSimple.iterload):

      1. Shebang and replay-reset
      2. restore-macro {...}                -- restore each macro binding from vdj file
      3. option scope name value            -- restored global options
      4. open-file data/xxx.vds             -- for each saved data sheet in order
      5. sheet SourceName + restore-graph {...}  -- for each graph
      6. JSON lines for the original cmdlog rows (with open-file filtered)
    """
    vdx_path = pkgdir / 'workspace.vdx'
    cmdlog_nrows = 0

    with open(str(vdx_path), 'w', encoding='utf-8') as fp:
        fp.write('#!/usr/bin/env -S vd -p\n')
        fp.write(f'# {visidata.__version_info__}\n')
        fp.write(f'# delivery package generated at {manifest["created_at"]}\n')
        fp.write('replay-reset\n')

        if vd.options.delivery_include_macros and macro_data:
            fp.write('\n# -- macros --\n')
            for m in macro_data:
                fp.write('restore-macro ' + json.dumps(m, separators=(',', ':'), default=str) + '\n')

        if vd.options.delivery_include_config and options_data:
            fp.write('\n# -- options --\n')
            for scope, opts in options_data.items():
                for oname, oval in opts.items():
                    fp.write(f'option {scope} {oname} {oval}\n')

        fp.write('\n# -- data sheets --\n')
        for s in manifest['sheets']:
            fp.write(f'open-file {s["file"]}\n')
            if data_format == 'vds':
                fp.write('row 0\n')
                fp.write('open-row\n')

        if vd.options.delivery_include_graphs and graph_sheets:
            fp.write('\n# -- graph sheets --\n')
            for gs in graph_sheets:
                state = _graph_state(gs)
                src_name = state['sourceName']
                fp.write(f'sheet {src_name}\n')
                fp.write('restore-graph ' + json.dumps(state, separators=(',', ':'), default=str) + '\n')

        if vd.options.delivery_include_cmdlog and vd.cmdlog and vd.cmdlog.rows:
            fp.write('\n# -- command log --\n')
            for r in vd.cmdlog.rows:
                if getattr(r, 'longname', None) == 'open-file':
                    input_val = getattr(r, 'input', '') or ''
                    if input_val in original_sources:
                        continue
                row_dict = dict(r) if hasattr(r, '__iter__') and not isinstance(r, (str, bytes)) else {}
                for f in ['sheet', 'col', 'row', 'longname', 'input', 'keystrokes', 'comment']:
                    v = getattr(r, f, None)
                    if v is not None:
                        row_dict[f] = v
                fp.write(json.dumps(row_dict, default=str) + '\n')
                cmdlog_nrows += 1

    return vdx_path, cmdlog_nrows


@VisiData.api
def exportDeliveryPackage(vd, outpath, scope='current_derived', data_format=None):
    data_format = data_format or vd.options.delivery_data_format
    if data_format not in ('vds', 'tsv', 'csv', 'json', 'jsonl'):
        vd.fail(f'unsupported data format: {data_format}')

    is_zip = str(outpath).lower().endswith('.zip')

    workdir = None
    if is_zip:
        workdir = tempfile.mkdtemp(prefix='vd_delivery_')
        pkgdir = Path(os.path.join(workdir, os.path.basename(str(outpath))[:-4] or 'delivery_package'))
    else:
        pkgdir = Path(outpath)

    try:
        os.makedirs(str(pkgdir), exist_ok=True)
        os.makedirs(str(pkgdir / 'data'), exist_ok=True)
        os.makedirs(str(pkgdir / 'config'), exist_ok=True)
        os.makedirs(str(pkgdir / 'config' / 'macros'), exist_ok=True)

        sheets = _collect_scope_sheets(scope)
        data_sheets = [vs for vs in sheets if hasattr(vs, 'columns') and vs.precious]

        graph_sheets = []
        if vd.options.delivery_include_graphs:
            graph_sheets = _collect_graph_sheets(sheets)

        original_sources = vd._collect_initial_sources(data_sheets)

        manifest = {
            'version': '2.0',
            'vd_version': visidata.__version_info__,
            'scope': scope,
            'data_format': data_format,
            'entry_point': 'workspace.vdx',
            'sheets': [],
            'graphs': [],
            'macros': [],
            'cmdlog_nrows': 0,
            'config': None,
            'created_at': datetime.datetime.now().isoformat(),
        }

        for vs in Progress(data_sheets, 'saving sheets'):
            vs.ensureLoaded()
            vd.sync()
            fname = _sanitize_filename(vs.name)
            fpath = pkgdir / 'data' / f'{fname}.{data_format}'
            savefunc = getattr(vs, 'save_' + data_format, None) or getattr(vd, 'save_' + data_format, None)
            if savefunc:
                vd.sync(savefunc(fpath, vs))
                manifest['sheets'].append({
                    'name': vs.name,
                    'file': f'data/{fname}.{data_format}',
                    'type': type(vs).__name__,
                    'nRows': getattr(vs, 'nRows', 0),
                    'nCols': len(getattr(vs, 'visibleCols', [])),
                    'sourceName': str(vs.source.name) if isinstance(vs.source, BaseSheet) else str(getattr(vs, 'source', '')),
                })
            else:
                vd.warning(f'no saver for {vs.name} as {data_format}, skipping')

        for gs in graph_sheets:
            state = _graph_state(gs)
            manifest['graphs'].append({
                'name': gs.name,
                'sourceName': state['sourceName'],
                'xcols': state['xcols'],
                'ycols': state['ycols'],
            })

        macro_data = []
        if vd.options.delivery_include_macros:
            try:
                for binding, cmdlog in vd.macrobindings.items():
                    fname = _sanitize_filename(binding)
                    fpath = pkgdir / 'config' / 'macros' / f'{fname}.vdj'
                    vd.sync(vd.save_vdj(fpath, cmdlog))
                    macro_entry = {
                        'binding': binding,
                        'file': f'config/macros/{fname}.vdj',
                        'helpstr': getattr(cmdlog, 'helpstr', ''),
                        'keystroke': getattr(cmdlog, 'keystroke', ''),
                    }
                    macro_data.append(macro_entry)
                    manifest['macros'].append(macro_entry)
            except Exception as e:
                vd.warning(f'error saving macros: {e}')

        options_data = {}
        if vd.options.delivery_include_config:
            options_data = vd._collect_options()
            opts_path = pkgdir / 'config' / 'options.json'
            try:
                with open(str(opts_path), 'w', encoding='utf-8') as fp:
                    json.dump(options_data, fp, indent=2, default=str)
                manifest['config'] = 'config/options.json'
            except Exception as e:
                vd.warning(f'error saving options: {e}')

        vdx_path, cmdlog_nrows = vd._write_workspace_vdx(
            pkgdir, data_sheets, graph_sheets, manifest, data_format, original_sources,
            options_data, macro_data
        )
        manifest['cmdlog_nrows'] = cmdlog_nrows

        with open(str(pkgdir / 'manifest.json'), 'w', encoding='utf-8') as fp:
            json.dump(manifest, fp, indent=2, default=str)

        readme = _generate_readme(manifest, data_format, is_zip)
        with open(str(pkgdir / 'README.md'), 'w', encoding='utf-8') as fp:
            fp.write(readme)

        _write_start_scripts(pkgdir)

        if is_zip:
            with zipfile.ZipFile(str(outpath), 'w', zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=9) as zfp:
                pkgdir_str = str(pkgdir)
                base = os.path.basename(pkgdir_str)
                for root, dirs, files in os.walk(pkgdir_str):
                    for f in files:
                        full = os.path.join(root, f)
                        arcname = os.path.join(base, os.path.relpath(full, pkgdir_str))
                        zfp.write(full, arcname)
            vd.status(f'delivery package saved to {outpath}')
        else:
            vd.status(f'delivery package saved to {pkgdir}')

    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _generate_readme(manifest, data_format, is_zip):
    lines = []
    lines.append('# VisiData Delivery Package')
    lines.append('')
    lines.append(f'This package was generated by {manifest["vd_version"]}.')
    lines.append(f'Created at: {manifest["created_at"]}')
    lines.append(f'Scope: `{manifest["scope"]}`')
    lines.append(f'Data format: `{data_format}`')
    lines.append('')
    lines.append('## Quick Start')
    lines.append('')
    lines.append('To reproduce this workspace exactly:')
    lines.append('')
    lines.append('```bash')
    lines.append('# Option 1: Use the start script')
    lines.append('./start.sh        # Linux/macOS')
    lines.append('# start.bat        # Windows')
    lines.append('')
    lines.append('# Option 2: Replay the unified workspace entry directly')
    lines.append('vd -p workspace.vdx')
    lines.append('```')
    lines.append('')
    lines.append('The single `workspace.vdx` replay file orchestrates everything:')
    lines.append('- restoring macro bindings and commands')
    lines.append('- restoring all non-default options')
    lines.append('- loading saved data sheets (column types preserved)')
    lines.append('- rebuilding graph sheets with exact view state')
    lines.append('- replaying the original command log')
    lines.append('')

    if manifest['sheets']:
        lines.append('## Data Sheets')
        lines.append('')
        lines.append('| Sheet | File | Rows | Cols |')
        lines.append('|-------|------|------|------|')
        for s in manifest['sheets']:
            lines.append(f"| {s['name']} | `{s['file']}` | {s['nRows']} | {s['nCols']} |")
        lines.append('')

    if manifest['graphs']:
        lines.append('## Graphs')
        lines.append('')
        lines.append('| Graph | Source Sheet | X Cols | Y Cols |')
        lines.append('|-------|--------------|--------|--------|')
        for g in manifest['graphs']:
            lines.append(f"| {g['name']} | {g['sourceName']} | {', '.join(g['xcols'])} | {', '.join(g['ycols'])} |")
        lines.append('')

    if manifest['macros']:
        lines.append('## Macros')
        lines.append('')
        lines.append('| Binding | File | Help |')
        lines.append('|---------|------|------|')
        for m in manifest['macros']:
            lines.append(f"| {m['binding']} | `{m['file']}` | {m.get('helpstr', '')} |")
        lines.append('')

    if manifest.get('cmdlog_nrows', 0):
        lines.append(f"## Command Log\n\n{manifest['cmdlog_nrows']} commands embedded in `workspace.vdx`\n")

    if manifest['config']:
        lines.append(f"## Configuration\n\nOptions saved in: `{manifest['config']}`\n")

    lines.append('## Package Structure')
    lines.append('')
    lines.append('```')
    lines.append('package/')
    lines.append('  README.md                This file')
    lines.append('  manifest.json            Machine-readable package metadata')
    lines.append('  workspace.vdx            **Single unified replay entry point**')
    lines.append('  start.sh / start.bat     Reproduce this workspace (vd -p workspace.vdx)')
    lines.append('  data/')
    lines.append(f'    *.{data_format}'.ljust(25) + f'Sheet data (.vds preserves exact column types)')
    lines.append('  config/')
    lines.append('    options.json           Exported option settings')
    lines.append('    macros/')
    lines.append('      *.vdj                Macro definition files')
    lines.append('```')
    lines.append('')
    lines.append('## Notes')
    lines.append('')
    lines.append('- `.vds` format preserves column types and attributes exactly.')
    lines.append('- Other formats (tsv, csv, json) preserve values but types may need re-setting.')
    lines.append('- Graph state (xcols/ycols/reflines/visibleBox) is embedded in workspace.vdx')
    lines.append('  and restored automatically via the `restore-graph` replay command.')
    lines.append('- Macro definitions and option settings are embedded in workspace.vdx')
    lines.append('  via `restore-macro` and `option` lines, restored automatically on replay.')
    lines.append('')

    return '\n'.join(lines)


def _write_start_scripts(pkgdir):
    sh_path = pkgdir / 'start.sh'
    with open(str(sh_path), 'w', encoding='utf-8') as fp:
        fp.write('#!/usr/bin/env bash\n')
        fp.write('# Auto-generated by VisiData delivery package\n\n')
        fp.write('set -e\n\n')
        fp.write('SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n')
        fp.write('cd "$SCRIPT_DIR"\n\n')
        fp.write('vd -p workspace.vdx "$@"\n')
    os.chmod(str(sh_path), 0o755)

    bat_path = pkgdir / 'start.bat'
    with open(str(bat_path), 'w', encoding='utf-8') as fp:
        fp.write('@echo off\n')
        fp.write('REM Auto-generated by VisiData delivery package\n\n')
        fp.write('cd /d "%~dp0"\n\n')
        fp.write('vd -p workspace.vdx %*\n')


globalCommand('gP', 'export-delivery-package',
    '''vd.exportDeliveryPackage(
        inputPath("export delivery package to (directory or .zip): ", value=vd.activeSheet.name+"_delivery"),
        scope=input("scope [current_derived|current|all|stacked]: ", value="current_derived", defaultLast=True)
    )''',
    'export reproducible delivery package with unified workspace.vdx entry')


vd.addMenuItems('''
    File > Export > Delivery package > export-delivery-package
''')
