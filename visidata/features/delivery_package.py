"""Export a VisiData workspace as a self-contained delivery package.

This module is a thin facade over the unified snapshot layer:

    vd.exportDeliveryPackage(...)  -->  vd.generate_snapshot(...)  -->  vd.write_snapshot(..., 'delivery', ...)

All actual package construction (manifest.json, data/, replay.vdj, README,
launcher scripts) lives in ``write_snapshot_delivery`` inside
``visidata/snapshot.py`` so nothing here maintains a parallel copy of the
state-collection or packaging logic.
"""

from visidata import vd, VisiData, BaseSheet, Path, asyncthread
from visidata.snapshot import _collect_derived_sheets


@VisiData.api
@asyncthread
def exportDeliveryPackage(vd, path, scope='current', data_format='vds'):
    """Export workspace as a delivery package.

    *path*
        Directory path (ends with ``/``) or ``.zip`` file.
    *scope*
        ``"current"``, ``"all"``, ``"selected"``, or an iterable of sheets.
    *data_format*
        Extension for the per-sheet data files (``vds``, ``tsv``, ``jsonl``, …).
    """
    if not isinstance(path, Path):
        path = Path(path)
    snap = vd.generate_snapshot(scope=scope, include_cmdlog=True, include_macros=True, include_data=False)

    data_sheets = {}
    for name in snap.get('order', []):
        vs = vd.getSheet(name)
        if vs is not None:
            data_sheets[name] = vs

    vd.write_snapshot(path, 'delivery', snap, data_format=data_format, data_sheets=data_sheets)


BaseSheet.addCommand('', 'export-delivery-package',
                     'vd.exportDeliveryPackage(inputPath("export delivery package to: ", value=name+".zip"), scope="current", data_format=input("data format [vds/tsv/jsonl/...]: ", value="vds"))',
                     'export current workspace as a self-contained delivery package')

vd.addGlobals({
    'exportDeliveryPackage': exportDeliveryPackage,
    '_collect_derived_sheets': _collect_derived_sheets,
})

vd.addMenuItems('''
    File > Export > delivery package > export-delivery-package
''')
