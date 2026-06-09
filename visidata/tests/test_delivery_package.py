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
