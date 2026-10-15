#!/usr/bin/env python3
"""Home-network stream checks; only decoded video qualifies for admission."""
from __future__ import annotations

import argparse
import base64
import copy
import fcntl
import html
import json
import logging
import re
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from channelbox_feed import atomic_write, download, render_m3u, validate_feed, web_url

SOURCE = 'https://iptv-org.github.io/iptv/countries/us.m3u'
LOG = logging.getLogger('scanner')
ATTR = re.compile(r'([\w-]+)="([^"]*)"')

# Explicit Arkansas / Mid-South selection, not every national network carried locally.
LOCAL_REVISION = 2
COMPATIBILITY_REVISION = 1
# Retry this previously excluded feed without waiting for the weekly full scan.
COMPATIBILITY_CHECK_IDS = {'GritXtra.us@SD'}
LOCAL_PAGES = {
    'ChannelBox.THV11News': ('THV11 Arkansas News', 'https://www.thv11.com/watch'),
    'ChannelBox.KFSMNews': ('5NEWS Arkansas', 'https://www.5newsonline.com/watch'),
    'ChannelBox.WATNNews': ('ABC24 Memphis News', 'https://www.abc24.com/watch'),
}
LOCAL_ORDER = (
    'ChannelBox.THV11News', 'ChannelBox.KFSMNews',
    'PBS.us@KETS', 'KVTNDT251.us@HD',
    'ChannelBox.WATNNews', 'ChannelBox.WHBQNews', 'PBS.us@KTEJ',
    'NBC.us@WMCTV', 'ABC.us@WATNTV', 'Fox.us@WHBQTV', 'Fox.us@WJKT',
)
# Exact broadcast feed IDs, not every stream for the national network.
LOCAL_UPSTREAM = {
    'PBS.us@KETS': 'Arkansas TV statewide (KETS/KTEJ)',
    'PBS.us@KTEJ': 'KTEJ Jonesboro',
    'NBC.us@WMCTV': 'WMC Memphis',
    'ABC.us@WATNTV': 'WATN Memphis broadcast',
    'Fox.us@WHBQTV': 'WHBQ Memphis broadcast',
    'Fox.us@WJKT': 'WJKT Jackson',
}
LOCAL_CHECK_IDS = set(LOCAL_PAGES) | set(LOCAL_UPSTREAM) | {'ChannelBox.WHBQNews'}
FOX13_PAGE = 'https://www.fox13memphis.com/video/'


def fox13_live_url(text):
    """Only accept the station's labelled live VideoObject, never a news clip."""
    urls = set()
    for raw in re.findall(r'<script[^>]*type=[\"\']application/ld\+json[\"\'][^>]*>(.*?)</script>', text, re.S | re.I):
        try:
            item = json.loads(raw)
            if (isinstance(item, dict) and item.get('@type') == 'VideoObject'
                    and item.get('name') == 'FOX13 Memphis News'
                    and item.get('description') == 'Watch FOX13 News LIVE Any Time for Free.'):
                url = web_url(html.unescape(item['contentUrl']), https=True)
                if '/linear/' in url and '.m3u8' in url:
                    urls.add(url)
        except (ValueError, KeyError, TypeError):
            continue
    if len(urls) != 1:
        raise ValueError('FOX13 live feed missing or ambiguous')
    return urls.pop()



def local_sources(upstream=None):
    """Resolve station-published live player URLs; never substitute VOD clips."""
    entries = {}
    if upstream is None:
        try:
            upstream = parse_source(download(SOURCE).decode('utf-8-sig'))
        except Exception:
            LOG.warning('Local broadcast discovery unavailable; retaining verified fallbacks')
            upstream = {}
    for identity, name in LOCAL_UPSTREAM.items():
        if identity in upstream:
            entries[identity] = upstream[identity]
        else:
            LOG.info('No direct feed listed for %s; will check again next scan', name)
    for identity, (name, page) in LOCAL_PAGES.items():
        try:
            text = download(page).decode('utf-8')
            matches = re.findall(r'data-elvs-stream-url="([^"<>]+)"', text)
            urls = {html.unescape(url) for url in matches if '.m3u8' in url}
            if len(urls) != 1:
                raise ValueError('Live player URL missing or ambiguous')
            entries[identity] = dict(id=identity, name=name, group='Local / Mid-South',
                                     url=web_url(urls.pop(), https=True))
        except Exception:
            # Do not log upstream exceptions: they may contain stream addresses.
            LOG.warning('Local station page unavailable: %s; retaining verified fallback', name)
    try:
        url = fox13_live_url(download(FOX13_PAGE).decode('utf-8'))
        entries['ChannelBox.WHBQNews'] = dict(id='ChannelBox.WHBQNews', name='FOX13 Memphis News',
                                            group='Local / Mid-South', url=url)
    except Exception:
        LOG.warning('FOX13 live page unavailable; retaining verified fallback')
    return entries


