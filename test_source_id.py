from visidata import vd, Path
from visidata.settings import SettingsMgr

print("=" * 70)
print("path.given vs str(path) vs SettingsMgr.objname(Path)")
print("=" * 70)

test_paths = [
    ('stdin dash', '-'),
    ('regular file', '/data/file.csv'),
    ('compressed gz', '/data/file.csv.gz'),
    ('http URL', 'http://example.com/data.csv'),
    ('https URL', 'https://example.com/data.csv.gz'),
    ('relative path', './data.csv'),
    ('parent path', '../data.csv'),
    ('path with space', '/tmp/my data.csv'),
]

for label, p in test_paths:
    path = Path(p)
    objname = vd._options.objname(path)
    print(f"\n[{label}]")
    print(f"  original input : {repr(p)}")
    print(f"  path.given     : {repr(path.given)}")
    print(f"  str(path)      : {repr(str(path))}")
    print(f"  objname(Path)  : {repr(objname)}")
    print(f"  given==str?    : {path.given == str(path)}")
    print(f"  given==objname?: {path.given == objname}")

# Also check what SettingsMgr.objname does with strings
print("\n" + "=" * 70)
print("SettingsMgr.objname for plain strings")
print("=" * 70)
test_strings = ['-', '/data/file.csv', 'http://example.com/data.csv', 'global', 'default']
for s in test_strings:
    objname = vd._options.objname(s)
    print(f"  objname({repr(s)}) = {repr(objname)}")

# Check how Path options get stored
print("\n" + "=" * 70)
print("Path.options store key (via objname) vs path.given")
print("=" * 70)

import tempfile
with tempfile.NamedTemporaryFile(suffix='.csv.gz', delete=False) as tf:
    testfile = tf.name

try:
    path = Path(testfile)
    print(f"\n[testfile: {testfile}]")
    print(f"  path.given     : {repr(path.given)}")
    print(f"  str(path)      : {repr(str(path))}")
    print(f"  objname(Path)  : {repr(vd._options.objname(path))}")
    
    # Set an option on this path
    from visidata import option
    path.options.set('encoding', 'latin-1')
    
    # Check where it was stored
    opt = vd._options._opts.get('encoding', {})
    for k, v in opt.items():
        if hasattr(v, 'value'):
            print(f"  stored option key: {repr(k)} = {repr(v.value)}")
    
    # Can we look it up with given?
    print(f"  lookup by str(path): {repr(path.options.getonly('encoding', path, None))}")
    print(f"  lookup by given str?: "
          f"{repr(vd.OptionsObject(vd._options, obj=path.given).getonly('encoding', path.given, None))}")
finally:
    import os
    os.unlink(testfile)
