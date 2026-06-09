import sys
import json
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import visidata
from visidata import vd, Canvas, Box, TableSheet, Column, AttrDict

vd.options.disp_graph_labels = False


def make_source_sheet(with_keys=True, sheet_name='testsrc'):
    src = TableSheet(sheet_name)

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
    if with_keys:
        src.setKeys([src.column('idx')])
    TableSheet.keyCols.fget.cache_clear()
    return src


def make_canvas(src, xcols=None, ycols=None):
    cvs = Canvas('testcanvas')
    cvs.source = src
    cvs.cursorBox = Box(0.0, 0.0, 10.0, 20.0)
    cvs.visibleBox = Box(0.0, 0.0, 10.0, 20.0)
    cvs.canvasBox = Box(0.0, 0.0, 10.0, 20.0)
    if xcols:
        cvs.xcols = xcols
    if ycols:
        cvs.ycols = ycols
    for r in src.rows:
        cvs.point(float(r.x), float(r.y), 'green', r)
    return cvs


def test_has_stable_rowkeys_true():
    src = make_source_sheet(with_keys=True)
    cvs = make_canvas(src)
    assert cvs._hasStableRowkeys() is True


def test_has_stable_rowkeys_false():
    src = make_source_sheet(with_keys=False)
    cvs = make_canvas(src)
    assert cvs._hasStableRowkeys() is False


def test_make_brush_context_has_vdbc_prefix():
    src = make_source_sheet(with_keys=True)
    cvs = make_canvas(src)
    bb = Box(0.0, 0.0, 10.0, 20.0)
    rows = cvs.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    ctxstr = cvs._makeBrushContext(rows, bb)
    assert ctxstr.startswith(Canvas.VD_BRUSH_CONTEXT_PREFIX), 'must have _vdbc: prefix'
    ctx = json.loads(ctxstr[len(Canvas.VD_BRUSH_CONTEXT_PREFIX):])
    assert ctx['stable_rowkeys'] is True
    assert 'rowkeys' in ctx
    assert len(ctx['rowkeys']) == 10
    assert ctx['source_sheet'] == 'testsrc'


def test_make_brush_context_no_keys_omits_rowkeys():
    src = make_source_sheet(with_keys=False)
    cvs = make_canvas(src)
    bb = Box(0.0, 0.0, 10.0, 20.0)
    rows = cvs.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    ctxstr = cvs._makeBrushContext(rows, bb)
    ctx = json.loads(ctxstr[len(Canvas.VD_BRUSH_CONTEXT_PREFIX):])
    assert ctx['stable_rowkeys'] is False
    assert 'rowkeys' not in ctx, 'should not store rowkeys without stable keycols'


def test_parse_brush_context_three_formats():
    src = make_source_sheet(with_keys=True)
    cvs = make_canvas(src)

    plain = '0.0 10.0 0.0 20.0'
    bbox, rk, ctx = cvs._parseBrushContext(plain)
    assert bbox == plain
    assert rk is None
    assert ctx is None

    bb = Box(0.0, 0.0, 10.0, 20.0)
    rows = cvs.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    prefixed = cvs._makeBrushContext(rows, bb)
    bbox2, rk2, ctx2 = cvs._parseBrushContext(prefixed)
    assert rk2 is not None
    assert len(rk2) == 10
    assert ctx2 is not None
    assert ctx2['stable_rowkeys'] is True

    bare_json = json.dumps({'bbox': '0.0 10.0 0.0 20.0', 'rowkeys': ['[1]']})
    bbox3, rk3, ctx3 = cvs._parseBrushContext(bare_json)
    assert bbox3 == '0.0 10.0 0.0 20.0'
    assert rk3 == ['[1]']
    assert ctx3 is not None


def test_parse_brush_context_strips_whitespace_vdx_runvdx_compat():
    src = make_source_sheet(with_keys=True)
    cvs = make_canvas(src)

    bb = Box(0.0, 0.0, 10.0, 20.0)
    rows = cvs.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    ctxstr = cvs._makeBrushContext(rows, bb)

    padded = '   ' + ctxstr + '  \n'
    bbox, rk, ctx = cvs._parseBrushContext(padded)
    assert rk is not None, 'leading whitespace should be stripped (VDX runvdx compat)'
    assert len(rk) == 10

    padded_plain = '  0.0 10.0 0.0 20.0  '
    bbox2, rk2, ctx2 = cvs._parseBrushContext(padded_plain)
    assert bbox2 == '0.0 10.0 0.0 20.0'
    assert rk2 is None