def channel_order(channel):
    identity = channel['id']
    rank = LOCAL_ORDER.index(identity) if identity in LOCAL_ORDER else len(LOCAL_ORDER)
    return rank, int(channel['number'])


def refresh_local_additions(state):
    """One-time local enrichment; leave nationwide scan dates/failure counts intact."""
    upstream = local_sources()
    results = {}
    for identity, c in upstream.items():
        good = probe(c)
        LOG.info('Local check: %s — %s', c['name'], 'passed' if good else 'failed; not newly admitted')
        if good:
            results[identity] = True
    now = datetime.now(timezone.utc).isoformat()
    new, feed = evolve(state, upstream, results, False, now)
    if 'last_scan' in state:
        new['last_scan'] = state['last_scan']
    else:
        new.pop('last_scan', None)
    new['local_revision'] = LOCAL_REVISION
    LOG.info('Local / Mid-South update: %d local streams verified; %d total channels', len(results), len(feed['channels']))
    return new, feed


def parse_source(text):
    entries, current = {}, None
    if not text.lstrip('\ufeff').startswith('#EXTM3U'):
        raise ValueError('Upstream is not M3U')
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#EXTINF:'):
            a = dict(ATTR.findall(line))
            current = dict(id=a.get('tvg-id', ''), name=line.split(',', 1)[-1],
                           group=a.get('group-title', ''), user_agent='', referrer='')
        elif line.startswith('#EXTVLCOPT:') and current:
            key, _, value = line[11:].partition('=')
            field = {'http-user-agent': 'user_agent', 'http-referrer': 'referrer'}.get(key)
            if field:
                current[field] = value
        elif line and not line.startswith('#'):
            if not current or not current['id'] or current['id'] in entries:
                raise ValueError('Missing or duplicate upstream identity')
            current['url'] = web_url(line)
            entries[current['id']] = current
            current = None
    if not entries or current:
        raise ValueError('Empty or truncated source')
    return entries


def refresh_compatibility_additions(state):
    """Admit newly decodable feeds without advancing scheduled failure cycles."""
    source = parse_source(download(SOURCE).decode('utf-8-sig'))
    candidates = {i: source[i] for i in COMPATIBILITY_CHECK_IDS if i in source}
    results = {}
    for identity, candidate in candidates.items():
        good = probe(candidate)
        LOG.info('Compatibility check: %s — %s', identity,
                 'passed' if good else 'unavailable; retrying on daily scans')
        if good:
            results[identity] = True
    now = datetime.now(timezone.utc).isoformat()
    new, feed = evolve(state, candidates, results, False, now)
    if 'last_scan' in state:
        new['last_scan'] = state['last_scan']
    else:
        new.pop('last_scan', None)
    new['compatibility_revision'] = COMPATIBILITY_REVISION
    return new, feed


def probe(channel, timeout=20):
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
               '-threads', '1', '-rw_timeout', '8000000',
               '-protocol_whitelist', 'http,https,tcp,tls,crypto',
               # HLS services such as Grit use URLs without conventional suffixes.
               # Keep the normal extension allowlists and transport restrictions;
               # disable only FFmpeg's strict extension/content matching check.
               '-extension_picky', '0']
    for field, flag in [('user_agent', '-user_agent'), ('referrer', '-referer')]:
        if channel.get(field):
            command += [flag, channel[field]]
    command += ['-i', channel['url'], '-map', '0:v:0', '-map', '0:a:0?',
                '-t', '3', '-threads', '1', '-progress', 'pipe:1', '-nostats', '-f', 'null', '-']
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=timeout, check=False, text=True)
        frames = [int(x) for x in re.findall(r'^frame=(\d+)\s*$', result.stdout, re.M)]
        return result.returncode == 0 and bool(frames) and max(frames) >= 2
    except subprocess.TimeoutExpired:
        return False


