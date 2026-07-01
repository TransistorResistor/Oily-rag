#!/usr/bin/env python3
"""
fetch_pages.py - download raw Wikipedia article HTML into pages/*.html so the
existing html_to_pages.py extractor can turn them into pages/*.json.

Unlike the original browser-saved snapshots, these are fetched straight over
HTTP, so they carry no injected <base href> -- html_to_pages.py falls back to
the <link rel="canonical"> the raw HTML always includes (see CONVERSION_NOTES,
"Fetching new pages"). Nothing in the saved HTML is modified.

A page is skipped if its derived pages/<slug>.json already exists, so re-runs
are cheap and this never re-fetches or duplicates a record already in the set.

Usage:
    python fetch_pages.py            # fetch every URL below that's missing
    python fetch_pages.py --list     # just print what would be fetched
"""

import sys
import time
import urllib.request
from pathlib import Path

from html_to_pages import slug_from_url

PAGES_DIR = Path(__file__).parent / "pages"
UA = "rag-demo-dataset-builder/1.0 (educational dataset; contact timcorr91@gmail.com)"

# New pages to add, grouped by the categories requested. Kept as full URLs so
# the record's canonical slug/title come straight from Wikipedia (× and other
# non-ASCII chars are percent-encoded here). Already-present slugs (Su-57,
# Tomahawk, the existing fighters/missiles/radars) are intentionally omitted.
URLS = [
    # --- more fighter aircraft ---
    "https://en.wikipedia.org/wiki/Chengdu_J-20",
    "https://en.wikipedia.org/wiki/Dassault_Rafale",
    "https://en.wikipedia.org/wiki/McDonnell_Douglas_F-15_Eagle",
    "https://en.wikipedia.org/wiki/Boeing_F/A-18E/F_Super_Hornet",
    # --- more missiles ---
    "https://en.wikipedia.org/wiki/AGM-88_HARM",
    "https://en.wikipedia.org/wiki/9K720_Iskander",
    "https://en.wikipedia.org/wiki/FIM-92_Stinger",
    "https://en.wikipedia.org/wiki/BrahMos",
    "https://en.wikipedia.org/wiki/S-400_missile_system",
    # --- land vehicles (tanks, AFVs, trucks) ---
    "https://en.wikipedia.org/wiki/M1_Abrams",
    "https://en.wikipedia.org/wiki/Leopard_2",
    "https://en.wikipedia.org/wiki/T-90",
    "https://en.wikipedia.org/wiki/Bradley_Fighting_Vehicle",
    "https://en.wikipedia.org/wiki/Humvee",
    "https://en.wikipedia.org/wiki/Ural-4320",
    # --- surface ships ---
    "https://en.wikipedia.org/wiki/Arleigh_Burke-class_destroyer",
    "https://en.wikipedia.org/wiki/Nimitz-class_aircraft_carrier",
    "https://en.wikipedia.org/wiki/Ticonderoga-class_cruiser",
    # --- submarines ---
    "https://en.wikipedia.org/wiki/Virginia-class_submarine",
    "https://en.wikipedia.org/wiki/Ohio-class_submarine",
    "https://en.wikipedia.org/wiki/Los_Angeles-class_submarine",
    # --- torpedoes ---
    "https://en.wikipedia.org/wiki/Mark_48_torpedo",
    "https://en.wikipedia.org/wiki/VA-111_Shkval",
    # --- guns ---
    "https://en.wikipedia.org/wiki/M2_Browning",
    "https://en.wikipedia.org/wiki/M4_carbine",
    "https://en.wikipedia.org/wiki/GAU-8_Avenger",
    "https://en.wikipedia.org/wiki/Rheinmetall_Rh-120",
    # --- ammunition ---
    "https://en.wikipedia.org/wiki/5.56%C3%9745mm_NATO",
    "https://en.wikipedia.org/wiki/7.62%C3%9751mm_NATO",
    "https://en.wikipedia.org/wiki/.50_BMG",
]


def safe_filename(slug):
    # slug is already filesystem-safe (html_to_pages.slug_from_url strips
    # parens/slashes); mirror the existing "<title> - Wikipedia.html" style.
    return f"{slug} - Wikipedia.html"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def main():
    list_only = "--list" in sys.argv
    fetched, skipped, failed = [], [], []
    for url in URLS:
        slug = slug_from_url(url)
        if (PAGES_DIR / f"{slug}.json").exists():
            skipped.append((url, "json exists"))
            continue
        out = PAGES_DIR / safe_filename(slug)
        if out.exists():
            skipped.append((url, "html exists"))
            continue
        if list_only:
            print(f"would fetch: {url} -> {out.name}")
            continue
        try:
            data = fetch(url)
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed.append((url, str(exc)))
            print(f"FAIL {url}: {exc}", file=sys.stderr)
            continue
        out.write_bytes(data)
        fetched.append((url, out.name, len(data)))
        print(f"ok   {slug:40} {len(data):>9,} bytes")
        time.sleep(0.5)  # be polite to Wikipedia

    print(f"\nFetched {len(fetched)}, skipped {len(skipped)}, failed {len(failed)}.")
    for url, reason in skipped:
        print(f"  skip: {url} ({reason})")
    for url, reason in failed:
        print(f"  FAIL: {url}: {reason}")


if __name__ == "__main__":
    main()
