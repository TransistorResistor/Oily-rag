#!/usr/bin/env python3
"""
gen_testdocs2.py - a SECOND, harder noisy PDF corpus into testdocs2/ + gold2.json.

New stress axes over the first corpus (gen_testdocs.py):
  * TABLES - spec sheets / comparison matrices. PyMuPDF flattens tables into
    whitespace-separated text, so this probes whether entity/attribute/value
    survive tabular layout, whether NEAR-identical values dedup (within the 2%
    tolerance) and NON-identical ones conflict, and whether a multi-system
    comparison table mis-attributes one row's numbers to a neighbouring record.
  * RED HERRINGS - documents that name a catalogued system but whose numbers are
    commercial/schedule/administrative (contract value, personnel, hours,
    sorties), which must NOT be mistaken for system parametrics.
  * INTEGRATION PRESS RELEASE - prose-rich announcement of a NEW capability
    integration between two catalogued records (a relation edge that does not yet
    exist), which the pipeline should surface as a relation proposal.

Facts are crafted against the ACTUAL reference values in test_records/ (verified
2026-07-04) so matches truly dedup, conflicts truly contradict, and gap-fills are
truly absent. Run:  python gen_testdocs2.py
"""

import json
import os

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle)
from reportlab.lib.enums import TA_JUSTIFY

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "testdocs2")
EN, EM = "–", "—"

