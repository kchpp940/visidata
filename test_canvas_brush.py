import sys
import json
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import visidata
from visidata import vd, Canvas, Box, TableSheet, Column, AttrDict

vd.options.disp_graph_labels = False


def make_source_sheet():
    src = TableSheet('testsrc')

    def make_col(name, **kwargs):
        c = Column(name, **kwargs)
        c.getter = lambda col, row: row[col.name]
        return c

    src.columns = [
        make_col('idx', type=int),
        make_col('x', type=float),
        make_col('y', type=float),
        make_col('label'),
    ]
    rows = []
    for i in range(10):
        r = AttrDict({'idx': i, 'x': float(i), 'y': float(i*2), 'label': 'p%d' % i})
        rows.append(r)
    src.rows = rows
    src.setKeys([src.column('idx')])
    TableSheet.keyCols.fget.cache_clear()
    return src


def make_canvas(src):
    cvs = Canvas('testcanvas')
    cvs.source = src
    cvs.cursorBox = Box(0.0, 0.0, 10.0, 20.0)
    cvs.visibleBox = Box(0.0, 0.0, 10.0, 20.0)
    cvs.canvasBox = Box(0.0, 0.0, 10.0, 20.0)
    for r in src.rows:
        cvs.point(float(r.x), float(r.y), 'green', r)
    return cvs


def test_rowkey_str_stability():
    src = make_source_sheet()
    cvs = make_canvas(src)

    for r in src.rows:
        s1 = cvs._rowkeyStr(r)
        s2 = cvs._rowkeyStr(r)
        assert s1 == s2, 'rowkeyStr should be stable for same row'
        assert s1.startswith('['), 'rowkeyStr should be JSON array: %s' % s1

    r0 = src.rows[0]
    r0_clone = AttrDict({'idx': 0, 'x': 0.0, 'y': 0.0, 'label': 'p0'})
    assert cvs._rowkeyStr(r0) == cvs._rowkeyStr(r0_clone), \
        'rowkeyStr should be same for rows with same key values'


def test_rowkey_from_str():
    src = make_source_sheet()
    cvs = make_canvas(src)

    s = cvs._rowkeyStr(src.rows[3])
    parsed = cvs._rowkeyFromStr(s)
    assert isinstance(parsed, tuple)
    assert parsed == (3,)


def test_match_rows_by_rowkeys():
    src = make_source_sheet()
    cvs = make_canvas(src)

    saved_keys = [cvs._rowkeyStr(src.rows[i]) for i in [1, 3, 5]]
    found, missing = cvs._matchRowsByRowkeys(saved_keys)
    assert len(found) == 3
    assert missing == 0
    assert [r.idx for r in found] == [1, 3, 5]

    extra_key = json.dumps([999])
    found2, missing2 = cvs._matchRowsByRowkeys(saved_keys + [extra_key])
    assert len(found2) == 3
    assert missing2 == 1


def test_make_brush_context():
    src = make_source_sheet()
    cvs = make_canvas(src)

    bb = Box(0.0, 0.0, 10.0, 20.0)
    rows = cvs.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    assert len(rows) == 10

    ctxstr = cvs._makeBrushContext(rows, bb)
    ctx = json.loads(ctxstr)
    assert 'bbox' in ctx
    assert 'rowkeys' in ctx
    assert 'source_sheet' in ctx
    assert ctx['source_sheet'] == 'testsrc'
    assert len(ctx['rowkeys']) == 10


def test_parse_brush_context_backward_compat():
    src = make_source_sheet()
    cvs = make_canvas(src)

    plain_bbox = '1.0 5.0 2.0 10.0'
    bbox, rowkeys = cvs._parseBrushContext(plain_bbox)
    assert bbox == plain_bbox
    assert rowkeys is None

    bb = Box(0.0, 0.0, 10.0, 20.0)
    rows = cvs.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    ctxstr = cvs._makeBrushContext(rows, bb)
    bbox2, rowkeys2 = cvs._parseBrushContext(ctxstr)
    assert bbox2 == cvs.formatBbox(bb)
    assert rowkeys2 is not None
    assert len(rowkeys2) == 10


def test_resolve_rows_from_context_rowkeys_priority():
    src = make_source_sheet()
    cvs = make_canvas(src)

    bb = Box(0.0, 0.0, 10.0, 20.0)
    all_rows = cvs.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    ctxstr = cvs._makeBrushContext(all_rows, bb)

    rows, used, missing = cvs._resolveRowsFromContext(ctxstr)
    assert len(rows) == 10
    assert used == 10
    assert missing == 0

    ctx = json.loads(ctxstr)
    ctx['rowkeys'] = ctx['rowkeys'][:5]
    ctxstr_partial = json.dumps(ctx)
    rows2, used2, missing2 = cvs._resolveRowsFromContext(ctxstr_partial)
    assert len(rows2) == 5
    assert used2 == 5
    assert missing2 == 0


def test_resolve_rows_from_context_missing_rowkeys_fallback():
    src = make_source_sheet()
    cvs = make_canvas(src)

    bb = Box(0.0, 0.0, 10.0, 20.0)
    all_rows = cvs.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    ctxstr = cvs._makeBrushContext(all_rows, bb)

    ctx = json.loads(ctxstr)
    ctx['rowkeys'].append(json.dumps([999]))
    ctxstr_bad = json.dumps(ctx)

    rows, used, missing = cvs._resolveRowsFromContext(ctxstr_bad)
    assert missing == 1
    assert len(rows) == 10