def evolve(state, upstream, results, full, now, threshold=3):
    """Failed cycles are independent dates, not immediate retries."""
    new = copy.deepcopy(state)
    registry, records = new['numbers'], new.setdefault('records', {})
    used = set(registry.values())
    next_number = max([999] + [int(n) for n in used]) + 1
    for identity in sorted(upstream):
        if identity not in registry:
            if next_number > 9999:
                raise ValueError('Channel numbers exhausted')
            registry[identity] = str(next_number)
            next_number += 1
    prior_active = {i for i, r in records.items() if r.get('active')}
    tested_active = prior_active & results.keys()
    # An outage should not progressively erase the last working lineup.
    if tested_active and sum(bool(results[i]) for i in tested_active) < len(tested_active) * 0.5:
        raise ValueError('Most established channels failed; keeping last good list')
    for identity, good in results.items():
        record = records.setdefault(identity, {'failures': 0, 'active': False})
        if good:
            c = dict(upstream[identity], number=registry[identity])
            record.update(channel=c, active=True, failures=0, last_success=now)
        else:
            record['failures'] = record.get('failures', 0) + 1
            if record['failures'] >= threshold:
                record['active'] = False
    channels = sorted([r['channel'] for r in records.values() if r.get('active')], key=channel_order)
    feed = validate_feed(dict(schema_version=1, generated_at=now, source=SOURCE, channels=channels))
    new['last_scan'] = now
    if full:
        new['last_full'] = now
    return new, feed


def scan(state, full, workers=2):
    upstream = parse_source(download(SOURCE).decode('utf-8-sig'))
    old_source_count = state.get('source_count', 0)
    if old_source_count and len(upstream) < old_source_count * 0.75:
        raise ValueError('Upstream list shrank unexpectedly')
    upstream.update(local_sources(upstream))
    records = state.get('records', {})
    identities = set(upstream) | set(records) if full else {i for i, r in records.items() if r.get('active')}
    if not identities:
        identities = set(upstream)
        full = True
    # Recheck local additions daily, including any that were temporarily offline.
    identities.update(LOCAL_CHECK_IDS)
    identities.update(COMPATIBILITY_CHECK_IDS)
    results = {}
    def check(identity):
        candidate = upstream.get(identity)
        good = bool(candidate and probe(candidate))
        # If a replacement URL fails, still test the previously verified stream.
        previous = records.get(identity, {}).get('channel')
        if not good and previous and (not candidate or previous['url'] != candidate['url']):
            if probe(previous):
                return identity, True, previous
        if identity in LOCAL_CHECK_IDS or identity in COMPATIBILITY_CHECK_IDS:
            LOG.info('%s check: %s — %s',
                     'Local' if identity in LOCAL_CHECK_IDS else 'Compatibility',
                     identity, 'passed' if good else 'unavailable')
        return identity, good, candidate
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for identity, good, candidate in pool.map(check, sorted(identities)):
            results[identity] = good
            if good:
                upstream[identity] = candidate
            if len(results) % 50 == 0:
                LOG.info('Checked %d/%d; %d passed', len(results), len(identities), sum(results.values()))
    now = datetime.now(timezone.utc).isoformat()
    new, feed = evolve(state, upstream, results, full, now)
    new['source_count'] = len(upstream)
    new['local_revision'] = LOCAL_REVISION
    LOG.info('Scan complete: %d checked, %d passed, %d retained', len(results), sum(results.values()), len(feed['channels']))
    return new, feed


