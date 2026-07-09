# Temporary online RAG query report: large-test-corpus

Generated: 2026-07-08T16:36:06

Settings:

- DB: `large_eval_test.db`
- Backend: `openrouter`
- Answer model: `qwen3-14b`
- Filter model: `mistral-small-24b`
- k: `10`
- max_context_tokens: `3000`
- Reranker: disabled via `RAGKIT_DISABLE_RERANK=1` to use the stronger large-corpus RRF baseline measured earlier.

## 1. Q1-parametric-exact-code

Purpose: Exact-code parametric lookup; exercises alias pinning + parameter passage retrieval.

Input:

```text
For MX00124, what are its weight and maximum range?
```

Output:

```text
The MX-00124 Air-to-Air-G4 has a **weight** of **601 kg** (launch weight of the munition) and a **maximum range** of **901 km** (maximum effective range of the weapon) [900124].
```

Filter / routing info:

```json
{
  "source": "model",
  "extraction": "ok",
  "applied": {},
  "errors": [
    "field 'name' is free_text, not value-filterable; dropped"
  ],
  "fell_back": false,
  "pinned_parameters_count": 1
}
```

Retrieved contexts:

```json
[
  {
    "rank": 1,
    "rid": "900148",
    "passage": "900148/2",
    "title": "MX-00148 Cruise-F6",
    "excerpt": "Title: MX-00148 Cruise-F6 Parameter Manufacturer [Manufacturer stated value; not independently verified] = Praxis Land Systems (Primary manufacturer or prime contractor) Parameter Type [Estimated from open technical reporting] = Cruise Missile (Primary role cl..."
  },
  {
    "rank": 2,
    "rid": "900093",
    "passage": "900093/3",
    "title": "AX-00093 Bomber-G2",
    "excerpt": "Title: AX-00093 Bomber-G2 Parameter Max takeoff weight [Manufacturer stated value; not independently verified] = 143213 kg (Maximum certified takeoff weight) Parameter Payload [Applies to baseline production lot] = 33754 kg (Maximum external or internal payloa..."
  },
  {
    "rank": 3,
    "rid": "900111",
    "passage": "900111/3",
    "title": "MX-00111 Surface-to-Air-G8",
    "excerpt": "Title: MX-00111 Surface-to-Air-G8 Parameter Weight [Estimated from open technical reporting] = 1315 kg (Launch weight of the munition) Parameter Status [Value varies by installation and support package] = In Development (Current life-cycle status of the system..."
  },
  {
    "rank": 4,
    "rid": "900124",
    "passage": "900124/0",
    "title": "MX-00124 Air-to-Air-G4",
    "excerpt": "Title: MX-00124 Air-to-Air-G4 serviceEntryYear: 1989 systemGroup: Missiles systemType: Air-to-Air Missile primaryEquipCode: MX00124 Overview: MX-00124 Air-to-Air-G4 is a synthetic air-to-air missile record in the Missiles group. It is attributed to Brazil and ..."
  },
  {
    "rank": 5,
    "rid": "900176",
    "passage": "900176/3",
    "title": "MX-00176 Surface-to-Air-E8",
    "excerpt": "Title: MX-00176 Surface-to-Air-E8 Parameter Warhead weight [Manufacturer stated value; not independently verified] = 235 kg (Mass of the warhead) Parameter Launch platform [Estimated from open technical reporting] = truck launcher (Platforms cleared to employ ..."
  },
  {
    "rank": 6,
    "rid": "900085",
    "passage": "900085/2",
    "title": "AX-00085 Fighter-C8",
    "excerpt": "Title: AX-00085 Fighter-C8 Parameter Status = In Production (Current life-cycle status of the system) Parameter Country of origin = Israel (Country in which the system was designed) Parameter Manufacturer = Maritime Systems Bureau (Primary manufacturer or prim..."
  },
  {
    "rank": 7,
    "rid": "900171",
    "passage": "900171/3",
    "title": "MX-00171 Air-to-Air-G7",
    "excerpt": "Title: MX-00171 Air-to-Air-G7 Parameter Warhead weight [Estimated from open technical reporting] = 125 kg (Mass of the warhead)"
  },
  {
    "rank": 8,
    "rid": "900089",
    "passage": "900089/2",
    "title": "AX-00089 UAV-B6",
    "excerpt": "Title: AX-00089 UAV-B6 Parameter Status = In Service (Current life-cycle status of the system) Parameter Country of origin = Sweden (Country in which the system was designed) Parameter Manufacturer = Ardent Missiles (Primary manufacturer or prime contractor) P..."
  },
  {
    "rank": 9,
    "rid": "900039",
    "passage": "900039/3",
    "title": "AX-00039 UAV-D4",
    "excerpt": "Title: AX-00039 UAV-D4 Parameter Max takeoff weight = 243853 kg (Maximum certified takeoff weight)"
  },
  {
    "rank": 10,
    "rid": "900158",
    "passage": "900158/2",
    "title": "MX-00158 Air-to-Air-C7",
    "excerpt": "Title: MX-00158 Air-to-Air-C7 Parameter Warhead weight [Estimated from open technical reporting] = 87 kg (Mass of the warhead) Parameter Maximum altitude [Manufacturer stated value; not independently verified] = 41 km (Maximum intercept altitude)"
  },
  {
    "rank": 11,
    "rid": "900124",
    "passage": "900124/1",
    "title": "MX-00124 Air-to-Air-G4",
    "excerpt": "Title: MX-00124 Air-to-Air-G4 Parameter Status = Retired (Current life-cycle status of the system) Parameter Country of origin = Italy (Country in which the system was designed) Parameter Manufacturer = Ardent Missiles (Primary manufacturer or prime contractor..."
  }
]
```

## 2. Q2-ranked-filter-missiles

