# Reverse-enrichment report - run 2

*Model:* `google/gemma-3-4b-it`  |  *Docs this run:* 2  |  *LLM calls:* 2  |  *Tokens:* 639 prompt + 322 completion

**7 live proposals** (2 new this run) across 5 records.


## AIM-120 AMRAAM

### New this run

- **Maximum altitude** = `65000 ft` (gap-fill) - corroborated by 2 sources
    - Value distribution across sources: 1 source(s) say `20 km`, 1 source(s) say `65000 ft`
    - Analyst Estimate: AMRAAM Engagement Ceiling (2/2)  (testdocs2\corrob_amraam_ceiling_b.pdf)
      > Separate open-source reporting puts the engagement ceiling of the AIM-120 AMRAAM air–to–air missile at approximately 65,000 ft

### Outstanding from prior runs

- **Maximum altitude** = `20 km` (gap-fill) - corroborated by 2 sources
    - Value distribution across sources: 1 source(s) say `20 km`, 1 source(s) say `65000 ft`
    - Analyst Estimate: AMRAAM Engagement Ceiling (1/2)  (testdocs2\corrob_amraam_ceiling_a.pdf)
      > the AIM-120 20 km ceiling figure


## Eurofighter Typhoon

### Outstanding from prior runs

- **Operated by (country)** = `Kuwait` (gap-fill)
    - Kuwait Declares Eurofighter Typhoon Squadron Operational  (testdocs2\gapfill_typhoon_operator.pdf)
      > Kuwait has declared its first Eurofighter Typhoon squadron operational, becoming a new operator of the multi-role fighter aircraft.


## Lockheed Martin F-35 Lightning II

### Outstanding from prior runs

- **relation**: Lockheed Martin F-35 Lightning II <-> ASRAAM
    - Lockheed Martin and MBDA Confirm F-35 / ASRAAM  (testdocs2\integration_f35_asraam.pdf)
      > The F-35 has been cleared to carry and employ the infrared-guided ASRAAM from its external wing stations


## S-400 Triumf

### Conflicts (DB vs document)

- **Deployment time** - conflict
    - DB value: `5 min`
    - Document value(s): `8 min` x1
    - Qualifier: _convoy halt to active_
    - S-400 Triumf - Consolidated Specification Sheet  (testdocs2\table_s400_spec.pdf)
      > Deploy Time 8 min

### Outstanding from prior runs

- **Maximum altitude** = `30 km` (gap-fill)
    - Qualifier: _assessed maximum_
    - S-400 Triumf - Consolidated Specification Sheet  (testdocs2\table_s400_spec.pdf)
      > Engagement Ceiling 30 km


## Sukhoi Su-57

### New this run

- **Operated by (country)** = `Algeria` (gap-fill)
    - Algeria Reported as Su-57 Export Customer  (testdocs2\gapfill_su57_operator.pdf)
      > Algeria would make it the first foreign operator of the Russian combat aircraft.


## Parked for review (rescue candidates)

*Unmapped/incomplete fragments the mapper could not place - grouped by linked record for human review.*

### 9M96
  - _missiles_ = `9M96` (unmapped) - S-350 Vityaz Medium-Range Air Defence System
      > The S-350 employs the 9M96 family of active-radar missiles

### ASRAAM
  - _range_ = `short-range` (incomplete) - Lockheed Martin and MBDA Confirm F-35 / ASRAAM
      > ASRAAM short-range air-to-air missile
  - _can employ_ = `infrared-guided` (unmapped) - Lockheed Martin and MBDA Confirm F-35 / ASRAAM
      > infrared-guided ASRAAM
  - _guidance_ = `infrared-guided` (unmapped) - Lockheed Martin and MBDA Confirm F-35 / ASRAAM
      > infrared-guided ASRAAM
  - _dogfight missile_ = `high-off-boresight` (unmapped) - Lockheed Martin and MBDA Confirm F-35 / ASRAAM
      > providing operators with a high-off-boresight dogfight missile
  - _complement_ = `beyond-visual-range weapons` (unmapped) - Lockheed Martin and MBDA Confirm F-35 / ASRAAM
      > that complements the beyond-visual-range weapons already carried internally

### Eurofighter Typhoon
  - _mission_ = `air-defence and strike` (unmapped) - Kuwait Declares Eurofighter Typhoon Squadron Operational
      > The Kuwaiti Typhoon fleet will undertake air-defence and strike missions.
  - _strengthens_ = `Kuwait's air combat capability` (unmapped) - Kuwait Declares Eurofighter Typhoon Squadron Operational
      > The introduction of the Eurofighter Typhoon strengthens Kuwait's air combat capability alongside its existing fast-jet fleet.

### Lockheed Martin F-35 Lightning II
  - _capability_ = `within-visual-range` (unmapped) - Lockheed Martin and MBDA Confirm F-35 / ASRAAM
      > marking a significant expansion of the aircraft's within-visual-range capability

### S-300
  - _personnel_ = `200 personnel` (unmapped) - Exercise Report: S-300 Readiness Drill
      > the participating S-300 surface-to-air missile regiment mobilised 200 personnel
  - _readiness posture_ = `250 hours` (unmapped) - Exercise Report: S-300 Readiness Drill
      > sustained a 250–hour continuous readiness posture
  - _sortie generation_ = `300 sorties` (unmapped) - Exercise Report: S-300 Readiness Drill
      > Air activity generated 300 simulated sorties
  - _crew rotation cycle_ = `12 hours` (unmapped) - Exercise Report: S-300 Readiness Drill
      > crew rotations on a 12-hour cycle

### S-400 Triumf
  - _value_ = `2.5 billion USD` (unmapped) - Almaz-Antey Books Multi-Year S-400 Support Contract
      > Russian manufacturer Almaz-Antey has signed a contract reportedly valued at 2.5 billion USD covering the production and multi-year sustainment of S-400 Triumf air defence systems for the Russian armed forces.
  - _delivery period_ = `8 months` (unmapped) - Almaz-Antey Books Multi-Year S-400 Support Contract
      > Under the agreement, 40 launcher vehicles are to be delivered over a period of 8 months
  - _logistics and training package duration_ = `5 years` (unmapped) - Almaz-Antey Books Multi-Year S-400 Support Contract
      > with a follow-on logistics and training package spanning 5 years
  - _programme workforce_ = `1200 personnel` (unmapped) - Almaz-Antey Books Multi-Year S-400 Support Contract
      > Industry sources put the programme workforce at some 1,200 personnel across two production facilities

### Sukhoi Su-57
  - _role_ = `multi-role role` (unmapped) - Algeria Reported as Su-57 Export Customer
      > The reported Su-57 acquisition would expand Algeria's air combat fleet with a low-observable multi-role fighter.
  - _deployment_ = `expand fleet` (unmapped) - Algeria Reported as Su-57 Export Customer
      > The reported Su-57 acquisition would expand Algeria's air combat fleet.


## Parked fragments (below surfacing bar)

- **incomplete**: 1 claim(s) held in state DB
- **text_field**: 1 claim(s) held in state DB
- **uncorroborated**: 1 claim(s) held in state DB
- **unlinked**: 7 claim(s) held in state DB
- **unmapped**: 18 claim(s) held in state DB

  Near-threshold (uncorroborated, awaiting a 2nd source):
  - AIM-120 AMRAAM - Maximum range = `20 km` (1 source(s)) - Analyst Estimate: AMRAAM Engagement Ceiling (1/2)
      > can engage targets at a maximum altitude of 20 km
