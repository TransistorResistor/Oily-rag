# pages/*.json -> schema-example.json conversion

## v3: land vehicles, ships/subs, torpedoes, guns, ammunition (current, 2026-07-01)

Added 30 more pages spanning the categories requested beyond the original
air-combat set: more fighters (J-20, Rafale, F-15, F/A-18E/F), more missiles
(AGM-88 HARM, 9K720 Iskander, FIM-92 Stinger, BrahMos, S-400), tanks (M1
Abrams, Leopard 2, T-90), an IFV (Bradley), utility vehicles (Humvee,
Ural-4320), surface ships (Arleigh Burke, Nimitz, Ticonderoga), submarines
(Virginia, Ohio, Los Angeles), torpedoes (Mark 48, VA-111 Shkval), guns (M2
Browning, M4 carbine, GAU-8 Avenger, Rheinmetall Rh-120), and cartridges
(5.56×45mm NATO, 7.62×51mm NATO, .50 BMG). The corpus went from 33 to **63
`pages_schema` records** and from 7 to **19 per-systemType schemas**.

### Fetching (`fetch_pages.py`, new)

The original `pages/*.html` were browser "Save As" snapshots; these 30 are
fetched straight over HTTP by `fetch_pages.py` (stdlib `urllib`, descriptive
User-Agent, 0.5s spacing, skips any URL whose `pages/<slug>.json` already
exists so re-runs are cheap). Run order is now:

```
python fetch_pages.py        # URLs -> pages/*.html (raw Wikipedia HTML)
python html_to_pages.py      # pages/*.html -> new pages/*.json (skips existing)
python pages_to_schema.py    # pages/*.json (all) -> pages_schema/*.json + schemas/
```

Raw fetched HTML has **no injected `<base href>`** (browsers add that), so
`html_to_pages.extract_page` now falls back to the `<link rel="canonical">`
that raw Wikipedia HTML always carries. `<base>` is still preferred, so the
existing snapshots parse byte-identically.

### New taxonomy + `classify()` made Type-first

New `(category, subcategory)` pairs (and matching `GROUP_TYPE` entries in
`pages_to_schema.py`): `land_vehicle`/{`main_battle_tank`,
`armored_fighting_vehicle`, `utility_vehicle`}, `naval_vessel`/{`submarine`,
`surface_combatant`, `aircraft_carrier`}, `weapon`/{`torpedo`, `firearm`,
`cannon`, `ballistic_missile`, `air_to_surface`}, and `ammunition`/`cartridge`.

`classify()` was changed to match the infobox **Type field first and the lead
paragraph only as a fallback**, instead of concatenating both into one
haystack. This eliminates cross-contamination that the mixed taxonomy exposed:
a gun's lead names the tank it arms, a tank's lead names its gun, a
guided-missile destroyer's lead says "missile". Matching the short, specific
Type field on its own (and only consulting the lead when there's no infobox
Type, e.g. TAI TF Kaan) makes each classify off the authoritative signal.
Rule ordering resolves deliberate overlaps: naval vessels before missiles
(so "ballistic/cruise missile submarine" and "guided-missile destroyer"
classify as the platform); "main battle tank" etc. rather than bare "tank"
(so a "tank gun" stays a gun); torpedo before cruise-missile (torpedoes are
"anti-ship" too); ammunition before firearms.

### Bugs found and fixed while extracting this batch

- **`submarine` rule over/under-matched.** `\bsubmarine\b` matched
  BrahMos's Type ("...**Submarine**-launched cruise missile" -> tagged a
  submarine) and *missed* Ohio-class's lead ("nuclear-powered **submarines**",
  plural). Fixed to `\bsubmarines?\b(?![ -]launched)`: matches the plural,
  excludes "submarine-launched"/"submarine launched". BrahMos now ->
  `cruise_missile`, Ohio -> `submarine`.
- **Automobile infobox has no "Type" row.** Ural-4320 uses the vehicle
  infobox whose type row is labelled **"Class"** ("Class: Truck"), so
  `first_type_value` returned None and it landed `uncategorized`. Added a
  "Class" fallback -> now `utility_vehicle`.