def test_validate_brush_context_all_match():
    src = make_source_sheet(with_keys=True)
    xcol = src.column('x')
    ycol = src.column('y')
    cvs = make_canvas(src, xcols=[xcol], ycols=[ycol])

    ctx = {
        'source_sheet': 'testsrc',
        'xcols': ['x'],
        'ycols': ['y'],
        'bbox': '0.0 10.0 0.0 20.0',
    }
    warns = cvs._validateBrushContext(ctx)
    assert warns == []


def test_validate_brush_context_source_mismatch():
    src = make_source_sheet(with_keys=True)
    cvs = make_canvas(src)
    ctx = {'source_sheet': 'wrong_name', 'bbox': '0.0 10.0 0.0 20.0'}
    warns = cvs._validateBrushContext(ctx)
    assert len(warns) >= 1
    assert any('source sheet mismatch' in w for w in warns)


def test_validate_brush_context_xcols_mismatch():
    src = make_source_sheet(with_keys=True)
    xcol = src.column('x')
    ycol = src.column('y')
    cvs = make_canvas(src, xcols=[xcol], ycols=[ycol])
    ctx = {
        'source_sheet': 'testsrc',
        'xcols': ['date'],
        'ycols': ['y'],
        'bbox': '0.0 10.0 0.0 20.0',
    }
    warns = cvs._validateBrushContext(ctx)
    assert any('x columns mismatch' in w for w in warns)


def test_validate_brush_context_ycols_mismatch():
    src = make_source_sheet(with_keys=True)
    xcol = src.column('x')
    ycol = src.column('y')
    cvs = make_canvas(src, xcols=[xcol], ycols=[ycol])
    ctx = {
        'source_sheet': 'testsrc',
        'xcols': ['x'],
        'ycols': ['revenue'],
        'bbox': '0.0 10.0 0.0 20.0',
    }
    warns = cvs._validateBrushContext(ctx)
    assert any('y columns mismatch' in w for w in warns)


def test_validate_brush_context_bbox_parse_error():
    src = make_source_sheet(with_keys=True)
    cvs = make_canvas(src)
    ctx = {
        'source_sheet': 'testsrc',
        'xcols': [],
        'ycols': [],
        'bbox': 'not a valid bbox',
    }
    warns = cvs._validateBrushContext(ctx)
    assert any('bbox parse error' in w for w in warns)


def test_resolve_rows_validation_mismatch_disables_rowkeys():
    src1 = make_source_sheet(with_keys=True, sheet_name='sheetA')
    cvs1 = make_canvas(src1)
    bb = Box(0.0, 0.0, 5.0, 10.0)
    rows = cvs1.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    ctxstr = cvs1._makeBrushContext(rows, bb)

    src2 = make_source_sheet(with_keys=True, sheet_name='sheetB')
    cvs2 = make_canvas(src2)

    resolved, used, missing = cvs2._resolveRowsFromContext(ctxstr)
    assert used == 0, 'rowkeys must not be used when source sheet mismatches'
    assert len(resolved) == 6, 'should fall back to bbox which still finds 6 rows in 0..5, 0..10'


def test_resolve_rows_no_stable_keys_on_target():
    src1 = make_source_sheet(with_keys=True)
    cvs1 = make_canvas(src1)
    bb = Box(0.0, 0.0, 5.0, 10.0)
    rows = cvs1.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    ctxstr = cvs1._makeBrushContext(rows, bb)

    src2 = make_source_sheet(with_keys=False)
    cvs2 = make_canvas(src2)

    resolved, used, missing = cvs2._resolveRowsFromContext(ctxstr)
    assert used == 0, 'rowkeys must not be used when target has no stable keycols'
    assert len(resolved) == 6


def test_save_named_selection_without_keys_warns():
    src = make_source_sheet(with_keys=False)
    cvs = make_canvas(src)
    cvs.cursorBox = Box(0.0, 0.0, 5.0, 10.0)
    vd.selections.clear()

    bb = cvs.cursorBox
    rows = cvs.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    has_stable = cvs._hasStableRowkeys()
    assert has_stable is False

    sel = {
        'name': 'testsel_nokeys',
        'sheet': cvs.name,
        'source_sheet': cvs.source.name if cvs.source else '',
        'xcols': [c.name for c in getattr(cvs, 'xcols', [])],
        'ycols': [c.name for c in getattr(cvs, 'ycols', [])],
        'xmin': float(bb.xmin),
        'xmax': float(bb.xmax),
        'ymin': float(bb.ymin),
        'ymax': float(bb.ymax),
        'stable_rowkeys': False,
    }
    list.append(vd.selections, AttrDict(sel))

    assert len(vd.selections) == 1
    s = vd.selections[0]
    assert s.stable_rowkeys is False
    assert 'rowkeys' not in s


