

from visidata import vd, VisiData, Sheet, IndexSheet, SequenceSheet, ColumnItem, Path, AttrDict, ColumnAttr, asyncthread


def _google_creds_fn():

    filename = 'google_creds.json'
    google_creds_path = vd.pkg_resources_files('vdplus.api.google') / filename
    import os
    if not os.path.exists(google_creds_path):
        vd.error(f'{filename} file does not exist in {google_creds_path.parent}\n'
                 'Create it by following this guide: https://github.com/saulpw/visidata/blob/develop/docs/gmail.md')
    else:
        return str(google_creds_path)


@VisiData.api
def google_auth(vd, scopes=None):
    import pickle
    import os.path
    import urllib.parse

    SCOPES = []
    for scope in scopes.split():
        if not scope.startswith('https://'):
            scope = 'https://www.googleapis.com/auth/' + scope
        SCOPES.append(scope)

    GOOGLE_TOKEN_FILE = vd.runtime_paths.get_file(
        'data',
        f'google-{urllib.parse.quote_plus(str(scopes))}.pickle',
        ensure_dir=True,
        writable=True
    )
    creds = None
    if GOOGLE_TOKEN_FILE and os.path.exists(str(GOOGLE_TOKEN_FILE)):
        with open(str(GOOGLE_TOKEN_FILE), 'rb') as fp:
            creds = pickle.load(fp)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        else:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(_google_creds_fn(), SCOPES)
            creds = flow.run_local_server(port=0)

        if GOOGLE_TOKEN_FILE:
            with open(str(GOOGLE_TOKEN_FILE), 'wb') as fp:
                pickle.dump(creds, fp)

    return creds