- **A mid-article "Gallery" truncated the whole article.** Wikipedia placed
  Ural-4320's `Gallery` section immediately after the lead, before
  Specifications/Versions/Users. The old code treated every boilerplate
  heading (including `gallery`) as a hard stop, so `full_text`/`sections`
  ended at the gallery (0 sections, lead-only text). Split the set into
  `STOP_HEADINGS` (references/see also/... -> stop) and `SKIP_HEADINGS`
  (`gallery` -> skip its image content but keep walking). Ural went 0 -> 8
  sections; harmless for pages whose gallery is at the tail.
- **`.50 BMG` was silently never parsed.** Its URL slug is `.50_BMG`, and
  Python's `glob("*.html")`/`glob("*.json")` skip dot-leading filenames, so
  the fetched HTML was ignored and no record was produced. `slug_from_url`
  now `lstrip(".")`s the slug (-> `50_BMG`; internal dots like `5.56` kept),
  and the real name stays in `title` (".50 BMG").
- **Cartridge Type rows name the gun, not the round.** The ammunition
  infobox's "Type" lists the firearms that fire it ("Rifle, carbine, LMG"),
  so the keyword rules classified 5.56/7.62/.50 BMG as `weapon/firearm`.
  Added `looks_like_ammunition(specs)`: cartridge-only dimension fields
  (`Case type`, `Bullet diameter`, `Parent case`, `Neck/Base/Rim diameter`,
  ...) are an unambiguous structural signal a firearm infobox never has;
  >=2 present -> override to `ammunition/cartridge`.

### `pages_to_schema.py`: tonnes as a mass unit

Armored vehicles give Mass in **tonnes** ("55.2 tonnes", T-90/Leopard) or the
abbreviation **"t"** ("54 t", M1/Bradley), which the mass alias table (kg/lb
only) couldn't parse -- every tank/IFV mass was falling to free text. Added
`tonnes?|metric tons?` and a bare `t` alias (factor 1000 -> kg), ordered so the
spelled form is claimed first and the `(?![a-zA-Z])` guard keeps `t` from
biting the "t" in "tonnes"/"short tons"/"long tons". Verified across all 63
records: M1 54,000 / Leopard 55,200 / T-90 46,000 / Bradley 27,600 kg, and no
existing missile/aircraft mass changed (the `Mass` catalogue field stays
single-unit `kg`, now spanning 2.9 kg small arms to 55,200 kg tanks). The
remaining `pages_to_schema` numeric warnings for this batch are by-design:
`Maximum speed` stays strict-Mach (km/h vehicle/torpedo speeds -> free text,
same as the Patriot case in v2), and Virginia-class `Range: "Unlimited"` has
no number to extract.

## v2: richer descriptions + per-systemType parametrics

