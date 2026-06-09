import pytest
import visidata
from visidata import vd, TableSheet, ItemColumn


@pytest.fixture(autouse=True)
def reset_vd():
    import visidata.snapshot
    import visidata.features.delivery_package
    vd.resetVisiData()
    yield
    vd.resetVisiData()


def test_snapshot_basic_keys():
    snap = vd.generate_snapshot(scope='all', include_cmdlog=False, include_macros=False)
    assert 'version' in snap
    assert 'order' in snap
    assert 'sheets' in snap
    assert 'global_options' in snap


def test_snapshot_columns_preserved():
    vs = TableSheet('test_sheet')
    vs.addColumn(ItemColumn('name', 0))
    vs.addColumn(ItemColumn('age', 1))
    vs.columns[0].type = str
    vs.columns[1].type = int
    vs.rows = [('Alice', 30), ('Bob', 25)]
    vd.push(vs)
    vd.sync(vs.ensureLoaded())

    snap = vd.generate_snapshot(scope='current', include_cmdlog=False, include_macros=False)
    assert snap['order'] == ['test_sheet']
    cols = snap['sheets']['test_sheet']['columns']
    assert len(cols) == 2
    assert cols[0]['name'] == 'name'
    assert cols[0]['typestr'] == 'str'
    assert cols[1]['name'] == 'age'
    assert cols[1]['typestr'] == 'int'


def test_load_snapshot_restores_columns():
    vs = TableSheet('test_sheet')
    vs.addColumn(ItemColumn('name', 0))
    vs.addColumn(ItemColumn('age', 1))
    vs.columns[0].type = str
    vs.columns[1].type = int
    vs.rows = [('Alice', 30), ('Bob', 25)]
    vd.push(vs)
    vd.sync(vs.ensureLoaded())

    snap = vd.generate_snapshot(scope='current', include_cmdlog=False, include_macros=False)
    vd.resetVisiData()
    restored = vd.load_snapshot(snap, apply_cmdlog=False, apply_macros=False)
    vs2 = restored['test_sheet']
    assert [c.name for c in vs2.columns] == ['name', 'age']
    assert vs2.columns[0].type == str
    assert vs2.columns[1].type == int


def test_snapshot_graph_state():
    from visidata.snapshot import _sheet_snapshot_graph

    class FakeGraph:
        reflines_x = [(1.0, 'one'), (2.0, 'two')]
        reflines_y = [(10.0, 'high')]
        xzoomlevel = 2.0
        yzoomlevel = 0.5

    gs = FakeGraph()
    gstate = _sheet_snapshot_graph(gs)
    assert gstate is not None
    assert gstate.get('xzoomlevel') == 2.0
    assert gstate.get('yzoomlevel') == 0.5
    assert (1.0, 'one') in [tuple(x) for x in gstate.get('reflines_x', [])]


def test_snapshot_global_options():
    vd.options.set('encoding', 'utf-16', 'global', cmdlog=False)
    snap = vd.generate_snapshot(scope='all', include_cmdlog=False, include_macros=False)
    assert snap['global_options'].get('encoding') == 'utf-16'


def test_snapshot_sheet_options():
    vs = TableSheet('opts_test')
    vs.rows = []
    vs.options.set('header', 0, vs, cmdlog=False)
    vd.push(vs)

    snap = vd.generate_snapshot(scope='current', include_cmdlog=False, include_macros=False)
    opts = snap['sheets']['opts_test'].get('options', {})
    assert opts.get('header') == 0


def test_collect_derived_sheets():
    from visidata.snapshot import _collect_derived_sheets
    root = TableSheet('root')
    root.rows = [(1,), (2,)]
    vd.push(root)

    derived = TableSheet('derived', source=root)
    derived.rows = []
    vd.push(derived)

    found = _collect_derived_sheets(root)
    names = {s.name for s in found}
    assert 'derived' in names


def test_snapshot_macros():
    from visidata.cmdlog import CommandLogJsonl
    rows = [{'sheet': '', 'col': '', 'row': '', 'longname': 'test-command',
             'input': '', 'keystrokes': '', 'comment': ''}]
    cmdlog = CommandLogJsonl('test_macro', rows=[vd.cmdlog.newRow(**r) for r in rows])
    cmdlog.keystroke = ''
    cmdlog.helpstr = 'a test macro'
    cmdlog.source = ''
    vd.setMacro('test-macro', cmdlog, helpstr='a test macro', keystroke='')

    snap = vd.generate_snapshot(scope='all', include_cmdlog=False, include_macros=True)
    macros = snap.get('macros', [])
    bindings = {m['binding'] for m in macros}
    assert 'test-macro' in bindings


def test_load_snapshot_macros():
    from visidata.cmdlog import CommandLogJsonl
    rows = [{'sheet': '', 'col': '', 'row': '', 'longname': 'test-command',
             'input': '', 'keystrokes': '', 'comment': ''}]
    cmdlog = CommandLogJsonl('test_macro', rows=[vd.cmdlog.newRow(**r) for r in rows])
    cmdlog.keystroke = ''
    cmdlog.helpstr = 'a test macro'
    cmdlog.source = ''
    vd.setMacro('test-macro', cmdlog, helpstr='a test macro', keystroke='')

    snap = vd.generate_snapshot(scope='all', include_cmdlog=False, include_macros=True)
    vd.resetVisiData()
    vd.load_snapshot(snap, apply_cmdlog=False, apply_macros=True)
    assert 'test-macro' in vd.macrobindings


