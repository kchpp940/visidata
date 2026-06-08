import json as _json

from visidata import vd, TypedWrapper, TypedExceptionWrapper


_GEOARROW_EXTENSIONS = (
    b'geoarrow.wkb',
    b'geoarrow.wkt',
    b'geoarrow.point',
    b'geoarrow.linestring',
    b'geoarrow.polygon',
    b'geoarrow.multipoint',
    b'geoarrow.multilinestring',
    b'geoarrow.multipolygon',
    b'geoarrow.geometrycollection',
)


def detect_geometry_columns(schema):
    '''Return set of column names that are geometries per GeoParquet metadata or GeoArrow extensions.'''
    names = set()
    meta = schema.metadata or {}
    geo = meta.get(b'geo')
    if geo:
        try:
            names.update(_json.loads(geo).get('columns', {}).keys())
        except Exception as e:
            vd.exceptionCaught(e)
    for i in range(len(schema)):
        field = schema.field(i)
        fmd = field.metadata or {}
        extname = fmd.get(b'ARROW:extension:name')
        if extname in _GEOARROW_EXTENSIONS:
            names.add(field.name)
    return names


def is_geometry_value(val):
    '''Return True if val looks like a geometry value (shapely obj, WKB bytes, GeoJSON dict, __geo_interface__).'''
    if val is None:
        return False
    if hasattr(val, '__geo_interface__'):
        return True
    if hasattr(val, 'geom_type'):
        return True
    if isinstance(val, dict) and 'type' in val and ('coordinates' in val or 'geometries' in val):
        return True
    return False


def to_geometry_value(val):
    '''Convert raw geometry storage value (WKB bytes, WKT str, GeoJSON dict) to display-ready value.

    If shapely is available:
        WKB bytes → shapely geometry object (empty geometry → None)
        WKT str   → shapely geometry object
        GeoJSON dict → shapely geometry object (via shape())
    If shapely is missing or parsing fails:
        WKB bytes → hex-encoded str
        WKT str   → original str
        GeoJSON dict → original dict
    Already a shapely geometry → passed through as-is (empty → None)
    '''
    if val is None:
        return None

    if hasattr(val, '__geo_interface__') or hasattr(val, 'geom_type'):
        try:
            import shapely
            if hasattr(val, 'is_empty') and val.is_empty:
                return None
        except Exception:
            pass
        return val

    if isinstance(val, (bytes, bytearray, memoryview)):
        raw = bytes(val)
        if len(raw) == 0:
            return None
        try:
            import shapely
            geom = shapely.from_wkb(raw)
            if geom.is_empty:
                return None
            return geom
        except Exception:
            try:
                return raw.hex()
            except Exception:
                return raw

    if isinstance(val, str):
        if val.strip() == '':
            return None
        try:
            import shapely
            geom = shapely.from_wkt(val)
            if geom.is_empty:
                return None
            return geom
        except Exception:
            return val

    if isinstance(val, dict) and 'type' in val and ('coordinates' in val or 'geometries' in val):
        try:
            import shapely
            geom = shapely.geometry.shape(val)
            if hasattr(geom, 'is_empty') and geom.is_empty:
                return None
            return geom
        except Exception:
            return val

    return val