The first pass (see "Original section" below) matched schema-example.json's
*shape* but not its spirit: every record got exactly two descriptions
(`Overview`, `Details`), and aircraft/helicopter records were missing nearly
all their numeric specs because those only exist in `full_text`, not in the
structured `specs` dict. `pages_to_schema.py` was rewritten to fix both,
and dropped the extra `parameters` dict the first pass added (it isn't part
of schema-example.json's shape -- see the ragkit caveat below).

**`descriptions` now use 5 canonical `descrType` buckets**: `Overview`,
`History`, `Design`, `Usage`, `Variants` (`Design` was added beyond the
originally-suggested `Overview`/`History`/`Usage`/`Variants` because a large,
consistent fraction of every record's prose -- cockpit, avionics, armament,
guidance, engines, antenna -- is technical/physical description, distinct
from either the program timeline or operational service). Each page's
`full_text` is segmented by its own internal section headings and each
heading is classified into one of the 5 buckets by keyword (`STOP_RE`,
`VARIANTS_RE`, `USAGE_RE`, `HISTORY_RE`, `DESIGN_RE`, `OVERVIEW_RE` in
`pages_to_schema.py`). Headings are detected two ways, because the source
data is inconsistent even within the hand-built records: on their own line
(`"DEVELOPMENT\nIn early 1968, ..."`) or inline with a colon
(`"Development history: The AIM-7 Sparrow ..."`). A heading candidate is
only trusted if it normalizes (parenthetical suffix stripped, lowercased) to
an entry in a whitelist built from the union of every page's own `sections`
array plus a small manual `EXTRA_HEADINGS` set for label wording that
doesn't exactly match a `sections` entry -- this is what keeps sentence
fragments like `"The Molniya (now Vympel) R-60 (NATO reporting name: ..."`
from being misread as a heading. A `SPECIFICATIONS`-style heading is a stop
marker: everything after it is a flat spec dump, not prose, so description
collection ends there.

About a third of the corpus (`AIM-7_Sparrow`, `RBE2`, the `AN/APG-63/65/79`
radar-family pages, ...) has *no* internal heading markers at all -- full
prose, no structure. For these, unlabeled paragraphs get a light per-
paragraph keyword scan of their own content (same regex tables) as a weak
hint for which bucket to continue in; the very first paragraph is always
forced to `Overview` regardless (it's the lead/summary) so it can't get
hijacked by its own keywords (e.g. a lead sentence containing "developed"
was originally flipping paragraph 0 to `History` and, because that then left
`Overview` empty, triggering the `summary`-fallback and duplicating the same
text into both buckets -- fixed by gating the content-hint path on
`seen_any`). This is inherently heuristic for the unlabeled third of the
corpus; it's a reasonable-effort segmentation, not a guaranteed-correct one.

**`parametrics` are widened per systemType.** Comparing `specs` dicts across
categories showed aircraft/helicopter records (`Sukhoi_Su-25`, `Sukhoi_Su-27`,
`General_Dynamics_F-16`, `Kamov_Ka-27`, ...) only carry qualitative infobox
fields (`Type`, `Manufacturer`, `First flight`, ...) in `specs` -- their
actual dimensions (`Length`, `Wingspan`, `Empty weight`, `Maximum speed`,
`Range`, ...) live in a `"Specifications"` (or `"Preliminary specifications"`)
bullet block inside `full_text`, one `"Field: value"` pair per paragraph,
untouched by the original conversion. `find_spec_block_fields()` locates that
block (same heading-whitelist detector as above) and parses it into
`(field, value)` pairs, which are merged into `specs` before parametric
extraction -- using the same unit tables as before, extended with `area`
(m²) and `climb_rate` (m/s) dimensions and several new field mappings
(`Wing area`, `Gross weight`, `Combat range`, `Ferry range`, `Rate of climb`,
`Main rotor diameter`, `Main rotor area`). Missile and radar records already
had this data in `specs` and are unaffected. Net effect: e.g. Sukhoi Su-25
went from 11 parametrics (0 numeric) to 29 (12 numeric); F-22 from 11 to 36
(13 numeric).

That bullet block also nests weapon-loadout sub-lists under `Hardpoints`
(`Rockets`, `Missiles`, `Bombs`, `Air-to-air missiles`, ... each just a
verbatim excerpt of `Hardpoints`' own value) and, after the block ends,
narrative trailer sections (`Accidents and incidents`, `Operators`, ...)
that are not spec fields. Both are filtered: a harvested field is dropped if
its value (length >= 8, to avoid short values like `Crew: "1"` trivially
matching) is already a substring of a field already accepted; harvesting
stops outright at a recognized boilerplate/narrative heading
(`BOILERPLATE_STOP`, expanded to include the accident/operator/display
trailers observed in this corpus).

**systemGroup/systemType were redesigned** to mirror schema-example.json's
`"Sensors"` / `"Laser Sensor"` pattern (broad group, specific human-readable
type) instead of a literal title-cased `category`/`subcategory`: `Weapon` /
{`Air-to-Air Missile`, `Surface-to-Air Missile`, `Cruise Missile`}, `Sensors`
/ `Aircraft Radar`, `Aircraft` / {`Fighter Aircraft`, `Attack Aircraft`,
`Helicopter`}. `R-60_missile.json` was recategorized from
`fighter_aircraft`/`None` to `weapon`/`air_to_air` (it's a missile; the
original html-extraction keyword rules matched on the wrong text) --
`CATEGORY_OVERRIDES` in the script. Separately,
`pages/Lockheed_Martin_F-22_Raptor.json`'s `title` field was fixed from
`"Development"` to `"Lockheed Martin F-22 Raptor"` (a leftover from the
mislabeled `Development [49].html` snapshot noted in the original
html-extraction section below -- the *filename* was already correctly
derived from `<base href>`, but the `title` field itself was never
corrected, and it feeds straight into `nomenclature`).

**`schemas/<systemType>.schema.json`** (one per systemType, 7 total) are
generated after the main conversion by aggregating which `parameter` names
actually occurred, with what `uom`, across that systemType's records --
sorted by observation count. These are reference manifests in
schema-example.json's instance style (not JSON-Schema-draft meta-documents),
derived from the real corpus rather than hand-guessed, so they stay accurate
if more pages are added later.

**ragkit.py caveat (unchanged from v1, restated for clarity):**
`ragkit.py`'s `flatten_record()` only reads top-level string/number fields
and a `parameters` dict (see its module docstring); `descriptions`,
`media`, and `parametrics` are all lists of dicts and are silently skipped
during ingestion. Since `parameters` was dropped in this pass to match
schema-example.json's shape exactly, records in this shape are not
currently ingestible by `ragkit.py ingest` at all -- `flatten_record` would
need to be extended to walk `descriptions`/`parametrics` if that's wanted.
Not fixed here since it's outside "match schema-example.json's structure."

## v1

**Contents:** this file covers two scripts:
- `html_to_pages.py` -- extracts `pages/*.json` records from raw Wikipedia
  HTML snapshots (`pages/*.html`) that had no JSON counterpart. Run this
  first.
- `pages_to_schema.py` -- converts every `pages/*.json` record (hand-built
  and HTML-extracted alike) into `pages_schema/*.json`. See the original
  section below for units/parametrics/parameters details, which apply
  equally to the newly-extracted records.

## HTML extraction (`html_to_pages.py`)

`pages/` also contained 26 raw Wikipedia HTML snapshots (`pages/*.html`),
saved separately from the 21 hand-built `*.json` files. 12 of the 26 had a
matching JSON record already; the other 14 didn't. `html_to_pages.py` reads
every `*.html` file, and for any whose derived filename doesn't already have
a `.json` counterpart, extracts the same shape as the hand-built files
(`title, url, category, subcategory, summary, specs, sections, full_text`)
and writes it to `pages/<name>.json` -- so `pages_to_schema.py` runs over the
combined set unchanged. Run order:

```
python html_to_pages.py      # pages/*.html -> new pages/*.json (skips existing)
python pages_to_schema.py    # pages/*.json (all of them) -> pages_schema/*.json
```

14 new records were added this way: `Kamov_Ka-27`, `Sukhoi_Su-25`,
`Sukhoi_Su-24`, `Mikoyan_MiG-29`, `Sukhoi_Su-35`, `Sukhoi_Su-27`,
`R-60_missile`, `Lockheed_Martin_F-35_Lightning_II`, `TAI_TF_Kaan`,
`Lockheed_Martin_F-22_Raptor`, `MIM-104_Patriot`, `MIM-23_Hawk`,
`Kalibr_missile_family`, `Tomahawk_missile` -- bringing the total from 19 to
33 records.

### How each field is pulled out of the HTML

- **`url`**: every snapshot has `<base href="https://en.wikipedia.org/wiki/...">`
  regardless of how much page chrome was kept in the snapshot -- this is the
  one reliably present anchor, so everything else (filename, title fallback)
  derives from it.
- **`title`**: the `<title>` tag with the trailing `" - Wikipedia"` stripped,
  falling back to the URL slug if the title tag is missing or unusable.
- **filename**: derived from the URL slug (`/wiki/R-60_(missile)` ->
  `R-60_missile.json`), not the `.html` filename -- the `.html` files are
  numbered snapshots (`[31].html` etc.) and one of them (`Development
  [49].html`) is mislabeled entirely: its `<title>` says "Development" (an
  artifact of how it was saved) but `<base href>` reveals it's actually the
  **Lockheed Martin F-22 Raptor** page. Deriving the filename from the URL
  rather than the HTML filename/title recovers this correctly --
  `Lockheed_Martin_F-22_Raptor.json`, not a stray "Development.json".
- **`specs`**: parsed from `<table class="infobox">` th/td rows, joining
  multi-item cells (`<li>` lists, `<br>`-separated lines) with `"; "` to
  match the hand-built files' convention. One page (TAI TF Kaan) has no
  infobox at all -- Wikipedia hasn't added one yet since the aircraft is
  still in development -- so for that case only, `specs` falls back to
  parsing the "General characteristics / Performance / Armament" bullet-list
  template further down the article (`<li><b>Label:</b> value</li>`). Very
  long nested list values (the Armament section's full missile/bomb
  inventory) are capped at 300 characters to avoid one field becoming a wall
  of text.
- **`summary`**: the first lead paragraph (before the first `h2`).
- **`full_text`**: all lead paragraphs, followed by each top-level `h2`
  section's prose with an `"H2 TITLE:"` label on its first paragraph --
  reverse-engineered from the labeling pattern already visible in the
  hand-built `Sukhoi_Su-57.json` (`"DEVELOPMENT:"`, `"DESIGN:"`, etc.). `h3`
  subsections fold into their parent's prose unlabeled, matching that same
  file's style. Extraction stops at the first boilerplate section (`See
  also`, `Notes`, `References`, `Citations`, `Bibliography`, `Further
  reading`, `External links`, `Sources`, `Gallery`) -- this is a
  reconstruction of the pattern, not a guaranteed match to whatever
  internal tool built the original files (one hand-built file,
  `AN_APG-63_radar_family.json`, does include "References" as a section, so
  the original tool's exact cutoff rule isn't fully known).
- **`sections`**: all `h2`/`h3` heading text up to that same boilerplate
  cutoff.
- **`category`/`subcategory`**: keyword rules over the infobox `Type` row
  (falling back to the lead paragraph when there's no infobox), checked in
  this order: `fighter` -> `fighter_aircraft`; `helicopter` -> `helicopter`;
  `bomber`/`attack aircraft`/`close air support`/`interdictor` ->
  `attack_aircraft`; `surface-to-air`/`anti-ballistic` -> `weapon` /
  `surface_to_air`; `air-to-air` -> `weapon` / `air_to_air` (matches the
  existing convention); `cruise missile`/`anti-ship`/`land-attack`/
  `surface-to-surface` -> `weapon` / `cruise_missile`. `attack_aircraft`,
  `helicopter`, `surface_to_air`, and `cruise_missile` are new taxonomy
  introduced for this batch (the original 19 records only needed
  `fighter_aircraft`, `radar`/`aircraft_radar`, and `weapon`/`air_to_air`) --
  easy to relabel if a different taxonomy is wanted. All 14 new records
  classified successfully (the script would print a warning to stderr for
  any that didn't).

### Bugs found and fixed while extracting

- **Footnote markers leaking into spec values.** `sup.reference` (footnote
  numbers like `[ 2 ]`) has to be stripped from the *entire* document before
  the infobox is read, not just from the prose -- an earlier version only
  stripped it while building `full_text`, so values like MIM-104 Patriot's
  `"In service": "Since 1981; initial operational capacity 1984 [ 1 ]"` kept
  the bracket. Fixed by stripping `sup.reference` (and `.mw-editsection`)
  globally, first thing, before any extraction runs.
- **Wrong content root on "good article" pages.** Pages with the little
  gold "good article" badge (e.g. the F-22 page) have a *second*, tiny
  `<div class="mw-parser-output">` earlier in the DOM (inside the badge
  icon markup) before the real article body. Selecting the first match
  returned that decoy and produced an empty summary/full_text/sections.
  Fixed by picking the *largest* `.mw-parser-output` element by text length
  instead of the first one.

## Numeric-parsing fixes made while extending `pages_to_schema.py`

Running the existing pipeline over the 14 new records surfaced real bugs in
the unit-extraction logic that the original 19-record set hadn't exercised.
All fixed in `pages_to_schema.py`, verified against `catalogue.py` output
(shown further below):

- **`Flight altitude` had no metric alias at all.** It was modeled on the
  one example that existed (R-77: `"5-25 km"`), so its alias table only knew
  `km`/`nmi`/`mi`. The new cruise-missile records report altitude in bare
  meters (`"20,000 m (66,000 ft)"`, `"50-150 m AGL"`), which silently failed
  to parse. Added `m`/`metres`/`meters` as aliases (factor 0.001, converting
  to the field's canonical `km`).
- **European decimal commas.** TAI TF Kaan's preliminary-specs bullet list
  (sourced from Turkish Aerospace Industries' own material, not standard
  en-wiki infobox formatting) uses a comma as the decimal point (`"Mach
  1,8"`, `"0,75"`), which the existing thousands-separator-stripping logic
  would have silently mangled into `18` and `75`. Added `_to_float()`, which
  distinguishes a thousands-grouping comma (`\d{1,3}(,\d{3})+`, e.g.
  `"34,750"` -> `34750`) from a short trailing decimal comma (`\d+,\d{1,2}`,
  e.g. `"1,8"` -> `1.8`).
- **`Maximum speed` unit contamination.** MIM-104 Patriot's speed is given
  only in km/h (`"5,630 km/h"`), never Mach. The dimension's original
  fallback logic ("prefer Mach, else scan for km/h/mph/kn/etc.") kicked in
  and returned `5630 "km/h"` for that one record, while all 20 other records
  with a `Maximum speed` value are in `Mach` (topping out around 5) --
  `catalogue.py`'s min/max for the field became `0.74-5630.0`, i.e.
  meaningless. Since Mach and km/h have no fixed conversion (Mach depends on
  altitude/temperature), the fix is to make the field strict: extract a
  value **only** when the source text says "Mach", otherwise leave it as
  free text. Patriot's `Maximum speed` is now free text; its raw
  `"5,630 km/h"` is still fully preserved in `parameterDescr`.
- **Currency values with a word/letter multiplier were truncated.** `"About
  US$1.09 billion"` (MIM-104 Patriot) parsed as `1.09` and `"$3.1M"`
  (Tomahawk) parsed as `3.1` -- the regex captured the number but had
  nothing to interpret `billion`/`M` with. Extended the currency regex to
  optionally capture a `million|billion|thousand|M|B|K` suffix and multiply
  accordingly (`1.09 billion` -> `1,090,000,000`; `3.1M` -> `3,100,000`).
  Note this does *not* resolve a separate, un-fixable ambiguity: Patriot's
  `Unit cost` text gives a **battery**-level cost first (`$1.09 billion`)
  and a **missile**-level cost second (`$4 million`); the extractor still
  takes the first figure, so Patriot's `Unit cost` is a system price, not a
  per-round price like every other record's `Unit cost`. Flagging this the
  same way the "Range" and "Flight altitude" field-reuse ambiguities are
  flagged below -- it's a source-data ambiguity (one field, two different
  things being priced), not something automatically resolvable without
  hand-labeling which clause is "the" unit cost.
- **A model designator's digits were mistaken for a count.** Su-35's
  `"Number built"` value (`"Su-27M: 12 ; Su-35S: 155+"`) extracted `27` --
  the "27" inside "Su-27M" -- instead of the real count `12`, because the
  `count` dimension has no unit word to anchor against (unlike mass/length/
  etc., which require the number to sit next to `kg`/`m`/etc.) and just took
  the first digit run in the string. Fixed by requiring the matched number
  to not be directly glued to a letter on either side (`Su-27M`'s `27` is
  followed immediately by `M` with no separator; `12` after `"Su-27M: "` has
  a colon+space before it and a space+semicolon after).

After these fixes, spot-checking every new numeric parametric by hand against
its `Source text` (all ~90 of them) turned up nothing further wrong.

## Extending the field-name/unit tables for new spec vocabulary

Most of the new aircraft (Su-24/25/27/35, MiG-29, F-35, F-22) use the
standard aircraft infobox, so their `Type`/`Manufacturer`/`Number built`/
etc. fields already matched the existing `NAME_ALIASES`/`FIELD_DIMENSION`
tables with no changes needed. TAI TF Kaan's bullet-list fallback introduced
several genuinely new one-off fields (`Wing area`, `Wing loading`,
`Thrust/weight`, `g limits`, `Guns`, `Hardpoints`, `Missiles`, `Bombs`,
`Crew`, and several IRFS/IEOS/ICNI avionics fields) -- these aren't in
`FIELD_DIMENSION`, so they're kept as free text (the existing, safe default
for any unrecognized field name), not forced into a unit that doesn't apply.

## Post-extension `catalogue.py` verification (33 records)

```
Mass                 unit='kg'   range=44.0-1300.0     median=175.0
Length               unit='m'    range=2.09-21.9       median=3.7
Range                unit='km'   range=8.0-3000.0      median=90.0
Flight altitude      unit='km'   range=0.05-25.0        median=10.075
Maximum speed        unit='Mach' range=0.74-4.0         median=2.2
Unit cost            unit='USD'  range=125000.0-1090000000.0
Number built         unit='units' range=12.0-70000.0    median=1000.0
```
21 fields classified numeric, all single-unit, all sane ranges (the one wide
spread, `Unit cost`, is the battery-vs-missile ambiguity noted above, not a
unit bug). `free_text: 26, categorical: 25, numeric: 21, date: 2` across the
full 33-record catalogue.

---

## Original section (19 hand-built records)

`pages_to_schema.py` converts each Wikipedia-sourced record in `pages/*.json`
into the parameter/description shape demonstrated by `pages/schema-example.json`,
and writes the result to `pages_schema/`. Run it with:

```
python pages_to_schema.py            # reads pages/, writes pages_schema/
```

19 of the 21 files in `pages/` were converted. `_index.json` (empty) and
`schema-example.json` (the template itself, not a page) are skipped.

## Bug found and fixed first

`pages/Sukhoi_Su-57.json` had unescaped literal `"` characters around
`"second stage"` inside `full_text`, which is invalid JSON — the file failed
to parse at all (`json.load` raised `Expecting ',' delimiter`). Fixed by
escaping both occurrences as `\"second stage\"`. This was blocking, not just
for this conversion but for anything reading that file, including `ragkit.py
ingest`.

## Field mapping

| schema-example.json field | source | notes |
|---|---|---|
| `modelID` | synthetic, `2001..2019` | source pages have no model ID; assigned sequentially by filename order — **not** a real registry ID |
| `systemGroup` | `category` | e.g. `radar` -> `Radar` |
| `systemType` | `subcategory` (falls back to `category`) | e.g. `aircraft_radar` -> `Aircraft Radar` |
| `nomenclature` | `title` | |
| `updatedDate` | today's date | date the conversion ran, **not** a real Wikipedia last-edited date (not available in the source) |
| `descriptions` | `summary` -> one entry, `descrType: "Overview"`; `full_text` -> one entry, `descrType: "Details"` | `shortDescription` is the first sentence (regex, capped ~200 chars). The source `sections` array (a table of contents) has no equivalent slot in schema-example.json and was dropped — `full_text` already contains that content as prose. |
| `media` | `url` | one entry per page; `mediaID` synthesized as `modelID*100+seq` |
| `parametrics` | `specs` | see below |
| `classification` | — | hardcoded `"U"` (unclassified) everywhere. All source content is public Wikipedia text; schema-example.json's `A`/`F` values look like placeholder lorem-ipsum data, not a real convention, so there was nothing meaningful to map from. |

## `parametrics` vs `parameters` — why both are present

The request was to match `schema-example.json`'s shape (a flat `parametrics`
list) **and** to be usable by "the RAG demo script." Those two things don't
automatically agree with each other, so both are included rather than picking
one silently:

- **`parametrics`**: a list of `{seq, parameter, parameterDescr,
  parameterUomValue, parameterValue, uom, classification}` rows — this is
  the literal shape shown in `schema-example.json`.
- **`parameters`**: a `{name: {value, unit, definition}}` dict — this is the
  shape `ragkit.py`'s own docstring documents and that `catalogue.py`'s
  `_is_param_dict`/`extract_fields` actually recognize at ingest time.

**Caveat:** `catalogue.py`'s field walker (`catalogue._walk`) skips any list
whose elements are objects ("lists of objects: skipped for now"). That means
`parametrics` alone, in its schema-example.json shape, is invisible to
`ragkit.py ingest` — none of those rows would become filterable catalogue
fields. Verified directly:

```
python -c "
import json, glob, catalogue as cat
recs = [json.load(open(f, encoding='utf-8')) for f in glob.glob('pages_schema/*.json')]
c = cat.build_catalogue(recs)
print(len(c), 'fields classified')
"
```

Only `parameters` feeds the catalogue. If `pages_schema/` is what actually
gets ingested by `ragkit.py ingest`, keep the `parameters` key. If a future
ingestion path is written specifically for the `parametrics` array shape,
`catalogue._walk` would need a small extension to walk lists of
`{parameter, parameterValue, uom}`-shaped dicts the same way it already
walks `{value, unit}`-shaped ones.

