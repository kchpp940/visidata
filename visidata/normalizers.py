import json as _json

from visidata import vd, TypedWrapper, TypedExceptionWrapper


def to_python_value(val):
    '''Normalize raw values from loaders (PyArrow, Pandas, NumPy) into native Python types.

    Used for display and filtering — the resulting values are VisiData-native:
    None, bool, int, float, str, bytes, list, dict, datetime, or shapely geometry objects.

    - PyArrow Scalar/ChunkedArray → recursively converted to native Python
    - NumPy ndarray → list, NumPy generic → native scalar
    - Pandas Timestamp → datetime, Timedelta → int(seconds), NA → None
    - Nested list/tuple/dict → recursively normalized
    - Shapely geometry objects and bytes → passed through as-is (GeometryColumn handles WKB parsing)
    - TypedWrapper/TypedExceptionWrapper → passed through unchanged
    '''
    if val is None:
        return None

    if isinstance(val, (TypedWrapper, TypedExceptionWrapper)):
        return val

    try:
        import pyarrow as pa
        if isinstance(val, pa.Scalar):
            return _pyarrow_scalar_to_python(val)
        if isinstance(val, pa.ChunkedArray):
            return [_pyarrow_scalar_to_python(v) for v in val]
    except Exception:
        pass

    try:
        import numpy as np
        if isinstance(val, np.ndarray):
            return [to_python_value(v) for v in val.tolist()]
        if isinstance(val, np.generic):
            try:
                return to_python_value(val.item())
            except Exception:
                pass
    except Exception:
        pass

    try:
        import pandas as pd
        if isinstance(val, pd.Timestamp):
            return val.to_pydatetime()
        if isinstance(val, pd.Timedelta):
            return int(val.total_seconds())
        if pd.isna(val):
            return None
    except Exception:
        pass

    if isinstance(val, (list, tuple)):
        return [to_python_value(v) for v in val]

    if isinstance(val, dict):
        return {k: to_python_value(v) for k, v in val.items()}

    return val


def _pyarrow_scalar_to_python(val):
    'Convert a single PyArrow Scalar to native Python (internal helper).'
    pa = vd.importExternal('pyarrow')

    if not val.is_valid:
        return None

    t = val.type
    try:
        is_ext = isinstance(t, pa.ExtensionType)
    except Exception:
        is_ext = False

    if is_ext:
        try:
            storage = val.storage
            return to_python_value(storage)
        except Exception:
            try:
                return to_python_value(val.as_py())
            except Exception:
                return str(val)

    tid = t.id
    list_ids = (pa.lib.Type_LIST, pa.lib.Type_LARGE_LIST, pa.lib.Type_FIXED_SIZE_LIST,
                pa.lib.Type_LIST_VIEW, pa.lib.Type_LARGE_LIST_VIEW)
    if tid in list_ids:
        try:
            raw = val.as_py()
            return [to_python_value(v) for v in raw] if isinstance(raw, (list, tuple)) else raw
        except Exception:
            return str(val)

    if tid == pa.lib.Type_STRUCT:
        try:
            return {k: to_python_value(val[k]) for k in val.type.names}
        except Exception:
            try:
                return to_python_value(val.as_py())
            except Exception:
                return str(val)

    if tid == pa.lib.Type_MAP:
        result = {}
        try:
            items = val.as_py()
            for item in items:
                if isinstance(item, dict) and 'key' in item and 'value' in item:
                    result[to_python_value(item['key'])] = to_python_value(item['value'])
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    result[to_python_value(item[0])] = to_python_value(item[1])
        except Exception:
            pass
        return result

    if tid == pa.lib.Type_DICTIONARY:
        try:
            return to_python_value(val.as_py())
        except Exception:
            return str(val)

    if tid in (pa.lib.Type_SPARSE_UNION, pa.lib.Type_DENSE_UNION):
        try:
            return to_python_value(val.as_py())
        except Exception:
            return str(val)

    string_ids = (pa.lib.Type_STRING, pa.lib.Type_LARGE_STRING, pa.lib.Type_STRING_VIEW)
    if tid in string_ids:
        try:
            return val.as_py()
        except Exception:
            try:
                return memoryview(val.as_buffer())[:2**20].tobytes().decode('utf-8', errors='replace')
            except Exception:
                return str(val)

    binary_ids = (pa.lib.Type_BINARY, pa.lib.Type_LARGE_BINARY, pa.lib.Type_BINARY_VIEW, pa.lib.Type_FIXED_SIZE_BINARY)
    if tid in binary_ids:
        try:
            raw = val.as_py()
            if isinstance(raw, (bytes, bytearray, memoryview)):
                return bytes(raw)
            return raw
        except Exception:
            return str(val)

    try:
        return val.as_py()
    except Exception:
        return str(val)


