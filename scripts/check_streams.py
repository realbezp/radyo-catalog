#!/usr/bin/env python3
"""
Periodic stream health checker & auto-repair script for catalog/stations.json.

Checks each stream URL by:
  1. Following HTTP redirects
  2. Verifying HTTP 200/206 and non-HTML audio/playlist Content-Type
  3. Reading real stream bytes to confirm live data flows
  4. Tracking consecutive_failures (marks 'failed' only after >= 2 failures)
  5. Querying Radio Browser API for replacement URLs when all streams of a
     station are failing.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

CATALOG_PATH = pathlib.Path(__file__).resolve().parent.parent / "stations.json"
USER_AGENT = "RadyoCatalogChecker/1.0 (+https://github.com/realbezp/radyo)"
TIMEOUT_SECONDS = 8
MIN_AUDIO_BYTES = 1024

AUDIO_CONTENT_HINTS = (
    "audio/",
    "application/ogg",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/octet-stream",
    "video/mp2t",
)


def probe_stream(url: str) -> tuple[bool, str]:
    """Return (is_healthy, detail_message) after verifying live stream bytes."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Icy-MetaData": "1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", 200)
            if status not in (200, 206):
                return False, f"HTTP {status}"

            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                return False, f"HTML response ({ctype})"

            is_hls = url.lower().endswith(".m3u8") or "mpegurl" in ctype
            chunk = resp.read(MIN_AUDIO_BYTES if not is_hls else 256)
            if not chunk:
                return False, "Empty response body"

            if is_hls:
                text = chunk.decode("utf-8", errors="ignore")
                if "#EXTM3U" not in text:
                    return False, "Invalid M3U8 playlist"
                return True, "HLS OK"

            if ctype and not any(h in ctype for h in AUDIO_CONTENT_HINTS):
                return False, f"Unexpected Content-Type: {ctype}"

            if len(chunk) < 256:
                return False, f"Too few bytes ({len(chunk)})"

            return True, f"Audio OK ({ctype or 'stream'})"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, str(exc)


def discover_replacement_stream(name: str, country_code: str) -> dict | None:
    """Query Radio Browser API for a working stream matching station name."""
    params = urllib.parse.urlencode(
        {
            "name": name,
            "countrycode": country_code,
            "hidebroken": "true",
            "order": "clickcount",
            "reverse": "true",
            "limit": "5",
        }
    )
    api_url = f"https://de1.api.radio-browser.info/json/stations/search?{params}"
    req = urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            candidates = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    for item in candidates:
        candidate_url = (item.get("url_resolved") or item.get("url") or "").strip()
        if not candidate_url.startswith(("http://", "https://")):
            continue
        ok, _ = probe_stream(candidate_url)
        if ok:
            return {
                "url": candidate_url,
                "priority": 1,
                "bitrate": int(item.get("bitrate") or 0),
                "codec": str(item.get("codec") or "").upper(),
                "status": "ok",
                "source": "radio_browser_auto",
                "consecutive_failures": 0,
            }
    return None


def main() -> int:
    if not CATALOG_PATH.exists():
        print(f"Catalog not found: {CATALOG_PATH}", file=sys.stderr)
        return 1

    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    ok_count = 0
    fail_count = 0
    repaired_count = 0

    for station in data.get("stations", []):
        streams = station.get("streams", [])
        any_ok = False
        existing_urls = {s.get("url") for s in streams}

        for stream in streams:
            url = stream.get("url", "").strip()
            if not url:
                continue
            healthy, info = probe_stream(url)
            stream["last_checked_at"] = now_iso
            if healthy:
                stream["status"] = "ok"
                stream["consecutive_failures"] = 0
                stream["last_success_at"] = now_iso
                any_ok = True
                ok_count += 1
            else:
                fails = int(stream.get("consecutive_failures", 0)) + 1
                stream["consecutive_failures"] = fails
                stream["status"] = "failed" if fails >= 2 else "degraded"
                fail_count += 1
            print(f"[{station['name']}] {url} -> {stream['status']} ({info})")

        if not any_ok:
            replacement = discover_replacement_stream(
                station.get("name", ""),
                station.get("country_code", ""),
            )
            if replacement and replacement["url"] not in existing_urls:
                replacement["last_checked_at"] = now_iso
                replacement["last_success_at"] = now_iso
                for s in streams:
                    s["priority"] = int(s.get("priority", 1)) + 1
                streams.insert(0, replacement)
                repaired_count += 1
                print(f"  -> Auto-repaired {station['name']} with {replacement['url']}")

    data["updated_at"] = now_iso
    CATALOG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Done. ok={ok_count}, failed/degraded={fail_count}, repaired={repaired_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
