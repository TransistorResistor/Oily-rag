#!/usr/bin/env python3
"""
gen_testdocs3.py - corpus 3 (testdocs3/ + gold3.json): an INPUT-ROBUSTNESS
corpus. Where corpus 2/2b stress table *structure*, corpus 3 stresses the
*shape of the incoming claim* -- the ways a real document can be malformed,
negated, dated, self-contradictory, or about something we don't catalogue at
all. The question each doc asks is "does the deterministic layer stay silent
(or flag a discrepancy) when it should, instead of fabricating a proposal?"

Six documents (all anchors verified against ../test_records, 2026-07-09):
  NASAMS : Radar detection range 120 km; Deployment time 10 min;
           Maximum altitude ABSENT (only Operating altitude range 0-35700 m).
  S-300  : Maximum range 200 km; Radar detection range 300 km;
           Deployment time 30 min; Maximum altitude ABSENT (0-27000 m).
  Derby  : Maximum range 50 km.
  S-500 Prometheus : NOT catalogued (new-entity probe).

  1. d3_new_entity      - clean figure for a NON-catalogued system (S-500).
                          Nothing resolves -> unlinked park -> ZERO proposals.
                          Guards against force-mapping onto a neighbour record.
  2. d3_negation        - a NEGATED restatement ("does not exceed 50 km") of a
                          catalogued value. Not an affirmative claim; even read
                          affirmatively 50 km == DB (dedup). ZERO proposals.
  3. d3_temporal        - a DATED, superseded figure ("As of 2014 ... 150 km")
                          that contradicts the current record (S-300 = 200 km).
                          A report-only curator wants the discrepancy surfaced
                          -> expect a CONFLICT (gold encodes desired behaviour;
                          if a hedge-detector parks it, that is the finding).
  4. d3_intradoc        - the SAME doc states two different values for the same
                          absent field (NASAMS max altitude: prose 21 km vs
                          table 16 km). Internally inconsistent -> park -> ZERO.
  5. d3_ocr_garbled     - OCR-style glyph corruption in the numerics ("3OO km",
                          "l0 minutes"). Unparseable -> drop/park; even if a
                          reader repairs 3OO->300, that == DB (dedup). ZERO.
  6. d3_valid_control   - a CLEAN gap-fill (S-300 maximum altitude 27 km, a
                          field genuinely absent from the record). The positive
                          control: proves the corpus is not silent by accident.

Run:  python gen_testdocs3.py
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
OUT = os.path.join(HERE, "testdocs3")

DOCS = [
    # ==================== 1: NEW / NON-CATALOGUED ENTITY =================== #
    dict(id="d3_new_entity", batch=1, trust="normal", noise=False,
         expect_zero=True,
         title="S-500 Prometheus - Preliminary Capability Note",
         body=[
            "The S-500 Prometheus is a next-generation Russian air and missile "
            "defence system. According to preliminary assessments, the S-500 "
            "Prometheus has a maximum engagement range of 600 km and can engage "
            "targets at altitudes beyond 180 km.",
            "No sub-systems of the S-500 are named in this note; the figures "
            "above refer to the system as a whole.",
         ],
         gold=[]),

    # ======================= 2: NEGATED RESTATEMENT ======================= #
    dict(id="d3_negation", batch=1, trust="normal", noise=False,
         expect_zero=True,
         title="Derby (I-Derby) - Range Clarification",
         body=[
            "Contrary to some published figures, the Derby beyond-visual-range "
            "air-to-air missile's maximum range does not exceed 50 km. The "
            "missile is not equipped for engagements beyond that distance.",
         ],
         gold=[]),

    # ==================== 3: DATED / SUPERSEDED FIGURE ==================== #
    dict(id="d3_temporal", batch=1, trust="normal", noise=False,
         title="S-300 - Historical Assessment (Archive Extract)",
         body=[
            "As of 2014, the S-300 surface-to-air missile system's maximum "
            "effective range was assessed at 150 km. This archived figure is "
            "reproduced here for historical reference.",
         ],
         gold=[dict(type="conflict", record="S-300",
                    field="Maximum range", value="150 km", db="200")]),

    # ================ 4: INTRA-DOCUMENT CONTRADICTION ==================== #
    # NASAMS Maximum altitude is ABSENT in the record; prose says 21 km, the
    # table says 16 km. Two conflicting values for one absent field within a
    # single source -> neither is trustworthy -> park -> ZERO proposals.
    dict(id="d3_intradoc", batch=1, trust="normal", noise=False,
         expect_zero=True,
         title="NASAMS - Performance Note (Draft)",
         body=[
            "The NASAMS surface-to-air missile system reaches a maximum "
            "altitude of 21 km against aerodynamic targets. Figures in the "
            "accompanying table are provided for cross-reference.",
         ],
         table=dict(caption="Table. NASAMS draft figures",
                    rows=[
                        ["Parameter", "Value", "Unit"],
                        ["Maximum Altitude", "16", "km"],
                    ]),
         gold=[]),

    # ===================== 5: OCR-GARBLED NUMERICS ======================= #
    # "3OO" uses letter O for zero; "l0" uses lowercase L for one. A correct
    # numeric parser cannot extract a clean value -> drop/park. Safety net: the
    # nearest legitimate readings (300 km radar range, 10 min deploy) both ==
    # DB, so even a lenient repair dedups to nothing. Must produce ZERO.
    dict(id="d3_ocr_garbled", batch=1, trust="normal", noise=False,
         expect_zero=True,
         title="S-300 - Scanned Field Report (OCR)",
         body=[
            "Scanned report, uncorrected OCR. The S-300 system's radar "
            "detection range is listed as 3OO km, with a deployment time of "
            "l0 minutes noted in the margin.",
         ],
         table=dict(caption="Table. S-300 scanned figures (uncorrected)",
                    rows=[
                        ["Parameter", "Value", "Unit"],
                        ["Radar Detection Range", "3OO", "km"],
                    ]),
         gold=[]),

    # ===================== 6: CLEAN POSITIVE CONTROL ===================== #
    # S-300 Maximum altitude is genuinely absent from the record; 27 km is a
    # valid gap-fill (consistent with Operating altitude range 25-27000 m).
    dict(id="d3_valid_control", batch=1, trust="normal", noise=False,
         title="S-300 - Engagement Envelope Supplement",
         body=[
            "This supplement records the vertical engagement envelope of the "
            "S-300 surface-to-air missile system. The S-300 has a maximum "
            "altitude of 27 km.",
         ],
         gold=[dict(type="gap_fill", record="S-300",
                    field="Maximum altitude", value="27 km")]),
]


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
    with open(os.path.join(HERE, "gold3.json"), "w", encoding="utf-8") as f:
        json.dump(gold, f, indent=2)
    print(f"\n{len(DOCS)} PDFs + gold3.json written.")


if __name__ == "__main__":
    main()
