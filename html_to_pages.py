#!/usr/bin/env python3
"""
html_to_pages.py - extract pages/*.json records from raw Wikipedia HTML
snapshots in pages/*.html that don't have a JSON counterpart yet.

Produces the SAME shape as the hand-built pages/*.json files (title, url,
category, subcategory, summary, specs, sections, full_text), so the existing
pages_to_schema.py pipeline can run over the combined set unchanged.

See CONVERSION_NOTES.md ("HTML extraction" section) for the extraction rules
and the judgment calls made (category taxonomy, section-labeling heuristic,
lead-vs-summary split, etc).

Usage: python html_to_pages.py [pages_dir]
"""

import glob
import json
import os
import re
import sys
import urllib.parse

from bs4 import BeautifulSoup, NavigableString, Tag

# Headings that mark the end of article prose (everything after them is
# citation/navigation apparatus, not content).
STOP_HEADINGS = {
    "see also", "notes", "references", "citations", "bibliography",
    "further reading", "external links", "notes and references",
    "sources", "notes and citations",
}
# Headings to skip WITHOUT terminating collection. A "Gallery" is an image
# block that can appear mid-article (e.g. Ural-4320 places it right after the
# lead, before Specifications/Versions/Users) -- treating it as a hard stop
# like STOP_HEADINGS would truncate everything after it. It carries no prose we
# want, so skip its (image-caption) content but keep walking.
SKIP_HEADINGS = {"gallery"}

# Type-field / lead-text keyword -> (category, subcategory). Checked in order;
# first match wins, so more-specific rules go first and the ordering resolves
# the deliberate overlaps noted inline below. Aircraft entries keep subcategory
# None to match the existing convention (Eurofighter/F-16/J-16/Su-57 all have
# subcategory null); every other entry gets a specific subcategory to match the
# existing "air_to_air" convention.
CLASSIFY_RULES = [
    # --- aircraft ---
    (r"\bfighter\b", ("fighter_aircraft", None)),
    (r"helicopter", ("helicopter", None)),
    (r"\b(bomber|attack aircraft|close air support|interdictor)\b", ("attack_aircraft", None)),
    # --- naval vessels (BEFORE the missile rules: "ballistic/cruise missile
    #     submarine" and "guided-missile destroyer" name the platform, not the
    #     weapon, so they must classify as vessels) ---
    (r"\bsubmarines?\b(?![ -]launched)", ("naval_vessel", "submarine")),
    (r"aircraft carrier", ("naval_vessel", "aircraft_carrier")),
    (r"\b(destroyer|frigate|cruiser|corvette|battleship|patrol boat)\b", ("naval_vessel", "surface_combatant")),
    # --- land vehicles ("main battle tank" etc. -- deliberately NOT bare
    #     "tank", so a "tank gun" classifies as a gun, not a vehicle) ---
    (r"\b(main battle tank|light tank|medium tank|heavy tank|tankette)\b", ("land_vehicle", "main_battle_tank")),
    (r"\b(infantry fighting vehicle|armou?red personnel carrier|armou?red fighting vehicle|reconnaissance vehicle|apc|ifv)\b", ("land_vehicle", "armored_fighting_vehicle")),
    (r"\b(utility vehicle|tactical vehicle|prime mover|truck)\b|military.{0,20}vehicle|high[- ]mobility", ("land_vehicle", "utility_vehicle")),
    # --- torpedo (BEFORE cruise_missile: torpedoes are "anti-ship" too) ---
    (r"torpedo", ("weapon", "torpedo")),
    # --- ammunition (BEFORE the gun rules: "rifle cartridge" is ammo) ---
    (r"\b(cartridge|ammunition)\b", ("ammunition", "cartridge")),
    # --- guns: small arms first, then larger cannon/gun mounts ---
    (r"\b(assault rifle|battle rifle|sniper rifle|carbine|machine gun|submachine gun|rifle|pistol|revolver|shotgun|sidearm)\b", ("weapon", "firearm")),
    (r"\b(autocannon|rotary cannon|revolver cannon|tank gun|naval gun|field gun|anti-aircraft gun|howitzer|cannon)\b", ("weapon", "cannon")),
    # --- missiles ---
    (r"anti-radiation|anti-radar|air-to-surface|air-to-ground", ("weapon", "air_to_surface")),
    (r"surface-to-air|anti-ballistic|air-defen[cs]e|\bsam\b|manpads", ("weapon", "surface_to_air")),
    (r"air-to-air", ("weapon", "air_to_air")),
    (r"ballistic missile", ("weapon", "ballistic_missile")),
    (r"cruise missile|anti-ship|land-attack|surface-to-surface", ("weapon", "cruise_missile")),
]


