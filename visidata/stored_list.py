import json

from visidata import vd, VisiData, Path, AttrDict


@VisiData.api
class StoredList(list):
    'Read existing persisted list from filesystem, and append new elements to .jsonl via runtime_paths'
    def __init__(self, *args, name:str='', **kwargs):
        super().__init__(*args, **kwargs)
        self.name = name

    @property
    def path(self):
        return vd.runtime_paths.get_file('data', self.name + '.jsonl', ensure_dir=True, writable=True)

    def reload(self):
        p = self.path
        if not p or not p.exists():
            return

        ret = []
        with p.open(encoding='utf-8-sig') as fp:
            for line in fp:
                value = vd.callNoExceptions(json.loads, line)
                if value is not None:
                    if isinstance(value, dict):
                        value = AttrDict(value)
                    ret.append(value)

        self[:] = ret   # replace without using .append

    def append(self, v):
        super().append(v)

        p = self.path
        if p is None:
            return

        if not vd.runtime_paths.ensure_dir(p.parent, writable=True):
            return

        with p.open(encoding='utf-8', mode='a') as fp:
            fp.write(json.dumps(v) + '\n')
