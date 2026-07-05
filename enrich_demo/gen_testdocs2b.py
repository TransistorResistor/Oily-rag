#!/usr/bin/env python3
"""
gen_testdocs2b.py - a SMALL supplementary corpus (testdocs2b/ + gold2b.json)
stressing HARDER TABLE STRUCTURES than corpus 2, for the Phase B text-vs-md A/B:

  5a. UNIT IN THE COLUMN HEADER, bare numeric cells ("Maximum Altitude (km)" | 21)
  5b. DUAL-UNIT HEADER + DUAL VALUE      ("Maximum Range (km / nm)" | 37 / 20)
  5c. A UNITS COLUMN                     (Parameter | Value | Unit)
  5d. TRAPS that must yield ZERO proposals:
        * an INTERNALLY-INCONSISTENT dual value on a catalogued record
        * a header-unit table for a NON-catalogued system (unlinked)

Every anchor is verified against ../test_records (2026-07-05):
  NASAMS         : Radar detection range 120 km; Deployment time 10 min;
                   Maximum altitude ABSENT (has only Operating altitude range).
  S-300          : Deployment time 30 min; Maximum range 200 km; Radar detection
                   range 300 km; Maximum altitude ABSENT.
  Derby          : Maximum range 50 km; Weight 118 kg.
  Python-5       : Maximum range 20 km.
  Patriot PAC-3, S-350 : NOT catalogued.
Conversions used: 20 nm = 37.04 km (consistent with 37 km, <5%); 30 nm = 55.6 km
(inconsistent with 20 km); 40 nm = 74.1 km (inconsistent with 50 km).

Run:  python gen_testdocs2b.py
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
OUT = os.path.join(HERE, "testdocs2b")

DOCS = [
    # ============ 5a: UNIT IN COLUMN HEADER, bare numeric cells ============ #
    # NASAMS. Radar Detection Range 120 (=DB, dedup) and Deployment Time 10
    # (=DB, dedup) both drop; Maximum Altitude 21 is a GAP-FILL (field absent).
    # The unit lives ONLY in the header parenthetical -> defeats naive unit-
    # adjacency; the header-unit fix must recover it.
    dict(id="tbl2b_header_units", batch=1, trust="normal", noise=False,
         title="NASAMS - Assessed Performance Summary",
         body=[
            "The following summary lists assessed performance figures for the "
            "NASAMS surface-to-air missile air defence system. All quantities are "
            "given in the column headers; cells contain bare numeric values.",
         ],
         table=dict(caption="Table. NASAMS assessed figures (units in header)",
                    rows=[
                        ["System", "Radar Detection Range (km)",
                         "Maximum Altitude (km)", "Deployment Time (min)"],
                        ["NASAMS", "120", "21", "10"],
                    ]),
         gold=[dict(type="gap_fill", record="NASAMS",
                    field="Maximum altitude", value="21 km")]),

    # ============ 5b: DUAL-UNIT HEADER + DUAL VALUE ======================== #
    # Derby. "Maximum Range (km / nm)" | "37 / 20": the pipeline must surface ONE
    # value (37 km, the DB-unit position) after cross-checking 37 km ~= 20 nm, and
    # never mix a number with the wrong unit. 37 km CONFLICTS with DB 50 km.
    # Launch Weight 118 (=DB Weight, dedup) drops.
    dict(id="tbl2b_dual_unit", batch=1, trust="normal", noise=False,
         title="Derby (I-Derby) Air-to-Air Missile - Reference Card",
         body=[
            "Reference figures for the Derby beyond-visual-range air-to-air "
            "missile are tabulated below. Range is quoted in both kilometres and "
            "nautical miles within a single column.",
         ],
         table=dict(caption="Table. Derby reference figures (dual-unit range)",
                    rows=[
                        ["Missile", "Maximum Range (km / nm)",
                         "Launch Weight (kg)"],
                        ["Derby", "37 / 20", "118"],
                    ]),
         gold=[dict(type="conflict", record="Derby",
                    field="Maximum range", value="37 km", db="50")]),

    # ============ 5c: A UNITS COLUMN (Parameter | Value | Unit) ============ #
    # S-300. Maximum Altitude 27 km is a GAP-FILL (field absent). Maximum Range
    # 200 (=DB) and Deployment Time 30 (=DB) dedup and drop. The unit sits in its
    # own cell, one column right of the value.
    dict(id="tbl2b_units_col", batch=1, trust="normal", noise=False,
         title="S-300 Air Defence System - Parameter Register",
         body=[
            "The parameter register for the S-300 surface-to-air missile system "
            "records each quantity with its unit in a dedicated column.",
         ],
         table=dict(caption="Table. S-300 parameter register (units column)",
                    rows=[
                        ["Parameter", "Value", "Unit"],
                        ["Maximum Altitude", "27", "km"],
                        ["Maximum Range", "200", "km"],
                        ["Deployment Time", "30", "min"],
                    ]),
         gold=[dict(type="gap_fill", record="S-300",
                    field="Maximum altitude", value="27 km")]),

    # ============ 5d-i: INCONSISTENT DUAL VALUE (trap) ===================== #
    # Python-5. "Maximum Range (km / nm)" | "20 / 30": 20 km vs 30 nm (=55.6 km)
    # are NOT mutually consistent -> the dual handler must PARK it. Safety net:
    # even if a reader collapses it to the km position, 20 km == DB (dedup), so
    # ZERO proposals either way. Must produce nothing.
    dict(id="tbl2b_dual_trap", batch=1, trust="normal", noise=False,
         expect_zero=True,
         title="Python-5 Missile - Unverified Range Note",
         body=[
            "An unverified note tabulates a range figure for the Python-5 "
            "short-range air-to-air missile in mixed units.",
         ],
         table=dict(caption="Table. Python-5 range note (inconsistent dual)",
                    rows=[
                        ["Missile", "Maximum Range (km / nm)"],
                        ["Python-5", "20 / 30"],
                    ]),
         gold=[]),

    # ============ 5d-ii: HEADER-UNIT TABLE, NON-CATALOGUED SYSTEM ========== #
    # S-350 is NOT in the catalogue and NO catalogued sub-component is named, so
    # no mention resolves -> every figure parks as unlinked -> ZERO proposals.
    # Guards that the header-unit machinery does not manufacture proposals for an
    # unknown system.
    dict(id="tbl2b_header_noncat", batch=1, trust="normal", noise=False,
         expect_zero=True,
         title="S-350 Vityaz - Assessed Figures",
         body=[
            "Assessed figures for the S-350 Vityaz medium-range surface-to-air "
            "missile system are given below, with units in the column headers.",
         ],
         table=dict(caption="Table. S-350 assessed figures (units in header)",
                    rows=[
                        ["System", "Maximum Range (km)",
                         "Radar Detection Range (km)"],
                        ["S-350", "120", "400"],
                    ]),
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
    with open(os.path.join(HERE, "gold2b.json"), "w", encoding="utf-8") as f:
        json.dump(gold, f, indent=2)
    print(f"\n{len(DOCS)} PDFs + gold2b.json written.")


if __name__ == "__main__":
    main()