def format_geometry_value(typedval, width=None):
    '''Format a geometry value for display.

    - shapely geometry → "PointType[123]" (geom_type with coordinate count)
    - WKB bytes → "WKB[42]"
    - hex str (from WKB fallback) → "WKB[21]" (decoded length)
    - WKT str / GeoJSON dict → truncated str
    - None → None
    '''
    if typedval is None:
        return None

    try:
        import shapely
        if hasattr(typedval, 'geom_type') and hasattr(typedval, 'is_empty'):
            try:
                n = shapely.get_num_coordinates(typedval)
                return f'{typedval.geom_type}[{n}]'
            except Exception:
                return str(typedval.geom_type)
    except Exception:
        pass

    if isinstance(typedval, (bytes, bytearray, memoryview)):
        try:
            return f'WKB[{len(bytes(typedval))}]'
        except Exception:
            pass

    if isinstance(typedval, str):
        if all(c in '0123456789abcdefABCDEF' for c in typedval) and len(typedval) % 2 == 0 and len(typedval) > 4:
            try:
                return f'WKB[{len(typedval) // 2}]'
            except Exception:
                pass
        if len(typedval) > 60:
            return typedval[:57] + '...'
        return typedval

    if isinstance(typedval, dict):
        try:
            t = typedval.get('type', '?')
            coords = typedval.get('coordinates')
            geoms = typedval.get('geometries')
            if coords or geoms:
                return f'{t}[GeoJSON]'
        except Exception:
            pass
        s = _json.dumps(typedval)
        if len(s) > 60:
            return s[:57] + '...'
        return s

    return str(typedval)


def _shapely_mapping(geom):
    'Convert a shapely geometry to a GeoJSON dict, handling shapely API differences.'
    try:
        import shapely.geometry
        return shapely.geometry.mapping(geom)
    except Exception:
        pass
    try:
        import shapely
        import json as _json
        return _json.loads(shapely.to_geojson(geom))
    except Exception:
        return None


def _geometry_to_geojson(val):
    '''Convert any geometry value (shapely, WKB bytes, hex, WKT str, GeoJSON dict) to a GeoJSON dict.

    Returns None on failure.
    '''
    if val is None:
        return None

    if hasattr(val, '__geo_interface__'):
        try:
            return _normalize_for_json(val.__geo_interface__)
        except Exception:
            pass

    if hasattr(val, 'geom_type'):
        geo = _shapely_mapping(val)
        if geo is not None:
            return _normalize_for_json(geo)

    if isinstance(val, (bytes, bytearray, memoryview)):
        try:
            import shapely
            geom = shapely.from_wkb(bytes(val))
            geo = _shapely_mapping(geom)
            if geo is not None:
                return _normalize_for_json(geo)
        except Exception:
            pass
        return None

    if isinstance(val, str):
        if all(c in '0123456789abcdefABCDEF' for c in val) and len(val) % 2 == 0 and len(val) > 4:
            try:
                import shapely
                raw = bytes.fromhex(val)
                geom = shapely.from_wkb(raw)
                geo = _shapely_mapping(geom)
                if geo is not None:
                    return _normalize_for_json(geo)
            except Exception:
                pass
            return None
        try:
            import shapely
            geom = shapely.from_wkt(val)
            geo = _shapely_mapping(geom)
            if geo is not None:
                return _normalize_for_json(geo)
        except Exception:
            pass
        return None

    if isinstance(val, dict) and 'type' in val and ('coordinates' in val or 'geometries' in val):
        if 'geometries' in val:
            normalized_geoms = []
            for g in val.get('geometries', []):
                ng = _geometry_to_geojson(g)
                if ng:
                    normalized_geoms.append(ng)
            return {'type': val.get('type', 'GeometryCollection'), 'geometries': normalized_geoms}
        return _normalize_for_json(val)

    return None


def _geoarrow_point_coord(coord_val):
    '''Convert a GeoArrow point coordinate (FixedSizeList or Struct x/y/[z]/[m]) to a Python list [x, y] or [x, y, z] or [x, y, z, m].'''
    if coord_val is None:
        return None

    try:
        raw = to_python_value(coord_val)
        if isinstance(raw, (list, tuple)):
            return [float(v) if v is not None else None for v in raw]
        if isinstance(raw, dict):
            coords = []
            for axis in ('x', 'y', 'z', 'm'):
                if axis in raw and raw[axis] is not None:
                    coords.append(float(raw[axis]))
                elif axis in ('x', 'y'):
                    coords.append(None)
            return coords
    except Exception:
        pass
    return None


