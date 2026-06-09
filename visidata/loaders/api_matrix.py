'''
# Matrix Chat Sheet
A list of messages from [:underline]{sheet.sourcename}[/].

- `Enter` to push a sheet with all the messages from [:underline]{sheet.cursorRow.room.display_name}[/].
- `a` to send a message to [:underline]{sheet.cursorRow.room.display_name}[/].
'''

import json
import hashlib

from visidata import vd, VisiData, Sheet, Column, ItemColumn, date, asyncthread, AttrDict, vlen, Path


vd.option('matrix_token', '', 'matrix API token')
vd.option('matrix_user_id', '', 'matrix user ID associated with token')
vd.option('matrix_device_id', 'VisiData', 'device ID associated with matrix login')

vd.matrix_client = None


def _matrix_source_params(homeserver, operation, params=None):
    '''Return cache-key params for a Matrix API call.'''
    return {
        'homeserver': homeserver,
        'operation': operation,
        'params': params or {},
        'token_hash': hashlib.sha256((vd.options.matrix_token or '').encode()).hexdigest()[:16],
    }


@VisiData.api
def openhttp_matrix(vd, p):
    vd.importExternal('matrix_client')

    if not vd.options.matrix_token:
        from matrix_client.client import MatrixClient

        username = vd.input(f'{p.given} username: ', record=False)
        password = vd.input('password: ', record=False, display=False)

        vd.matrix_client = MatrixClient(p.given)
        matrix_token = vd.matrix_client.login(username, password, device_id=vd.options.matrix_device_id)

        vd.setPersistentOptions(matrix_user_id=username, matrix_token=matrix_token)

    vd.timeouts_before_idle = -1
    return MatrixSheet(p.base_stem, source=p)

VisiData.open_matrix = VisiData.openhttp_matrix


class MatrixRoomsSheet(Sheet):
    def iterload(self):
        yield from vd.matrix_client.get_rooms().values()


class MatrixSheet(Sheet):
    help = __doc__
    columns = [
        Column('room', width=12, getter=lambda c,r: r.room and r.room.display_name),
        ItemColumn('sender', width=0),
        Column('sender', width=10, getter=lambda c,r: r.sender.split(':')[0][1:]),
        Column('timestamp', width=16, type=date, getter=lambda c,r: r and r.origin_server_ts/1000, fmtstr='%Y-%m-%d %H:%m'),
        ItemColumn('type', width=15),
        ItemColumn('content', width=0),
        ItemColumn('content.body', width=50),
        ItemColumn('received', type=vlen, width=0),
        ItemColumn('content.msgtype'),
    ]

    @property
    def sourcename(self):
        from matrix_client.room import Room
        if isinstance(self.source, Room):
            return self.source.display_name or self.source.room_id
        else:
            return str(self.source)

    @asyncthread
    def reload(self):
        from matrix_client.client import MatrixClient
        from matrix_client.room import Room
        if not vd.matrix_client:
            vd.matrix_client = MatrixClient(self.source.given,
                                        token=self.options.matrix_token,
                                        user_id=self.options.matrix_user_id)

        if isinstance(self.source, Room):
            self.add_room(self.source)
            self.get_room_messages(self.source)
            return

        for room in vd.matrix_client.get_rooms().values():
            self.add_room(room)
            room.backfill_previous_messages(limit=1)

        vd.matrix_client.add_listener(self.global_event)
        vd.matrix_client.add_ephemeral_listener(self.global_event)

        vd.matrix_client.start_listener_thread(exception_handler=vd.exceptionCaught)

        vd.matrix_client._sync()

        vd.matrix_client.listen_for_events()

    def add_room(self, room):
        room.add_listener(self.room_event)
        room.add_ephemeral_listener(self.room_event)
        room.add_state_listener(self.global_event)

    @asyncthread
    def get_room_messages(self, room):
            homeserver = getattr(vd.matrix_client, 'homeserver', self.source.given)
            while room.prev_batch:
                source_params = _matrix_source_params(
                    homeserver, 'get_room_messages',
                    {'room_id': room.room_id, 'prev_batch': room.prev_batch}
                )

                def _fetch(room=room):
                    ret = vd.matrix_client.api.get_room_messages(room.room_id, room.prev_batch, direction='b', limit=100)
                    return json.dumps(ret, ensure_ascii=False, default=str)

                spec = vd.make_remote_spec(
                    'matrix', source_params, _fetch,
                    days=0,
                    parse_fn=json.loads,
                    status_online=f'fetching messages from {room.display_name or room.room_id}',
                    error_msg=f'cannot fetch matrix messages from `{room.display_name or room.room_id}`',
                )
                ret = vd.remote_open(spec)

                for r in ret['chunk']:
                    r['room'] = room
                    self.addRow(r)

                if 'end' not in ret or ret['end'] == room.prev_batch:
                    break
                room.prev_batch = ret['end']

    def addRow(self, r, **kwargs):
        r = AttrDict(r)
        if r.event_id not in self.event_index:
            super().addRow(r, **kwargs)
            self.event_index[r.event_id] = r

    def global_event(self, chunk):
        self.addRow(chunk)

    def room_event(self, room, chunk):
        ev = AttrDict(chunk)
        t = chunk['type']
        if t == 'm.receipt':
            for msgid, content in chunk['content'].items():
                msg = self.event_index.setdefault(msgid, {})
                if 'received' not in msg:
                    msg['received'] = {}

                for t, c in content.items():
                    assert t == 'm.read'
                    for userid, v in c.items():
                        msg['received'][(t, userid)] = v['ts']/1000
            return

        chunk['room'] = room
        self.addRow(chunk)

    def add_message(self, text):
        vd.matrix_client.send_text(text)

    def openRow(self, row):
        return MatrixSheet(row.room.display_name, source=row.room, last_id=row.event_id)


MatrixSheet.init('event_index', dict)

MatrixSheet.addCommand('a', 'add-row', 'cursorRow.room.send_text(input(cursorRow.room.display_name+"> "))', 'send chat message to current room')
vd.addMenuItems('Row > Add > send matrix message > add-msg')
