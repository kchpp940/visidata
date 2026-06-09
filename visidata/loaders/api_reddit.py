'''# RedditSheet

- [:keystrokes]Ctrl+O[/] to open a browser tab to [:code]{sheet.cursorRow.display_name_prefixed}[/]
- [:keystrokes]g Ctrl+O[/] to open browser windows for {sheet.nSelectedRows} selected subreddits

- [:keystrokes]Enter[/] to open sheet with top ~1000 submissions for [:code]{sheet.cursorRow.display_name_prefixed}[/]
- [:keystrokes]g Enter[/] to open sheet with top ~1000 submissions for {sheet.nSelectedRows} selected subreddits

- [:keystrokes]ga[/] to append more subreddits matching input by name or description
'''

import json
import visidata
from visidata import vd, VisiData, Sheet, AttrColumn, asyncthread, anytype, date, AttrDict


vd.option('reddit_client_id', '', 'client_id for reddit API')
vd.option('reddit_client_secret', '', 'client_secret for reddit API')
vd.option('reddit_user_agent', visidata.__version_info__, 'user_agent for reddit API')


def _strip_attr_prefix(attr):
    '''Strip type prefix chars (#, @, -) from an attr name.'''
    while attr and not attr[0].isalpha():
        attr = attr[1:]
    return attr


def _praw_attr_list(attrs_str):
    '''Parse a hidden_attrs string into a list of plain attribute names.'''
    return [_strip_attr_prefix(a) for a in attrs_str.split()]


def _praw_row_type_and_id(obj):
    '''Infer (row_type, id_value) tuple from a live PRAW object.

    row_type is one of: 'subreddit', 'submission', 'redditor', 'comment', 'unknown'.
    '''
    cls_name = type(obj).__name__.lower()
    if 'subreddit' in cls_name:
        return 'subreddit', getattr(obj, 'display_name', None) or getattr(obj, 'name', None)
    if 'submission' in cls_name or 'link' in cls_name:
        return 'submission', getattr(obj, 'id', None)
    if 'redditor' in cls_name:
        return 'redditor', getattr(obj, 'name', None)
    if 'comment' in cls_name:
        return 'comment', getattr(obj, 'id', None)
    return 'unknown', None


def _make_praw_builder(praw_info):
    '''Return a callable() -> PRAW object from a _praw_info dict.'''
    row_type = (praw_info or {}).get('type', 'unknown')
    if row_type == 'subreddit':
        name = praw_info.get('name')
        return lambda: vd.reddit.subreddit(name) if name else None
    if row_type == 'submission':
        sid = praw_info.get('id')
        return lambda: vd.reddit.submission(id=sid) if sid else None
    if row_type == 'redditor':
        name = praw_info.get('name')
        return lambda: vd.reddit.redditor(name) if name else None
    if row_type == 'comment':
        cid = praw_info.get('id')
        return lambda: vd.reddit.comment(id=cid) if cid else None
    return lambda: None