Purpose: Ranked filtering over systemGroup, Status, and Maximum range.

Input:

```text
Which in-production missiles have maximum range over 1000 km? Rank them by maximum range.
```

Output:

```text
Here are the in-production missiles with a maximum range over 1000 km, ranked by maximum range (highest to lowest), based on the provided records:

1.  MX-00156 Anti-Ship-G8: 1941 km [900156]
2.  MX-00118 Air-to-Air-F5: 1916 km [900118]
3.  MX-00149 Anti-Ship-H8: 1889 km [900149]
4.  MX-00127 Air-to-Air-G1: 1648 km [900127]
5.  MX-00102 Air-to-Air-G2: 1618 km [900102]
6.  MX-00112 Anti-Ship-G8: 1581 km [900112]
7.  MX-00104 Air-to-Air-D7: 1533 km [900104]
8.  MX-00196 Cruise-B3: 1403 km [900196]
9.  MX-00144 Anti-Ship-B6: 1391 km [900144]
10. MX-00116 Surface-to-Air-D3: 1345 km [900116]
11. MX-00163 Surface-to-Air-B2: 1339 km [900163]
12. MX-00197 Surface-to-Air-C3: 1318 km [900197]
13. MX-00181 Air-to-Air-C7: 1283 km [900181]
14. MX-00173 Cruise-E5: 1240 km [900173]
15. MX-00107 Cruise-D1: 1231 km [900107]
16. MX-00147 Surface-to-Air-A2: 1410 km [900147]
17. MX-00189 Surface-to-Air-H1: 1426 km [900189]
18. MX-00165 Cruise-A6: 591 km / 1635 km [900165] (Note: This missile has two listed ranges, I've included it as it has a naval variant exceeding 1000km)
```

Note: the first attempt with `qwen3-14b` returned an OpenRouter response with `message.content = null`, which currently crashes `llm_provider.chat_with_usage()` because it calls `.strip()` unguarded. The output above is a retry using `gemma3-27b` against the same retrieval/filter path.

Filter / routing info:

```json
{
  "source": "model",
  "extraction": "ok",
  "applied": {
    "systemGroup": {
      "type": "categorical",
      "in": [
        "Missiles"
      ]
    },
    "Status": {
      "type": "categorical",
      "in": [
        "In Production"
      ]
    },
    "Maximum range": {
      "type": "numeric",
      "min": 1000.0
    }
  },
  "errors": [],
  "matched_records": 18,
  "fell_back": false,
  "table_rows": 18,
  "table_columns": [
    "systemGroup",
    "Status",
    "Maximum range",
    "Maximum speed",
    "Combat range",
    "Ferry range"
  ]
}
```

Retrieved contexts:

```json
[
  {"rank": 1, "rid": "900165", "passage": "900165/2", "title": "MX-00165 Cruise-A6"},
  {"rank": 2, "rid": "900189", "passage": "900189/1", "title": "MX-00189 Surface-to-Air-H1"},
  {"rank": 3, "rid": "900107", "passage": "900107/2", "title": "MX-00107 Cruise-D1"},
  {"rank": 4, "rid": "900112", "passage": "900112/2", "title": "MX-00112 Anti-Ship-G8"},
  {"rank": 5, "rid": "900147", "passage": "900147/2", "title": "MX-00147 Surface-to-Air-A2"},
  {"rank": 6, "rid": "900181", "passage": "900181/2", "title": "MX-00181 Air-to-Air-C7"},
  {"rank": 7, "rid": "900163", "passage": "900163/2", "title": "MX-00163 Surface-to-Air-B2"},
  {"rank": 8, "rid": "900127", "passage": "900127/2", "title": "MX-00127 Air-to-Air-G1"},
  {"rank": 9, "rid": "900102", "passage": "900102/2", "title": "MX-00102 Air-to-Air-G2"},
  {"rank": 10, "rid": "900173", "passage": "900173/2", "title": "MX-00173 Cruise-E5"}
]
```

## 3. Q3-child-relation-single

Purpose: Direct child-model retrieval from a known parent-child edge.

Input:

```text
How is SX00377 related to AX00002?
```

Output:

```text
The records provided do not indicate a direct relationship between SX-00377 (Aircraft-A5) and AX-00002 (Maritime-A8). SX-00377 is a synthetic aircraft radar system attributed to Norway, while AX-00002 is a synthetic maritime patrol aircraft attributed to Singapore. Neither record explicitly links them through proliferation, integration, or related systems [900377][900002].
```

Filter / routing info:

```json
{
  "source": "model",
  "extraction": "ok",
  "applied": {
    "Fitted to": {
      "type": "multi_value",
      "contains": [
        "AX-00002 Maritime-A8"
      ]
    }
  },
  "errors": [
    "field 'primaryEquipCode' is free_text, not value-filterable; dropped"
  ],
  "matched_records": 1,
  "fell_back": false,
  "related_count": 3
}
```

Retrieved contexts:

