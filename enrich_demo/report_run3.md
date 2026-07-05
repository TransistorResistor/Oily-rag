# Reverse-enrichment report - run 3

*Model:* `gemma3-4b`  |  *Docs this run:* 1  |  *LLM calls:* 1  |  *Tokens:* 323 prompt + 142 completion

**10 live proposals** (0 new this run) across 5 records.


## Lockheed Martin F-35 Lightning II

### Outstanding from prior runs

- **relation**: Lockheed Martin F-35 Lightning II <-> AIM-9X Sidewinder
    - F-35 Cleared to Carry AIM-9X Short-Range Missile  (testdocs\relation_f35_aim9x.pdf)
      > The F-35 Lightning II is now cleared to carry the AIM-9X Sidewinder short-range air-to-air missile for within-visual-range engagements.


## Python-5

### Conflicts (DB vs document)

- **Maximum range** - conflict
    - DB value: `20 km`
    - Document value(s): `35 km` x1
    - Python-5 Extended Engagement Range Claim  (testdocs\conflict_python5_range.pdf)
      > The Python-5 has a maximum range of 35 km in its latest production configuration.

### Outstanding from prior runs

- **Operated by (country)** = `Vietnam` (gap-fill)
    - Vietnam Acquires Python-5 Air-to-Air Missiles  (testdocs\python5_vietnam.pdf)
      > Vietnam has been reported as a new operator of the Israeli Python-5 short-range air-to-air missile


## S-300

### Conflicts (DB vs document)

- **Maximum range** - conflict
    - DB value: `200 km`
    - Document value(s): `250 km` x1
    - Qualifier: _up to_
    - S-300 Reach and Readiness (Preliminary)  (testdocs\hedged_s300.pdf)
      > S-300 Reach and Readiness (Preliminary) According to a preliminary summary, the S-300 surface-to-air missile system can reportedly engage targets at ranges of up to 250 km, an estimated figure for the longest-range missile variant.

### Outstanding from prior runs

- **Detection range** = `300 km` (gap-fill)
    - S-300 Acquisition Radar Detection Envelope  (testdocs\gapfill_s300_detection.pdf)
      > detection range of 300 km against fighter-sized targets at altitude


## S-400 Triumf

### Conflicts (DB vs document)

- **Deployment time** - conflict
    - DB value: `5 min`
    - Document value(s): `8 minutes` x1
    - S-400 Battery Emplacement Assessment  (testdocs\conflict_s400_deploytime.pdf)
      > A field assessment of the S-400 Triumf air defence system reports a deployment time of 8 minutes from convoy halt to radar-active status for the mobile surface-to-air missile battery.

### Outstanding from prior runs

- **Maximum altitude** = `30 km` (gap-fill) - corroborated by 2 sources
    - Value distribution across sources: 1 source(s) say `30 km`, 1 source(s) say `98000 ft`
    - Analyst Note: S-400 Engagement Ceiling (1/2)  (testdocs\corrob_s400_altitude_a.pdf)
      > the S-400 Triumf surface–to–air missile system can engage aerial targets at a maximum altitude of 30 km

- **alias** = `40R6` (gap-fill)
    - Naming Conventions of the S-400 Air Defence System  (testdocs\gapfill_s400_alias.pdf)
      > The S-400 Triumf surface-to-air missile system is, at the complete system level, sometimes designated the 40R6 in Russian service documentation.

- **Maximum altitude** = `98000 ft` (gap-fill) - corroborated by 2 sources
    - Value distribution across sources: 1 source(s) say `30 km`, 1 source(s) say `98000 ft`
    - Analyst Note: S-400 Engagement Ceiling (2/2)  (testdocs\corrob_s400_altitude_b.pdf)
      > the S-400 air defence system has an engagement ceiling of roughly 98,000 ft


## SPYDER

### Outstanding from prior runs

- **Operated by (country)** = `Poland` (gap-fill)
    - Poland Evaluates SPYDER Point-Defence System  (testdocs\spyder_poland.pdf)
      > Poland has been named as a new operator of the SPYDER surface-to-air missile system


## Suppressed (rejected but recurring)

- _Seen again x2_: S-400 Triumf - Operated by (country) = `Belarus` (gap_fill) - previously rejected, not resurfaced


## Parked for review (rescue candidates)

*Unmapped/incomplete fragments the mapper could not place - grouped by linked record for human review.*

### Python-5
  - _range_ = `short-range` (incomplete) - Vietnam Acquires Python-5 Air-to-Air Missiles
      > Python-5 short-range air-to-air missile

### S-300
  - _surveillance coverage_ = `panoramic` (unmapped) - S-300 Acquisition Radar Detection Envelope
      > reflects the panoramic surveillance coverage of the phased-array antenna
  - _emplacement time_ = `5 days` (incomplete) - S-300 Reach and Readiness (Preliminary)
      > The same summary lists the emplacement time of the S-300 air defence battery simply as 5

### S-400 Triumf
  - _can engage_ = `aerial targets` (unmapped) - Analyst Note: S-400 Engagement Ceiling (1/2)
      > the S-400 Triumf surface–to–air missile system can engage aerial targets at a maximum altitude of 30 km
  - _deployment_ = `Belarus none` (unmapped) - Belarus Confirmed as New S-400 Operator
      > The S-400 air defence system has now been fielded by Belarus to protect its western airspace.
  - _purpose_ = `protect none` (unmapped) - Belarus Confirmed as New S-400 Operator
      > ...to protect its western airspace.
  - _deployment time_ = `long-range coverage` (incomplete) - Belarus S-400 Regiment Reaches Operational Status
      > The S-400 deployment gives Belarus long-range coverage of its airspace

### SPYDER
  - _firing_ = `Python and Derby interceptors` (unmapped) - Poland Evaluates SPYDER Point-Defence System
      > a mobile air defence system firing Python and Derby interceptors
  - _range_ = `short-to-medium` (incomplete) - Poland Evaluates SPYDER Point-Defence System
      > gives Poland a short-to-medium range point defence capability


## Parked fragments (below surfacing bar)

- **incomplete**: 4 claim(s) held in state DB
- **text_field**: 1 claim(s) held in state DB
- **unlinked**: 9 claim(s) held in state DB
- **unmapped**: 5 claim(s) held in state DB