def _geoarrow_storage_to_geojson(ext_name, storage_val):
    '''Convert a GeoArrow extension storage value (PyArrow Scalar) to a GeoJSON dict.

    ext_name is the extension name bytes, e.g. b'geoarrow.point'.
    '''
    if storage_val is None:
        return None

    ext_str = ext_name.decode('utf-8') if isinstance(ext_name, bytes) else str(ext_name)

    try:
        if ext_str == 'geoarrow.point':
            coord = _geoarrow_point_coord(storage_val)
            if coord and any(v is not None for v in coord):
                return {'type': 'Point', 'coordinates': coord}
            return None

        if ext_str == 'geoarrow.linestring':
            coords = []
            raw_points = to_python_value(storage_val)
            if isinstance(raw_points, (list, tuple)):
                for p in raw_points:
                    c = _geoarrow_point_coord(p)
                    if c:
                        coords.append(c)
            if coords:
                return {'type': 'LineString', 'coordinates': coords}
            return None

        if ext_str == 'geoarrow.polygon':
            rings = []
            raw_rings = to_python_value(storage_val)
            if isinstance(raw_rings, (list, tuple)):
                for raw_ring in raw_rings:
                    ring_coords = []
                    if isinstance(raw_ring, (list, tuple)):
                        for p in raw_ring:
                            c = _geoarrow_point_coord(p)
                            if c:
                                ring_coords.append(c)
                    if ring_coords:
                        rings.append(ring_coords)
            if rings:
                return {'type': 'Polygon', 'coordinates': rings}
            return None

        if ext_str == 'geoarrow.multipoint':
            coords = []
            raw_points = to_python_value(storage_val)
            if isinstance(raw_points, (list, tuple)):
                for p in raw_points:
                    c = _geoarrow_point_coord(p)
                    if c:
                        coords.append(c)
            if coords:
                return {'type': 'MultiPoint', 'coordinates': coords}
            return None

        if ext_str == 'geoarrow.multilinestring':
            lines = []
            raw_lines = to_python_value(storage_val)
            if isinstance(raw_lines, (list, tuple)):
                for raw_line in raw_lines:
                    line_coords = []
                    if isinstance(raw_line, (list, tuple)):
                        for p in raw_line:
                            c = _geoarrow_point_coord(p)
                            if c:
                                line_coords.append(c)
                    if line_coords:
                        lines.append(line_coords)
            if lines:
                return {'type': 'MultiLineString', 'coordinates': lines}
            return None

        if ext_str == 'geoarrow.multipolygon':
            polys = []
            raw_polys = to_python_value(storage_val)
            if isinstance(raw_polys, (list, tuple)):
                for raw_poly in raw_polys:
                    rings = []
                    if isinstance(raw_poly, (list, tuple)):
                        for raw_ring in raw_poly:
                            ring_coords = []
                            if isinstance(raw_ring, (list, tuple)):
                                for p in raw_ring:
                                    c = _geoarrow_point_coord(p)
                                    if c:
                                        ring_coords.append(c)
                            if ring_coords:
                                rings.append(ring_coords)
                    if rings:
                        polys.append(rings)
            if polys:
                return {'type': 'MultiPolygon', 'coordinates': polys}
            return None

        if ext_str == 'geoarrow.geometrycollection':
            geoms = []
            raw_union = to_python_value(storage_val)
            if isinstance(raw_union, (list, tuple)):
                for child in raw_union:
                    g = _geometry_to_geojson(child)
                    if g:
                        geoms.append(g)
            if geoms:
                return {'type': 'GeometryCollection', 'geometries': geoms}
            return None

        if ext_str in ('geoarrow.wkb', 'geoarrow.wkt'):
            return None

    except Exception:
        pass

    return None


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
            ext_name = None
            try:
                ext_name = t.extension_name
            except Exception:
                pass

            storage_val = None
            try:
                storage_val = val.value
            except Exception:
                try:
                    storage_val = val.cast(t.storage_type)
                except Exception:
                    pass

            if ext_name and isinstance(ext_name, (bytes, str)):
                ext_bytes = ext_name.encode('utf-8') if isinstance(ext_name, str) else ext_name
                if ext_bytes in _GEOARROW_EXTENSIONS:
                    if storage_val is not None:
                        geojson = _geoarrow_storage_to_geojson(ext_bytes, storage_val)
                        if geojson is not None:
                            return geojson
                        if ext_bytes in (b'geoarrow.wkb', b'geoarrow.wkt'):
                            return to_python_value(storage_val)

            if storage_val is not None:
                return to_python_value(storage_val)

            try:
                return to_python_value(val.as_py())
            except Exception:
                return str(val)
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