```json
[
  {
    "rank": 1,
    "rid": "900377",
    "passage": "900377/0",
    "title": "SX-00377 Aircraft-A5",
    "excerpt": "Title: SX-00377 Aircraft-A5 serviceEntryYear: 2020 systemGroup: Sensors systemType: Aircraft Radar primaryEquipCode: SX00377 Overview: SX-00377 Aircraft-A5 is a synthetic aircraft radar record in the Sensors group. It is attributed to Norway and is intended fo..."
  },
  {
    "rank": 2,
    "rid": "900337",
    "passage": "900337/0",
    "title": "SX-00337 Electronic-H1",
    "excerpt": "Title: SX-00337 Electronic-H1 serviceEntryYear: 2009 systemGroup: Sensors systemType: Electronic Support Sensor primaryEquipCode: SX00337 Overview: SX-00337 Electronic-H1 is a synthetic electronic support sensor record in the Sensors group. It is attributed to..."
  },
  {
    "rank": 3,
    "rid": "900322",
    "passage": "900322/0",
    "title": "SX-00322 Electronic-C8",
    "excerpt": "Title: SX-00322 Electronic-C8 serviceEntryYear: 2018 systemGroup: Sensors systemType: Electronic Support Sensor primaryEquipCode: SX00322 Overview: SX-00322 Electronic-C8 is a synthetic electronic support sensor record in the Sensors group. It is attributed to..."
  },
  {
    "rank": 4,
    "rid": "900301",
    "passage": "900301/0",
    "title": "SX-00301 Electronic-F2",
    "excerpt": "Title: SX-00301 Electronic-F2 serviceEntryYear: 2018 systemGroup: Sensors systemType: Electronic Support Sensor primaryEquipCode: SX00301 Overview: SX-00301 Electronic-F2 is a synthetic electronic support sensor record in the Sensors group. It is attributed to..."
  },
  {
    "rank": 5,
    "rid": "900002",
    "passage": "900002/0",
    "title": "AX-00002 Maritime-A8",
    "excerpt": "Title: AX-00002 Maritime-A8 serviceEntryYear: 2022 systemGroup: Aircraft systemType: Maritime Patrol Aircraft primaryEquipCode: AX00002 Overview: AX-00002 Maritime-A8 is a synthetic maritime patrol aircraft record in the Aircraft group. It is attributed to Sin..."
  },
  {
    "rank": 6,
    "rid": "900032",
    "passage": "900032/0",
    "title": "AX-00032 Transport-H2",
    "excerpt": "Title: AX-00032 Transport-H2 serviceEntryYear: 2009 systemGroup: Aircraft systemType: Transport Aircraft primaryEquipCode: AX00032 Overview: AX-00032 Transport-H2 is a synthetic transport aircraft record in the Aircraft group. It is attributed to United Arab E..."
  },
  {
    "rank": 7,
    "rid": "900330",
    "passage": "900330/0",
    "title": "SX-00330 Electronic-D6",
    "excerpt": "Title: SX-00330 Electronic-D6 serviceEntryYear: 2006 systemGroup: Sensors systemType: Electronic Support Sensor primaryEquipCode: SX00330 Overview: SX-00330 Electronic-D6 is a synthetic electronic support sensor record in the Sensors group. It is attributed to..."
  },
  {
    "rank": 8,
    "rid": "900569",
    "passage": "900569/0",
    "title": "NVX-00569 Frigate-B3",
    "excerpt": "Title: NVX-00569 Frigate-B3 serviceEntryYear: 1997 systemGroup: Naval Platforms systemType: Frigate primaryEquipCode: NVX00569 Overview: NVX-00569 Frigate-B3 is a synthetic frigate record in the Naval Platforms group. It is attributed to Sweden and is intended..."
  },
  {
    "rank": 9,
    "rid": "900369",
    "passage": "900369/0",
    "title": "SX-00369 Air-B2",
    "excerpt": "Title: SX-00369 Air-B2 serviceEntryYear: 2005 systemGroup: Sensors systemType: Air Defense Radar primaryEquipCode: SX00369 Overview: SX-00369 Air-B2 is a synthetic air defense radar record in the Sensors group. It is attributed to France and is intended for re..."
  },
  {
    "rank": 10,
    "rid": "900036",
    "passage": "900036/0",
    "title": "AX-00036 Transport-E9",
    "excerpt": "Title: AX-00036 Transport-E9 serviceEntryYear: 2015 systemGroup: Aircraft systemType: Transport Aircraft primaryEquipCode: AX00036 Overview: AX-00036 Transport-E9 is a synthetic transport aircraft record in the Aircraft group. It is attributed to Poland and is..."
  }
]
```

## 4. Q4-child-relation-multiple

Purpose: Parent record with multiple fitted child systems.

Input:

```text
What is AX00006 fitted with, and what roles do those child systems have?
```

Output:

```text
AX00006 (Transport-H5) is fitted with two child systems:  
1. **SX-00337 Electronic-H1** [900337]: An *Electronic Support Sensor* designed for detection and surveillance, with a maximum detection range of 428 km and VHF frequency band coverage. Its role is likely electronic warfare support or signal intelligence.  
2. **WCX-00667 Torpedo-G8** [900667]: A *Torpedo* with an effective range of 112 km and unguided propulsion, intended for offensive underwater warfare or anti-submarine operations.  

These systems extend AX00006's capabilities in sensor integration and weaponization.
```

Filter / routing info:

```json
{
  "source": "model",
  "extraction": "ok",
  "applied": {},
  "errors": [
    "Fitted to contains 'AX-00006' not a known element, dropped"
  ],
  "fell_back": false,
  "related_count": 3,
  "pinned_parameters_count": 1
}
```

Retrieved contexts:

