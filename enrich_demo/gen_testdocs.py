#!/usr/bin/env python3
"""
gen_testdocs.py - generate the deliberately-noisy PDF corpus into testdocs/ and
write gold.json (expected proposals + expected non-proposals). Facts are crafted
relative to the ACTUAL reference values in test_records/ so gap-fills are truly
absent, conflicts truly contradict, and distractors truly collide on aliases.

Run:  python gen_testdocs.py
"""

import json
import os

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.enums import TA_JUSTIFY

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "testdocs")

# en-dash / em-dash / soft hyphen to sprinkle OCR-style noise
EN, EM = "–", "—"

DOCS = [
    # ---------------- clean gap-fills (should SURFACE) ------------------- #
    dict(id="gapfill_s400_belarus", batch=1, trust="normal", noise=False,
         title="Belarus Confirmed as New S-400 Operator",
         body=[
            "In a defence procurement announcement this week, Belarus was "
            "confirmed as a new operator of the Russian S-400 Triumf long-range "
            "surface-to-air missile system.",
            "The S-400 air defence system has now been fielded by Belarus to "
            "protect its western airspace, joining existing operators. Officials "
            "described the delivery as a strengthening of integrated air defence.",
         ],
         gold=[dict(type="gap_fill", record="S-400 Triumf",
                    field="Operated by (country)", value="Belarus")]),

    dict(id="gapfill_s300_detection", batch=1, trust="normal", noise=True,
         title="S-300 Acquisition Radar Detection Envelope",
         body=[
            f"Technical analysis of the S-300 surface{EN}to{EN}air missile system "
            "indicates that its 64N6E acquisition radar has a detection range of "
            "300 km against fighter-sized targets at altitude.",
            "This detection range figure applies to the search radar of the "
            "S-300 air defence system and reflects the panoramic surveillance "
            "coverage of the phased-array antenna.",
         ],
         gold=[dict(type="gap_fill", record="S-300",
                    field="Detection range", value="300 km")]),

    dict(id="gapfill_s400_alias", batch=1, trust="normal", noise=False,
         title="Naming Conventions of the S-400 Air Defence System",
         body=[
            "The S-400 Triumf surface-to-air missile system is, at the complete "
            "system level, sometimes designated the 40R6 in Russian service "
            "documentation.",
            "While NATO uses the reporting name SA-21 Growler for the S-400, the "
            "40R6 designation refers to the full battalion set of the air defence "
            "system including radars and launchers.",
         ],
         gold=[dict(type="gap_fill", record="S-400 Triumf",
                    field="alias", value="40R6")]),

    # ---------------- conflicts (should SURFACE as CONFLICT) ------------- #
    dict(id="conflict_s400_deploytime", batch=1, trust="normal", noise=False,
         title="S-400 Battery Emplacement Assessment",
         body=[
            "A field assessment of the S-400 Triumf air defence system reports a "
            "deployment time of 8 minutes from convoy halt to radar-active status "
            "for the mobile surface-to-air missile battery.",
            "This emplacement figure for the S-400 reflects crew drills under "
            "exercise conditions with the phased-array radar.",
         ],
         gold=[dict(type="conflict", record="S-400 Triumf",
                    field="Deployment time", value="8 min", db="5")]),

    dict(id="conflict_python5_range", batch=1, trust="normal", noise=False,
         title="Python-5 Extended Engagement Range Claim",
         body=[
            "A recent briefing on the Python-5 short-range air-to-air missile "
            "stated that the Python-5 has a maximum range of 35 km in its latest "
            "production configuration.",
            "The infrared-guided Python-5 missile, widely carried by fighter "
            "aircraft, was described as reaching this 35 km range against "
            "aerial targets.",
         ],
         gold=[dict(type="conflict", record="Python-5",
                    field="Maximum range", value="35 km", db="20")]),

    # ---------------- corroboration pair (low-trust) -------------------- #
    dict(id="corrob_s400_altitude_a", batch=1, trust="low", noise=True,
         title="Analyst Note: S-400 Engagement Ceiling (1/2)",
         body=[
            "Unconfirmed analyst reporting suggests that the S-400 Triumf "
            f"surface{EN}to{EN}air missile system can engage aerial targets at a "
            "maximum altitude of 30 km.",
            "Analysts assess that this 30 km engagement ceiling for the S-400 air "
            "defence system would extend its reach against high-flying targets, "
            "though the figure has not been officially confirmed.",
         ],
         gold=[dict(type="gap_fill", record="S-400 Triumf",
                    field="Maximum altitude", value="30 km",
                    corroborated=True, surfaces_in_batch=2)]),

    dict(id="corrob_s400_altitude_b", batch=2, trust="low", noise=True,
         title="Analyst Note: S-400 Engagement Ceiling (2/2)",
         body=[
            "Open-source speculation continues to circulate that the S-400 air "
            f"defence system has an engagement ceiling of roughly 98,000 ft "
            f"{EM} an altitude corresponding to the upper edge of its envelope.",
            "This unverified maximum altitude figure for the S-400 surface-to-air "
            "missile system aligns broadly with earlier analyst estimates.",
         ],
         gold=[dict(type="gap_fill", record="S-400 Triumf",
                    field="Maximum altitude", value="98,000 ft",
                    corroborated=True, surfaces_in_batch=2)]),

    # ---------------- distractors (must yield ZERO proposals) ----------- #
    dict(id="distractor_python_lang", batch=1, trust="normal", noise=False,
         title="Python and Apache Derby: A Developer Primer",
         body=[
            "Python is a high-level programming language created by Guido van "
            "Rossum in the Netherlands and first released in 1991. Python is "
            "prized for its readable syntax and extensive standard library.",
            "Many Python web applications persist data in relational databases "
            "such as Apache Derby, an embeddable Java database. Developers often "
            "pair Python with Derby for lightweight prototypes, using the range() "
            "builtin to iterate over query results.",
         ],
         expect_zero=True, gold=[]),

    dict(id="distractor_derby_horse", batch=1, trust="normal", noise=False,
         title="The Derby: A History of the Great Horse Race",
         body=[
            "The Derby is one of the oldest and most prestigious flat horse races "
            "in the world. The Epsom Derby in England and the Kentucky Derby in "
            "the United States both draw enormous crowds each year.",
            "Thoroughbreds bred for stamina compete over the Derby distance, and "
            "winning owners receive a substantial purse. A colt named Python once "
            "placed in a minor Derby undercard.",
         ],
         expect_zero=True, gold=[]),

    # ---------------- unlinked (real system NOT in reference) ----------- #
    dict(id="unlinked_patriot", batch=1, trust="normal", noise=False,
         title="Patriot PAC-3 Missile Segment Enhancement",
         body=[
            "The Patriot PAC-3, designated MIM-104F, is a hit-to-kill surface-to-"
            "air missile interceptor produced in the United States. The Patriot "
            "air defence system engages ballistic missiles and aircraft.",
            "The PAC-3 missile uses an active radar seeker and has a maximum range "
            "of 35 km against aerodynamic targets. The Patriot radar provides "
            "fire-control for the interceptor.",
         ],
         expect_zero=True, gold=[]),

    # ---------------- hedged / partial claims --------------------------- #
    dict(id="hedged_s300", batch=1, trust="normal", noise=False,
         title="S-300 Reach and Readiness (Preliminary)",
         body=[
            "According to a preliminary summary, the S-300 surface-to-air missile "
            "system can reportedly engage targets at ranges of up to 250 km, an "
            "estimated figure for the longest-range missile variant.",
            "The same summary lists the emplacement time of the S-300 air defence "
            "battery simply as 5, with no unit stated in the source document.",
         ],
         gold=[dict(type="conflict", record="S-300", field="Maximum range",
                    value="250 km", db="200", qualifier_expected=True),
               dict(type="park", park_reason="incomplete", record="S-300",
                    field="Deployment time")]),

    # ---------------- relation (both records in reference) -------------- #
    dict(id="relation_f35_aim9x", batch=1, trust="normal", noise=False,
         title="F-35 Cleared to Carry AIM-9X Short-Range Missile",
         body=[
            "The F-35 Lightning II is now cleared to carry the AIM-9X Sidewinder "
            "short-range air-to-air missile for within-visual-range engagements.",
            "Integration testing confirmed the F-35 can employ the AIM-9X "
            "infrared-guided missile from its wing stations.",
         ],
         gold=[dict(type="relation", record="Lockheed Martin F-35 Lightning II",
                    other="AIM-9X Sidewinder")]),

    # ---------------- batch-2 extra gap-fills --------------------------- #
    dict(id="python5_vietnam", batch=2, trust="normal", noise=False,
         title="Vietnam Acquires Python-5 Air-to-Air Missiles",
         body=[
            "Vietnam has been reported as a new operator of the Israeli Python-5 "
            "short-range air-to-air missile, integrating the infrared-guided "
            "missile onto its fighter fleet.",
            "The Python-5 acquisition strengthens Vietnam's air defence posture "
            "with a modern within-visual-range interceptor.",
         ],
         gold=[dict(type="gap_fill", record="Python-5",
                    field="Operated by (country)", value="Vietnam")]),

    # ---------------- batch-3 recurrence (for the reject/suppression test) --- #
    # Restates the S-400/Belarus fact in DIFFERENT words so it has a new content
    # hash (won't be hash-skipped). After the Belarus proposal is rejected, this
    # must NOT resurface -- it should land in "Seen again".
    dict(id="rerun_s400_belarus", batch=3, trust="normal", noise=False,
         title="Belarus S-400 Regiment Reaches Operational Status",
         body=[
            "A second report this month again lists Belarus as an operator of the "
            "S-400 Triumf air defence system, stating that a Belarusian S-400 "
            "surface-to-air missile regiment has reached operational status.",
            "The S-400 deployment gives Belarus long-range coverage of its "
            "airspace against aircraft and cruise missiles.",
         ],
         gold=[], suppress_test=True),

    dict(id="spyder_poland", batch=2, trust="normal", noise=False,
         title="Poland Evaluates SPYDER Point-Defence System",
         body=[
            "Poland has been named as a new operator of the SPYDER surface-to-air "
            "missile system, a mobile air defence system firing Python and Derby "
            "interceptors.",
            "The SPYDER acquisition gives Poland a short-to-medium range point "
            "defence capability against aircraft and cruise missiles.",
         ],
         gold=[dict(type="gap_fill", record="SPYDER",
                    field="Operated by (country)", value="Poland")]),
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
    path = os.path.join(OUT, spec["id"] + ".pdf")
    d = SimpleDocTemplate(path, pagesize=A4, topMargin=2.2 * cm,
                          bottomMargin=2 * cm)
    flow = [Paragraph(spec["title"], title), Spacer(1, 4)]
    for para in spec["body"]:
        flow.append(Paragraph(para, body))
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
                     "gold": spec["gold"]})
        print("wrote", os.path.basename(p))
    with open(os.path.join(HERE, "gold.json"), "w", encoding="utf-8") as f:
        json.dump(gold, f, indent=2)
    print(f"\n{len(DOCS)} PDFs + gold.json written.")
    b1 = [d["id"] for d in DOCS if d["batch"] == 1]
    b2 = [d["id"] for d in DOCS if d["batch"] == 2]
    print("batch1:", ",".join(b1))
    print("batch2:", ",".join(b2))


if __name__ == "__main__":
    main()