def to_export_value(val, fmt=None, as_geometry=False):
    '''Convert a Python value (from to_python_value / to_geometry_value) to a format-appropriate export value.

    Formats:
    - 'csv'/'tsv'/'txt' (default):
        list/dict → JSON string, bytes → UTF-8 string,
        geometry → GeoJSON string,
        everything else → as-is (will be str()'d by text formatters if needed)
    - 'json'/'jsonl':
        list/dict → kept as list/dict (recursively normalized),
        bytes → UTF-8 string,
        geometry → GeoJSON dict,
        dict keys coerced to str,
        TypedExceptionWrapper → str
    - 'arrow'/'parquet':
        list/dict → JSON string,
        bytes → preserved as bytes,
        geometry → WKB bytes (if shapely available) or GeoJSON string

    as_geometry: when True, treat val as a geometry value (WKB bytes, hex str, WKT str,
    shapely obj, GeoJSON dict) and export it with geometry semantics even if it doesn't
    have __geo_interface__.
    '''
    if val is None:
        return None

    if fmt is None:
        fmt = 'csv'

    fmt = fmt.lower()
    stringify_complex = fmt in ('csv', 'tsv', 'txt', 'arrow', 'parquet')
    json_fmt = fmt.startswith('json')

    if as_geometry or is_geometry_value(val):
        if fmt in ('arrow', 'parquet'):
            try:
                import shapely
                if hasattr(val, 'geom_type') or hasattr(val, '__geo_interface__'):
                    return shapely.to_wkb(val)
                if isinstance(val, (bytes, bytearray, memoryview)):
                    return bytes(val)
                if isinstance(val, str):
                    if all(c in '0123456789abcdefABCDEF' for c in val) and len(val) % 2 == 0 and len(val) > 4:
                        return bytes.fromhex(val)
                    geom = shapely.from_wkt(val)
                    return shapely.to_wkb(geom)
                if isinstance(val, dict) and 'type' in val and ('coordinates' in val or 'geometries' in val):
                    geom = shapely.geometry.shape(val)
                    return shapely.to_wkb(geom)
            except Exception:
                pass
        geojson = _geometry_to_geojson(val)
        if geojson is not None:
            if json_fmt:
                return geojson
            try:
                return _json.dumps(geojson, ensure_ascii=False, default=str)
            except Exception:
                return str(geojson)
        if isinstance(val, (bytes, bytearray, memoryview)):
            try:
                return bytes(val).hex()
            except Exception:
                return str(val)
        if isinstance(val, str):
            return val
        if isinstance(val, dict):
            try:
                return _json.dumps(val, ensure_ascii=False, default=str) if not json_fmt else _normalize_for_json(val)
            except Exception:
                return str(val)

    if isinstance(val, TypedExceptionWrapper):
        if json_fmt:
            return str(val)
        return val

    if isinstance(val, TypedWrapper):
        return to_export_value(val.val, fmt, as_geometry=as_geometry)

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


vd.addGlobals(
    to_python_value=to_python_value,
    to_export_value=to_export_value,
    to_geometry_value=to_geometry_value,
    format_geometry_value=format_geometry_value,
    detect_geometry_columns=detect_geometry_columns,
    is_geometry_value=is_geometry_value,
)