```json
[
  {
    "rank": 1,
    "rid": "900006",
    "passage": "900006/0",
    "title": "AX-00006 Transport-H5",
    "excerpt": "Title: AX-00006 Transport-H5 serviceEntryYear: 1986 systemGroup: Aircraft systemType: Transport Aircraft primaryEquipCode: AX00006 Overview: AX-00006 Transport-H5 is a synthetic transport aircraft record in the Aircraft group. It is attributed to South Korea a..."
  },
  {
    "rank": 2,
    "rid": "900032",
    "passage": "900032/0",
    "title": "AX-00032 Transport-H2",
    "excerpt": "Title: AX-00032 Transport-H2 serviceEntryYear: 2009 systemGroup: Aircraft systemType: Transport Aircraft primaryEquipCode: AX00032 Overview: AX-00032 Transport-H2 is a synthetic transport aircraft record in the Aircraft group. It is attributed to United Arab E..."
  },
  {
    "rank": 3,
    "rid": "900089",
    "passage": "900089/0",
    "title": "AX-00089 UAV-B6",
    "excerpt": "Title: AX-00089 UAV-B6 serviceEntryYear: 1985 systemGroup: Aircraft systemType: UAV primaryEquipCode: AX00089 Overview: AX-00089 UAV-B6 is a synthetic uav record in the Aircraft group. It is attributed to United States and is intended for retrieval, filtering,..."
  },
  {
    "rank": 4,
    "rid": "900401",
    "passage": "900401/2",
    "title": "GVX-00401 Armored-H2",
    "excerpt": "Title: GVX-00401 Armored-H2 Parameter Status = In Development (Current life-cycle status of the system) Parameter Country of origin = Singapore (Country in which the system was designed) Parameter Manufacturer = Maritime Systems Bureau (Primary manufacturer or..."
  },
  {
    "rank": 5,
    "rid": "900091",
    "passage": "900091/0",
    "title": "AX-00091 Transport-D9",
    "excerpt": "Title: AX-00091 Transport-D9 serviceEntryYear: 2022 systemGroup: Aircraft systemType: Transport Aircraft primaryEquipCode: AX00091 Overview: AX-00091 Transport-D9 is a synthetic transport aircraft record in the Aircraft group. It is attributed to South Africa ..."
  },
  {
    "rank": 6,
    "rid": "900431",
    "passage": "900431/2",
    "title": "GVX-00431 Armored-D9",
    "excerpt": "Title: GVX-00431 Armored-D9 Parameter Country of origin = India (Country in which the system was designed) Parameter Manufacturer = Praxis Land Systems (Primary manufacturer or prime contractor) Parameter Type = Armored Personnel Carrier (Primary role classifi..."
  },
  {
    "rank": 7,
    "rid": "900086",
    "passage": "900086/0",
    "title": "AX-00086 UAV-E9",
    "excerpt": "Title: AX-00086 UAV-E9 serviceEntryYear: 2011 systemGroup: Aircraft systemType: UAV primaryEquipCode: AX00086 Overview: AX-00086 UAV-E9 is a synthetic uav record in the Aircraft group. It is attributed to South Africa and is intended for retrieval, filtering, ..."
  },
  {
    "rank": 8,
    "rid": "900444",
    "passage": "900444/2",
    "title": "GVX-00444 Armored-G6",
    "excerpt": "Title: GVX-00444 Armored-G6 Parameter Status = In Production (Current life-cycle status of the system) Parameter Country of origin = Russia (Country in which the system was designed) Parameter Manufacturer = Praxis Land Systems (Primary manufacturer or prime c..."
  },
  {
    "rank": 9,
    "rid": "900048",
    "passage": "900048/0",
    "title": "AX-00048 Transport-E6",
    "excerpt": "Title: AX-00048 Transport-E6 serviceEntryYear: 1990 systemGroup: Aircraft systemType: Transport Aircraft primaryEquipCode: AX00048 Overview: AX-00048 Transport-E6 is a synthetic transport aircraft record in the Aircraft group. It is attributed to Australia and..."
  },
  {
    "rank": 10,
    "rid": "900490",
    "passage": "900490/2",
    "title": "GVX-00490 Self-Propelled-A7",
    "excerpt": "Title: GVX-00490 Self-Propelled-A7 Parameter Country of origin = Poland (Country in which the system was designed) Parameter Manufacturer = Northbridge Systems (Primary manufacturer or prime contractor) Parameter Type = Self-Propelled Artillery (Primary role c..."
  },
  {
    "rank": 11,
    "rid": "900006",
    "passage": "900006/2",
    "title": "AX-00006 Transport-H5",
    "excerpt": "Title: AX-00006 Transport-H5 Parameter Status = Retired (Current life-cycle status of the system) Parameter Country of origin = Sweden (Country in which the system was designed) Parameter Manufacturer = Northbridge Systems (Primary manufacturer or prime contra..."
  }
]
```

## 5. Q5-semantic-recall-aircraft

Purpose: Semantic recall and synthesis without exact target IDs.

Input:

```text
Find aircraft with high service ceilings and long combat range; summarize the strongest candidates.
```

Output:

```text
The strongest candidates for aircraft with high service ceilings and long combat ranges are:

1. **AX-00059 Bomber-G2**  
   - Service ceiling: 19,039 m (Maximum operational altitude)  
   - Combat range: 3,439 km (Range on a representative combat mission)  
   *[900059]*  

2. **AX-00006 Transport-H5**  
   - Service ceiling: 15,720 m (Maximum operational altitude)  
   - Combat range: 3,374 km (Range on a representative combat mission)  
   *[900006]*  

3. **AX-00055 Transport-A5 (Naval variant)**  
   - Service ceiling: 15,222 m (Maximum operational altitude)  
   - Combat range: 3,279 km (Range on a representative combat mission)  
   *[900055]*  

**Rationale**: AX-00059 Bomber-G2 leads with the highest service ceiling and a combat range exceeding 3,400 km. AX-00006 Transport-H5 and AX-00055 Transport-A5 (naval variant) follow with notable service ceilings and combat ranges, though slightly lower than AX-00059.
```

Filter / routing info:

```json
{
  "source": "model",
  "extraction": "ok",
  "applied": {
    "systemType": {
      "type": "categorical",
      "in": [
        "Fighter Aircraft",
        "Bomber",
        "Transport Aircraft",
        "Maritime Patrol Aircraft",
        "UAV"
      ]
    },
    "Service ceiling": {
      "type": "numeric",
      "min": 15000.0
    },
    "Combat range": {
      "type": "numeric",
      "min": 3000.0
    }
  },
  "errors": [],
  "matched_records": 5,
  "fell_back": false,
  "table_rows": 5,
  "table_columns": [
    "systemType",
    "Service ceiling",
    "Combat range",
    "Ferry range",
    "Maximum range",
    "Minimum range"
  ]
}
```

