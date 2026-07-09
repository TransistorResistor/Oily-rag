#!/usr/bin/env python3
"""
Generate a large synthetic-but-realistic pages_schema-style corpus.

Output defaults to large-test-corpus/ with 1,000 JSON records. The corpus is
designed for RAG/enrichment scale testing, not factual evaluation:

- exactly 10 systemGroup values;
- each group has 3-5 grounded systemType values;
- each group owns a common parameter dictionary capped at 30 parameters;
- each record fills a variable subset: sparse records have 4-5 parameters,
  dense records have up to 25;
- 4-5 core parameters per group are populated for ~90% of records;
- ~20% of records include row-specific comments and/or parameterSubTitle values;
- proliferations include IOC Year rows and Projected Fielding "0 - 5 years";
- cross-group parent/child relations link platforms, weapons, sensors,
  propulsion systems, C4ISR nodes, and reusable subcomponents.

All random choices are deterministic by default. The generated system-specific
values are invented but kept within plausible engineering ranges.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


DEFAULT_OUT = Path("large-test-corpus")
DEFAULT_COUNT = 1000
DEFAULT_SEED = 20260708


COUNTRIES = [
    ("United States", "North America", "US Armed Forces"),
    ("United Kingdom", "Europe", "UK Ministry of Defence"),
    ("France", "Europe", "French Armed Forces"),
    ("Germany", "Europe", "Bundeswehr"),
    ("Italy", "Europe", "Italian Armed Forces"),
    ("Norway", "Europe", "Norwegian Armed Forces"),
    ("Sweden", "Europe", "Swedish Armed Forces"),
    ("Poland", "Europe", "Polish Armed Forces"),
    ("Turkey", "Europe, Asia", "Turkish Armed Forces"),
    ("Israel", "Middle East", "Israeli Defense Forces"),
    ("India", "South Asia", "Indian Armed Forces"),
    ("Japan", "Asia-Pacific", "Japan Self-Defense Forces"),
    ("South Korea", "Asia-Pacific", "Republic of Korea Armed Forces"),
    ("Australia", "Oceania", "Australian Defence Force"),
    ("Singapore", "Asia-Pacific", "Singapore Armed Forces"),
    ("Brazil", "South America", "Brazilian Armed Forces"),
    ("Russia", "Europe, Asia", "Russian Armed Forces"),
    ("China", "Asia", "People's Liberation Army"),
    ("South Africa", "Africa", "South African National Defence Force"),
    ("United Arab Emirates", "Middle East", "UAE Armed Forces"),
]

MANUFACTURERS = [
    "Asterion Defence", "Northbridge Systems", "Kestrel Dynamics",
    "Rheinmetall Atlas", "Mitsuba Aerospace", "Nordic Signal Works",
    "Orion Naval Group", "Helios Electronics", "Praxis Land Systems",
    "Vector Propulsion", "Maritime Systems Bureau", "Ardent Missiles",
]

STATUS_VALUES = ["In Service", "In Production", "In Development", "Retired"]


@dataclass(frozen=True)
class ParamSpec:
    name: str
    unit: str
    dtype: str
    component: str
    descr: str
    lo: float | None = None
    hi: float | None = None
    values: tuple[str, ...] = ()


def nrange(name, unit, component, descr, lo, hi):
    return ParamSpec(name, unit, "Number", component, descr, lo, hi)


def lov(name, component, descr, values):
    return ParamSpec(name, "", "LOV", component, descr, values=tuple(values))


def text(name, component, descr, values):
    return ParamSpec(name, "", "Text", component, descr, values=tuple(values))


COMMON_PARAMS = [
    lov("Status", "General", "Current life-cycle status of the system", STATUS_VALUES),
    lov("Country of origin", "General", "Country in which the system was designed",
        [c[0] for c in COUNTRIES]),
    text("Manufacturer", "General", "Primary manufacturer or prime contractor",
         MANUFACTURERS),
    lov("Type", "General", "Primary role classification of the system", ()),
]


GROUPS: dict[str, dict] = {
    "Aircraft": {
        "types": ["Fighter Aircraft", "Bomber", "UAV", "Maritime Patrol Aircraft", "Transport Aircraft"],
        "prefix": "AX",
        "core": ["Length", "Maximum speed", "Service ceiling", "Combat range", "Unit cost"],
        "params": [
            nrange("Length", "m", "Dimensions", "Overall length of the aircraft", 7, 55),
            nrange("Wingspan", "m", "Dimensions", "Distance between the wingtips", 8, 70),
            nrange("Height", "m", "Dimensions", "Overall height", 2, 18),
            nrange("Empty weight", "kg", "Mass", "Weight without fuel, payload or crew", 2000, 120000),
            nrange("Max takeoff weight", "kg", "Mass", "Maximum certified takeoff weight", 7000, 380000),
            nrange("Maximum speed", "Mach", "Performance", "Maximum speed at altitude", 0.45, 2.6),
            nrange("Cruise speed", "km/h", "Performance", "Economical cruise speed", 250, 1050),
            nrange("Combat range", "km", "Performance", "Range on a representative combat mission", 300, 4500),
            nrange("Ferry range", "km", "Performance", "Maximum unrefuelled transfer range", 900, 14000),
            nrange("Service ceiling", "m", "Performance", "Maximum operational altitude", 6000, 21000),
            nrange("Payload", "kg", "Payload", "Maximum external or internal payload", 300, 45000),
            nrange("Crew", "crew", "General", "Number of crew members", 0, 7),
            text("Powerplant", "Propulsion", "Engine type, model and quantity",
                 ["turbofan", "turboprop", "diesel-electric hybrid", "piston engine"]),
            text("Radar", "Avionics", "Fire-control or surveillance radar designation",
                 ["AESA fire-control radar", "maritime search radar", "synthetic aperture radar"]),
            text("Datalink", "Avionics", "Primary tactical datalink",
                 ["Link-16 compatible", "national tactical data link", "satellite relay"]),
            nrange("Unit cost", "USD", "Cost", "Approximate unit flyaway cost", 2_000_000, 260_000_000),
        ],
    },
    "Missiles": {
        "types": ["Air-to-Air Missile", "Surface-to-Air Missile", "Anti-Ship Missile", "Cruise Missile"],
        "prefix": "MX",
        "core": ["Length", "Weight", "Maximum range", "Maximum speed", "Guidance system"],
        "params": [
            nrange("Length", "m", "Dimensions", "Overall missile length", 1.2, 9.0),
            nrange("Diameter", "m", "Dimensions", "Body diameter", 0.08, 0.9),
            nrange("Weight", "kg", "Mass", "Launch weight of the munition", 12, 3200),
            nrange("Warhead weight", "kg", "Warhead", "Mass of the warhead", 3, 650),
            nrange("Maximum range", "km", "Performance", "Maximum effective range of the weapon", 5, 2500),
            nrange("Minimum range", "km", "Performance", "Minimum effective engagement range", 0.2, 40),
            nrange("Maximum speed", "Mach", "Performance", "Maximum speed of the missile", 0.7, 5.5),
            nrange("Maximum altitude", "km", "Performance", "Maximum intercept altitude", 1, 45),
            text("Guidance system", "Guidance", "Guidance method employed across flight phases",
                 ["active radar homing", "imaging infrared seeker", "inertial navigation with datalink",
                  "terrain-contour matching", "semi-active radar homing"]),
            lov("Propellant", "Propulsion", "Class of propulsion system fitted",
                ["Solid-fuel rocket motor", "Turbojet", "Ramjet", "Dual-pulse rocket motor"]),
            text("Launch platform", "Integration", "Platforms cleared to employ this weapon",
                 ["fighter aircraft", "vertical launch cell", "truck launcher", "naval canister launcher"]),
            nrange("Unit cost", "USD", "Cost", "Approximate unit cost", 50_000, 6_000_000),
        ],
    },
    "Air Defense Systems": {
        "types": ["Long-Range SAM System", "Medium-Range SAM System", "SHORAD System", "C-RAM System"],
        "prefix": "ADX",
        "core": ["Maximum range", "Radar detection range", "Deployment time", "Simultaneous targets engaged"],
        "params": [
            nrange("Maximum range", "km", "Performance", "Maximum engagement range", 5, 450),
            nrange("Radar detection range", "km", "Sensors", "Maximum radar detection range", 20, 750),
            nrange("Maximum altitude", "km", "Performance", "Maximum engagement altitude", 1, 45),
            nrange("Deployment time", "min", "Operations", "Time required to emplace and become operational", 3, 60),
            nrange("Simultaneous targets tracked", "targets", "Fire Control", "Number of targets tracked at once", 8, 300),
            nrange("Simultaneous targets engaged", "targets", "Fire Control", "Number of targets engaged at once", 2, 48),
            text("Missiles employed", "Armament", "Interceptor missiles used by the system",
                 ["short-range missile", "medium-range missile", "long-range missile", "mixed missile battery"]),
            text("Radar type", "Sensors", "Primary search or engagement radar type",
                 ["AESA surveillance radar", "PESA engagement radar", "rotating 3D radar"]),
            text("Launcher configuration", "Launcher", "Launcher vehicle or cell arrangement",
                 ["truck-mounted canisters", "vertical launch cells", "towed pedestal launcher"]),
            nrange("Battery vehicles", "vehicles", "Logistics", "Typical number of major vehicles per battery", 2, 18),
            nrange("System unit cost", "USD", "Cost", "Approximate battery cost", 5_000_000, 900_000_000),
        ],
    },
    "Sensors": {
        "types": ["Aircraft Radar", "Air Defense Radar", "EO/IR Sensor", "Sonar", "Electronic Support Sensor"],
        "prefix": "SX",
        "core": ["Detection range", "Frequency band", "Coverage", "Simultaneous targets tracked"],
        "params": [
            nrange("Detection range", "km", "Performance", "Maximum detection range", 5, 900),
            nrange("Instrumented range", "km", "Performance", "Maximum instrumented range", 10, 1200),
            lov("Frequency band", "RF", "Operating frequency band", ["L-band", "S-band", "C-band", "X-band", "Ku-band", "VHF"]),
            nrange("Coverage", "deg", "Antenna", "Azimuth coverage", 60, 360),
            nrange("Elevation coverage", "deg", "Antenna", "Elevation coverage", 20, 120),
            nrange("Simultaneous targets tracked", "targets", "Processing", "Targets tracked concurrently", 8, 1200),
            text("Antenna type", "Antenna", "Antenna architecture",
                 ["AESA", "PESA", "mechanically scanned array", "staring EO/IR turret", "towed array"]),
            text("Operating modes", "Processing", "Principal operating modes",
                 ["track-while-scan", "SAR mapping", "GMTI", "passive detection", "low-altitude search"]),
            nrange("Power consumption", "kW", "Power", "Electrical power consumption", 0.5, 250),
            nrange("Total weight", "kg", "Mass", "Installed system weight", 15, 12000),
            nrange("Unit cost", "USD", "Cost", "Approximate unit cost", 100_000, 80_000_000),
        ],
    },
    "Ground Vehicles": {
        "types": ["Main Battle Tank", "Infantry Fighting Vehicle", "Armored Personnel Carrier", "Self-Propelled Artillery"],
        "prefix": "GVX",
        "core": ["Combat weight", "Road speed", "Operational range", "Main armament"],
        "params": [
            nrange("Combat weight", "kg", "Mass", "Loaded combat weight", 8000, 72000),
            nrange("Length", "m", "Dimensions", "Hull length", 4, 12),
            nrange("Width", "m", "Dimensions", "Overall width", 2, 4.5),
            nrange("Height", "m", "Dimensions", "Overall height", 1.6, 4.0),
            nrange("Crew", "crew", "General", "Number of crew members", 2, 6),
            nrange("Passengers", "personnel", "Payload", "Dismounts carried", 0, 14),
            nrange("Engine power", "hp", "Propulsion", "Engine output", 250, 1800),
            nrange("Road speed", "km/h", "Performance", "Maximum road speed", 45, 115),
            nrange("Operational range", "km", "Performance", "Road range on internal fuel", 180, 900),
            text("Main armament", "Armament", "Primary weapon fitted",
                 ["120 mm smoothbore gun", "30 mm autocannon", "155 mm howitzer", "heavy machine gun"]),
            nrange("Armor equivalent", "mm RHAe", "Protection", "Approximate frontal protection equivalent", 20, 950),
            nrange("Unit cost", "USD", "Cost", "Approximate vehicle unit cost", 350_000, 18_000_000),
        ],
    },
    "Naval Platforms": {
        "types": ["Frigate", "Destroyer", "Submarine", "Patrol Vessel", "Aircraft Carrier"],
        "prefix": "NVX",
        "core": ["Displacement", "Length", "Maximum sea speed", "Operational range", "Crew"],
        "params": [
            nrange("Displacement", "t", "Dimensions", "Full-load displacement", 250, 105000),
            nrange("Length", "m", "Dimensions", "Overall ship length", 30, 335),
            nrange("Beam", "m", "Dimensions", "Maximum beam", 6, 80),
            nrange("Draft", "m", "Dimensions", "Draft at full load", 1.2, 13),
            nrange("Maximum sea speed", "kn", "Performance", "Maximum ship speed", 18, 35),
            nrange("Operational range", "nmi", "Performance", "Range at economical speed", 800, 12000),
            nrange("Crew", "crew", "General", "Crew complement", 12, 5200),
            nrange("Vertical launch cells", "cells", "Armament", "Number of VLS cells", 0, 128),
            text("Main gun", "Armament", "Principal gun armament",
                 ["57 mm naval gun", "76 mm naval gun", "127 mm naval gun", "none"]),
            text("Radar suite", "Sensors", "Primary radar suite",
                 ["multifunction AESA radar", "3D air-search radar", "navigation radar set"]),
            nrange("Unit cost", "USD", "Cost", "Approximate ship cost", 25_000_000, 13_000_000_000),
        ],
    },
    "Weapons and Components": {
        "types": ["Warhead", "Proximity Fuze", "Naval Gun", "Torpedo", "Rocket Artillery"],
        "prefix": "WCX",
        "core": ["Weight", "Diameter", "Effective range", "Unit cost"],
        "params": [
            nrange("Weight", "kg", "Mass", "Component or munition weight", 1, 2500),
            nrange("Length", "m", "Dimensions", "Overall length", 0.1, 8),
            nrange("Diameter", "m", "Dimensions", "Body or calibre diameter", 0.02, 0.65),
            nrange("Explosive mass", "kg", "Warhead", "Explosive fill mass", 0.2, 450),
            nrange("Effective range", "km", "Performance", "Effective range", 0.5, 180),
            nrange("Muzzle velocity", "m/s", "Performance", "Projectile muzzle velocity", 150, 1800),
            text("Fuzing mode", "Warhead", "Fuzing or initiation mode",
                 ["proximity", "contact", "programmable airburst", "delayed impact"]),
            lov("Guidance", "Guidance", "Guidance class", ["Unguided", "Laser", "GPS/INS", "Acoustic", "Active radar"]),
            nrange("Unit cost", "USD", "Cost", "Approximate unit cost", 500, 2_500_000),
        ],
    },
    "Power and Propulsion Systems": {
        "types": ["Turbofan Engine", "Diesel Engine", "Gas Turbine", "Rocket Motor"],
        "prefix": "PPX",
        "core": ["Maximum thrust", "Dry weight", "Length", "Diameter"],
        "params": [
            nrange("Maximum thrust", "kN", "Performance", "Maximum rated thrust", 5, 250),
            nrange("Dry weight", "kg", "Mass", "Dry installed weight", 80, 9500),
            nrange("Length", "m", "Dimensions", "Overall length", 0.8, 9),
            nrange("Diameter", "m", "Dimensions", "Maximum diameter", 0.25, 3.5),
            nrange("Power output", "kW", "Performance", "Shaft or electrical power output", 50, 45000),
            nrange("Specific fuel consumption", "kg/(N*s)", "Performance", "Specific fuel consumption", 0.00001, 0.002),
            nrange("Bypass ratio", "ratio", "Performance", "Engine bypass ratio", 0.2, 12),
            nrange("Operating temperature high", "degC", "Environment", "Maximum operating temperature", 45, 80),
            nrange("Operating temperature low", "degC", "Environment", "Minimum operating temperature", -60, -20),
            text("Control system", "Controls", "Engine control system",
                 ["FADEC", "hydromechanical governor", "digital turbine controller"]),
            nrange("Unit cost", "USD", "Cost", "Approximate unit cost", 80_000, 45_000_000),
        ],
    },
    "C4ISR and Electronic Warfare": {
        "types": ["Battle Management System", "Data Link", "Jammer", "Communications System"],
        "prefix": "CEX",
        "core": ["Node capacity", "Effective range", "Bandwidth", "Power consumption"],
        "params": [
            nrange("Node capacity", "nodes", "Networking", "Maximum network participants", 8, 3000),
            nrange("Effective range", "km", "Networking", "Operational communications or jamming range", 5, 1200),
            nrange("Bandwidth", "Mbps", "Networking", "Nominal data throughput", 0.05, 2000),
            nrange("Frequency low", "MHz", "RF", "Lower operating frequency", 1, 8000),
            nrange("Frequency high", "MHz", "RF", "Upper operating frequency", 30, 40000),
            nrange("Power consumption", "kW", "Power", "Electrical power consumption", 0.1, 500),
            text("Encryption", "Security", "Encryption or waveform security mode",
                 ["AES-class encryption", "frequency hopping", "national waveform", "line-of-sight relay"]),
            text("Operating modes", "Operations", "Principal operating modes",
                 ["network relay", "command post gateway", "stand-in jamming", "satellite backhaul"]),
            nrange("Unit cost", "USD", "Cost", "Approximate unit cost", 50_000, 120_000_000),
        ],
    },
    "Space Systems": {
        "types": ["Reconnaissance Satellite", "Navigation Satellite", "Communications Satellite", "Launch Vehicle"],
        "prefix": "SPX",
        "core": ["Launch mass", "Orbital altitude", "Design life", "Payload power"],
        "params": [
            nrange("Launch mass", "kg", "Mass", "Mass at launch", 50, 24000),
            nrange("Dry mass", "kg", "Mass", "Mass excluding propellant", 30, 19000),
            nrange("Orbital altitude", "km", "Orbit", "Representative operating altitude", 300, 35786),
            nrange("Inclination", "deg", "Orbit", "Orbital inclination", 0, 98),
            nrange("Design life", "years", "Operations", "Planned operational life", 2, 18),
            nrange("Payload power", "kW", "Power", "Payload electrical power", 0.2, 35),
            nrange("Downlink rate", "Mbps", "Communications", "Maximum downlink data rate", 1, 12000),
            nrange("Revisit time", "h", "Operations", "Nominal revisit interval", 1, 96),
            text("Sensor payload", "Payload", "Primary payload type",
                 ["SAR radar", "electro-optical imager", "signals intelligence package", "communications repeater"]),
            nrange("Unit cost", "USD", "Cost", "Approximate unit or launch cost", 5_000_000, 650_000_000),
        ],
    },
}


def slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
    return s or "record"


def fmt_num(x: float, unit: str) -> str:
    if unit in {"USD", "kg", "m", "km", "kW", "targets", "crew", "nodes", "cells", "vehicles"}:
        return str(int(round(x)))
    if abs(x) >= 100:
        return str(round(x, 1))
    if abs(x) >= 10:
        return str(round(x, 2))
    return str(round(x, 3))


def value_for(rng: random.Random, spec: ParamSpec, system_type: str) -> str:
    if spec.dtype in {"LOV", "Text"}:
        if spec.name == "Type":
            return system_type
        return rng.choice(spec.values)
    assert spec.lo is not None and spec.hi is not None
    raw = rng.triangular(spec.lo, spec.hi, (spec.lo + spec.hi) / 2)
    if spec.name in {"Crew", "Passengers", "Vertical launch cells", "Node capacity",
                     "Simultaneous targets tracked", "Simultaneous targets engaged",
                     "Battery vehicles"}:
        raw = round(raw)
    return fmt_num(raw, spec.unit)


def make_param(seq: int, spec: ParamSpec, value: str, rng: random.Random,
               with_comment: bool, variant: int | None = None) -> dict:
    row = {
        "seq": seq,
        "parameterOnly": spec.name,
        "parameter": spec.name if variant is None else f"{spec.name} - {variant}",
        "parameterValue": value,
        "uom": spec.unit,
        "parameterDescr": spec.descr,
        "dataType": spec.dtype,
        "component": spec.component,
    }
    if spec.unit:
        row["parameterUomValue"] = f"{value} {spec.unit}"
    if variant is not None:
        row["parameterSubTitle"] = rng.choice(
            ["Block I", "Block II", "Export configuration", "Naval variant",
             "High-performance setting", "Standard configuration"])
    if with_comment:
        row["comments"] = rng.choice([
            "Manufacturer stated value; not independently verified",
            "Export configuration may differ",
            "Applies to baseline production lot",
            "Estimated from open technical reporting",
            "Value varies by installation and support package",
        ])
    return row


def choose_param_specs(rng: random.Random, group_info: dict) -> list[ParamSpec]:
    params_by_name = {p.name: p for p in COMMON_PARAMS + group_info["params"]}
    core_names = list(group_info["core"])
    chosen: list[ParamSpec] = []
    for name in ["Status", "Country of origin", "Manufacturer", "Type"] + core_names:
        if name in params_by_name and rng.random() < 0.90:
            chosen.append(params_by_name[name])

    density = rng.random()
    if density < 0.25:
        target = rng.randint(4, 6)
    elif density < 0.75:
        target = rng.randint(8, 16)
    else:
        target = rng.randint(17, 25)
    remaining = [p for p in COMMON_PARAMS + group_info["params"] if p not in chosen]
    rng.shuffle(remaining)
    chosen.extend(remaining[:max(0, target - len(chosen))])
    return chosen[:25]


def make_proliferations(rng: random.Random, origin: tuple[str, str, str], year: int) -> list[dict]:
    country, region, org = origin
    rows = [
        {"country": country, "proliferation": "Using", "region": region, "organization": org},
        {"country": country, "type": "IOC Year", "proliferation": year, "region": region, "organization": org},
    ]
    if rng.random() < 0.65:
        rows.append({
            "country": country,
            "proliferation": "Production",
            "region": region,
            "organization": rng.choice(MANUFACTURERS),
        })
    for ctry, reg, mil in rng.sample(COUNTRIES, rng.randint(0, 4)):
        if ctry == country:
            continue
        rows.append({"country": ctry, "proliferation": "Using", "region": reg, "organization": mil})
        if rng.random() < 0.18:
            rows.append({
                "country": ctry,
                "type": "Projected Fielding",
                "proliferation": "0 - 5 years",
                "region": reg,
                "organization": mil,
            })
    return rows


def make_description(model: str, group: str, stype: str, origin: str) -> list[dict]:
    return [
        {
            "descrType": "Overview",
            "description": (
                f"{model} is a synthetic {stype.lower()} record in the {group} group. "
                f"It is attributed to {origin} and is intended for retrieval, filtering, "
                "proliferation, and relationship-scale testing. Values are plausible "
                "dummy engineering figures, not real-world intelligence."
            ),
            "shortDescription": f"Synthetic {stype.lower()} fixture for large corpus testing.",
            "classification": "U",
        },
        {
            "descrType": "Integration",
            "description": (
                f"{model} uses a pages-schema style record with typed parametrics, IOC-year "
                "proliferation rows, and optional parent-child integration edges. "
                "Some rows deliberately include comments or subtitles to exercise variant handling."
            ),
            "classification": "U",
        },
    ]


def add_relation(records_by_group: dict[str, list[dict]], rec: dict, rng: random.Random) -> None:
    group = rec["systemGroup"]
    targets: list[dict] = []
    relation_type = "Fitted with"
    component = "Integration"

    def from_groups(*groups: str) -> list[dict]:
        out: list[dict] = []
        for g in groups:
            out.extend(records_by_group.get(g, []))
        return out

    if group in {"Aircraft", "Ground Vehicles", "Naval Platforms"}:
        targets = from_groups("Missiles", "Sensors", "Power and Propulsion Systems",
                              "C4ISR and Electronic Warfare", "Weapons and Components")
        component = rng.choice(["Armament", "Sensors", "Propulsion", "Mission Systems"])
    elif group == "Air Defense Systems":
        targets = from_groups("Missiles", "Sensors", "C4ISR and Electronic Warfare")
        component = rng.choice(["Interceptor", "Radar", "Command Post"])
    elif group == "Missiles":
        targets = from_groups("Weapons and Components", "Sensors", "Power and Propulsion Systems")
        component = rng.choice(["Warhead", "Seeker", "Propulsion"])
    elif group == "Sensors":
        targets = from_groups("C4ISR and Electronic Warfare")
        component = "Data Link"
    elif group == "Space Systems":
        targets = from_groups("Sensors", "C4ISR and Electronic Warfare", "Power and Propulsion Systems")
        component = rng.choice(["Payload", "Communications", "Propulsion"])
    else:
        targets = []

    if not targets:
        return
    n_edges = 1 if rng.random() < 0.75 else rng.randint(2, 4)
    chosen = rng.sample(targets, min(n_edges, len(targets)))
    for child in chosen:
        rec.setdefault("relations", []).append({
            "childModelID": child["modelID"],
            "childModel": child["nomenclature"],
            "parentModelID": rec["modelID"],
            "parentModel": rec["nomenclature"],
            "parentComponent": component,
            "relationType": relation_type,
            "childSystemGroup": child["systemGroup"],
            "childSystemType": child["systemType"],
            "childPrimaryEquipCode": child["primaryEquipCode"],
        })


def generate_records(count: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    group_names = list(GROUPS)
    records: list[dict] = []
    base_per_group = count // len(group_names)
    extra = count % len(group_names)
    model_id = 900000

    for gi, group in enumerate(group_names):
        group_info = GROUPS[group]
        n_group = base_per_group + (1 if gi < extra else 0)
        for i in range(n_group):
            model_id += 1
            stype = rng.choice(group_info["types"])
            origin = rng.choice(COUNTRIES)
            prefix = group_info["prefix"]
            model = f"{prefix}-{model_id % 100000:05d} {stype.split()[0]}-{rng.choice('ABCDEFGH')}{rng.randint(1, 9)}"
            equip = f"{prefix}{model_id % 100000:05d}"
            year = rng.randint(1980, 2026)
            comment_record = rng.random() < 0.20
            specs = choose_param_specs(rng, group_info)
            parametrics = []
            seq = 1000
            for spec in specs:
                seq += 1
                value = value_for(rng, spec, stype)
                variant = None
                # Add occasional variant rows for real duplicate-parameter coverage.
                if comment_record and spec.dtype == "Number" and rng.random() < 0.18:
                    variant = 1
                    parametrics.append(make_param(seq, spec, value, rng, comment_record, variant=variant))
                    seq += 1
                    v2 = value_for(rng, spec, stype)
                    parametrics.append(make_param(seq, spec, v2, rng, comment_record, variant=2))
                else:
                    parametrics.append(make_param(seq, spec, value, rng, comment_record))

            rec = {
                "modelID": model_id,
                "releaseID": 1,
                "name": model,
                "systemGroup": group,
                "systemType": stype,
                "nomenclature": model,
                "primaryEquipCode": equip,
                "reviewDate": "2026-07-08",
                "createdDate": f"{rng.randint(2000, 2025)}-01-01",
                "updatedDate": f"2026-{rng.randint(1, 7):02d}-{rng.randint(1, 28):02d}",
                "versionDate": "2026-07-08",
                "productLink": f"https://example.invalid/synthetic/{model_id}",
                "aliasList": f"{equip}, {model.split()[0]}",
                "aliases": [
                    {"alias": equip, "type": "Primary Code", "urls": None},
                    {"alias": model.split()[0], "type": "Short Name", "urls": None},
                ],
                "codes": [{"code": equip, "isPrimary": "Y"}],
                "descriptions": make_description(model, group, stype, origin[0]),
                "proliferations": make_proliferations(rng, origin, year),
                "media": [
                    {
                        "seq": 1,
                        "mediaID": model_id * 10 + 1,
                        "url": f"https://example.invalid/images/{model_id}.png",
                        "mimeType": "image/png",
                        "type": "MEDIA",
                        "title": f"{model} reference image",
                    },
                    {
                        "seq": 2,
                        "mediaID": model_id * 10 + 2,
                        "url": f"https://example.invalid/docs/{model_id}.pdf",
                        "mimeType": "application/pdf",
                        "type": "MEDIA",
                        "title": f"{model} technical summary",
                        "caption": "Synthetic PDF media row for indexing-pipeline tests",
                    },
                ],
                "parametrics": parametrics,
                "parametricUrls": [],
                "relations": [],
            }
            records.append(rec)

    records_by_group: dict[str, list[dict]] = {}
    for rec in records:
        records_by_group.setdefault(rec["systemGroup"], []).append(rec)

    # Add enough edges for useful relation tests while keeping many no-relation
    # records, and naturally creating shared child components.
    for rec in records:
        if rng.random() < 0.62:
            add_relation(records_by_group, rec, rng)

    return records


def write_records(records: list[dict], out_dir: Path, clean: bool) -> None:
    if clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for rec in records:
        filename = f"{rec['modelID']}_{slug(rec['nomenclature'])}.json"
        path = out_dir / filename
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(rec, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    write_readme(records, out_dir)


def write_readme(records: list[dict], out_dir: Path) -> None:
    groups = sorted({r["systemGroup"] for r in records})
    types_by_group = {
        g: sorted({r["systemType"] for r in records if r["systemGroup"] == g})
        for g in groups
    }
    rels = sum(len(r.get("relations") or []) for r in records)
    commented = sum(
        1 for r in records
        if any(("comments" in p or "parameterSubTitle" in p) for p in r["parametrics"])
    )
    lines = [
        "# large-test-corpus",
        "",
        "Synthetic pages_schema-style corpus for scale and schema-flexibility testing.",
        "",
        "Generated by:",
        "",
        "```powershell",
        "& 'C:\\Users\\robot\\anaconda3\\python.exe' generate_large_test_corpus.py --out large-test-corpus --count 1000 --seed 20260708",
        "```",
        "",
        "Generate the matching gold eval set with:",
        "",
        "```powershell",
        "& 'C:\\Users\\robot\\anaconda3\\python.exe' generate_large_eval_set.py --corpus large-test-corpus --out large_eval_set.json --seed 20260708",
        "```",
        "",
        "For retrieval evaluation, this corpus is intentionally homogeneous enough",
        "that the repo's default `k=4` is a stress test. Use `--k 10` for the",
        "recommended large-corpus baseline:",
        "",
        "```powershell",
        "$env:RAGKIT_DISABLE_RERANK='1'",
        "& 'C:\\Users\\robot\\anaconda3\\python.exe' eval.py --db large_eval_test.db --eval-set large_eval_set.json --k 10",
        "```",
        "",
        "Properties:",
        "",
        f"- Records: {len(records)}",
        f"- System groups: {len(groups)}",
        f"- System types: {sum(len(v) for v in types_by_group.values())}",
        "- Per-record parametric rows: variable, intentionally sparse-to-dense",
        f"- Records with comments and/or parameter subtitles: {commented}",
        f"- Parent/child relation rows: {rels}",
        "- Proliferations: every record has an IOC Year row; a subset has Projected Fielding `0 - 5 years` rows",
        "",
        "The records are plausible dummy engineering data, not real-world facts.",
        "Numeric fields use defined units; LOV/text fields use empty `uom` by design.",
        "",
        "Groups and types:",
        "",
    ]
    for group in groups:
        lines.append(f"- {group}: {', '.join(types_by_group[group])}")
    lines.append("")
    (out_dir / "README.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--count", type=int, default=DEFAULT_COUNT)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--no-clean", action="store_true",
                   help="do not remove the output directory before writing")
    args = p.parse_args(argv)
    if args.count < 10:
        raise SystemExit("--count must be at least 10")
    records = generate_records(args.count, args.seed)
    write_records(records, args.out, clean=not args.no_clean)
    rels = sum(len(r.get("relations") or []) for r in records)
    commented = sum(
        1 for r in records
        if any(("comments" in p or "parameterSubTitle" in p) for p in r["parametrics"])
    )
    print(f"Wrote {len(records)} records to {args.out}")
    print(f"Groups: {len({r['systemGroup'] for r in records})}; "
          f"types: {len({r['systemType'] for r in records})}; "
          f"relations: {rels}; comment/subtitle records: {commented}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