def to_export_value(val, fmt=None):
    '''Convert a Python value (from to_python_value) to a format-appropriate export value.

    Formats:
    - 'csv'/'tsv'/'txt' (default):
        list/dict → JSON string, bytes → UTF-8 string,
        geometry (with __geo_interface__) → GeoJSON string,
        everything else → as-is (will be str()'d by text formatters if needed)
    - 'json'/'jsonl':
        list/dict → kept as list/dict (recursively normalized),
        bytes → UTF-8 string,
        geometry → GeoJSON dict (via __geo_interface__),
        dict keys coerced to str,
        TypedExceptionWrapper → str
    - 'arrow'/'parquet':
        list/dict → JSON string,
        bytes → preserved as bytes,
        geometry → GeoJSON string (via __geo_interface__)
    '''
    if val is None:
        return None

    if fmt is None:
        fmt = 'csv'

    fmt = fmt.lower()
    stringify_complex = fmt in ('csv', 'tsv', 'txt', 'arrow', 'parquet')
    json_fmt = fmt.startswith('json')

    if isinstance(val, TypedExceptionWrapper):
        if json_fmt:
            return str(val)
        return val

    if isinstance(val, TypedWrapper):
        return to_export_value(val.val, fmt)

    try:
        import numpy as np
        if isinstance(val, np.ndarray):
            if stringify_complex:
                val = to_python_value(val)
            else:
                val = [to_export_value(v, fmt) for v in val.tolist()]
        elif isinstance(val, np.generic):
            try:
                if stringify_complex:
                    val = to_python_value(val)
                else:
                    val = to_export_value(val.item(), fmt)
            except Exception:
                pass
    except Exception:
        pass

    try:
        import pandas as pd
        if isinstance(val, pd.Timestamp):
            if json_fmt:
                return val.isoformat()
            return val.to_pydatetime()
        if isinstance(val, pd.Timedelta):
            return int(val.total_seconds())
        if pd.isna(val):
            return None
    except Exception:
        pass

    if isinstance(val, (bytes, bytearray, memoryview)):
        if fmt in ('arrow', 'parquet'):
            try:
                return bytes(val)
            except Exception:
                return None
        try:
            return bytes(val).decode('utf-8', errors='replace')
        except Exception:
            if json_fmt:
                return str(val)
            return val

    if hasattr(val, '__geo_interface__'):
        try:
            geo = val.__geo_interface__
            if json_fmt:
                return _normalize_for_json(geo)
            return _json.dumps(geo, ensure_ascii=False, default=str)
        except Exception:
            return str(val)

    if isinstance(val, (list, tuple)):
        if stringify_complex:
            cleaned = to_python_value(list(val))
            try:
                return _json.dumps(cleaned, ensure_ascii=False, default=str)
            except Exception:
                return str(cleaned)
        return [to_export_value(v, fmt) for v in val]

    if isinstance(val, dict):
        if stringify_complex:
            cleaned = to_python_value(val)
            try:
                return _json.dumps(cleaned, ensure_ascii=False, default=str)
            except Exception:
                return str(cleaned)
        normalized = {k: to_export_value(v, fmt) for k, v in val.items()}
        if json_fmt:
            return {str(k): v for k, v in normalized.items()}
        return normalized

    return val


def _normalize_for_json(val):
    'Recursively normalize a value for JSON serialization (internal helper).'
    if val is None:
        return None
    if isinstance(val, (TypedExceptionWrapper, TypedWrapper)):
        return _normalize_for_json(val.val) if isinstance(val, TypedWrapper) else str(val)
    if isinstance(val, (bytes, bytearray, memoryview)):
        try:
            return bytes(val).decode('utf-8', errors='replace')
        except Exception:
            return str(val)
    if isinstance(val, (list, tuple)):
        return [_normalize_for_json(v) for v in val]
    if isinstance(val, dict):
        return {str(k): _normalize_for_json(v) for k, v in val.items()}
    try:
        import numpy as np
        if isinstance(val, np.ndarray):
            return [_normalize_for_json(v) for v in val.tolist()]
        if isinstance(val, np.generic):
            try:
                return _normalize_for_json(val.item())
            except Exception:
                return str(val)
    except Exception:
        pass
    try:
        import pandas as pd
        if isinstance(val, pd.Timestamp):
            return val.isoformat()
        if isinstance(val, pd.Timedelta):
            return int(val.total_seconds())
        if pd.isna(val):
            return None
    except Exception:
        pass
    if hasattr(val, '__geo_interface__'):
        try:
            return _normalize_for_json(val.__geo_interface__)
        except Exception:
            return str(val)
    return val


vd.addGlobals(to_python_value=to_python_value, to_export_value=to_export_value)