def test_resolve_rows_from_context_plain_bbox():
    src = make_source_sheet()
    cvs = make_canvas(src)

    plain = '0.0 10.0 0.0 20.0'
    rows, used, missing = cvs._resolveRowsFromContext(plain)
    assert len(rows) == 10
    assert used == 0
    assert missing == 0


def test_selectbbox_replay_with_rowkeys():
    src = make_source_sheet()
    cvs = make_canvas(src)

    bb = Box(0.0, 0.0, 5.0, 10.0)
    rows = cvs.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    ctxstr = cvs._makeBrushContext(rows, bb)

    resolved, _, _ = cvs._resolveRowsFromContext(ctxstr)
    src.select(resolved, add_undo=False)

    selected = [r for r in src.rows if src.isSelected(r)]
    assert len(selected) == len(rows)
    for r in rows:
        assert src.isSelected(r)


def test_save_named_selection_stores_rowkeys():
    src = make_source_sheet()
    cvs = make_canvas(src)

    cvs.cursorBox = Box(0.0, 0.0, 5.0, 10.0)
    vd.selections.clear()

    bb = cvs.cursorBox
    rows = cvs.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    sel = {
        'name': 'testsel',
        'sheet': cvs.name,
        'source_sheet': cvs.source.name if cvs.source else '',
        'xcols': [c.name for c in getattr(cvs, 'xcols', [])],
        'ycols': [c.name for c in getattr(cvs, 'ycols', [])],
        'xmin': float(bb.xmin),
        'xmax': float(bb.xmax),
        'ymin': float(bb.ymin),
        'ymax': float(bb.ymax),
    }
    if cvs.source and rows:
        sel['rowkeys'] = [cvs._rowkeyStr(r) for r in rows]
    list.append(vd.selections, AttrDict(sel))

    assert len(vd.selections) == 1
    s = vd.selections[0]
    assert s.name == 'testsel'
    assert s.source_sheet == 'testsrc'
    assert 'rowkeys' in s
    assert len(s.rowkeys) > 0


def test_load_named_selection_by_rowkey():
    src = make_source_sheet()
    cvs = make_canvas(src)

    cvs.cursorBox = Box(0.0, 0.0, 5.0, 10.0)
    vd.selections.clear()

    bb = cvs.cursorBox
    rows = cvs.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    sel = {
        'name': 'testsel2',
        'sheet': cvs.name,
        'source_sheet': cvs.source.name if cvs.source else '',
        'xcols': [c.name for c in getattr(cvs, 'xcols', [])],
        'ycols': [c.name for c in getattr(cvs, 'ycols', [])],
        'xmin': float(bb.xmin),
        'xmax': float(bb.xmax),
        'ymin': float(bb.ymin),
        'ymax': float(bb.ymax),
        'rowkeys': [cvs._rowkeyStr(r) for r in rows],
    }
    list.append(vd.selections, AttrDict(sel))

    for r in src.rows:
        src.unselect(r)

    saved_rowkeys = list(vd.selections[0].rowkeys)
    found_rows, _ = cvs._matchRowsByRowkeys(saved_rowkeys)
    src.select(found_rows)

    selected = [r for r in src.rows if src.isSelected(r)]
    expected = cvs.rowsWithinDataBox(0.0, 0.0, 5.0, 10.0)
    assert len(selected) == len(expected)


def test_cross_session_rowkey_stability():
    '''Simulate cross-session: save rowkeys, then reload data with brand-new row objects
    (different Python id()) and verify rowkeys still match the right rows.'''

    src1 = make_source_sheet()
    cvs1 = make_canvas(src1)
    bb = Box(2.0, 4.0, 7.0, 14.0)
    rows1 = cvs1.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    saved_rowkeys = [cvs1._rowkeyStr(r) for r in rows1]
    original_idxs = sorted([r.idx for r in rows1])

    src2 = make_source_sheet()
    cvs2 = make_canvas(src2)

    for r in src2.rows:
        src2.unselect(r)

    found_rows, missing = cvs2._matchRowsByRowkeys(saved_rowkeys)
    assert missing == 0
    found_idxs = sorted([r.idx for r in found_rows])
    assert found_idxs == original_idxs

    for r in src1.rows:
        for r2 in src2.rows:
            if r.idx == r2.idx:
                assert id(r) != id(r2), 'simulated reload must produce different Python objects'
                assert cvs1._rowkeyStr(r) == cvs2._rowkeyStr(r2), 'same data must produce same rowkey'


def test_cross_session_rowkey_with_missing_fallback():
    '''Simulate: save selection with 5 rowkeys, reload with only 3 matching,
    ensure missing ones trigger bbox fallback.'''

    src1 = make_source_sheet()
    cvs1 = make_canvas(src1)
    bb = Box(0.0, 0.0, 5.0, 10.0)
    rows1 = cvs1.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    saved_rowkeys = [cvs1._rowkeyStr(r) for r in rows1]

    src2 = make_source_sheet()
    src2.rows = [r for r in src2.rows if r.idx not in (1, 3)]
    cvs2 = make_canvas(src2)

    ctx = {
        'bbox': cvs2.formatBbox((bb.xmin, bb.xmax, bb.ymin, bb.ymax)),
        'source_sheet': 'testsrc',
        'xcols': [],
        'ycols': [],
        'rowkeys': saved_rowkeys,
    }
    ctxstr = json.dumps(ctx)
    resolved, used, missing = cvs2._resolveRowsFromContext(ctxstr)

    assert used == 4
    assert missing == 2

    resolved_idxs = sorted([r.idx for r in resolved])
    assert resolved_idxs == [0, 2, 4, 5]


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