## Unit normalization

`catalogue.py` computes one min/median/max per *field name* — it assumes
every value under that name is already in the same unit. The source specs
are not: Wikipedia infoboxes mix `lb`/`kg`, `ft`/`m`, `mi`/`km`, `mph`/`Mach`,
often in the same string (imperial primary + metric in parentheses, or vice
versa depending on the system's country of origin).

Rule applied: for each canonical field, scan the raw text for every
recognizable `<number> <unit>` occurrence, and pick the leftmost occurrence
whose unit belongs to the metric/SI family (`kg`, `m`/`mm`/`cm`/`km`, `GHz`,
`kW`, `km/h`); only fall back to the leftmost imperial occurrence (and
convert) if no metric figure is present anywhere in the string. Wikipedia
infoboxes give a metric figure in nearly every case, so almost all values
below came from a straight parse rather than a conversion.

Canonical unit chosen per field (same unit for that field across every
record):

| field | unit | field | unit |
|---|---|---|---|
| Mass, Weight, Warhead weight, Empty weight, Max takeoff weight | kg | Frequency | GHz |
| Length, Diameter, Antenna diameter, Wingspan, Height, Service ceiling | m | Power | kW |
| Range, Flight altitude | km | Azimuth, Elevation | deg |
| Maximum speed | Mach (only unit seen in this dataset) | Unit cost | USD |
| Number built, Units delivered | units (count) | | |

Verified against `catalogue.build_catalogue()` on the actual output — every
one of these fields comes back `numeric` with a single consistent unit and a
sane range, e.g.:

```
Mass          numeric  unit='kg'   range=85.3-230.0    median=161.5
Range         numeric  unit='km'   range=26.0-3000.0   median=100.0
Maximum speed numeric  unit='Mach' range=1.1-4.0        median=2.5
Unit cost     numeric  unit='USD'  range=125000.0-1095000.0
```

Everything else (Manufacturer, Guidance system, Country of origin, etc.) is
left as free text — `uom: ""`, `parameterValue` is the raw string. Forcing
those into numbers would be meaningless.

### Known imprecision (by design, not a bug)

- **Multi-variant fields** (e.g. AIM-120 `Range`: `"AIM-120A/B: 40 nmi (75 km);
  AIM-120C: 49 nmi (90 km); AIM-120D: 70-86 nmi (130-160 km)"`) collapse to a
  single representative number — the first metric value in the string (here,
  75 km, the A/B variant). The full original text is always preserved
  verbatim in `parameterDescr`, so nothing is lost, but `parameterValue`
  alone is not "the" definitive figure for multi-variant systems.
- **Ranges written as `"X-Y unit"` or `"X to Y unit"`** report the number
  adjacent to the unit word, which is the *upper* bound (e.g. Euroradar
  CAPTOR `Frequency: "X-band, 8 to 12 GHz"` -> `12 GHz`, not 8).
- **`+/-` fields** (Azimuth/Elevation) report the magnitude with a `Note:`
  flagging it as a symmetric range in `parameterDescr`.
- **Mixed numeric/free-text fields dilute in `catalogue.py`.** `Frequency`
  has 5 numeric values (radars that give GHz) and several free-text values
  (`"X-band"` with no number). Because fewer than 80% of the values are
  numeric, `catalogue.py`'s classifier treats the whole field as
  categorical/free-text and **silently drops the 5 numeric readings** from
  the catalogue summary (they're still stored per-record, just not
  summarized). This is existing `catalogue.py` behavior, not something this
  conversion changed — flagging it because it's easy to miss.

## Field-name unification

A few Wikipedia infoboxes label the same concept differently. These were
merged to a single canonical name so `catalogue.py` actually sees them as one
field instead of several near-empty ones:

- `No. built` -> `Number built`
- `National origin` / `Place of origin` / `Country` -> `Country of origin`
- `Introduction date` -> `Introduced`
- `Operational range` -> `Range`

Note this means, e.g., a radar's "detection range" and a missile's "flight
range" now share the `Range` field/unit — semantically different quantities
merged under one label. That ambiguity already existed in the source data
(Wikipedia infoboxes use "Range" for both); this conversion didn't introduce
it, just surfaced it by unifying the aliases.

The same characteristic showed up in the 14 HTML-extracted records for
`Flight altitude`: R-77 and R-60 use it for missile engagement altitude
(kilometers-scale, e.g. `20 km`), while Kalibr and Tomahawk use it for
sea-skimming cruise altitude (meters-scale, e.g. `0.05-0.15 km`). Same field
name, same now-consistent unit (km), genuinely different physical scale —
again inherited from the source data's field-naming, not introduced here.

## Reproducing / re-running

```
python pages_to_schema.py                 # regenerate pages_schema/*.json
python pages_to_schema.py pages out2       # custom in/out dirs
```

The script prints a per-file numeric/text parametric count and flags any
field it expected to be numeric (per `FIELD_DIMENSION`) but couldn't parse a
quantity from — currently only `Frequency: "X band"/"X-band"` (no number
present in the source text at all, nothing to extract).