def test_rowkey_str_stability():
    src = make_source_sheet(with_keys=True)
    cvs = make_canvas(src)

    for r in src.rows:
        s1 = cvs._rowkeyStr(r)
        s2 = cvs._rowkeyStr(r)
        assert s1 == s2
        assert s1.startswith('[')

    r0 = src.rows[0]
    r0_clone = AttrDict({'idx': 0, 'x': 0.0, 'y': 0.0, 'label': 'p0'})
    assert cvs._rowkeyStr(r0) == cvs._rowkeyStr(r0_clone)


def test_match_rows_by_rowkeys():
    src = make_source_sheet(with_keys=True)
    cvs = make_canvas(src)

    saved_keys = [cvs._rowkeyStr(src.rows[i]) for i in [1, 3, 5]]
    found, missing = cvs._matchRowsByRowkeys(saved_keys)
    assert len(found) == 3
    assert missing == 0
    assert [r.idx for r in found] == [1, 3, 5]


def test_cross_session_rowkey_stability():
    src1 = make_source_sheet(with_keys=True)
    cvs1 = make_canvas(src1)
    bb = Box(2.0, 4.0, 7.0, 14.0)
    rows1 = cvs1.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    ctxstr = cvs1._makeBrushContext(rows1, bb)
    original_idxs = sorted([r.idx for r in rows1])

    src2 = make_source_sheet(with_keys=True)
    cvs2 = make_canvas(src2)

    resolved, used, missing = cvs2._resolveRowsFromContext(ctxstr)
    assert missing == 0
    found_idxs = sorted([r.idx for r in resolved])
    assert found_idxs == original_idxs

    for r in src1.rows:
        for r2 in src2.rows:
            if r.idx == r2.idx:
                assert id(r) != id(r2)
                assert cvs1._rowkeyStr(r) == cvs2._rowkeyStr(r2)


def test_cross_session_rowkey_with_missing_fallback():
    src1 = make_source_sheet(with_keys=True)
    cvs1 = make_canvas(src1)
    bb = Box(0.0, 0.0, 5.0, 10.0)
    rows1 = cvs1.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    ctxstr = cvs1._makeBrushContext(rows1, bb)

    src2 = make_source_sheet(with_keys=True)
    src2.rows = [r for r in src2.rows if r.idx not in (1, 3)]
    cvs2 = make_canvas(src2)

    resolved, used, missing = cvs2._resolveRowsFromContext(ctxstr)
    assert used == 4
    assert missing == 2
    resolved_idxs = sorted([r.idx for r in resolved])
    assert resolved_idxs == [0, 2, 4, 5]


def test_load_named_selection_validates_source():
    src1 = make_source_sheet(with_keys=True, sheet_name='original')
    cvs1 = make_canvas(src1)
    cvs1.cursorBox = Box(0.0, 0.0, 5.0, 10.0)
    vd.selections.clear()

    bb = cvs1.cursorBox
    rows = cvs1.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax)
    sel = {
        'name': 'cross_sheet_sel',
        'sheet': cvs1.name,
        'source_sheet': 'original',
        'xcols': [],
        'ycols': [],
        'xmin': float(bb.xmin),
        'xmax': float(bb.xmax),
        'ymin': float(bb.ymin),
        'ymax': float(bb.ymax),
        'stable_rowkeys': True,
        'rowkeys': [cvs1._rowkeyStr(r) for r in rows],
    }
    list.append(vd.selections, AttrDict(sel))

    src2 = make_source_sheet(with_keys=True, sheet_name='different')
    cvs2 = make_canvas(src2)

    ctx_for_validate = {
        'source_sheet': 'original',
        'xcols': [],
        'ycols': [],
        'bbox': '%s %s %s %s' % (sel['xmin'], sel['xmax'], sel['ymin'], sel['ymax']),
    }
    warns = cvs2._validateBrushContext(ctx_for_validate)
    assert any('source sheet mismatch' in w for w in warns), 'must detect source sheet mismatch on load'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