Retrieved contexts:

```json
[
  {
    "rank": 1,
    "rid": "900065",
    "passage": "900065/1",
    "title": "AX-00065 Maritime-E7",
    "excerpt": "Title: AX-00065 Maritime-E7 Parameter Status = In Service (Current life-cycle status of the system) Parameter Country of origin = Japan (Country in which the system was designed) Parameter Manufacturer = Vector Propulsion (Primary manufacturer or prime contrac..."
  },
  {
    "rank": 2,
    "rid": "900059",
    "passage": "900059/1",
    "title": "AX-00059 Bomber-G2",
    "excerpt": "Title: AX-00059 Bomber-G2 Also known as: AX00059, AX-00059 Proliferation: South Africa - Using (South African National Defence Force); South Africa - IOC Year 1989 (South African National Defence Force); South Africa - Production (Asterion Defence); Turkey - U..."
  },
  {
    "rank": 3,
    "rid": "900055",
    "passage": "900055/1",
    "title": "AX-00055 Transport-A5",
    "excerpt": "Title: AX-00055 Transport-A5 Parameter Status [Estimated from open technical reporting] = In Production (Current life-cycle status of the system) Parameter Country of origin [Estimated from open technical reporting] = United Kingdom (Country in which the syste..."
  },
  {
    "rank": 4,
    "rid": "900077",
    "passage": "900077/2",
    "title": "AX-00077 Bomber-B8",
    "excerpt": "Title: AX-00077 Bomber-B8 Parameter Crew = 3 crew (Number of crew members)"
  },
  {
    "rank": 5,
    "rid": "900006",
    "passage": "900006/1",
    "title": "AX-00006 Transport-H5",
    "excerpt": "Title: AX-00006 Transport-H5 Also known as: AX00006, AX-00006 Proliferation: South Korea - Using (Republic of Korea Armed Forces); South Korea - IOC Year 1986 (Republic of Korea Armed Forces); South Korea - Production (Maritime Systems Bureau); Japan - Using (..."
  },
  {
    "rank": 6,
    "rid": "900008",
    "passage": "900008/2",
    "title": "AX-00008 Fighter-E8",
    "excerpt": "Title: AX-00008 Fighter-E8 Parameter Status [Estimated from open technical reporting] = In Development (Current life-cycle status of the system) Parameter Country of origin [Applies to baseline production lot] = India (Country in which the system was designed)..."
  },
  {
    "rank": 7,
    "rid": "900070",
    "passage": "900070/2",
    "title": "AX-00070 Fighter-H5",
    "excerpt": "Title: AX-00070 Fighter-H5 Parameter Country of origin [Applies to baseline production lot] = South Korea (Country in which the system was designed) Parameter Manufacturer [Export configuration may differ] = Helios Electronics (Primary manufacturer or prime co..."
  },
  {
    "rank": 8,
    "rid": "900093",
    "passage": "900093/2",
    "title": "AX-00093 Bomber-G2",
    "excerpt": "Title: AX-00093 Bomber-G2 Parameter Status [Export configuration may differ] = In Service (Current life-cycle status of the system) Parameter Country of origin [Value varies by installation and support package] = Japan (Country in which the system was designed..."
  },
  {
    "rank": 9,
    "rid": "900045",
    "passage": "900045/2",
    "title": "AX-00045 Maritime-C5",
    "excerpt": "Title: AX-00045 Maritime-C5 Parameter Status [Manufacturer stated value; not independently verified] = In Service (Current life-cycle status of the system) Parameter Country of origin [Estimated from open technical reporting] = France (Country in which the sys..."
  },
  {
    "rank": 10,
    "rid": "900049",
    "passage": "900049/1",
    "title": "AX-00049 Bomber-F1",
    "excerpt": "Title: AX-00049 Bomber-F1 Also known as: AX00049, AX-00049 Proliferation: China - Using (People's Liberation Army); China - IOC Year 2021 (People's Liberation Army); China - Production (Rheinmetall Atlas); South Africa - Using (South African National Defence F..."
  }
]
```

## 6. Q6-compound-sensor-filter

Purpose: Compound numeric filter over sensor records.

Input:

```text
Which sensors have detection range above 700 km and power consumption under 50 kW?
```

Output:

```text
There are no sensors in the provided context that have a detection range above 700 km and power consumption under 50 kW. The only system with a detection range exceeding 700 km is [900357] SX-00357 Air-E8 (702 km), but its power consumption is 129 kW, which exceeds the 50 kW threshold. All other systems either have insufficient detection ranges or power consumption values above 50 kW.
```

Filter / routing info:

```json
{
  "source": "model",
  "extraction": "ok",
  "applied": {
    "systemGroup": {
      "type": "categorical",
      "in": [
        "Sensors"
      ]
    }
  },
  "errors": [
    "unknown field 'detection range' dropped",
    "unknown field 'power consumption' dropped"
  ],
  "matched_records": 100,
  "fell_back": false
}
```

Retrieved contexts:

```json
[
  {
    "rank": 1,
    "rid": "900362",
    "passage": "900362/1",
    "title": "SX-00362 EO/IR-F2",
    "excerpt": "Title: SX-00362 EO/IR-F2 Parameter Status = In Production (Current life-cycle status of the system) Parameter Country of origin = Norway (Country in which the system was designed) Parameter Manufacturer = Mitsuba Aerospace (Primary manufacturer or prime contra..."
  },
  {
    "rank": 2,
    "rid": "900303",
    "passage": "900303/1",
    "title": "SX-00303 EO/IR-A8",
    "excerpt": "Title: SX-00303 EO/IR-A8 Parameter Status = Retired (Current life-cycle status of the system) Parameter Country of origin = Italy (Country in which the system was designed) Parameter Manufacturer = Rheinmetall Atlas (Primary manufacturer or prime contractor) P..."
  },
  {
    "rank": 3,
    "rid": "900353",
    "passage": "900353/1",
    "title": "SX-00353 EO/IR-E7",
    "excerpt": "Title: SX-00353 EO/IR-E7 Parameter Status = In Development (Current life-cycle status of the system) Parameter Country of origin = United Kingdom (Country in which the system was designed) Parameter Manufacturer = Vector Propulsion (Primary manufacturer or pri..."
  },
  {
    "rank": 4,
    "rid": "900352",
    "passage": "900352/2",
    "title": "SX-00352 Electronic-C8",
    "excerpt": "Title: SX-00352 Electronic-C8 Parameter Status = In Development (Current life-cycle status of the system) Parameter Country of origin = Turkey (Country in which the system was designed) Parameter Manufacturer = Maritime Systems Bureau (Primary manufacturer or ..."
  },
  {
    "rank": 5,
    "rid": "900357",
    "passage": "900357/2",
    "title": "SX-00357 Air-E8",
    "excerpt": "Title: SX-00357 Air-E8 Parameter Status = Retired (Current life-cycle status of the system) Parameter Country of origin = India (Country in which the system was designed) Parameter Manufacturer = Helios Electronics (Primary manufacturer or prime contractor) Pa..."
  },
  {
    "rank": 6,
    "rid": "900312",
    "passage": "900312/1",
    "title": "SX-00312 EO/IR-E8",
    "excerpt": "Title: SX-00312 EO/IR-E8 Parameter Status = In Production (Current life-cycle status of the system) Parameter Country of origin = South Africa (Country in which the system was designed) Parameter Manufacturer = Orion Naval Group (Primary manufacturer or prime ..."
  },
  {
    "rank": 7,
    "rid": "900302",
    "passage": "900302/2",
    "title": "SX-00302 Electronic-F5",
    "excerpt": "Title: SX-00302 Electronic-F5 Parameter Status = In Production (Current life-cycle status of the system) Parameter Country of origin = Norway (Country in which the system was designed) Parameter Manufacturer = Praxis Land Systems (Primary manufacturer or prime..."
  },
  {
    "rank": 8,
    "rid": "900360",
    "passage": "900360/1",
    "title": "SX-00360 EO/IR-D2",
    "excerpt": "Title: SX-00360 EO/IR-D2 Parameter Status = Retired (Current life-cycle status of the system) Parameter Country of origin = Germany (Country in which the system was designed) Parameter Manufacturer = Praxis Land Systems (Primary manufacturer or prime contracto..."
  },
  {
    "rank": 9,
    "rid": "900365",
    "passage": "900365/2",
    "title": "SX-00365 Air-F9",
    "excerpt": "Title: SX-00365 Air-F9 Parameter Status = In Production (Current life-cycle status of the system) Parameter Country of origin = Poland (Country in which the system was designed) Parameter Manufacturer = Praxis Land Systems (Primary manufacturer or prime contra..."
  },
  {
    "rank": 10,
    "rid": "900346",
    "passage": "900346/2",
    "title": "SX-00346 Aircraft-H5",
    "excerpt": "Title: SX-00346 Aircraft-H5 Parameter Status = In Development (Current life-cycle status of the system) Parameter Country of origin = Japan (Country in which the system was designed) Parameter Manufacturer = Rheinmetall Atlas (Primary manufacturer or prime con..."
  }
]
```

## 7. Q7-proliferation-projected

Purpose: Multi-value proliferation filter plus projected fielding facet.

Input:

```text
Which systems operated by India have projected fielding in the next 0 to 5 years?
```

Output:

```text
The systems operated by India with projected fielding in the next 0 to 5 years are:  
1. **SX-00364 Sonar-F4** [9
```

Filter / routing info:

```json
{
  "source": "model",
  "extraction": "ok",
  "applied": {
    "Fielding status": {
      "type": "multi_value",
      "contains": [
        "Projected"
      ]
    },
    "serviceEntryYear": {
      "type": "numeric",
      "min": 2026.0,
      "max": 2031.0
    }
  },
  "errors": [
    "unknown field 'Operated by' dropped"
  ],
  "matched_records": 8,
  "fell_back": false,
  "table_rows": 8,
  "table_columns": [
    "Fielding status",
    "serviceEntryYear",
    "Operated by (country)",
    "systemGroup",
    "systemType",
    "Status"
  ]
}
```

Retrieved contexts:

```json
[
  {
    "rank": 1,
    "rid": "900364",
    "passage": "900364/1",
    "title": "SX-00364 Sonar-F4",
    "excerpt": "Title: SX-00364 Sonar-F4 Also known as: SX00364, SX-00364 Proliferation: India - Using (Indian Armed Forces); India - IOC Year 2026 (Indian Armed Forces); United Kingdom - Using (UK Ministry of Defence); Norway - Using (Norwegian Armed Forces); United States -..."
  },
  {
    "rank": 2,
    "rid": "900375",
    "passage": "900375/1",
    "title": "SX-00375 EO/IR-C7",
    "excerpt": "Title: SX-00375 EO/IR-C7 Also known as: SX00375, SX-00375 Proliferation: South Korea - Using (Republic of Korea Armed Forces); South Korea - IOC Year 2026 (Republic of Korea Armed Forces); United Kingdom - Using (UK Ministry of Defence); India - Using (Indian ..."
  },
  {
    "rank": 3,
    "rid": "900116",
    "passage": "900116/1",
    "title": "MX-00116 Surface-to-Air-D3",
    "excerpt": "Title: MX-00116 Surface-to-Air-D3 Also known as: MX00116, MX-00116 Proliferation: South Africa - Using (South African National Defence Force); South Africa - IOC Year 2026 (South African National Defence Force); Italy - Using (Italian Armed Forces); Japan - Us..."
  },
  {
    "rank": 4,
    "rid": "900275",
    "passage": "900275/1",
    "title": "ADX-00275 C-RAM-E3",
    "excerpt": "Title: ADX-00275 C-RAM-E3 Also known as: ADX00275, ADX-00275 Proliferation: Germany - Using (Bundeswehr); Germany - IOC Year 2026 (Bundeswehr); Germany - Production (Kestrel Dynamics); India - Using (Indian Armed Forces); China - Using (People's Liberation Arm..."
  },
  {
    "rank": 5,
    "rid": "900923",
    "passage": "900923/1",
    "title": "SPX-00923 Communications-D9",
    "excerpt": "Title: SPX-00923 Communications-D9 Also known as: SPX00923, SPX-00923 Proliferation: South Korea - Using (Republic of Korea Armed Forces); South Korea - IOC Year 2026 (Republic of Korea Armed Forces); South Korea - Production (Northbridge Systems); Poland - Us..."
  },
  {
    "rank": 6,
    "rid": "900113",
    "passage": "900113/0",
    "title": "MX-00113 Anti-Ship-F6",
    "excerpt": "Title: MX-00113 Anti-Ship-F6 serviceEntryYear: 2026 systemGroup: Missiles systemType: Anti-Ship Missile primaryEquipCode: MX00113 Overview: MX-00113 Anti-Ship-F6 is a synthetic anti-ship missile record in the Missiles group. It is attributed to Brazil and is i..."
  },
  {
    "rank": 7,
    "rid": "900483",
    "passage": "900483/1",
    "title": "GVX-00483 Armored-E2",
    "excerpt": "Title: GVX-00483 Armored-E2 Also known as: GVX00483, GVX-00483 Proliferation: Israel - Using (Israeli Defense Forces); Israel - IOC Year 2026 (Israeli Defense Forces); Poland - Using (Polish Armed Forces); Brazil - Using (Brazilian Armed Forces); Brazil - Proj..."
  },
  {
    "rank": 8,
    "rid": "900538",
    "passage": "900538/1",
    "title": "NVX-00538 Frigate-E7",
    "excerpt": "Title: NVX-00538 Frigate-E7 Also known as: NVX00538, NVX-00538 Proliferation: Singapore - Using (Singapore Armed Forces); Singapore - IOC Year 2026 (Singapore Armed Forces); United States - Using (US Armed Forces); United States - Projected Fielding (0 - 5 yea..."
  },
  {
    "rank": 9,
    "rid": "900322",
    "passage": "900322/1",
    "title": "SX-00322 Electronic-C8",
    "excerpt": "Title: SX-00322 Electronic-C8 Also known as: SX00322, SX-00322 Proliferation: India - Using (Indian Armed Forces); India - IOC Year 2018 (Indian Armed Forces); Singapore - Using (Singapore Armed Forces); Australia - Using (Australian Defence Force); Australia ..."
  },
  {
    "rank": 10,
    "rid": "900582",
    "passage": "900582/1",
    "title": "NVX-00582 Frigate-F5",
    "excerpt": "Title: NVX-00582 Frigate-F5 Also known as: NVX00582, NVX-00582 Proliferation: India - Using (Indian Armed Forces); India - IOC Year 2015 (Indian Armed Forces); United States - Using (US Armed Forces); Australia - Using (Australian Defence Force); Australia - P..."
  }
]
```

## 8. Q8-negative-missing-record

Purpose: Negative handling for a non-existent designation.

Input:

```text
What is the range of the QZ-999 missile?
```

Output:

```text
I don't have information about the QZ-999 missile in the provided context records. None of the listed missile systems match this designation.
```

Filter / routing info:

```json
{
  "source": "model",
  "extraction": "ok",
  "applied": {
    "systemGroup": {
      "type": "categorical",
      "in": [
        "Missiles"
      ]
    }
  },
  "errors": [
    "field 'name' is free_text, not value-filterable; dropped"
  ],
  "matched_records": 100,
  "fell_back": false
}
```

Retrieved contexts:

```json
[
  {
    "rank": 1,
    "rid": "900180",
    "passage": "900180/2",
    "title": "MX-00180 Anti-Ship-D9",
    "excerpt": "Title: MX-00180 Anti-Ship-D9 Parameter Country of origin = Israel (Country in which the system was designed) Parameter Manufacturer = Vector Propulsion (Primary manufacturer or prime contractor) Parameter Type = Anti-Ship Missile (Primary role classification o..."
  },
  {
    "rank": 2,
    "rid": "900128",
    "passage": "900128/2",
    "title": "MX-00128 Air-to-Air-G7",
    "excerpt": "Title: MX-00128 Air-to-Air-G7 Parameter Status = In Service (Current life-cycle status of the system) Parameter Country of origin = South Africa (Country in which the system was designed) Parameter Manufacturer = Mitsuba Aerospace (Primary manufacturer or prim..."
  },
  {
    "rank": 3,
    "rid": "900112",
    "passage": "900112/2",
    "title": "MX-00112 Anti-Ship-G8",
    "excerpt": "Title: MX-00112 Anti-Ship-G8 Parameter Status = In Production (Current life-cycle status of the system) Parameter Country of origin = United States (Country in which the system was designed) Parameter Manufacturer = Nordic Signal Works (Primary manufacturer or..."
  },
  {
    "rank": 4,
    "rid": "900187",
    "passage": "900187/2",
    "title": "MX-00187 Air-to-Air-A4",
    "excerpt": "Title: MX-00187 Air-to-Air-A4 Parameter Country of origin = Australia (Country in which the system was designed) Parameter Manufacturer = Northbridge Systems (Primary manufacturer or prime contractor) Parameter Type = Air-to-Air Missile (Primary role classific..."
  },
  {
    "rank": 5,
    "rid": "900151",
    "passage": "900151/2",
    "title": "MX-00151 Surface-to-Air-A7",
    "excerpt": "Title: MX-00151 Surface-to-Air-A7 Parameter Status = Retired (Current life-cycle status of the system) Parameter Country of origin = United States (Country in which the system was designed) Parameter Manufacturer = Orion Naval Group (Primary manufacturer or pr..."
  },
  {
    "rank": 6,
    "rid": "900155",
    "passage": "900155/2",
    "title": "MX-00155 Surface-to-Air-F8",
    "excerpt": "Title: MX-00155 Surface-to-Air-F8 Parameter Status = Retired (Current life-cycle status of the system) Parameter Country of origin = Israel (Country in which the system was designed) Parameter Manufacturer = Praxis Land Systems (Primary manufacturer or prime c..."
  },
  {
    "rank": 7,
    "rid": "900198",
    "passage": "900198/1",
    "title": "MX-00198 Anti-Ship-G3",
    "excerpt": "Title: MX-00198 Anti-Ship-G3 Parameter Status = In Service (Current life-cycle status of the system) Parameter Country of origin = France (Country in which the system was designed) Parameter Manufacturer = Helios Electronics (Primary manufacturer or prime cont..."
  },
  {
    "rank": 8,
    "rid": "900184",
    "passage": "900184/2",
    "title": "MX-00184 Surface-to-Air-C3",
    "excerpt": "Title: MX-00184 Surface-to-Air-C3 Parameter Status = In Service (Current life-cycle status of the system) Parameter Country of origin = South Korea (Country in which the system was designed) Parameter Manufacturer = Mitsuba Aerospace (Primary manufacturer or p..."
  },
  {
    "rank": 9,
    "rid": "900186",
    "passage": "900186/1",
    "title": "MX-00186 Air-to-Air-G4",
    "excerpt": "Title: MX-00186 Air-to-Air-G4 Parameter Status = Retired (Current life-cycle status of the system) Parameter Country of origin = Germany (Country in which the system was designed) Parameter Manufacturer = Nordic Signal Works (Primary manufacturer or prime cont..."
  },
  {
    "rank": 10,
    "rid": "900109",
    "passage": "900109/2",
    "title": "MX-00109 Surface-to-Air-A1",
    "excerpt": "Title: MX-00109 Surface-to-Air-A1 Parameter Status [Value varies by installation and support package] = In Service (Current life-cycle status of the system) Parameter Country of origin [Export configuration may differ] = Italy (Country in which the system was ..."
  }
]
```

## Overall read

Summary: useful, but it exposed several concrete issues. The best path succeeded when exact entity pinning and structured tables were available, but relation explanation and filter-field normalization need work.

What worked:

- Q1 exact-code parametric lookup answered correctly: MX00124 weight and maximum range were recovered, and `pinned_parameters_count = 1` shows the same-record parameter passage pathway helped.
- Q5 semantic/ranked aircraft query worked well. The filter model produced a sensible broad aircraft filter plus numeric thresholds, generated a 5-row table, and the answer summarized the strongest candidates coherently.
- Q8 negative query behaved properly: it did not fabricate QZ-999 and said the designation was not in context.
- Q2’s filter path worked: `systemGroup = Missiles`, `Status = In Production`, `Maximum range >= 1000 km`, 18 matched records, and an analytic table was built.

What was weak or failed:

- Q2 exposed an OpenRouter/transport robustness bug: `qwen3-14b` returned `message.content = null`, and `llm_provider.chat_with_usage()` crashed on `.strip()`. The retry with `gemma3-27b` succeeded. The adapter should explicitly handle null content and surface the provider finish reason/raw choice.
- Q2 answer quality was imperfect even after retry: the model listed all relevant rows, but the final ordering had two out-of-order rows and one multi-variant range row was awkwardly placed. For ranked table answers, we should consider rendering the table already sorted and telling the model to preserve row order exactly.
- Q3 direct child relation retrieval was poor. The system retrieved both AX00002 and SX00377 and had a validated `Fitted to` filter, but the answer incorrectly said no direct relation existed. This suggests the relation evidence is not explicit enough in the final prompt, or the model is not being told that the related-systems block/table is authoritative.
- Q4 multi-child relation retrieval was better, but incomplete: AX00006 has three relation edges in the DB; the answer only discussed two child systems. Relation queries need a deterministic relation table/block rather than relying on prose synthesis from retrieved passages.
- Q6 filter extraction failed on field-name casing: the model emitted lowercase `detection range` and `power consumption`; validation dropped both as unknown fields. The answer still reasoned from retrieved passages, but the hard filter did not run. We need fuzzy/normalized field-name mapping, not just label mapping.
- Q7 was mixed. A retry produced the right structured filter (`Operated by (country)` + `Fielding status` + `serviceEntryYear`) and 4 matched records, but the generated answer truncated mid-sentence. The first run also showed that the filter model may emit `Operated by` instead of the canonical `Operated by (country)`, another field-name aliasing gap.

Most important fixes suggested by this run:

1. Add field-name alias/fuzzy mapping in `validate_filter()` or immediately before it. This would fix `detection range`, `power consumption`, and `Operated by`.
2. Make relation answers deterministic: when a query names two related records or asks "fitted with", render an explicit relation table with parent, child, relation type, and component.
3. Harden `llm_provider.chat_with_usage()` for null/empty provider content.
4. For analytic/ranked table answers, sort the table deterministically before prompt assembly and add a stronger "preserve this row order" instruction.
5. Keep exact-code pin finalization on the backlog: Q1 worked because the parameter passage was appended, but the target record was still only rank 4 and appeared again as an 11th context entry.
