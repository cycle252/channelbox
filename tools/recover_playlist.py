#!/usr/bin/env python3
"""Recover a direct local M3U from a Threadfin backup, without contacting servers."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse


def recover(backup: Path, output: Path, state_dir: Path | None = None, last_channel_source: Path | None = None):
    lines = (backup / "data/threadfin.m3u").read_text(encoding="utf-8-sig").splitlines()
    mappings = json.loads((backup / "xepg.json").read_text())
    result = ["#EXTM3U"]
    url_map = {}
    direct_urls = set()
    proxy_hosts = {urlparse(line.strip()).netloc for line in lines if line.strip() and not line.startswith("#")}
    pending = None
    count = 0
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF"):
            pending = line
        elif line and not line.startswith("#") and pending:
            match = re.search(r'channelID="([^"]+)"', pending)
            if not match or match.group(1) not in mappings:
                raise ValueError("A curated channel has no exact backup mapping; refusing a partial recovery")
            direct = mappings[match.group(1)].get("url", "").strip()
            parsed = urlparse(direct)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or "\n" in direct or "\r" in direct:
                raise ValueError("Invalid direct stream address in backup")
            if parsed.netloc in proxy_hosts:
                raise ValueError("A mapped stream still targets the Threadfin proxy host")
            result.extend([pending, direct])
            url_map[line] = direct
            direct_urls.add(direct)
            count += 1
            pending = None
    if pending or count == 0:
        raise ValueError("Incomplete or empty curated playlist")
    restored_last_channel = 0
    # Optional recovery of a remembered channel omitted from the curated backup.
    # Only one exact display-name match in a supplied upstream M3U is accepted.
    if last_channel_source and state_dir and (state_dir / "state.json").exists():
        saved = json.loads((state_dir / "state.json").read_text())
        saved_url = saved.get("url")
        if saved_url and saved_url not in url_map and saved_url not in direct_urls:
            candidates = []
            metadata = None
            for line in last_channel_source.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line.startswith("#EXTINF"):
                    metadata = line
                elif line and not line.startswith("#") and metadata:
                    name = metadata.split(",", 1)[1].strip() if "," in metadata else ""
                    if name == saved.get("name"):
                        candidates.append((metadata, line))
                    metadata = None
            if len(candidates) != 1:
                raise ValueError("Remembered channel requires one exact name match; refusing an ambiguous replacement")
            metadata, direct = candidates[0]
            parsed = urlparse(direct)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.netloc in proxy_hosts:
                raise ValueError("Remembered-channel source is not a direct HTTP(S) stream")
            number = str(saved.get("number", ""))
            numbers = {match.group(1) for line in result for match in re.finditer(r'tvg-chno="([^"]*)"', line)}
            if not number.isdigit() or len(number) > 4 or number in numbers:
                raise ValueError("Remembered channel number is missing, invalid, or already used")
            if re.search(r'tvg-chno="[^"]*"', metadata):
                metadata = re.sub(r'tvg-chno="[^"]*"', f'tvg-chno="{number}"', metadata)
            else:
                attributes, name = metadata.split(",", 1)
                metadata = f'{attributes} tvg-chno="{number}",{name}'
            result.extend([metadata, direct])
            url_map[saved_url] = direct
            direct_urls.add(direct)
            count += 1
            restored_last_channel = 1
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o700)
    playlist = output / "channels.m3u"
    if playlist.exists():
        raise FileExistsError("Output playlist exists; use a new output folder")
    playlist.write_text("\n".join(result) + "\n")
    playlist.chmod(0o600)
    summary = {"channels": count, "extra_remembered_channels": restored_last_channel,
               "threadfin_proxy_addresses_remaining": 0,
               "live_streams_checked": False, "state_urls_remapped": 0,
               "favorites_remapped": 0, "favorites_unmatched": 0,
               "state_urls_unmatched": 0}
    if state_dir:
        for name in ["state.json", "favorites.json"]:
            src = state_dir / name
            if not src.exists():
                continue
            data = json.loads(src.read_text())
            if name == "state.json":
                for key in ["url", "previous_url"]:
                    value = data.get(key)
                    if value in url_map:
                        data[key] = url_map[value]
                        summary["state_urls_remapped"] += 1
                    elif value and value not in direct_urls:
                        summary["state_urls_unmatched"] += 1
            else:
                summary["favorites_remapped"] = sum(value in url_map for value in data)
                summary["favorites_unmatched"] = sum(value not in url_map and value not in direct_urls for value in data)
                data = [url_map.get(value, value) for value in data]
            dst = output / name
            dst.write_text(json.dumps(data, indent=2) + "\n")
            dst.chmod(0o600)
            originals = output / "original-state"
            originals.mkdir(exist_ok=True, mode=0o700)
            original = originals / name
            original.write_bytes(src.read_bytes())
            original.chmod(0o600)
    (output / "recovery-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--last-channel-source", type=Path, help="Optional upstream M3U to restore an omitted last-viewed channel by unique exact name")
    args = parser.parse_args()
    print(json.dumps(recover(args.backup, args.output, args.state_dir, args.last_channel_source), indent=2))