def publish_github(feed, options):
    """One atomic content update. Token stays in HA options, never on the Pi."""
    repo, token = options.get('github_repository', ''), options.get('github_token', '')
    if not repo or not token:
        return False
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid repository')
    url = f'https://api.github.com/repos/{repo}/contents/channels.json'
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
               'User-Agent': 'ChannelBox-Scanner', 'X-GitHub-Api-Version': '2022-11-28'}
    data = {'message': 'Refresh verified US channels', 'branch': 'main',
            'content': base64.b64encode(json.dumps(feed, ensure_ascii=False, indent=2).encode()).decode()}
    try:
        with urllib.request.urlopen(urllib.request.Request(url + '?ref=main', headers=headers), timeout=30) as response:
            data['sha'] = json.load(response)['sha']
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    with urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method='PUT'), timeout=30) as response:
        if response.status not in (200, 201):
            raise RuntimeError('GitHub publication failed')
    return True


def due(state, now, hour):
    today = now.date().isoformat()
    if now.hour < hour or state.get('last_scan_day') == today:
        return None
    last = state.get('last_full')
    full = not last or (now.date() - datetime.fromisoformat(last).astimezone(now.tzinfo).date()).days >= 7
    return full


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path('/data'))
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--full', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    args.data.mkdir(parents=True, exist_ok=True)
    lock_file = (args.data / 'scanner.lock').open('w')
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    state_path = args.data / 'scanner-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'numbers': json.loads(Path(__file__).with_name('numbers.json').read_text()), 'records': {}}
    while True:
        try:
            options = json.loads((args.data / 'options.json').read_text())
            now = datetime.now(ZoneInfo(options.get('timezone', 'America/Chicago')))
            request = options.get('full_scan_request', 0)
            manual_full = request > state.get('completed_full_scan_request', 0)
            full = True if args.full else due(state, now, options.get('scan_hour', 4))
            if manual_full:
                full = True
                LOG.info('Manual full scan request %d: checking all upstream US channels', request)
            if state.get('records') and state.get('local_revision', 0) < LOCAL_REVISION:
                new, feed = refresh_local_additions(state)
                new['publish_pending'] = True
                atomic_write(args.data / 'channels.json', json.dumps(feed, ensure_ascii=False, indent=2))
                atomic_write(args.data / 'channels.m3u', render_m3u(feed['channels']))
                atomic_write(state_path, json.dumps(new, ensure_ascii=False, indent=2))
                state = new
            if state.get('records') and state.get('compatibility_revision', 0) < COMPATIBILITY_REVISION:
                new, feed = refresh_compatibility_additions(state)
                new['publish_pending'] = True
                atomic_write(args.data / 'channels.json', json.dumps(feed, ensure_ascii=False, indent=2))
                atomic_write(args.data / 'channels.m3u', render_m3u(feed['channels']))
                atomic_write(state_path, json.dumps(new, ensure_ascii=False, indent=2))
                state = new
            if args.once or full is not None:
                new, feed = scan(state, bool(full), options.get('workers', 2))
                new['last_scan_day'] = now.date().isoformat()
                if manual_full:
                    new['completed_full_scan_request'] = request
                new['publish_pending'] = True
                # Save a good scan before upload; publication retries never rescan or add failures.
                atomic_write(args.data / 'channels.json', json.dumps(feed, ensure_ascii=False, indent=2))
                atomic_write(args.data / 'channels.m3u', render_m3u(feed['channels']))
                atomic_write(state_path, json.dumps(new, ensure_ascii=False, indent=2))
                state = new
            if state.get('publish_pending'):
                feed = json.loads((args.data / 'channels.json').read_text())
                if publish_github(feed, options):
                    state['publish_pending'] = False
                    atomic_write(state_path, json.dumps(state))
                    LOG.info('Published %d channels to GitHub', len(feed['channels']))
                else:
                    LOG.info('Scan saved locally; GitHub repository/token not configured')
            if args.once:
                return
            time.sleep(300)
        except Exception as error:
            # Exception text can include tokens or stream addresses: print type only.
            LOG.error('Scan or publication failed (%s); previous playlist retained', type(error).__name__)
            if args.once:
                raise SystemExit(1)
            time.sleep(3600)


if __name__ == '__main__':
    main()
