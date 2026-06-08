from collections import defaultdict

from visidata import Sheet, VisiData, TypedWrapper, anytype, date, vlen, Column, vd


def pyarrow_to_python(val):
    'Recursively convert pyarrow scalar/array values to native Python types.'
    pa = vd.importExternal('pyarrow')

    if val is None:
        return None

    if isinstance(val, pa.Scalar):
        if val.is_valid:
            t = val.type
            tid = t.id
            try:
                is_ext = isinstance(t, pa.ExtensionType)
            except Exception:
                is_ext = False

            if is_ext:
                try:
                    storage = val.storage
                    return pyarrow_to_python(storage)
                except Exception:
                    try:
                        return val.as_py()
                    except Exception:
                        return str(val)
            elif tid in (pa.lib.Type_LIST, pa.lib.Type_LARGE_LIST, pa.lib.Type_FIXED_SIZE_LIST, pa.lib.Type_LIST_VIEW, pa.lib.Type_LARGE_LIST_VIEW):
                try:
                    raw = val.as_py()
                    return [pyarrow_to_python(v) for v in raw] if isinstance(raw, (list, tuple)) else raw
                except Exception:
                    return str(val)
            elif tid == pa.lib.Type_STRUCT:
                try:
                    return {k: pyarrow_to_python(val[k]) for k in val.type.names}
                except Exception:
                    try:
                        return val.as_py()
                    except Exception:
                        return str(val)
            elif tid == pa.lib.Type_MAP:
                result = {}
                try:
                    items = val.as_py()
                    for item in items:
                        if isinstance(item, dict) and 'key' in item and 'value' in item:
                            result[pyarrow_to_python(item['key'])] = pyarrow_to_python(item['value'])
                        elif isinstance(item, (list, tuple)) and len(item) >= 2:
                            result[pyarrow_to_python(item[0])] = pyarrow_to_python(item[1])
                except Exception:
                    pass
                return result
            elif tid == pa.lib.Type_DICTIONARY:
                try:
                    return pyarrow_to_python(val.as_py())
                except Exception:
                    return str(val)
            elif tid in (pa.lib.Type_SPARSE_UNION, pa.lib.Type_DENSE_UNION):
                try:
                    return pyarrow_to_python(val.as_py())
                except Exception:
                    return str(val)
            elif tid in (pa.lib.Type_LARGE_STRING, pa.lib.Type_STRING, pa.lib.Type_STRING_VIEW):
                try:
                    return val.as_py()
                except Exception:
                    try:
                        return memoryview(val.as_buffer())[:2**20].tobytes().decode('utf-8', errors='replace')
                    except Exception:
                        return str(val)
            elif tid in (pa.lib.Type_BINARY, pa.lib.Type_LARGE_BINARY, pa.lib.Type_BINARY_VIEW, pa.lib.Type_FIXED_SIZE_BINARY):
                try:
                    raw = val.as_py()
                    if isinstance(raw, (bytes, bytearray, memoryview)):
                        return bytes(raw)
                    return raw
                except Exception:
                    return str(val)
            else:
                try:
                    return val.as_py()
                except Exception:
                    return str(val)
        else:
            return None

    if isinstance(val, pa.ChunkedArray):
        return [pyarrow_to_python(v) for v in val]

    if isinstance(val, (list, tuple)):
        return [pyarrow_to_python(v) for v in val]

    if isinstance(val, dict):
        return {k: pyarrow_to_python(v) for k, v in val.items()}

    return val


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
                                  getter=lambda c,r: pyarrow_to_python(c.sheet.coldata[c.expr][r[0]])))

        for rownum in range(max(len(c) for c in self.coldata)):
            yield [rownum]


def _python_to_pyarrow_val(val):
    'Convert complex Python values to types pyarrow can serialize.'
    import json as _json
    if val is None:
        return None
    if isinstance(val, (dict, list, tuple)):
        try:
            return _json.dumps(val, ensure_ascii=False, default=str)
        except Exception:
            return str(val)
    return val


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

    databycol = defaultdict(list)   # col -> [values]

    for typedvals in sheet.iterdispvals(format=False):
        for col, val in typedvals.items():
            if isinstance(val, TypedWrapper):
                val = None

            databycol[col].append(_python_to_pyarrow_val(val))

    data = []
    for col, vals in databycol.items():
        pa_type = typemap.get(col.type, pa.string())
        try:
            data.append(pa.array(vals, type=pa_type))
        except Exception:
            try:
                data.append(pa.array([_python_to_pyarrow_val(v) for v in vals], type=pa.string()))
            except Exception:
                data.append(pa.array([str(v) if v is not None else None for v in vals], type=pa.string()))

    schema_fields = []
    for c in sheet.visibleCols:
        try:
            schema_fields.append((c.name, typemap.get(c.type, pa.string())))
        except Exception:
            schema_fields.append((c.name, pa.string()))

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