# --------------------------------------------------------------------------- #
# Corpus                                                                       #
# body: list of paragraphs (str).  table: optional dict(caption, rows=[...]).  #
# --------------------------------------------------------------------------- #
DOCS = [
    # =================== TABLE 1: single-system spec sheet ================= #
    # Header labels are deliberately abbreviated/variant to test attribute
    # mapping from tabular headers. Near-identical values (402~400, 600=600,
    # 6=6) must DEDUP; non-identical (8 vs 5 min) must CONFLICT; a field the
    # record lacks (Maximum altitude) must GAP-FILL.
    dict(id="table_s400_spec", batch=1, trust="normal", noise=True,
         title="S-400 Triumf - Consolidated Specification Sheet",
         body=[
            "The following consolidated specification sheet summarises assessed "
            "parameters for the S-400 Triumf long-range surface-to-air missile "
            "system, as compiled from open sources for analytical reference.",
         ],
         table=dict(caption="Table 1. S-400 Triumf assessed parameters",
                    rows=[
                        ["Parameter", "Assessed Value", "Notes"],
                        ["Max. Range", "402 km", "longest-reach missile"],
                        ["Radar Det. Range", "600 km", "acquisition radar"],
                        ["Simult. Targets Engaged", "6", "per battery"],
                        ["Deploy Time", "8 min", "convoy halt to active"],
                        ["Engagement Ceiling", "30 km", "assessed maximum"],
                    ]),
         gold=[dict(type="conflict", record="S-400 Triumf",
                    field="Deployment time", value="8 min", db="5"),
               dict(type="gap_fill", record="S-400 Triumf",
                    field="Maximum altitude", value="30 km")]),

    # =================== TABLE 2: multi-system comparison ================== #
    # Every catalogued value MATCHES the DB (within 2%: 1805~1800) -> dedup.
    # The "Patriot PAC-3" column is a RED HERRING / attribution trap: a
    # non-catalogued system whose numbers must NOT be assigned to a neighbouring
    # catalogued missile. Net expectation: ZERO proposals.
    dict(id="table_missile_compare", batch=1, trust="normal", noise=False,
         expect_zero=True,
         title="Comparative Missile Parameter Matrix",
         body=[
            "The comparison matrix below places several surface-to-air and "
            "air-to-air missiles side by side for parameter reference. Figures "
            "are drawn from standard reference data.",
         ],
         table=dict(caption="Table 2. Comparative missile parameters",
                    rows=[
                        ["Missile", "Max Range", "Launch Weight", "Top Speed"],
                        ["9M96", "120 km", "190 kg", "Mach 3"],
                        ["48N6M", "250 km", "1805 kg", "Mach 2.8"],
                        ["AIM-120 AMRAAM", "180 km", "152 kg", "Mach 4"],
                        ["Patriot PAC-3", "35 km", "316 kg", "Mach 5"],
                    ]),
         gold=[]),

    # =================== INTEGRATION PRESS RELEASE ========================= #
    # F-35 and ASRAAM are BOTH catalogued; there is NO existing relation edge
    # between them (F-35 -> AIM-120 only; ASRAAM -> Eurofighter Typhoon only).
    # A prose-rich announcement of the new integration should yield a RELATION.
    dict(id="integration_f35_asraam", batch=1, trust="normal", noise=False,
         title="Lockheed Martin and MBDA Confirm F-35 / ASRAAM Integration Milestone",
         body=[
            "Lockheed Martin and MBDA today announced the successful completion of "
            "flight-test integration of the ASRAAM short-range air-to-air missile "
            "onto the F-35 Lightning II stealth fighter, marking a significant "
            "expansion of the aircraft's within-visual-range capability.",
            "Under the programme, the F-35 has been cleared to carry and employ the "
            "infrared-guided ASRAAM from its external wing stations, with the "
            "missile integrated into the aircraft's sensor-fusion and cueing "
            "architecture. A series of guided firings against representative aerial "
            "targets validated the F-35 / ASRAAM weapon pairing across the "
            "engagement envelope.",
            "Officials described the ASRAAM integration onto the F-35 as providing "
            "operators with a high-off-boresight dogfight missile that complements "
            "the beyond-visual-range weapons already carried internally. Deliveries "
            "of the integrated capability to F-35 operators are expected to follow "
            "operational test.",
         ],
         gold=[dict(type="relation", record="Lockheed Martin F-35 Lightning II",
                    other="ASRAAM")]),

    # =================== RED HERRING: procurement / schedule =============== #
    # Names S-400 but every number is commercial/administrative: contract value,
    # launcher count, delivery months, support years. None are system
    # parametrics. Expectation: ZERO proposals.
    dict(id="redherring_procurement", batch=1, trust="normal", noise=False,
         expect_zero=True,
         title="Almaz-Antey Books Multi-Year S-400 Support Contract",
         body=[
            "Russian manufacturer Almaz-Antey has signed a contract reportedly "
            "valued at 2.5 billion USD covering the production and multi-year "
            "sustainment of S-400 Triumf air defence systems for the Russian armed "
            "forces.",
            "Under the agreement, 40 launcher vehicles are to be delivered over a "
            "period of 8 months, with a follow-on logistics and training package "
            "spanning 5 years. Industry sources put the programme workforce at some "
            "1,200 personnel across two production facilities.",
            "The contract does not alter the technical configuration of the S-400 "
            "system; it covers manufacturing throughput, spares provisioning and "
            "crew training rather than any change to the weapon's performance.",
         ],
         gold=[]),

    # =================== RED HERRING: misleading units ==================== #
    # Names S-300; numbers 200 / 250 / 300 value-collide with S-300's range and
    # detection-range, but their units are personnel / hours / sorties, so they
    # must NOT be mapped onto range-type fields. Expectation: ZERO proposals.
    dict(id="redherring_units", batch=1, trust="normal", noise=True,
         expect_zero=True,
         title="Exercise Report: S-300 Readiness Drill",
         body=[
            "During the recent air defence exercise, the participating S-300 "
            "surface-to-air missile regiment mobilised 200 personnel and sustained "
            f"a 250{EN}hour continuous readiness posture over the exercise period.",
            "Air activity generated 300 simulated sorties against the S-300 "
            "battery, which maintained crew rotations on a 12-hour cycle. The drill "
            "assessed command-and-control procedures rather than missile "
            "performance, and no live firings were conducted.",
         ],
         gold=[]),

    # =================== DISTRACTOR: Derby County F.C. ==================== #
    # "Derby" is a catalogued ambiguous alias, but a football context carries no
    # defence domain term or operator-country signal -> must not link. ZERO.
    dict(id="distractor_derby_football", batch=1, trust="normal", noise=False,
         expect_zero=True,
         title="Derby County: A Football Club History",
         body=[
            "Derby County Football Club, nicknamed the Rams, is an English "
            "association football club founded in 1884. The Derby side has spent "
            "several seasons in the top flight and enjoys a historic local rivalry "
            "with Nottingham Forest, known as the East Midlands derby.",
            "The club plays its home matches at Pride Park Stadium, which has a "
            "capacity of around 33,000 spectators. A young winger named Python "
            "briefly featured for the Derby reserves before moving on.",
         ],
         gold=[]),

    # =================== UNLINKED: non-catalogued S-350 =================== #
    # A detailed, genuinely defence-domain spec for a system NOT in the
    # reference catalogue -> no mention resolves -> ZERO proposals (its facts
    # may persist only as unlinked parked fragments).
    dict(id="unlinked_s350", batch=1, trust="normal", noise=False,
         expect_zero=True,
         title="S-350 Vityaz Medium-Range Air Defence System",
         body=[
            "The S-350 Vityaz is a medium-range surface-to-air missile system of "
            "Russian origin, designed to engage aircraft, cruise missiles and "
            "precision munitions. The S-350 employs the 9M96 family of active-radar "
            "missiles from a vertical-launch cell configuration.",
            "The Vityaz air defence system reportedly offers a maximum engagement "
            "range of 120 km and can track a large number of targets simultaneously "
            "using its 50N6A phased-array radar. It entered Russian service in 2019 "
            "as a complement to longer-range systems.",
         ],
         gold=[]),

    # =================== CLEAN PROSE GAP-FILL: Typhoon operator =========== #
    dict(id="gapfill_typhoon_operator", batch=1, trust="normal", noise=False,
         title="Kuwait Declares Eurofighter Typhoon Squadron Operational",
         body=[
            "Kuwait has declared its first Eurofighter Typhoon squadron "
            "operational, becoming a new operator of the multi-role fighter "
            "aircraft. The Kuwaiti Typhoon fleet will undertake air-defence and "
            "strike missions.",
            "The introduction of the Eurofighter Typhoon strengthens Kuwait's air "
            "combat capability alongside its existing fast-jet fleet.",
         ],
         gold=[dict(type="gap_fill", record="Eurofighter Typhoon",
                    field="Operated by (country)", value="Kuwait")]),

    # =================== CORROBORATION pair (low-trust) =================== #
    # AIM-120 AMRAAM has NO Maximum altitude field. Two low-trust sources, one
    # per batch, state it in different units (20 km vs ~65,000 ft ~= 19.8 km,
    # within the 2% cluster). Parks in batch 1; graduates in batch 2.
    dict(id="corrob_amraam_ceiling_a", batch=1, trust="low", noise=False,
         title="Analyst Estimate: AMRAAM Engagement Ceiling (1/2)",
         body=[
            "An unconfirmed analyst estimate assesses that the AIM-120 AMRAAM "
            "beyond-visual-range air-to-air missile can engage targets at a "
            "maximum altitude of 20 km.",
            "This 20 km ceiling figure for the AIM-120 has not been officially "
            "confirmed and is presented as an open-source assessment only.",
         ],
         gold=[dict(type="gap_fill", record="AIM-120 AMRAAM",
                    field="Maximum altitude", value="20 km",
                    corroborated=True, surfaces_in_batch=2)]),

    dict(id="corrob_amraam_ceiling_b", batch=2, trust="low", noise=True,
         title="Analyst Estimate: AMRAAM Engagement Ceiling (2/2)",
         body=[
            "Separate open-source reporting puts the engagement ceiling of the "
            f"AIM-120 AMRAAM air{EN}to{EN}air missile at approximately 65,000 ft, "
            "an altitude broadly consistent with earlier analyst estimates.",
            "This unverified maximum altitude figure for the AMRAAM remains an "
            "analytical assessment pending confirmation.",
         ],
         gold=[dict(type="gap_fill", record="AIM-120 AMRAAM",
                    field="Maximum altitude", value="65,000 ft",
                    corroborated=True, surfaces_in_batch=2)]),

    # =================== BATCH-2 extra clean gap-fill ===================== #
    dict(id="gapfill_su57_operator", batch=2, trust="normal", noise=False,
         title="Algeria Reported as Su-57 Export Customer",
         body=[
            "Algeria has been reported as an export customer for the Su-57 Felon "
            "fifth-generation stealth fighter, which would make it the first "
            "foreign operator of the Russian combat aircraft.",
            "The reported Su-57 acquisition would expand Algeria's air combat fleet "
            "with a low-observable multi-role fighter.",
         ],
         gold=[dict(type="gap_fill", record="Sukhoi Su-57",
                    field="Operated by (country)", value="Algeria")]),

    # =================== BATCH-3 recurrence (suppression test) ============ #
    # Restates the Kuwait/Typhoon fact in different words (new content hash).
    # After the Kuwait gap-fill is rejected, this must NOT resurface.
    dict(id="rerun_typhoon_kuwait", batch=3, trust="normal", noise=False,
         suppress_test=True,
         title="Kuwaiti Air Force Completes Typhoon Induction",
         body=[
            "The Kuwaiti Air Force has completed induction of the Eurofighter "
            "Typhoon, confirming Kuwait among the operators of the multi-role "
            "fighter aircraft following the delivery of its final airframes.",
            "Kuwait's Typhoon force is now reported fully manned and available for "
            "air-defence tasking.",
         ],
         gold=[]),
]


