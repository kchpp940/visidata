'''Backwards-compatible StoredList, now backed by visidata.session_state.StateStore.

Prefer using ``StateStore`` directly in new code.  ``StoredList`` is kept
here so existing imports like ``from visidata.stored_list import StoredList``
and ``vd.StoredList`` continue to work unchanged.
'''

from visidata import VisiData, vd
from visidata.session_state import StateStore, StatePhase


class StoredList(StateStore):
    '''Read existing persisted list from filesystem, and append new elements to .jsonl in .visidata.

    Drop-in compatible with the pre-3.5 StoredList: list subclass API,
    ``.reload()``, ``.append()``, and ``.path`` all work as before.
    '''

    def __init__(self, *args, name: str = '', phase=StatePhase.LAYOUT, **kwargs):
        super().__init__(name=name, **kwargs)
        self.phase = phase

    @property
    def path(self):
        return super().path

    def append(self, v):
        self.add(v)
        self.save()


VisiData.StoredList = StoredList
vd.addGlobals({'StoredList': StoredList})