def classify(type_text, lead_text):
    # The infobox "Type" field is authoritative and short; the lead paragraph
    # is only a fallback for pages with no infobox Type. Matching them
    # separately (Type first, then lead) rather than as one concatenated
    # haystack avoids cross-contamination -- a gun's lead names the tank it
    # arms, a tank's lead names its gun, a destroyer's lead says "guided
    # missile" -- which would otherwise let a lower-priority keyword in the
    # lead win over the correct Type.
    for haystack in (type_text, lead_text):
        if not haystack:
            continue
        h = haystack.lower()
        for pattern, result in CLASSIFY_RULES:
            if re.search(pattern, h):
                return result
    return ("uncategorized", None)


def slug_from_url(url):
    path = urllib.parse.unquote(url.rsplit("/wiki/", 1)[1])
    path = path.replace("/", "_")
    path = re.sub(r"[()]", "", path)
    path = re.sub(r"\s+", "_", path)
    path = re.sub(r"_+", "_", path).strip("_")
    # A leading dot (e.g. ".50_BMG") makes the file a dotfile that glob("*.json")
    # and glob("*.html") silently skip -- drop it so the record is processable
    # (internal dots like "5.56×45mm" are kept). The real name stays in `title`.
    path = path.lstrip(".")
    return path


def clean_text(s):
    s = s.replace("\xa0", " ").replace("−", "-")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def cell_text(td):
    # Multi-value cells (lists / <br>-separated lines) get joined the same
    # way the hand-built pages/*.json files join them: "; " between items.
    for br in td.find_all("br"):
        br.replace_with("; ")
    items = td.find_all("li")
    if items:
        parts = [clean_text(li.get_text(" ", strip=True)) for li in items]
        parts = [p for p in parts if p]
        return "; ".join(parts)
    return clean_text(td.get_text(" ", strip=True))


def extract_infobox_specs(soup):
    ib = soup.select_one("table.infobox")
    if not ib:
        return None
    specs = {}
    for tr in ib.select("tr"):
        th = tr.find("th")
        td = tr.find("td")
        if not th or not td:
            continue  # header/caption/divider rows
        key = clean_text(th.get_text(" ", strip=True))
        value = cell_text(td)
        if not key or not value:
            continue
        specs[key] = value
    return specs


VALUE_CAP = 300  # cap on a single bullet-list spec value, see notes


def extract_bulletlist_specs(soup):
    """Fallback for pages with no infobox table (e.g. TAI TF Kaan): parse the
    "General characteristics / Performance / Armament" bullet-list template
    used further down aircraft articles, where each <li> is "<b>Label:</b>
    value"."""
    heading = None
    for h in soup.find_all(["h2", "h3"]):
        hid = (h.get("id") or "").lower()
        if "specification" in hid:
            heading = h
            break
    if heading is None:
        return None

    specs = {}
    node = heading.parent if heading.parent and heading.parent.name == "div" else heading
    for sib in node.find_next_siblings():
        if isinstance(sib, Tag) and sib.name in ("h2",):
            break
        if not isinstance(sib, Tag) or sib.name != "ul":
            continue
        for li in sib.find_all("li", recursive=False):
            b = li.find("b")
            if not b:
                continue
            label = clean_text(b.get_text(" ", strip=True)).rstrip(":")
            rest = li.get_text(" ", strip=True)
            value = clean_text(rest[len(b.get_text(strip=True)):].lstrip(": "))
            if not label or not value:
                continue
            if len(value) > VALUE_CAP:
                value = value[:VALUE_CAP].rsplit(" ", 1)[0] + "... (truncated, see Wikipedia source)"
            specs[label] = value
    return specs or None


