"""Delivery Package feature for exporting reproducible VisiData workspaces."""

import datetime
import json
import os
import shutil
import tempfile
import zipfile

from visidata import vd, VisiData, BaseSheet, GraphSheet, Path, Progress
from visidata import globalCommand
import visidata


vd.option('delivery_data_format', 'vds', 'default data format for delivery package: vds|tsv|csv|json', replay=True)
vd.option('delivery_include_derived', True, 'include derived sheets in delivery package', replay=True)
vd.option('delivery_include_graphs', True, 'include graph sheets in delivery package', replay=True)
vd.option('delivery_include_cmdlog', True, 'include command log in delivery package', replay=True)
vd.option('delivery_include_macros', True, 'include macro files in delivery package', replay=True)
vd.option('delivery_include_config', True, 'include options/configuration in delivery package', replay=True)


def _sanitize_filename(name):
    'Return a filesystem-safe filename from a sheet or other name.'
    keepcharacters = (' ', '.', '_', '-')
    return ''.join(c for c in name if c.isalnum() or c in keepcharacters).rstrip().replace(' ', '_')


def _collect_derived_sheets(sheet):
    'Collect all sheets derived from the given sheet (direct and indirect).'
    derived = []
    for vs in vd.allSheets:
        src = getattr(vs, 'source', None)
        while isinstance(src, BaseSheet):
            if src is sheet:
                derived.append(vs)
                break
            src = getattr(src, 'source', None)
    return derived