class CachedPRAWWrapper:
    '''Hybrid row object: fast column access from cached snapshot + lazy PRAW fallback for behavior.

    Supports:
      - attribute access via `__getattr__`: checks snapshot first, falls back to live PRAW object
      - attribute setting via `__setattr__`: writes to snapshot (e.g. for _comments_ref)
      - `__str__`, `__repr__` from snapshot or fallback
      - nested dict/list values are recursively wrapped
    '''

    __slots__ = ('_snapshot', '_praw_builder', '_praw_cache', '__dict__')

    def __init__(self, snapshot, praw_builder=None, praw_info=None):
        object.__setattr__(self, '_snapshot', dict(snapshot or {}))
        object.__setattr__(self, '_praw_builder', praw_builder or _make_praw_builder(praw_info))
        object.__setattr__(self, '_praw_cache', None)

    @classmethod
    def from_snapshot(cls, snapshot):
        '''Build wrapper from a cached snapshot dict (which may contain a `_praw_info` key).'''
        snap = dict(snapshot or {})
        praw_info = snap.pop('_praw_info', None)
        return cls(snap, praw_info=praw_info)

    def _live_praw(self):
        '''Return the live PRAW object (lazily built and cached).'''
        if self._praw_cache is None:
            object.__setattr__(self, '_praw_cache', self._praw_builder())
        return self._praw_cache

    def __getattr__(self, name):
        snap = object.__getattribute__(self, '_snapshot')
        if name in snap:
            val = snap[name]
            if isinstance(val, dict):
                return CachedPRAWWrapper.from_snapshot(val)
            if isinstance(val, list):
                return [CachedPRAWWrapper.from_snapshot(x) if isinstance(x, dict) else x for x in val]
            return val
        praw = object.__getattribute__(self, '_live_praw')()
        if praw is not None:
            return getattr(praw, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        snap = object.__getattribute__(self, '_snapshot')
        snap[name] = value

    def __delattr__(self, name):
        snap = object.__getattribute__(self, '_snapshot')
        snap.pop(name, None)

    def __hasattr__(self, name):
        snap = object.__getattribute__(self, '_snapshot')
        if name in snap:
            return True
        praw = object.__getattribute__(self, '_praw_cache')
        if praw is not None:
            return hasattr(praw, name)
        return False

    def __str__(self):
        snap = object.__getattribute__(self, '_snapshot')
        if 'display_name' in snap:
            return str(snap['display_name'])
        if 'name' in snap:
            return str(snap['name'])
        if 'id' in snap:
            return str(snap['id'])
        praw = object.__getattribute__(self, '_praw_cache')
        if praw is not None:
            return str(praw)
        return object.__repr__(self)

    def __repr__(self):
        return f'CachedPRAWWrapper({object.__getattribute__(self, "_snapshot")})'

    def __getstate__(self):
        return object.__getattribute__(self, '_snapshot')

    def __eq__(self, other):
        if isinstance(other, CachedPRAWWrapper):
            return object.__getattribute__(self, '_snapshot') == object.__getattribute__(other, '_snapshot')
        return NotImplemented

    def __hash__(self):
        snap = object.__getattribute__(self, '_snapshot')
        return hash(tuple(sorted((k, str(v)) for k, v in snap.items() if k.startswith('_') or not callable(v))))


def _fallback_attrs_for(v):
    '''Return a best-effort attrs_str for a PRAW-like object when no explicit attrs given.'''
    markers = ('display_name', 'display_name_prefixed', 'name', 'title', 'id', 'fullname',
               'author', 'body', 'selftext', 'url', 'score', 'ups', 'downs',
               'created', 'created_utc', 'num_comments', 'depth', 'edited',
               'over_18', 'subreddit_type', 'subscribers', 'description',
               'comment_karma', 'link_karma')
    return ' '.join(a for a in markers if hasattr(v, a))


def _val_to_cacheable(v, attrs_str=None):
    '''Recursively convert a value into JSON-serializable cacheable form.

    - PRAW objects → snapshot dict with _praw_info
    - list/tuple → list of cacheable values
    - dict → dict of cacheable values
    - primitives → unchanged
    - other → str()
    '''
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    if isinstance(v, dict):
        return {k: _val_to_cacheable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_val_to_cacheable(x) for x in v]
    # Detect PRAW-like objects (have id/display_name/name attributes and not JSON-serializable)
    has_praw_markers = any(hasattr(v, attr) for attr in ('display_name', 'name', 'id', 'fullname'))
    if has_praw_markers:
        row_type, rid = _praw_row_type_and_id(v)
        inner_attrs = attrs_str if attrs_str else _fallback_attrs_for(v)
        inner_snap = _praw_obj_to_snapshot(v, inner_attrs)
        inner_snap['_praw_info'] = {'type': row_type, 'id': rid,
                                     'name': getattr(v, 'display_name', None) or getattr(v, 'name', None)}
        return inner_snap
    try:
        return str(v)
    except Exception:
        return None


def _praw_obj_to_snapshot(obj, attrs_str=None):
    '''Extract snapshot dict from a live PRAW object (no _praw_info injected yet).'''
    d = {}
    attrs = _praw_attr_list(attrs_str) if attrs_str else None

    if attrs:
        for attr in attrs:
            try:
                v = getattr(obj, attr)
                d[attr] = _val_to_cacheable(v)
            except Exception:
                pass
    else:
        try:
            for k, v in vars(obj).items():
                if k.startswith('_'):
                    continue
                d[k] = _val_to_cacheable(v)
        except Exception:
            pass
    return d


def _praw_to_cacheable(obj, attrs_str=None):
    '''Convert a single PRAW object into its fully cacheable dict form (with _praw_info).'''
    row_type, rid = _praw_row_type_and_id(obj)
    snap = _praw_obj_to_snapshot(obj, attrs_str)
    snap['_praw_info'] = {
        'type': row_type,
        'id': rid,
        'name': getattr(obj, 'display_name', None) or getattr(obj, 'name', None),
    }
    return snap


def _reddit_source_params(operation, params):
    '''Return cache-key params for a Reddit API operation.'''
    return {
        'operation': operation,
        'params': params,
        'client_id': vd.options.reddit_client_id,
    }


def _reddit_fetch_rows(operation, params, fetch_fn, attrs_str=None,
                       status_online=None, error_msg=None):
    '''Build spec, call remote_open, return list of CachedPRAWWrapper rows.'''
    source_params = _reddit_source_params(operation, params)

    def _fetch():
        objs = list(fetch_fn())
        cacheable = [_praw_to_cacheable(o, attrs_str) for o in objs]
        return json.dumps(cacheable, ensure_ascii=False, default=str)

    spec = vd.make_remote_spec(
        'reddit', source_params, _fetch,
        days=0,
        parse_fn=lambda raw: [CachedPRAWWrapper.from_snapshot(d) for d in json.loads(raw)],
        status_online=status_online or f'fetching {operation} from reddit',
        status_offline=f'offline: using cached data for reddit `{operation}`',
        error_msg=error_msg or f'cannot fetch reddit `{operation}`',
    )
    return vd.remote_open(spec)


subreddit_hidden_attrs='''
name #accounts_active accounts_active_is_fuzzed advertiser_category
all_original_content allow_chat_post_creation allow_discovery
allow_galleries allow_images allow_polls allow_predictions
allow_predictions_tournament allow_videogifs allow_videos
banner_background_color banner_background_image banner_img banner_size
can_assign_link_flair can_assign_user_flair collapse_deleted_comments
comment_score_hide_mins community_icon community_reviewed @created
@created_utc description_html disable_contributor_requests
display_name display_name_prefixed emoji emojis_custom_size
emojis_enabled filters free_form_reports fullname has_menu_widget
header_img header_size header_title hide_ads icon_img icon_size
is_chat_post_feature_enabled is_crosspostable_subreddit
is_enrolled_in_new_modmail key_color lang link_flair_enabled
link_flair_position mobile_banner_image mod notification_level
original_content_tag_enabled over18 prediction_leaderboard_entry_type
primary_color public_description public_description_html public_traffic
quaran quarantine restrict_commenting restrict_posting show_media
show_media_preview spoilers_enabled submission_type submit_link_label
submit_text submit_text_html submit_text_label suggested_comment_sort
user_can_flair_in_sr user_flair_background_color user_flair_css_class
user_flair_enabled_in_sr user_flair_position user_flair_richtext
user_flair_template_id user_flair_text user_flair_text_color
user_flair_type user_has_favorited user_is_banned user_is_contributor
user_is_moderator user_is_muted user_is_subscriber user_sr_flair_enabled
user_sr_theme_enabled #videostream_links_count whitelist_status widgets
wiki wiki_enabled wls
'''

post_hidden_attrs='''
all_awardings allow_live_comments @approved_at_utc approved_by archived
author_flair_background_color author_flair_css_class author_flair_richtext
author_flair_template_id author_flair_text author_flair_text_color
author_flair_type author_fullname author_patreon_flair author_premium
awarders @banned_at_utc banned_by can_gild can_mod_post category clicked
comment_limit comment_sort content_categories contest_mode @created_utc
discussion_type distinguished domain edited flair fullname gilded
gildings hidden hide_score is_crosspostable is_meta is_original_content
is_reddit_media_domain is_robot_indexable is_self is_video likes
link_flair_background_color link_flair_css_class link_flair_richtext
link_flair_text link_flair_text_color link_flair_type locked media
media_embed media_only mod mod_note mod_reason_by mod_reason_title
mod_reports name no_follow num_crossposts num_duplicates num_reports
over_18 parent_whitelist_status permalink pinned pwls quarantine
removal_reason removed_by removed_by_category report_reasons saved
score secure_media secure_media_embed selftext_html send_replies
shortlink spoiler stickied subreddit_id subreddit_name_prefixed
subreddit_subscribers subreddit_type suggested_sort thumbnail
thumbnail_height thumbnail_width top_awarded_type total_awards_received
treatment_tags upvote_ratio user_reports #view_count visited
whitelist_status wls
'''

comment_hidden_attrs='''
all_awardings @approved_at_utc approved_by archived associated_award
author_flair_background_color author_flair_css_class author_flair_richtext
author_flair_template_id author_flair_text author_flair_text_color
author_flair_type author_fullname author_patreon_flair author_premium
awarders @banned_at_utc banned_by body_html can_gild can_mod_post
collapsed collapsed_because_crowd_control collapsed_reason comment_type
controversiality @created_utc distinguished fullname gilded gildings
is_root is_submitter likes link_id locked mod mod_note mod_reason_by
mod_reason_title mod_reports name no_follow num_reports parent_id
permalink removal_reason report_reasons saved #score #score_hidden
send_replies stickied submission subreddit_id subreddit_name_prefixed
subreddit_type top_awarded_type total_awards_received treatment_tags
user_reports
'''

redditor_hidden_attrs='''
#awardee_karma #awarder_karma @created @created_utc
fullname has_subscribed has_verified_email hide_from_robots icon_img id
is_employee is_friend is_gold is_mod pref_show_snoovatar
snoovatar_img snoovatar_size stream #total_karma verified
subreddit.banner_img subreddit.name subreddit.over_18 subreddit.public_description #subreddit.subscribers subreddit.title
'''

def hiddenCols(hidden_attrs):
    coltypes = { t.icon:t.typetype for t in vd.typemap.values() if not t.icon.isalpha() }
    for attr in hidden_attrs.split():
        coltype = anytype
        if attr[0] in coltypes:
            coltype = coltypes.get(attr[0])
            attr = attr[1:]
        yield AttrColumn(attr, type=coltype, width=0)


@VisiData.api
def open_reddit(vd, p):
    vd.importExternal('praw')

    if not vd.options.reddit_client_id:
        return RedditGuide('reddit_guide')

    if p.given.startswith('r/') or p.given.startswith('/r/'):
        return SubredditSheet(p.base_stem, source=p.base_stem.split('+'), search=(p.given[0]=='/'))

    if p.given.startswith('u/') or p.given.startswith('/u/'):
        return RedditorsSheet(p.base_stem, source=p.base_stem.split('+'), search=(p.given[0]=='/'))

    return SubredditSheet(p.base_stem, source=p)

vd.new_reddit = vd.open_reddit

@VisiData.cached_property
def reddit(vd):
    import praw
    return praw.Reddit(check_for_updates=False, **vd.options.getall('reddit_'))


class SubredditSheet(Sheet):
    guide = __doc__
    rowtype = 'subreddits'
    nKeys=1
    search=False
    columns = [
        AttrColumn('display_name_prefixed', width=15),
        AttrColumn('active_user_count', type=int),
        AttrColumn('subscribers', type=int),
        AttrColumn('subreddit_type'),
        AttrColumn('title'),
        AttrColumn('description', width=50),
        AttrColumn('url', width=10),
    ] + list(hiddenCols(subreddit_hidden_attrs))

    def iterload(self):
        all_attrs = 'display_name_prefixed active_user_count subscribers subreddit_type title description url ' + subreddit_hidden_attrs
        for name in self.source:
            name = name.strip()
            if self.search:
                results = _reddit_fetch_rows(
                    'subreddit_search', {'query': name},
                    lambda: vd.reddit.subreddits.search(name),
                    attrs_str=all_attrs,
                    status_online=f'searching subreddits matching `{name}`',
                    error_msg=f'cannot search subreddits matching `{name}`',
                )
                yield from results
            else:
                try:
                    results = _reddit_fetch_rows(
                        'subreddit_get', {'name': name},
                        lambda: [vd.reddit.subreddit(name)],
                        attrs_str=all_attrs,
                        status_online=f'loading subreddit `{name}`',
                        error_msg=f'cannot load subreddit `{name}`',
                    )
                    yield from results
                except Exception as e:
                    vd.exceptionCaught(e)

    def openRow(self, row):
        return RedditSubmissions(row.display_name_prefixed, source=_SubredditRef(row.display_name))

    def openRows(self, rows):
        comboname = '+'.join(row.display_name for row in rows)
        return RedditSubmissions(comboname, source=_SubredditRef(comboname))


class _SubredditRef:
    '''Serializable reference to a subreddit (replaces raw PRAW object for caching).'''
    def __init__(self, name):
        self.name = name
        self.display_name = name
        self.display_name_prefixed = 'r/' + name

    def new(self, limit=None):
        return vd.reddit.subreddit(self.name).new(limit=limit)

    def hot(self, limit=None):
        return vd.reddit.subreddit(self.name).hot(limit=limit)

    def top(self, limit=None):
        return vd.reddit.subreddit(self.name).top(limit=limit)

    def search(self, query, limit=None):
        return vd.reddit.subreddit(self.name).search(query, limit=limit)


class _RedditorRef:
    '''Serializable reference to a redditor.'''
    def __init__(self, name):
        self.name = name
        self.fullname = name

    @property
    def submissions(self):
        return vd.reddit.redditor(self.name).submissions

    @property
    def comments(self):
        return vd.reddit.redditor(self.name).comments


class RedditorsSheet(Sheet):
    rowtype = 'redditors'
    nKeys=1
    columns = [
        AttrColumn('name', width=15),
        AttrColumn('comment_karma', type=int),
        AttrColumn('link_karma', type=int),
        AttrColumn('comments'),
        AttrColumn('submissions'),
    ] + list(hiddenCols(redditor_hidden_attrs))

    def iterload(self):
        all_attrs = 'name comment_karma link_karma ' + redditor_hidden_attrs
        for name in self.source:
            if self.search:
                results = _reddit_fetch_rows(
                    'redditor_popular', {'query': name},
                    lambda: vd.reddit.redditors.popular(name),
                    attrs_str=all_attrs,
                    status_online=f'searching redditors matching `{name}`',
                    error_msg=f'cannot search redditors matching `{name}`',
                )
                yield from results
            else:
                results = _reddit_fetch_rows(
                    'redditor_get', {'name': name},
                    lambda: [vd.reddit.redditor(name)],
                    attrs_str=all_attrs,
                    status_online=f'loading redditor `{name}`',
                    error_msg=f'cannot load redditor `{name}`',
                )
                yield from results

    def openRow(self, row):
        return RedditSubmissions(row.fullname, source=_RedditorRef(row.name))

    def openRows(self, rows):
        comboname = '+'.join(row.name for row in rows)
        return RedditSubmissions(comboname, source=_RedditorRef(comboname).submissions)


class RedditSubmissions(Sheet):
    guide = '''# Reddit Submissions

  [:keys]Enter[/] to open sheet with comments for the current post
  [:keys]ga[/] to add posts in this subreddit matching input'''

    rowtype='reddit posts'
    nKeys=2
    columns = [
        AttrColumn('subreddit'),
        AttrColumn('id', width=0),
        AttrColumn('created', width=12, type=date),
        AttrColumn('author'),
        AttrColumn('ups', width=8, type=int),
        AttrColumn('downs', width=8, type=int),
        AttrColumn('num_comments', width=8, type=int),
        AttrColumn('title', width=50),
        AttrColumn('selftext', width=60),
        AttrColumn('url'),
        AttrColumn('comments', width=0),
    ] + list(hiddenCols(post_hidden_attrs))

    def _source_kind_and_params(self):
        src = self.source
        if isinstance(src, _SubredditRef):
            return 'subreddit_submissions', {'subreddit': src.name, 'kind': 'new'}
        if isinstance(src, _RedditorRef):
            return 'redditor_submissions', {'redditor': src.name}
        if hasattr(src, 'display_name'):
            return 'subreddit_submissions', {'subreddit': src.display_name, 'kind': 'new'}
        if hasattr(src, 'name'):
            return 'redditor_submissions', {'redditor': src.name}
        return 'unknown_submissions', {}

    def iterload(self):
        kind = 'new'
        operation, params = self._source_kind_and_params()
        all_attrs = 'subreddit id created author ups downs num_comments title selftext url comments ' + post_hidden_attrs

        f = getattr(self.source, kind, None)
        if f:
            results = _reddit_fetch_rows(
                operation, params,
                lambda: f(limit=10000),
                attrs_str=all_attrs,
                status_online=f'fetching submissions from reddit',
                error_msg='cannot fetch reddit submissions',
            )
            for row in results:
                try:
                    row._comments_ref = _SubmissionCommentsRef(row.id)
                except Exception:
                    pass
                yield row

    def openRow(self, row):
        return RedditComments(row.id, source=getattr(row, '_comments_ref', _SubmissionCommentsRef(row.id)))


class _SubmissionCommentsRef:
    '''Serializable reference to a submission's comments.'''
    def __init__(self, submission_id):
        self.submission_id = submission_id

    def list(self):
        return vd.reddit.submission(id=self.submission_id).comments.list()


class RedditComments(Sheet):
    rowtype='comments'
    nKeys=2
    columns=[
        AttrColumn('subreddit', width=0),
        AttrColumn('id', width=0),
        AttrColumn('ups', width=4, type=int),
        AttrColumn('downs', width=4, type=int),
        AttrColumn('replies', type=list),
        AttrColumn('created', type=date),
        AttrColumn('author'),
        AttrColumn('depth', type=int),
        AttrColumn('body', width=60),
        AttrColumn('edited', width=0),
    ] + list(hiddenCols(comment_hidden_attrs))

    def iterload(self):
        src = self.source
        if isinstance(src, _SubmissionCommentsRef):
            sid = src.submission_id
            operation, params = 'submission_comments', {'submission_id': sid}
        else:
            operation, params = 'comments_list', {}

        all_attrs = 'subreddit id ups downs replies created author depth body edited ' + comment_hidden_attrs

        def _list_comments():
            if isinstance(src, _SubmissionCommentsRef):
                return src.list()
            return list(src)

        results = _reddit_fetch_rows(
            operation, params,
            _list_comments,
            attrs_str=all_attrs,
            status_online=f'fetching comments from reddit',
            error_msg='cannot fetch reddit comments',
        )
        yield from results

    def openRow(self, row):
        return RedditComments(row.id, source=row.replies if hasattr(row, 'replies') else [])


class RedditGuide(RedditSubmissions):
    guide = '''# Authenticate Reddit
The Reddit API must be configured before use.

1. Login to Reddit and go to [:underline]https://www.reddit.com/prefs/apps[/].
2. Create a "script" app. (Use "[:underline]http://localhost:8000[/]" for the redirect uri)
3. Add credentials to visidatarc:

    options.reddit_client_id = '...'      # below the description in the upper left
    options.reddit_client_secret = '...'

## Use [:code]reddit[/] filetype for subreddits or users

Multiple may be specified, joined with "+".

    vd r/commandline.reddit
    vd u/gallowboob.reddit
    vd r/rust+golang+python.reddit
    vd u/spez+kn0thing.reddit
'''

@SubredditSheet.api
@asyncthread
def addRowsFromQuery(sheet, q):
    all_attrs = 'display_name_prefixed active_user_count subscribers subreddit_type title description url ' + subreddit_hidden_attrs
    results = _reddit_fetch_rows(
        'subreddit_search', {'query': q},
        lambda: vd.reddit.subreddits.search(q),
        attrs_str=all_attrs,
        status_online=f'searching subreddits matching `{q}`',
        error_msg=f'cannot search subreddits matching `{q}`',
    )
    for r in results:
        sheet.addRow(r, index=sheet.cursorRowIndex+1)


@RedditSubmissions.api
@asyncthread
def addRowsFromQuery(sheet, q):
    operation, params = sheet._source_kind_and_params()
    params['query'] = q
    all_attrs = 'subreddit id created author ups downs num_comments title selftext url comments ' + post_hidden_attrs

    results = _reddit_fetch_rows(
        operation + '_search', params,
        lambda: sheet.source.search(q, limit=None),
        attrs_str=all_attrs,
        status_online=f'searching submissions matching `{q}`',
        error_msg=f'cannot search submissions matching `{q}`',
    )
    for r in results:
        try:
            r._comments_ref = _SubmissionCommentsRef(r.id)
        except Exception:
            pass
        sheet.addRow(r, index=sheet.cursorRowIndex+1)


@VisiData.api
def sysopen_subreddits(vd, *subreddits):
    url = "https://www.reddit.com/r/"+"+".join(subreddits)
    vd.launchBrowser(url)


SubredditSheet.addCommand('Ctrl+O', 'sysopen-subreddit', 'sysopen_subreddits(cursorRow.display_name)', 'open browser window with subreddit')
SubredditSheet.addCommand('gCtrl+O', 'sysopen-subreddits', 'sysopen_subreddits(*(row.display_name for row in selectedRows))', 'open browser window with messages from selected subreddits')
SubredditSheet.addCommand('gEnter', 'open-subreddits', 'vd.push(openRows(selectedRows))', 'open sheet with top ~1000 submissions for each selected subreddit')
SubredditSheet.addCommand('ga', 'add-subreddits-match', 'addRowsFromQuery(input("add subreddits matching: "))', 'add subreddits matching input by name or description')
RedditSubmissions.addCommand('ga', 'add-submissions-match', 'addRowsFromQuery(input("add posts matching: "))', 'add posts in this subreddit matching input')

vd.addMenuItems('''
    File > Reddit > open selected subreddits > open-subreddits
    File > Reddit > add > matching subreddits > add-subreddits-match
    File > Reddit > add > matching submissions > add-submissions-match
    File > Reddit > open in browser > subreddit in current row > sysopen-subreddit
    File > Reddit > open in browser > selected subreddits > sysopen-subreddits
''')
