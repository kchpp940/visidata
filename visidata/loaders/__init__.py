from visidata import vd, IndexSheet

from visidata.loaders.registry import (
    LoaderDependency,
    LoaderCapability,
    LoaderRegistry,
)

vd.option('load_lazy', False, 'load subsheets always (False) or lazily (True)')
vd.option('skip', 0, 'skip N rows before header', replay=True)
vd.option('header', 1, 'parse first N rows as column names', replay=True)

IndexSheet.options.header = 0
IndexSheet.options.skip = 0

__all__ = ['LoaderDependency', 'LoaderCapability', 'LoaderRegistry']
