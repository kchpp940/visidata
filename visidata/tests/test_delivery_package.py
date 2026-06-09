import sys
sys.path.insert(0, '.')

import os
import json
import shutil
import tempfile

import visidata
from visidata import vd, Sheet, Path, ItemColumn, TableSheet, AttrDict

def make_sheet(name, rows, coltypes=None):
    vs = TableSheet(name, rows=list(rows), precious=True)
    if rows:
        first = rows[0]
        for i, key in enumerate(first.keys()):
            col = ItemColumn(key, i)
            if coltypes and key in coltypes:
                col.type = coltypes[key]
            vs.addColumn(col)
    vs.cursorRowIndex = 0
    vd.sync(vs.ensureLoaded())
    return vs


def test_workspace_vdx_structure():
    print('=== Workspace VDX structure ===')
    tmpdir = tempfile.mkdtemp(prefix='vd_dp_test_')
    try:
        vd.resetVisiData()

        rows = [{'a': 1, 'b': 'x'}, {'a': 2, 'b': 'y'}]
        vs = make_sheet('main_sheet', rows)
        vd.push(vs)

        pkgdir = os.path.join(tmpdir, 'pkg_vdx')
        sys.path.insert(0, '.')
        from visidata.features import delivery_package
        vd.exportDeliveryPackage(Path(pkgdir), scope='current')

        assert os.path.exists(os.path.join(pkgdir, 'workspace.vdx')), 'workspace.vdx not found'
        print('  workspace.vdx OK')

        assert not os.path.exists(os.path.join(pkgdir, 'replay.vdj')), 'replay.vdj should no longer exist'
        assert not any(f.endswith('.graph.json') for f in os.listdir(os.path.join(pkgdir, 'data'))), 'no .graph.json files'
        print('  legacy files removed OK')

        with open(os.path.join(pkgdir, 'workspace.vdx')) as fp:
            content = fp.read()

        assert '#!/usr/bin/env -S vd -p' in content, 'missing shebang'
        print('  shebang OK')
        assert 'replay-reset' in content, 'missing replay-reset'
        print('  replay-reset OK')
        assert 'open-file data/main_sheet.vds' in content, 'missing open-file'
        print('  open-file OK')

        print('\n=== Workspace VDX structure: PASSED ===')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_directory_export():
    print('=== Directory export structure ===')
    tmpdir = tempfile.mkdtemp(prefix='vd_dp_test_')
    try:
        vd.resetVisiData()

        rows = [{'a': 1, 'b': 'x'}, {'a': 2, 'b': 'y'}]
        vs = make_sheet('test_sheet', rows)
        vd.push(vs)

        pkgdir = os.path.join(tmpdir, 'test_pkg')
        from visidata.features import delivery_package
        vd.exportDeliveryPackage(Path(pkgdir), scope='current')

        required = [
            'README.md', 'manifest.json', 'workspace.vdx',
            'start.sh', 'start.bat',
            'config/options.json',
        ]
        for r in required:
            assert os.path.exists(os.path.join(pkgdir, r)), f'missing {r}'
            print(f'  {r}')

        data_dir = os.path.join(pkgdir, 'data')
        data_files = os.listdir(data_dir)
        print(f'Data files: {data_files}')
        assert any('test_sheet' in f for f in data_files), 'no data file for test_sheet'

        with open(os.path.join(pkgdir, 'manifest.json')) as fp:
            manifest = json.load(fp)
        print(f'Manifest sheets: {[s["name"] for s in manifest["sheets"]]}')
        assert manifest['version'] == '2.0', 'manifest version should be 2.0'
        assert manifest['entry_point'] == 'workspace.vdx', 'entry_point should be workspace.vdx'

        with open(os.path.join(pkgdir, 'README.md')) as fp:
            readme = fp.read()
        assert 'vd -p workspace.vdx' in readme, 'README should reference workspace.vdx'
        print('README OK')

        start_sh = os.path.join(pkgdir, 'start.sh')
        with open(start_sh) as fp:
            sh_content = fp.read()
        assert 'vd -p workspace.vdx' in sh_content, 'start.sh should call vd -p workspace.vdx'
        assert os.access(start_sh, os.X_OK), 'start.sh not executable'
        print('start.sh OK (executable)')

        start_bat = os.path.join(pkgdir, 'start.bat')
        with open(start_bat) as fp:
            bat_content = fp.read()
        assert 'vd -p workspace.vdx' in bat_content, 'start.bat missing vd -p workspace.vdx'
        print('start.bat OK')

        print('\n=== Directory export: PASSED ===')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_zip_export():
    print('\n=== Zip export ===')
    tmpdir = tempfile.mkdtemp(prefix='vd_dp_test_')
    try:
        vd.resetVisiData()
        rows = [{'a': 1, 'b': 'x'}, {'a': 2, 'b': 'y'}]
        vs = make_sheet('zip_test', rows)
        vd.push(vs)

        zip_path = os.path.join(tmpdir, 'test.zip')
        from visidata.features import delivery_package
        vd.options.delivery_data_format = 'tsv'
        vd.exportDeliveryPackage(Path(zip_path), scope='current')
        vd.options.delivery_data_format = 'vds'

        assert os.path.exists(zip_path)
        import zipfile
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            print('Zip contents:')
            for n in sorted(names):
                print(f'  {n}')
            assert any('workspace.vdx' in n for n in names), 'no workspace.vdx in zip'
            assert any('start.sh' in n for n in names), 'no start.sh in zip'
            assert any('zip_test.tsv' in n for n in names), 'no data file in zip'

        print('\n=== Zip export: PASSED ===')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_column_types_preserved_in_vds():
    print('\n=== Column types preserved in vds ===')
    tmpdir = tempfile.mkdtemp(prefix='vd_dp_test_')
    try:
        vd.resetVisiData()
        coltypes = {'name': str, 'age': int, 'score': float}
        rows = [
            {'name': 'Alice', 'age': 30, 'score': 95.5},
            {'name': 'Bob', 'age': 25, 'score': 87.0},
        ]
        vs = make_sheet('typed_sheet', rows, coltypes)
        vd.push(vs)

        pkgdir = os.path.join(tmpdir, 'typed_pkg')
        from visidata.features import delivery_package
        vd.exportDeliveryPackage(Path(pkgdir), scope='current')

        vds_file = os.path.join(pkgdir, 'data', 'typed_sheet.vds')
        assert os.path.exists(vds_file), f'vds file not found at {vds_file}'

        col_types = []
        with open(vds_file) as fp:
            for line in fp:
                line = line.rstrip('\n')
                if line.startswith('#{'):
                    meta = json.loads(line[1:])
                    print(f'  {meta}')
                    col_types.append(meta.get('typestr', ''))

        print(f'Column types: {col_types}')
        assert 'int' in col_types, 'int type not preserved'
        assert 'str' in col_types, 'str type not preserved'
        assert 'float' in col_types, 'float type not preserved'

        print('\n=== Column types preserved in vds: PASSED ===')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_restore_graph_command_exists():
    print('\n=== restore-graph command registration ===')
    vd.resetVisiData()
    from visidata.features import delivery_package
    from visidata.settings import getCommand

    vs = Sheet('test')
    cmd = getCommand(vs, 'restore-graph')
    assert cmd is not None, 'restore-graph command not registered'
    print(f'  restore-graph registered: {cmd.longname}')
    print('\n=== restore-graph command: PASSED ===')


