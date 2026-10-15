#!/usr/bin/env python3
"""Prepare a private local cache without putting source URLs on the command line."""
import argparse
import json
import sys
import urllib.parse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from channelbox import read_json
from channelbox_feed import atomic_write
from channelbox_m3u import download_config, prepare


def normalize_drive(url):
    p = urllib.parse.urlsplit(url)
    match = re.fullmatch(r'/file/d/([^/]+)(?:/.*)?', p.path)
    if p.scheme == 'https' and p.hostname == 'drive.google.com' and match:
        query = {'export': 'download', 'id': match.group(1)}
        resource = urllib.parse.parse_qs(p.query).get('resourcekey')
        if resource:
            query['resourcekey'] = resource[0]
        return 'https://drive.google.com/uc?' + urllib.parse.urlencode(query)
    return url


def prepare_cache(config, output, local=None, replace=False):
    output, config = Path(output), Path(config)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.parent.chmod(0o700)
    options = json.loads(config.read_text()) if config.exists() else {}
    if not local:
        if not options.get('playlist_url'):
            raise ValueError('Configure playlist_url or use --local')
        normalized = normalize_drive(options['playlist_url'])
        if normalized != options['playlist_url']:
            options['playlist_url'] = normalized
            atomic_write(config, json.dumps(options, indent=2) + '\n')
        config.chmod(0o600)
        raw = download_config(config)
    else:
        if options.get('playlist_url'):
            raise ValueError('Set playlist_url to empty when using a local file')
        raw = Path(local).expanduser().read_bytes()
    metadata_path = output.with_suffix('.feed-state.json')
    metadata = {} if replace else read_json(metadata_path, {})
    result = prepare(raw, metadata)
    if output.exists():
        atomic_write(output.with_suffix('.m3u.previous'), output.read_bytes())
    if metadata_path.exists():
        atomic_write(metadata_path.with_suffix('.json.previous'), metadata_path.read_bytes())
    atomic_write(output, result['text'])
    atomic_write(metadata_path, json.dumps({k: v for k, v in result.items() if k != 'text'}))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'private/source.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'private/channels.m3u')
    parser.add_argument('--local', type=Path, help='Import a local M3U instead of downloading')
    parser.add_argument('--replace', action='store_true', help='Explicitly reset number reservations for a new lineup')
    args = parser.parse_args()
    try:
        value = prepare_cache(args.config, args.output, args.local, args.replace)
    except Exception as error:
        print(f'Playlist preparation failed ({type(error).__name__}). Check the private config and direct download access.', file=sys.stderr)
        return 1
    print(f'Prepared {value["count"]:,} entries; filled {value["repaired"]} unusable numbers. Private URLs were not displayed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