def test_vdj_embedded_manifest_roundtrip(tmp_path):
    vs = TableSheet('people')
    vs.addColumn(ItemColumn('name', 0))
    vs.addColumn(ItemColumn('age', 1))
    vs.columns[0].type = str
    vs.columns[1].type = int
    vs.rows = [('Alice', 30), ('Bob', 25)]
    vd.push(vs)
    vd.sync(vs.ensureLoaded())

    snap = vd.generate_snapshot(scope='current', include_cmdlog=True, include_macros=True, include_data=True)
    cmdlog_rows = [{'sheet': 'people', 'col': '', 'row': '', 'longname': 'open-file',
                    'input': '', 'keystrokes': '', 'comment': ''}]
    snap['cmdlog'] = cmdlog_rows

    p = tmp_path / 'workspace.vdj'
    vd.write_snapshot(str(p), 'vdj', snap)

    text = p.read_text()
    assert '#snapshot-manifest:' in text
    assert '"sheets"' in text

    vd.resetVisiData()
    read_back = vd.read_snapshot(str(p), fmt='vdj')
    assert 'people' in read_back['sheets']
    assert read_back['sheets']['people']['columns'][0]['typestr'] == 'str'
    assert read_back['cmdlog'][0]['longname'] == 'open-file'


def test_vdx_embedded_manifest_roundtrip(tmp_path):
    vs = TableSheet('people')
    vs.addColumn(ItemColumn('name', 0))
    vs.columns[0].type = str
    vs.rows = [('Alice',)]
    vd.push(vs)
    vd.sync(vs.ensureLoaded())

    snap = vd.generate_snapshot(scope='current', include_cmdlog=True, include_macros=True, include_data=True)
    snap['cmdlog'] = [{'sheet': 'people', 'col': '', 'row': '', 'longname': 'open-file',
                       'input': '', 'keystrokes': '', 'comment': ''}]

    p = tmp_path / 'workspace.vdx'
    vd.write_snapshot(str(p), 'vdx', snap)

    text = p.read_text()
    assert '#snapshot-manifest:' in text

    vd.resetVisiData()
    read_back = vd.read_snapshot(str(p), fmt='vdx')
    assert 'people' in read_back['sheets']
    assert read_back['cmdlog'][0]['longname'] == 'open-file'


def test_vds_roundtrip_preserves_types(tmp_path):
    vs = TableSheet('nums')
    vs.addColumn(ItemColumn('val', 0))
    vs.columns[0].type = int
    vs.rows = [(42,), (99,)]
    vd.push(vs)
    vd.sync(vs.ensureLoaded())

    p = tmp_path / 'data.vds'
    vd.save_vds(str(p), vs)

    vd.resetVisiData()
    idx = vd.open_vds(str(p))
    vd.sync(idx.ensureLoaded())
    sheet = idx.rows[0]
    vd.sync(sheet.ensureLoaded())
    assert [c.name for c in sheet.columns] == ['val']
    assert sheet.columns[0].type == int


def test_delivery_vdz_roundtrip(tmp_path):
    vs = TableSheet('fruits')
    vs.addColumn(ItemColumn('name', 0))
    vs.rows = [('apple',), ('banana',)]
    vd.push(vs)
    vd.sync(vs.ensureLoaded())

    p = tmp_path / 'pkg.vdz'
    vd.write_snapshot(str(p), 'delivery',
                       vd.generate_snapshot(scope='current', include_cmdlog=True, include_macros=False),
                       data_format='vds', data_sheets={'fruits': vs})

    assert p.exists()
    vd.resetVisiData()
    restored = vd.open_vdz(str(p))
    fruit_names = {s.name for s in restored.rows}
    assert 'fruits' in fruit_names


def test_guess_snapshot_detects_delivery_directory(tmp_path):
    from visidata import Path
    pkgdir = tmp_path / 'mypkg'
    pkgdir.mkdir()
    (pkgdir / 'manifest.json').write_text('{"sheets": {}, "order": []}')
    (pkgdir / 'data').mkdir()

    guess = vd.guess_snapshot(Path(str(pkgdir)))
    assert guess is not None
    assert guess['filetype'] == 'delivery'
    assert guess['_likelihood'] == 10


def test_guess_snapshot_detects_vdz(tmp_path):
    from visidata import Path
    vs = TableSheet('x')
    vs.rows = [(1,)]
    vd.push(vs)

    p = tmp_path / 'test.vdz'
    vd.write_snapshot(str(p), 'delivery',
                       vd.generate_snapshot(scope='current'),
                       data_format='vds', data_sheets={'x': vs})

    guess = vd.guess_snapshot(Path(str(p)))
    assert guess is not None
    assert guess['filetype'] == 'delivery'


def test_write_snapshot_dispatch_all_formats(tmp_path):
    snap = {
        'version': 'test',
        'order': [],
        'sheets': {},
        'cmdlog': [{'sheet': '', 'col': '', 'row': '', 'longname': 'noop',
                    'input': '', 'keystrokes': '', 'comment': ''}],
    }
    for fmt in ('vd', 'vdj', 'vdx'):
        p = tmp_path / f'out.{fmt}'
        vd.write_snapshot(str(p), fmt, snap)
        assert p.exists()
        back = vd.read_snapshot(str(p), fmt=fmt)
        assert back['cmdlog'][0]['longname'] == 'noop'