def _header_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.drawString(2 * cm, 28.2 * cm, "CONFIDENTIAL // OSINT DIGEST // FOR ANALYSIS")
    canvas.drawRightString(19 * cm, 1.2 * cm, f"Page {doc.page}  -  UNCLASSIFIED DRAFT")
    canvas.restoreState()


def build_pdf(spec):
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=11,
                          leading=15, alignment=TA_JUSTIFY, spaceAfter=8)
    title = ParagraphStyle("title", parent=styles["Title"], fontSize=15,
                           spaceAfter=14)
    cap = ParagraphStyle("cap", parent=styles["Normal"], fontSize=9,
                         textColor=colors.grey, spaceAfter=4)
    path = os.path.join(OUT, spec["id"] + ".pdf")
    d = SimpleDocTemplate(path, pagesize=A4, topMargin=2.2 * cm,
                          bottomMargin=2 * cm)
    flow = [Paragraph(spec["title"], title), Spacer(1, 4)]
    for para in spec["body"]:
        flow.append(Paragraph(para, body))
    tbl = spec.get("table")
    if tbl:
        if tbl.get("caption"):
            flow.append(Spacer(1, 6))
            flow.append(Paragraph(tbl["caption"], cap))
        t = Table(tbl["rows"], hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d9d9d9")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        flow.append(t)
    if spec.get("noise"):
        d.build(flow, onFirstPage=_header_footer, onLaterPages=_header_footer)
    else:
        d.build(flow)
    return path


def main():
    os.makedirs(OUT, exist_ok=True)
    gold = []
    for spec in DOCS:
        p = build_pdf(spec)
        gold.append({"doc_id": spec["id"], "batch": spec["batch"],
                     "trust": spec["trust"], "noise": spec.get("noise", False),
                     "expect_zero": spec.get("expect_zero", False),
                     "suppress_test": spec.get("suppress_test", False),
                     "has_table": bool(spec.get("table")),
                     "gold": spec["gold"]})
        print("wrote", os.path.basename(p))
    with open(os.path.join(HERE, "gold2.json"), "w", encoding="utf-8") as f:
        json.dump(gold, f, indent=2)
    print(f"\n{len(DOCS)} PDFs + gold2.json written.")
    for b in (1, 2, 3):
        ids = [d["id"] for d in DOCS if d["batch"] == b]
        print(f"batch{b}:", ",".join(ids))


if __name__ == "__main__":
    main()