def test_restore_macro_command_exists():
    print('\n=== restore-macro command registration ===')
    vd.resetVisiData()
    from visidata.features import delivery_package
    from visidata.settings import getCommand

    vs = Sheet('test')
    cmd = getCommand(vs, 'restore-macro')
    assert cmd is not None, 'restore-macro command not registered'
    print(f'  restore-macro registered: {cmd.longname}')
    print('\n=== restore-macro command: PASSED ===')


def test_collect_options_non_default():
    print('\n=== Collect non-default options ===')
    vd.resetVisiData()
    from visidata.features import delivery_package

    original = vd.options.delivery_data_format
    try:
        vd.options.delivery_data_format = 'tsv'
        opts = vd._collect_options()
        print(f'  collected options scopes: {list(opts.keys())}')
        assert 'global' in opts or any(
            any(v == 'tsv' for v in scope_opts.values())
            for scope_opts in opts.values()
        ), 'non-default option should be collected'
        print(f'  delivery_data_format=tsv captured OK')
    finally:
        vd.options.delivery_data_format = original

    print('\n=== Collect non-default options: PASSED ===')


def test_workspace_vdx_contains_macros():
    print('\n=== Workspace VDX contains macros ===')
    tmpdir = tempfile.mkdtemp(prefix='vd_dp_test_')
    try:
        vd.resetVisiData()
        rows = [{'a': 1, 'b': 'x'}, {'a': 2, 'b': 'y'}]
        vs = make_sheet('macro_sheet', rows)
        vd.push(vs)

        from visidata.features import delivery_package
        from visidata.cmdlog import CommandLogJsonl
        from visidata import AttrDict

        fake_cmdlog = CommandLogJsonl('test_macro', rows=[
            AttrDict(longname='show-version', sheet='', col='', row='', keystrokes='', input='', comment=''),
        ])
        fake_cmdlog.helpstr = 'test macro help'
        fake_cmdlog.keystroke = 'Alt+t'
        vd.macrobindings['test-macro'] = fake_cmdlog

        pkgdir = os.path.join(tmpdir, 'macro_pkg')
        vd.exportDeliveryPackage(Path(pkgdir), scope='current')

        vdx_path = os.path.join(pkgdir, 'workspace.vdx')
        with open(vdx_path) as fp:
            content = fp.read()

        assert 'restore-macro' in content, 'restore-macro not found in workspace.vdx'
        assert 'test-macro' in content, 'macro binding not found in workspace.vdx'
        print('  restore-macro in vdx OK')
        print('  macro binding in vdx OK')

        macro_dir = os.path.join(pkgdir, 'config', 'macros')
        macro_files = os.listdir(macro_dir)
        print(f'  macro files: {macro_files}')
        assert any('test' in f for f in macro_files), 'no vdj macro file saved'

        with open(os.path.join(pkgdir, 'manifest.json')) as fp:
            manifest = json.load(fp)
        assert len(manifest['macros']) == 1, 'manifest should have 1 macro'
        assert manifest['macros'][0]['binding'] == 'test-macro'
        print('  manifest macros OK')

        del vd.macrobindings['test-macro']
        print('\n=== Workspace VDX macros: PASSED ===')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_workspace_vdx_contains_options():
    print('\n=== Workspace VDX contains options ===')
    tmpdir = tempfile.mkdtemp(prefix='vd_dp_test_')
    try:
        vd.resetVisiData()
        rows = [{'a': 1, 'b': 'x'}]
        vs = make_sheet('opts_sheet', rows)
        vd.push(vs)

        from visidata.features import delivery_package

        vd.options.delivery_data_format = 'tsv'
        pkgdir = os.path.join(tmpdir, 'opts_pkg')
        vd.exportDeliveryPackage(Path(pkgdir), scope='current')
        vd.options.delivery_data_format = 'vds'

        vdx_path = os.path.join(pkgdir, 'workspace.vdx')
        with open(vdx_path) as fp:
            content = fp.read()

        assert '# -- options --' in content, 'options section missing'
        assert 'option global delivery_data_format tsv' in content, 'non-default option not embedded'
        print('  options section OK')
        print('  non-default option embedded OK')

        opts_json_path = os.path.join(pkgdir, 'config', 'options.json')
        with open(opts_json_path) as fp:
            opts_json = json.load(fp)
        assert any(
            any(v == 'tsv' for v in scope_opts.values())
            for scope_opts in opts_json.values()
        ), 'options.json missing non-default value'
        print('  options.json OK')

        print('\n=== Workspace VDX options: PASSED ===')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_zip_extract_paths():
    print('\n=== Zip extract relative paths ===')
    tmpdir = tempfile.mkdtemp(prefix='vd_dp_test_')
    try:
        vd.resetVisiData()
        rows = [{'a': 1, 'b': 'x'}, {'a': 2, 'b': 'y'}]
        vs = make_sheet('zip_extract', rows)
        vd.push(vs)

        from visidata.features import delivery_package
        import zipfile

        zip_path = os.path.join(tmpdir, 'test_extract.zip')
        vd.exportDeliveryPackage(Path(zip_path), scope='current')

        extract_dir = os.path.join(tmpdir, 'extracted')
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)

        entries = os.listdir(extract_dir)
        pkg_subdir = os.path.join(extract_dir, entries[0])
        print(f'  extracted package dir: {os.path.basename(pkg_subdir)}')

        required = ['README.md', 'manifest.json', 'workspace.vdx', 'start.sh', 'start.bat']
        for r in required:
            p = os.path.join(pkg_subdir, r)
            assert os.path.exists(p), f'missing after extract: {r}'
            print(f'  {r} exists OK')

        data_dir = os.path.join(pkg_subdir, 'data')
        assert os.path.isdir(data_dir), 'data/ missing after extract'
        data_files = os.listdir(data_dir)
        assert any('zip_extract' in f for f in data_files), 'no data file after extract'
        print(f'  data/ OK: {data_files}')

        config_dir = os.path.join(pkg_subdir, 'config')
        assert os.path.isdir(config_dir), 'config/ missing after extract'
        assert os.path.exists(os.path.join(config_dir, 'options.json')), 'options.json missing'
        print('  config/ OK')

        macros_dir = os.path.join(config_dir, 'macros')
        macro_vdj_exists = os.path.isdir(macros_dir) and any(f.endswith('.vdj') for f in os.listdir(macros_dir)) if os.path.isdir(macros_dir) else False
        print(f'  config/macros/: exists={os.path.isdir(macros_dir)}, has_vdj={macro_vdj_exists}')
        if vd.macrobindings:
            assert os.path.isdir(macros_dir), 'config/macros/ should exist when macros present'
        else:
            print('  (no macros present, so config/macros/ not required)')

        start_sh = os.path.join(pkg_subdir, 'start.sh')
        with open(start_sh) as fp:
            sh_content = fp.read()
        assert 'cd "$SCRIPT_DIR"' in sh_content, 'start.sh missing cd to script dir'
        print('  start.sh relative-path guard OK')

        print('\n=== Zip extract relative paths: PASSED ===')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_derived_sheet_discovery():
    print('\n=== Derived sheet discovery ===')
    tmpdir = tempfile.mkdtemp(prefix='vd_dp_test_')
    try:
        vd.resetVisiData()
        parent = make_sheet('parent', [{'x': 1}, {'x': 2}])
        child = make_sheet('derived', [{'x': 3}])
        child.source = parent
        vd.push(parent)
        vd.push(child)

        from visidata.features.delivery_package import _collect_derived_sheets
        derived = _collect_derived_sheets(parent)
        names = [s.name for s in derived]
        print(f'Derived sheets found: {names}')
        assert 'derived' in names, 'derived sheet not discovered'

        print('\n=== Derived sheet discovery: PASSED ===')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_workspace_vdx_contains_cmdlog():
    print('\n=== Workspace VDX contains cmdlog rows ===')
    tmpdir = tempfile.mkdtemp(prefix='vd_dp_test_')
    try:
        vd.resetVisiData()
        rows = [{'a': 1, 'b': 'x'}, {'a': 2, 'b': 'y'}]
        vs = make_sheet('logged_sheet', rows)
        vd.push(vs)

        vd.cmdlog.addRow(AttrDict(longname='show-version', sheet='logged_sheet', col='', row='', keystrokes='', input='', comment='test'))

        pkgdir = os.path.join(tmpdir, 'logged_pkg')
        from visidata.features import delivery_package
        vd.exportDeliveryPackage(Path(pkgdir), scope='current')

        vdx_path = os.path.join(pkgdir, 'workspace.vdx')
        with open(vdx_path) as fp:
            content = fp.read()

        assert 'show-version' in content, 'cmdlog row not embedded in workspace.vdx'
        print('  cmdlog row embedded OK')
        print(content[:500])
        print('\n=== Workspace VDX cmdlog: PASSED ===')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_workspace_vdx_filters_original_openfile():
    print('\n=== Workspace VDX filters original open-file ===')
    tmpdir = tempfile.mkdtemp(prefix='vd_dp_test_')
    try:
        vd.resetVisiData()
        rows = [{'a': 1, 'b': 'x'}]
        vs = make_sheet('filter_sheet', rows)
        vs.source = Path('/original/path/data.tsv')
        vd.push(vs)

        vd.cmdlog.addRow(AttrDict(
            longname='open-file', sheet='filter_sheet', col='', row='',
            keystrokes='o', input='/original/path/data.tsv', comment='original open'
        ))
        vd.cmdlog.addRow(AttrDict(
            longname='show-version', sheet='filter_sheet', col='', row='',
            keystrokes='', input='', comment='other'
        ))

        pkgdir = os.path.join(tmpdir, 'filter_pkg')
        from visidata.features import delivery_package
        vd.exportDeliveryPackage(Path(pkgdir), scope='current')

        vdx_path = os.path.join(pkgdir, 'workspace.vdx')
        with open(vdx_path) as fp:
            content = fp.read()

        assert '/original/path/data.tsv' not in content, 'original open-file path not filtered'
        print('  original open-file filtered OK')
        assert 'show-version' in content, 'other cmdlog rows should remain'
        print('  other cmdlog rows preserved OK')

        print('\n=== Workspace VDX open-file filtering: PASSED ===')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    tests = [
        test_workspace_vdx_structure,
        test_directory_export,
        test_zip_export,
        test_column_types_preserved_in_vds,
        test_restore_graph_command_exists,
        test_restore_macro_command_exists,
        test_collect_options_non_default,
        test_workspace_vdx_contains_macros,
        test_workspace_vdx_contains_options,
        test_zip_extract_paths,
        test_derived_sheet_discovery,
        test_workspace_vdx_contains_cmdlog,
        test_workspace_vdx_filters_original_openfile,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f'\nFAILED: {t.__name__}: {e}')
            import traceback
            traceback.print_exc()
            sys.exit(1)
    print('\nALL TESTS PASSED')