# Cartridge infoboxes label their "Type" row with the *firearms that use the
# round* ("Rifle, carbine, LMG"), not the word "cartridge", so keyword rules
# over Type/lead misfire to weapon/firearm. These dimension fields, by
# contrast, appear only on ammunition infoboxes (never on a firearm's), so
# their presence is an unambiguous ammunition signal.
AMMO_SPEC_MARKERS = {
    "case type", "bullet diameter", "parent case", "neck diameter",
    "base diameter", "rim diameter", "case length", "rifling twist",
    "primer type", "shoulder diameter",
}


def looks_like_ammunition(specs):
    if not specs:
        return False
    keys = {k.lower() for k in specs}
    return len(keys & AMMO_SPEC_MARKERS) >= 2


def first_type_value(specs):
    if not specs:
        return None
    for k, v in specs.items():
        if k.lower() == "type":
            return v
    # Automobile/vehicle infoboxes label the type row "Class" (e.g. Ural-4320
    # has no "Type" row, only "Class: Truck") -- fall back to it so wheeled
    # vehicles still classify.
    for k, v in specs.items():
        if k.lower() == "class":
            return v
    return None


def content_root(soup):
    # Some snapshots have a decoy ".mw-parser-output" div earlier in the DOM
    # (e.g. inside the "good article" badge icon at #mw-indicator-good-star),
    # which is tiny compared to the real article body -- pick the largest
    # match rather than the first.
    candidates = soup.select(".mw-parser-output")
    if candidates:
        return max(candidates, key=lambda c: len(c.get_text()))
    return soup.body


def strip_refs(soup):
    """Remove footnote markers and edit-section links everywhere, including
    inside the infobox -- must run before spec extraction, not just before
    prose extraction, or "[ 1 ]"-style markers leak into parametric values."""
    for sel in ("sup.reference", ".mw-editsection", ".shortdescription",
                ".mw-indicators", "style", "script"):
        for tag in soup.select(sel):
            tag.decompose()


def strip_noise(root):
    for sel in ("table", ".hatnote", ".navbox", ".reflist", ".thumb"):
        for tag in root.select(sel):
            tag.decompose()


def gather_lead_and_sections(root):
    """Walk root's children in order; return (lead_paragraphs, section_blocks)
    where section_blocks is [(h2_text, [paragraph_texts])] stopping at the
    first boilerplate heading (References, See also, ...)."""
    lead = []
    sections = []
    current = None  # (heading_text, [texts])
    seen_h2 = False

    def flush_text(node):
        if isinstance(node, Tag) and node.name == "p":
            txt = clean_text(node.get_text(" ", strip=True))
            return txt or None
        if isinstance(node, Tag) and node.name == "li":
            txt = clean_text(node.get_text(" ", strip=True))
            return txt or None
        return None

    for node in root.find_all(["h2", "h3", "p", "li"], recursive=True):
        # only consider top-level-ish li's (skip ones nested inside infobox,
        # already removed) -- direct prose lists under a section
        if node.name == "h2":
            heading_text = clean_text(node.get_text(" ", strip=True))
            heading_text = re.sub(r"\[edit\]$", "", heading_text).strip()
            norm = heading_text.lower()
            if norm in STOP_HEADINGS:
                break
            if current:
                sections.append(current)
            seen_h2 = True
            if norm in SKIP_HEADINGS:
                current = None  # discard the gallery's content, keep walking
                continue
            current = (heading_text, [])
            continue
        if node.name == "h3":
            continue  # subheadings fold into the parent h2's prose, unlabeled
        txt = flush_text(node)
        if not txt:
            continue
        if not seen_h2:
            lead.append(txt)
        elif current:
            current[1].append(txt)
    if current:
        sections.append(current)
    return lead, sections


def build_full_text(lead, sections):
    blocks = list(lead)
    for heading_text, paras in sections:
        if not paras:
            continue
        label = heading_text.upper() + ": " + paras[0]
        blocks.append(label)
        blocks.extend(paras[1:])
    return "\n\n".join(blocks)


