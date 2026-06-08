'''Behavior verification for GraphSheet reference line cache invalidation.

Tests that:
  1. Cursor-only movement does NOT trigger plot_reflines recomputation.
  2. Zoom (xzoomlevel/yzoomlevel change) DOES trigger recomputation.
  3. Window resize (plotviewBox change) DOES trigger recomputation.
  4. Adding/erasing reference lines DOES trigger recomputation.
  5. Direct visibleBox modification (e.g., go-pagedown) DOES trigger recomputation.

Run with: python3 visidata/tests/test_graph_refline_cache.py
'''
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import Mock, patch
import visidata
from visidata import GraphSheet, Sheet, Column, BoundingBox

vd = visidata.vd

vd.options.overwrite = 'y'

def _make_graph():
    src = Sheet('test')
    xcol = Column('x', type=float)
    ycol = Column('y', type=float)
    src.addColumn(xcol)
    src.addColumn(ycol)
    rows = []
    for i in range(10):
        row = object()
        rows.append(row)
        xcol.setValue(row, float(i))
        ycol.setValue(row, float(i * 2))
    src.rows = rows
    src.sourceRows = rows
    g = GraphSheet('graph', 'graph', source=src, sourceRows=rows,
                   xcols=[src.columns[0]], ycols=[src.columns[1]])
    g.resetCanvasDimensions(25, 80)
    g.canvasBox = BoundingBox(0.0, 0.0, 10.0, 20.0)
    g.resetBounds(refresh=False)
    g.reflines_x = [3.0, 7.0]
    g.reflines_y = [5.0, 15.0]
    g.plot_reflines()
    assert g._reflines_dirty is False, 'after plot_reflines, dirty must be False'
    assert g._reflines_signature is not None, 'after plot_reflines, signature must be set'
    return g

def test_cursor_move_no_recompute():
    '''Moving cursor (cursorBox only) must NOT mark dirty or call plot_reflines.'''
    g = _make_graph()
    call_count = [0]
    orig = g.plot_reflines
    def counting_plot():
        call_count[0] += 1
        orig()
    g.plot_reflines = counting_plot

    old_sig = g._reflines_signature

    g.cursorBox.ymin += g.canvasCharHeight
    g.resetBounds(refresh=False)

    assert g._reflines_dirty is False, 'cursor move: dirty must stay False'
    assert g._reflines_signature == old_sig, 'cursor move: signature must not change'
    assert call_count[0] == 0, f'cursor move: plot_reflines called {call_count[0]} times (expected 0)'
    print('PASS: cursor move does not trigger plot_reflines')

def test_zoom_triggers_recompute():
    '''Changing zoom levels MUST mark dirty and plot_reflines must be called via draw path.'''
    g = _make_graph()
    call_count = [0]
    orig = g.plot_reflines
    def counting_plot():
        call_count[0] += 1
        orig()
    g.plot_reflines = counting_plot

    g.xzoomlevel *= 2.0
    g.yzoomlevel *= 2.0
    g.resetBounds(refresh=False)

    assert g._reflines_dirty is True, 'zoom: dirty must be True after resetBounds'
    g._ensure_reflines_cache()
    assert call_count[0] == 1, f'zoom: plot_reflines called {call_count[0]} times (expected 1)'
    print('PASS: zoom triggers plot_reflines exactly once')

def test_resize_triggers_recompute():
    '''Changing window dimensions (plotviewBox) MUST mark dirty.'''
    g = _make_graph()
    call_count = [0]
    orig = g.plot_reflines
    def counting_plot():
        call_count[0] += 1
        orig()
    g.plot_reflines = counting_plot

    g.resetCanvasDimensions(50, 120)

    assert g._reflines_dirty is True, 'resize: dirty must be True after resetCanvasDimensions'
    g._ensure_reflines_cache()
    assert call_count[0] == 1, f'resize: plot_reflines called {call_count[0]} times (expected 1)'
    print('PASS: window resize triggers plot_reflines')

def test_add_refline_triggers_recompute():
    '''Adding a reference line MUST mark dirty.'''
    g = _make_graph()
    call_count = [0]
    orig = g.plot_reflines
    def counting_plot():
        call_count[0] += 1
        orig()
    g.plot_reflines = counting_plot

    g.reflines_x.append(5.0)
    g._mark_reflines_dirty()

    assert g._reflines_dirty is True, 'add refline: dirty must be True'
    g._ensure_reflines_cache()
    assert call_count[0] == 1, f'add refline: plot_reflines called {call_count[0]} times (expected 1)'
    print('PASS: adding reference line triggers plot_reflines')

def test_erase_refline_triggers_recompute():
    '''Erasing a reference line MUST mark dirty.'''
    g = _make_graph()
    call_count = [0]
    orig = g.plot_reflines
    def counting_plot():
        call_count[0] += 1
        orig()
    g.plot_reflines = counting_plot

    g.reflines_x.remove(3.0)
    g._mark_reflines_dirty()

    assert g._reflines_dirty is True, 'erase refline: dirty must be True'
    g._ensure_reflines_cache()
    assert call_count[0] == 1, f'erase refline: plot_reflines called {call_count[0]} times (expected 1)'
    print('PASS: erasing reference line triggers plot_reflines')

def test_visiblebox_direct_modification_triggers_recompute():
    '''Direct visibleBox.ymin modification (go-pagedown pattern) MUST mark dirty via resetBounds.'''
    g = _make_graph()
    call_count = [0]
    orig = g.plot_reflines
    def counting_plot():
        call_count[0] += 1
        orig()
    g.plot_reflines = counting_plot

    t = g.visibleBox.h
    g.visibleBox.ymin -= t
    g.resetBounds(refresh=False)

    assert g._reflines_dirty is True, 'visibleBox direct mod: dirty must be True'
    g._ensure_reflines_cache()
    assert call_count[0] == 1, f'visibleBox direct mod: plot_reflines called {call_count[0]} times (expected 1)'
    print('PASS: direct visibleBox modification (go-pagedown pattern) triggers plot_reflines')

def test_signature_unchanged_on_cursor_only_render():
    '''Full render() path on cursor-only move: signature match prevents recomputation.'''
    g = _make_graph()
    call_count = [0]
    orig = g.plot_reflines
    def counting_plot():
        call_count[0] += 1
        orig()
    g.plot_reflines = counting_plot

    g.cursorBox.ymin += g.canvasCharHeight
    g.render(25, 80)

    assert call_count[0] == 0, (
        f'cursor-only render: plot_reflines called {call_count[0]} times (expected 0). '
        'The signature check should short-circuit.')
    print('PASS: full render() on cursor-only move does not call plot_reflines')

if __name__ == '__main__':
    test_cursor_move_no_recompute()
    test_zoom_triggers_recompute()
    test_resize_triggers_recompute()
    test_add_refline_triggers_recompute()
    test_erase_refline_triggers_recompute()
    test_visiblebox_direct_modification_triggers_recompute()
    test_signature_unchanged_on_cursor_only_render()
    print('\nAll refline cache behavior tests passed.')
