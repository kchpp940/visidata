import pytest
import visidata
from visidata import vd, TableSheet, ItemColumn


@pytest.fixture(autouse=True)
def reset_vd():
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
