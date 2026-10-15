"""Read the shared playlist metadata and EPG without downloading or changing it."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3

HALF_HOUR = 1800
WINDOW = 4 * HALF_HOUR
ROWS = 6
FILTERS = [('all', 'All channels'), ('favorites', 'Favorites'), ('local', 'Network TV'),
           ('news', 'News'), ('movies', 'Movies'), ('sports', 'Sports')]


def clean(value):
    # Keep international text, but omit pictographs unsupported by the TV font.
    return ''.join(c for c in str(value) if c.isprintable()
                   and not 0x1F000 <= ord(c) <= 0x1FFFF
                   and ord(c) not in (0xFE0F, 0xFE0E, 0x200D)).strip()


def clock(stamp):
    return datetime.fromtimestamp(stamp).strftime('%I:%M %p').lstrip('0')


def metadata(playlist):
    from channelbox import parse_extinf
    result = {}
    try:
        pending = None
        for line in Path(playlist).read_text(encoding='utf-8-sig').splitlines():
            if line.startswith('#EXTINF:'):
                pending = parse_extinf(line)[0]
            elif line.strip() and not line.startswith('#') and pending is not None:
                number = (pending.get('tvg-chno') or pending.get('channel-number')
                          or pending.get('ch-number') or str(len(result) + 1))
                result[number] = {'epg_id': pending.get('tvg-id', ''),
                                  'group': clean(pending.get('group-title', 'Other channels'))}
                pending = None
    except (OSError, UnicodeError):
        pass
    return result


def matches(kind, channel, info, favorite):
    group = info.get('group', '').casefold()
    if kind == 'all':
        return True
    if kind == 'favorites':
        return favorite
    if kind == 'local':
        return any(word in group for word in ('local', 'affiliate', 'arkansas', 'memphis'))
    words = {'news': ('news', 'weather'), 'movies': ('movie', 'cinema'),
             'sports': ('sport', 'espn', 'nfl', 'nba', 'mlb', 'nhl', 'flo ', 'ncaa')}
    return any(word in group for word in words.get(kind, ()))


def read_schedule(database, ids, start, stop):
    """Bounded read-only query. Missing feeds, gaps and old schemas remain usable."""
    ids = list(dict.fromkeys(identity for identity in ids if identity))[:ROWS]
    if not ids or not Path(database).is_file():
        return {}
    result = {}
    try:
        uri = Path(database).resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info(programmes)')}
            description = 'description' if 'description' in columns else "''"
            marks = ','.join('?' for _ in ids)
            rows = db.execute(f'SELECT channel,start,stop,title,subtitle,{description} '
                              f'FROM programmes WHERE channel IN ({marks}) '
                              'AND start<? AND stop>? ORDER BY channel,start,stop LIMIT 500',
                              [*ids, stop, start])
            for identity, begin, end, title, subtitle, desc in rows:
                if end <= begin:
                    continue
                result.setdefault(identity, []).append({'start': begin, 'stop': end,
                    'title': clean(title), 'subtitle': clean(subtitle or ''),
                    'description': clean(desc or '')})
    except (OSError, sqlite3.Error):
        pass
    return result


def segments(programmes, start, stop):
    """Fill uncovered airtime explicitly; never invent a programme for a gap."""
    result, cursor = [], start
    for programme in sorted(programmes, key=lambda p: (p['start'], p['stop'])):
        begin, end = max(cursor, programme['start']), min(stop, programme['stop'])
        if end <= begin:
            continue
        if begin > cursor:
            result.append({'start': cursor, 'stop': begin, 'title': 'No listings', 'missing': True})
        result.append({**programme, 'visible_start': begin, 'visible_stop': end})
        cursor = end
    if cursor < stop:
        result.append({'start': cursor, 'stop': stop, 'title': 'No listings', 'missing': True})
    return result