def gather_section_titles(soup):
    root = content_root(soup)
    titles = []
    for h in root.find_all(["h2", "h3"]):
        text = clean_text(h.get_text(" ", strip=True))
        norm = text.lower()
        if h.name == "h2" and norm in STOP_HEADINGS:
            break
        if h.name == "h2" and norm in SKIP_HEADINGS:
            continue
        if text:
            titles.append(text)
    return titles


def extract_page(html_path):
    with open(html_path, "r", encoding="utf-8", errors="ignore") as fh:
        raw = fh.read()
    soup = BeautifulSoup(raw, "html.parser")
    strip_refs(soup)

    # Browser-saved snapshots inject <base href>; genuine raw Wikipedia HTML
    # (fetched over HTTP) has no <base> but always carries
    # <link rel="canonical" href="https://en.wikipedia.org/wiki/...">. Prefer
    # <base> to keep the existing snapshots byte-identical, fall back to
    # canonical so the same extractor works on freshly fetched pages.
    url = None
    base = soup.find("base")
    if base and base.get("href"):
        url = base["href"]
    if not url:
        canon = soup.select_one('link[rel="canonical"]')
        if canon and canon.get("href"):
            url = canon["href"]
    if not url:
        return None
    slug = slug_from_url(url)

    title_tag = soup.find("title")
    title = clean_text(title_tag.get_text()) if title_tag else slug.replace("_", " ")
    title = re.sub(r"\s*-\s*Wikipedia$", "", title).strip()
    if not title or title.lower() == "wikipedia":
        title = slug.replace("_", " ")

    # section titles need the raw soup (before infobox/table stripping)
    sections = gather_section_titles(soup)

    specs = extract_infobox_specs(soup)
    if not specs:
        specs = extract_bulletlist_specs(soup) or {}

    root = content_root(soup)
    strip_noise(root)
    lead, section_blocks = gather_lead_and_sections(root)

    summary = lead[0] if lead else ""
    full_text = build_full_text(lead, section_blocks)

    type_text = first_type_value(specs)
    lead_text_for_classify = " ".join(lead[:1])
    category, subcategory = classify(type_text, lead_text_for_classify)
    # Ammunition infoboxes' Type row names the guns that fire the round, so the
    # keyword rules land on weapon/firearm -- override from the structural
    # cartridge-field signal instead.
    if category != "ammunition" and looks_like_ammunition(specs):
        category, subcategory = "ammunition", "cartridge"

    page = {
        "title": title,
        "url": url,
        "category": category,
        "subcategory": subcategory,
        "summary": summary,
        "specs": specs,
        "sections": sections,
        "full_text": full_text,
    }
    return slug, page


def main():
    pages_dir = sys.argv[1] if len(sys.argv) > 1 else "pages"
    html_files = sorted(glob.glob(os.path.join(pages_dir, "*.html")))

    converted, skipped, failed = [], [], []
    for fp in html_files:
        result = extract_page(fp)
        if result is None:
            failed.append((fp, "no <base href> found"))
            continue
        slug, page = result
        out_path = os.path.join(pages_dir, f"{slug}.json")
        if os.path.exists(out_path):
            skipped.append((fp, out_path))
            continue
        if page["category"] == "uncategorized":
            print(f"! {fp}: could not classify (Type={first_type_value(page['specs'])!r}); "
                  f"writing with category='uncategorized', please review", file=sys.stderr)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(page, fh, indent=2, ensure_ascii=False)
        converted.append((fp, out_path, page))

    print(f"{'html file':50} -> {'json file':40} specs sections")
    for fp, out_path, page in converted:
        print(f"{os.path.basename(fp):50} -> {os.path.basename(out_path):40} "
              f"{len(page['specs']):5} {len(page['sections']):8}")
    print(f"\nConverted {len(converted)}, skipped {len(skipped)} (already had JSON), "
          f"failed {len(failed)}.")
    for fp, out_path in skipped:
        print(f"  skip: {os.path.basename(fp)} (-> {os.path.basename(out_path)} already exists)")
    for fp, reason in failed:
        print(f"  FAIL: {os.path.basename(fp)}: {reason}")


if __name__ == "__main__":
    main()