def _collect_graph_sheets(sheets):
    'Collect all GraphSheet instances from the given sheets and their sources.'
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
    '''Collect sheets based on scope string.
    scope: 'current' | 'current_derived' | 'all' | 'stacked'
    '''
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
    'Return a serializable dict of GraphSheet state.'
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
def exportDeliveryPackage(vd, outpath, scope='current_derived', data_format=None):
    '''Export a delivery package to outpath (directory or .zip).
    scope: 'current' | 'current_derived' | 'all' | 'stacked'
    data_format: 'vds' | 'tsv' | 'csv' | 'json' (None -> vd.options.delivery_data_format)
    '''
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

    cmdlog_nrows = len(vd.cmdlog.rows) if vd.cmdlog else 0

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

        manifest = {
            'version': '1.0',
            'vd_version': visidata.__version_info__,
            'scope': scope,
            'data_format': data_format,
            'sheets': [],
            'graphs': [],
            'macros': [],
            'cmdlog': None,
            'cmdlog_nrows': cmdlog_nrows,
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

        for gs in Progress(graph_sheets, 'saving graphs'):
            fname = _sanitize_filename(gs.name)
            fpath = pkgdir / 'data' / f'{fname}.graph.json'
            state = _graph_state(gs)
            with open(str(fpath), 'w', encoding='utf-8') as fp:
                json.dump(state, fp, indent=2, default=str)
            manifest['graphs'].append({
                'name': gs.name,
                'file': f'data/{fname}.graph.json',
                'sourceName': state['sourceName'],
            })

        if vd.options.delivery_include_cmdlog and vd.cmdlog and vd.cmdlog.rows:
            cmdlog_path = pkgdir / 'replay.vdj'
            vd.sync(vd.save_vdj(cmdlog_path, vd.cmdlog))
            manifest['cmdlog'] = 'replay.vdj'

        if vd.options.delivery_include_macros:
            try:
                for binding, cmdlog in vd.macrobindings.items():
                    fname = _sanitize_filename(binding)
                    fpath = pkgdir / 'config' / 'macros' / f'{fname}.vdj'
                    vd.sync(vd.save_vdj(fpath, cmdlog))
                    manifest['macros'].append({
                        'binding': binding,
                        'file': f'config/macros/{fname}.vdj',
                        'helpstr': getattr(cmdlog, 'helpstr', ''),
                        'keystroke': getattr(cmdlog, 'keystroke', ''),
                    })
            except Exception as e:
                vd.warning(f'error saving macros: {e}')

        if vd.options.delivery_include_config:
            opts_path = pkgdir / 'config' / 'options.json'
            try:
                options_data = {}
                if vd.cmdlog:
                    set_option_rows = [r for r in vd.cmdlog.rows if r.longname == 'set-option']
                    for r in set_option_rows:
                        opt_scope = r.sheet or 'global'
                        name = r.row
                        value = r.input
                        if opt_scope not in options_data:
                            options_data[opt_scope] = {}
                        options_data[opt_scope][name] = value

                with open(str(opts_path), 'w', encoding='utf-8') as fp:
                    json.dump(options_data, fp, indent=2, default=str)
                manifest['config'] = 'config/options.json'
            except Exception as e:
                vd.warning(f'error saving options: {e}')

        with open(str(pkgdir / 'manifest.json'), 'w', encoding='utf-8') as fp:
            json.dump(manifest, fp, indent=2, default=str)

        readme = _generate_readme(manifest, data_format, is_zip)
        with open(str(pkgdir / 'README.md'), 'w', encoding='utf-8') as fp:
            fp.write(readme)

        _write_start_scripts(pkgdir, manifest)

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
    'Generate README.md content for the delivery package.'
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
    lines.append('To reproduce this workspace:')
    lines.append('')
    lines.append('```bash')
    lines.append('# Option 1: Use the start script')
    lines.append('./start.sh        # Linux/macOS')
    lines.append('# start.bat        # Windows')
    lines.append('')
    lines.append('# Option 2: Replay manually')
    lines.append('vd -p replay.vdj')
    lines.append('```')
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
        lines.append('| Graph | File | Source Sheet |')
        lines.append('|-------|------|--------------|')
        for g in manifest['graphs']:
            lines.append(f"| {g['name']} | `{g['file']}` | {g['sourceName']} |")
        lines.append('')

    if manifest['macros']:
        lines.append('## Macros')
        lines.append('')
        lines.append('| Binding | File | Help |')
        lines.append('|---------|------|------|')
        for m in manifest['macros']:
            lines.append(f"| {m['binding']} | `{m['file']}` | {m.get('helpstr', '')} |")
        lines.append('')

    if manifest['cmdlog']:
        lines.append(f"## Command Log\n\nReplay file: `{manifest['cmdlog']}` ({manifest.get('cmdlog_nrows', 0)} commands)\n")

    if manifest['config']:
        lines.append(f"## Configuration\n\nOptions saved in: `{manifest['config']}`\n")

    lines.append('## Package Structure')
    lines.append('')
    lines.append('```')
    lines.append('package/')
    lines.append('  README.md                This file')
    lines.append('  manifest.json            Machine-readable package metadata')
    lines.append('  replay.vdj               Full command log replay file')
    lines.append('  start.sh / start.bat     Reproduce this workspace')
    lines.append('  data/')
    lines.append('    *.vds / *.tsv / ...    Sheet data (column types preserved in .vds)')
    lines.append('    *.graph.json           Graph sheet state (view, axes, reflines)')
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
    lines.append('- The replay file (`replay.vdj`) reproduces the exact workflow used.')
    lines.append('')

    return '\n'.join(lines)


def _write_start_scripts(pkgdir, manifest):
    'Write shell and batch scripts for reproducing the workspace.'
    has_cmdlog = bool(manifest.get('cmdlog'))
    first_data = manifest['sheets'][0]['file'] if manifest['sheets'] else ''

    sh_path = pkgdir / 'start.sh'
    with open(str(sh_path), 'w', encoding='utf-8') as fp:
        fp.write('#!/usr/bin/env bash\n')
        fp.write('# Auto-generated by VisiData delivery package\n\n')
        fp.write('set -e\n\n')
        fp.write('SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n')
        fp.write('cd "$SCRIPT_DIR"\n\n')
        if has_cmdlog:
            fp.write('vd -p replay.vdj "$@"\n')
        elif first_data:
            fp.write(f'vd {first_data} "$@"\n')
        else:
            fp.write('vd "$@"\n')
    os.chmod(str(sh_path), 0o755)

    bat_path = pkgdir / 'start.bat'
    with open(str(bat_path), 'w', encoding='utf-8') as fp:
        fp.write('@echo off\n')
        fp.write('REM Auto-generated by VisiData delivery package\n\n')
        fp.write('cd /d "%~dp0"\n\n')
        if has_cmdlog:
            fp.write('vd -p replay.vdj %*\n')
        elif first_data:
            fp.write(f'vd {first_data} %*\n')
        else:
            fp.write('vd %*\n')


globalCommand('gP', 'export-delivery-package',
    '''vd.exportDeliveryPackage(
        inputPath("export delivery package to (directory or .zip): ", value=vd.activeSheet.name+"_delivery"),
        scope=input("scope [current_derived|current|all|stacked]: ", value="current_derived", defaultLast=True)
    )''',
    'export reproducible delivery package with data, cmdlog, graphs, macros, and config')


vd.addMenuItems('''
    File > Export > Delivery package > export-delivery-package
''')
