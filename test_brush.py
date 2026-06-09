import visidata
from visidata import vd, Sheet, Canvas, GraphSheet, ColumnItem

vs = Sheet('test')
vs.rows = []
for i in range(10):
    vs.rows.append((i, i * 2, i * i))
vs.columns = [
    ColumnItem('x', 0, type=int),
    ColumnItem('y', 1, type=int),
    ColumnItem('z', 2, type=int),
]
vs.setKeys([vs.columns[0]])

print(f"keyCols: {[c.name for c in vs.keyCols]}")
print(f"keyCols types: {[c.type for c in vs.keyCols]}")
print(f"numeric keyCols: {[c.name for c in vd.numericCols(vs.keyCols)]}")

gs = GraphSheet('test_graph', source=vs, sourceRows=vs.rows, xcols=vs.keyCols, ycols=[vs.columns[1], vs.columns[2]])
gs.ensureLoaded()
vd.sync()

print(f"Number of polylines (points): {len(gs.polylines)}")
assert len(gs.polylines) > 0, "No polylines created"

rows = gs.rowsWithinDataBox(2, 4, 5, 10)
print(f"rowsWithinDataBox(2,4,5,10) returned {len(rows)} rows")
for r in rows:
    print(f"  row: {r}")

gs.source.select(rows)
print(f"Selected {gs.source.nSelectedRows} rows on source sheet")
assert gs.source.nSelectedRows == len(rows), "Selection count mismatch"

bbox_str = gs.formatBbox(gs.cursorBox)
print(f"formatBbox(cursorBox): '{bbox_str}'")
parsed = gs.parseBbox(bbox_str)
print(f"parseBbox result: {parsed}")
assert len(parsed) == 4, "parseBbox should return 4 values"

gs.source.clearSelected()
gs.selectBbox("2 5 4 10")
vd.sync()
print(f"After selectBbox('2 5 4 10'): {gs.source.nSelectedRows} selected")
assert gs.source.nSelectedRows > 0, "selectBbox should select rows"

before_ids = set(gs.source.rowid(r) for r in gs.source.selectedRows)
gs.source.rows = list(reversed(gs.source.rows))
after_ids = set(gs.source.rowid(r) for r in gs.source.selectedRows)
print(f"Selection survives sort/reverse: {before_ids == after_ids}")
assert before_ids == after_ids, "Selection should survive row reordering"

gs.source.clearSelected()
gs.cursorBox.xmin = 2
gs.cursorBox.xmax = 5
gs.cursorBox.ymin = 4
gs.cursorBox.ymax = 10
gs.saveNamedSelection('test_region')
print(f"Saved named selection 'test_region'")

gs.source.clearSelected()
assert gs.source.nSelectedRows == 0, "Should have 0 selected after clear"
gs.loadNamedSelection('test_region')
vd.sync()
print(f"After loading named selection: {gs.source.nSelectedRows} selected")
assert gs.source.nSelectedRows > 0, "loadNamedSelection should select rows"

gs.deleteNamedSelection('test_region')
print(f"Deleted named selection 'test_region'")

sl = gs.statusLine
print(f"statusLine: {sl}")
assert 'selected' in sl, "statusLine should include selected count"

print("\n=== ALL TESTS PASSED ===")
