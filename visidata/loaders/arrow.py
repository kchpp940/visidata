from collections import defaultdict

from visidata import Sheet, VisiData, TypedWrapper, anytype, date, vlen, Column, vd
from visidata.normalizers import (
    to_python_value, to_export_value, geometry_values_all_bytes, is_geometry_value,
)


@VisiData.api
def open_arrow(vd, p):
    'Apache Arrow IPC file format'
    return ArrowSheet(p.base_stem, source=p)


@VisiData.api
def open_arrows(vd, p):
    'Apache Arrow IPC streaming format'
    return ArrowSheet(p.base_stem, source=p)


def arrow_to_vdtype(t):
    pa = vd.importExternal('pyarrow')

    try:
        if isinstance(t, pa.ExtensionType):
            return anytype
    except Exception:
        pass

    arrow_to_vd_typemap = {
        pa.lib.Type_BOOL: bool,
        pa.lib.Type_UINT8: int,
        pa.lib.Type_UINT16: int,
        pa.lib.Type_UINT32: int,
        pa.lib.Type_UINT64: int,
        pa.lib.Type_INT8: int,
        pa.lib.Type_INT16: int,
        pa.lib.Type_INT32: int,
        pa.lib.Type_INT64: int,
        pa.lib.Type_HALF_FLOAT: float,
        pa.lib.Type_FLOAT: float,
        pa.lib.Type_DOUBLE: float,
        pa.lib.Type_DATE32: date,
        pa.lib.Type_DATE64: date,
        pa.lib.Type_TIME32: date,
        pa.lib.Type_TIME64: date,
        pa.lib.Type_TIMESTAMP: date,
        pa.lib.Type_DURATION: int,
        pa.lib.Type_BINARY: bytes,
        pa.lib.Type_LARGE_BINARY: vlen,
        pa.lib.Type_BINARY_VIEW: bytes,
        pa.lib.Type_STRING: str,
        pa.lib.Type_LARGE_STRING: str,
        pa.lib.Type_STRING_VIEW: str,
        pa.lib.Type_LIST: list,
        pa.lib.Type_LARGE_LIST: list,
        pa.lib.Type_FIXED_SIZE_LIST: list,
        pa.lib.Type_LIST_VIEW: list,
        pa.lib.Type_LARGE_LIST_VIEW: list,
        pa.lib.Type_STRUCT: dict,
        pa.lib.Type_MAP: dict,
        pa.lib.Type_DICTIONARY: anytype,
        pa.lib.Type_RUN_END_ENCODED: anytype,
    }
    return arrow_to_vd_typemap.get(t.id, anytype)

class ArrowSheet(Sheet):
    def iterload(self):
        pa = vd.importExternal('pyarrow')

        try:
            with pa.OSFile(str(self.source), 'rb') as fp:
                self.coldata = pa.ipc.open_file(fp).read_all()
        except pa.lib.ArrowInvalid as e:
            with pa.OSFile(str(self.source), 'rb') as fp:
                self.coldata = pa.ipc.open_stream(fp).read_all()

        self.columns = []
        for colnum, col in enumerate(self.coldata):
            coltype = arrow_to_vdtype(self.coldata.schema.types[colnum])
            colname = self.coldata.schema.names[colnum]

            self.addColumn(Column(colname, type=coltype, expr=colnum,
                                  getter=lambda c,r: to_python_value(c.sheet.coldata[c.expr][r[0]])))

        for rownum in range(max(len(c) for c in self.coldata)):
            yield [rownum]


@VisiData.api
def save_arrow(vd, p, sheet, streaming=False):
    pa = vd.importExternal('pyarrow')
    np = vd.importExternal('numpy')

    typemap = {
        anytype: pa.string(),
        int: pa.int64(),
        vlen: pa.int64(),
        float: pa.float64(),
        str: pa.string(),
        date: pa.date64(),
        list: pa.string(),
        dict: pa.string(),
    }

    for t in vd.numericTypes:
        if t not in typemap:
            typemap[t] = pa.float64()

    geom_cols = set()
    for c in sheet.visibleCols:
        if getattr(c, 'is_geometry', False):
            geom_cols.add(c)
            continue
        sample = []
        for i, row in enumerate(sheet.rows):
            if i >= 10:
                break
            try:
                val = c.getValue(row)
                if val is not None:
                    sample.append(val)
            except Exception:
                pass
        if sample and any(is_geometry_value(v) for v in sample):
            geom_cols.add(c)

    databycol = defaultdict(list)   # col -> [values]

    for typedvals in sheet.iterdispvals(format=False):
        for col, val in typedvals.items():
            if isinstance(val, TypedWrapper):
                val = None

            as_geom = col in geom_cols
            databycol[col].append(to_export_value(val, fmt='arrow', as_geometry=as_geom))

    col_pa_types = {}
    for col in sheet.visibleCols:
        vals = databycol.get(col, [])
        if col in geom_cols:
            if geometry_values_all_bytes(vals):
                col_pa_types[col] = pa.binary()
            else:
                col_pa_types[col] = pa.string()
        else:
            col_pa_types[col] = typemap.get(col.type, pa.string())

    data = []
    for col in sheet.visibleCols:
        vals = databycol.get(col, [])
        pa_type = col_pa_types[col]
        as_geom = col in geom_cols
        try:
            data.append(pa.array(vals, type=pa_type))
        except Exception:
            fallback_type = pa.string()
            try:
                data.append(pa.array([to_export_value(v, fmt='arrow', as_geometry=as_geom) for v in vals], type=fallback_type))
            except Exception:
                data.append(pa.array([str(v) if v is not None else None for v in vals], type=fallback_type))
            col_pa_types[col] = fallback_type

    schema_fields = []
    for c in sheet.visibleCols:
        schema_fields.append((c.name, col_pa_types.get(c, pa.string())))

    schema = pa.schema(schema_fields)
    with p.open_bytes(mode='w') as outf:
        if streaming:
            with pa.ipc.new_stream(outf, schema) as writer:
                writer.write_batch(pa.record_batch(data, names=[c.name for c in sheet.visibleCols]))
        else:
            with pa.ipc.new_file(outf, schema) as writer:
                writer.write_batch(pa.record_batch(data, names=[c.name for c in sheet.visibleCols]))


@VisiData.api
def save_arrows(vd, p, sheet):
    return vd.save_arrow(p, sheet, streaming=True)
