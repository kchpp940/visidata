import os
import tempfile
import json
import shutil
import importlib

import visidata
from visidata import vd, Sheet, Path, ItemColumn, TableSheet
delivery_package = importlib.import_module('visidata.features.delivery_package')


def make_sheet(name, rows, coltypes=None):
    'Helper to create a TableSheet with columns from dict rows.'
    if coltypes is None:
        coltypes = {}
    vs = TableSheet(name)
    vs.rows = list(rows)
    keys = set()
    for r in vs.rows:
        if isinstance(r, dict):
            keys.update(r.keys())
    for k in sorted(keys):
        c = ItemColumn(k)
        c.type = coltypes.get(k, str)
        vs.addColumn(c)
    return vs


def test_directory_export():
    vd.resetVisiData()

    rows = [
        {'name': 'Alice', 'age': 30, 'score': 95.5},
        {'name': 'Bob', 'age': 25, 'score': 87.3},
        {'name': 'Charlie', 'age': 35, 'score': 92.1},
    ]

    vs = make_sheet('test_sheet', rows, coltypes={'name': str, 'age': int, 'score': float})
    vd.push(vs)
    vd.sync(vs.ensureLoaded())

    tmpdir = tempfile.mkdtemp(prefix='vd_test_')
    pkgdir = os.path.join(tmpdir, 'test_pkg')

    try:
        vd.exportDeliveryPackage(Path(pkgdir), scope='current', data_format='vds')
        vd.sync()

        print('=== Directory export structure ===')
        for root, dirs, files in os.walk(pkgdir):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, pkgdir)
                print('  ' + rel)

        with open(os.path.join(pkgdir, 'manifest.json')) as fp:
            manifest = json.load(fp)
        print('Manifest sheets: ' + str([s['name'] for s in manifest['sheets']]))
        assert len(manifest['sheets']) >= 1, 'No sheets in manifest'
        assert manifest['sheets'][0]['name'] == 'test_sheet'
        assert manifest['data_format'] == 'vds'

        readme_path = os.path.join(pkgdir, 'README.md')
        assert os.path.exists(readme_path), 'README.md not found'
        with open(readme_path) as fp:
            readme = fp.read()
        assert 'VisiData Delivery Package' in readme
        assert 'test_sheet' in readme
        print('README OK')

        data_dir = os.path.join(pkgdir, 'data')
        data_files = [f for f in os.listdir(data_dir) if f.endswith('.vds')]
        assert len(data_files) > 0, 'No vds data files found'
        print('Data files: ' + str(data_files))

        start_sh = os.path.join(pkgdir, 'start.sh')
        assert os.path.exists(start_sh), 'start.sh not found'
        assert os.access(start_sh, os.X_OK), 'start.sh not executable'
        with open(start_sh) as fp:
            sh_content = fp.read()
        assert 'vd -p replay.vdj' in sh_content or 'vd data/' in sh_content, 'start.sh missing vd command'
        print('start.sh OK (executable)')

        start_bat = os.path.join(pkgdir, 'start.bat')
        assert os.path.exists(start_bat), 'start.bat not found'
        print('start.bat OK')

        print('\n=== Directory export: PASSED ===')

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_zip_export():
    vd.resetVisiData()

    rows = [
        {'id': 1, 'value': 'a'},
        {'id': 2, 'value': 'b'},
    ]

    vs = make_sheet('zip_test', rows, coltypes={'id': int, 'value': str})
    vd.push(vs)
    vd.sync(vs.ensureLoaded())

    tmpdir = tempfile.mkdtemp(prefix='vd_test_')
    zippath = os.path.join(tmpdir, 'test.zip')

    try:
        vd.exportDeliveryPackage(Path(zippath), scope='current', data_format='tsv')
        vd.sync()

        assert os.path.exists(zippath), 'zip file not created'
        assert os.path.getsize(zippath) > 0, 'zip file is empty'

        import zipfile
        with zipfile.ZipFile(zippath, 'r') as zfp:
            names = zfp.namelist()
            print('Zip contents:')
            for n in names:
                print('  ' + n)

            has_readme = any(n.endswith('README.md') for n in names)
            has_manifest = any(n.endswith('manifest.json') for n in names)
            has_data = any('.tsv' in n for n in names)
            has_start_sh = any(n.endswith('start.sh') for n in names)

            assert has_readme, 'README.md not in zip'
            assert has_manifest, 'manifest.json not in zip'
            assert has_data, 'No data files in zip'
            assert has_start_sh, 'start.sh not in zip'

        print('\n=== Zip export: PASSED ===')

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_column_types_preserved_in_vds():
    vd.resetVisiData()

    rows = [
        {'name': 'Alice', 'age': 30, 'score': 95.5},
        {'name': 'Bob', 'age': 25, 'score': 87.3},
    ]

    vs = make_sheet('type_test', rows, coltypes={'name': str, 'age': int, 'score': float})
    vd.push(vs)
    vd.sync(vs.ensureLoaded())

    tmpdir = tempfile.mkdtemp(prefix='vd_test_')
    pkgdir = os.path.join(tmpdir, 'type_pkg')

    try:
        vd.exportDeliveryPackage(Path(pkgdir), scope='current', data_format='vds')
        vd.sync()

        data_file = None
        for f in os.listdir(os.path.join(pkgdir, 'data')):
            if f.endswith('.vds'):
                data_file = os.path.join(pkgdir, 'data', f)
                break

        assert data_file is not None, 'vds file not found'

        with open(data_file) as fp:
            lines = fp.readlines()

        col_states = []
        for line in lines:
            if line.startswith('#{'):
                d = json.loads(line[1:])
                if 'col' in d:
                    col_states.append(d)

        print('Column metadata found:')
        for cs in col_states:
            print('  ' + str(cs))

        typestrs = [cs.get('typestr', '') for cs in col_states]
        print('Column types: ' + str(typestrs))

        assert any(t == 'str' for t in typestrs), 'str column type not preserved'
        assert any(t == 'int' for t in typestrs), 'int column type not preserved'
        assert any(t == 'float' for t in typestrs), 'float column type not preserved'

        print('\n=== Column types preserved in vds: PASSED ===')

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_tsv_export():
    vd.resetVisiData()

    rows = [
        {'a': 1, 'b': 'x'},
        {'a': 2, 'b': 'y'},
    ]

    vs = make_sheet('tsv_test', rows, coltypes={'a': int, 'b': str})
    vd.push(vs)
    vd.sync(vs.ensureLoaded())

    tmpdir = tempfile.mkdtemp(prefix='vd_test_')
    pkgdir = os.path.join(tmpdir, 'tsv_pkg')

    try:
        vd.exportDeliveryPackage(Path(pkgdir), scope='current', data_format='tsv')
        vd.sync()

        data_dir = os.path.join(pkgdir, 'data')
        tsv_files = [f for f in os.listdir(data_dir) if f.endswith('.tsv')]
        assert len(tsv_files) > 0, 'No tsv data files found'

        with open(os.path.join(data_dir, tsv_files[0])) as fp:
            content = fp.read()
        print('TSV content:')
        print(content)
        assert 'a' in content and 'b' in content, 'TSV missing headers'

        print('\n=== TSV export: PASSED ===')

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_derived_sheet_discovery():
    vd.resetVisiData()

    rows = [
        {'category': 'A', 'value': 10},
        {'category': 'B', 'value': 20},
        {'category': 'A', 'value': 30},
    ]

    parent = make_sheet('parent', rows, coltypes={'category': str, 'value': int})
    vd.push(parent)

    child = make_sheet('derived', rows, coltypes={'category': str, 'value': int})
    child.source = parent
    vd.push(child)
    vd.allSheets.append(child)
    vd.sync()

    from visidata.features.delivery_package import _collect_derived_sheets
    derived = _collect_derived_sheets(parent)
    print('Derived sheets found: ' + str([s.name for s in derived]))
    assert any(s.name == 'derived' for s in derived), 'Derived sheet not discovered'

    print('\n=== Derived sheet discovery: PASSED ===')


if __name__ == '__main__':
    test_directory_export()
    print()
    test_zip_export()
    print()
    test_column_types_preserved_in_vds()
    print()
    test_tsv_export()
    print()
    test_derived_sheet_discovery()
    print()
    print('ALL TESTS PASSED')
