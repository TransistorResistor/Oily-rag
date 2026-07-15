#!/usr/bin/env python3
"""
eval.py - offline evaluation harness for ragkit (REVIEW_FINDINGS F2).

Nothing in this project measured retrieval hit-rate or filter-extraction
accuracy before this phase, so "is this change actually better" could only be
judged by eyeballing a handful of manual queries -- exactly the "consistency
can't be tuned or regression-checked" gap F2 flags. This harness runs a fixed
gold set (eval_set.json) against a db and reports:

  - retrieval hit-rate (overall + per query CLASS: lookup/parametric/
    comparison/analytic/prose/negative), and the rank of the first expected
    record in the retrieved contexts;
  - prompt-size stats (min/median/p95/max tokens, at the default 3000-token
    cap AND uncapped) so the token-budgeter's effect (REVIEW_FINDINGS B1) is
    directly visible, not just asserted;
  - OPTIONALLY (see below), three LLM-dependent checks: filter-extraction
    field precision/recall against each case's `expected_filter`, an answer
    check (expected substrings present; negative cases correctly say "not
    available"), and citation verification (REVIEW_FINDINGS G6 -- any [rid]
    in the answer that isn't actually in the prompt's context is flagged).

Retrieval is evaluated the way an actual OFFLINE (no-LLM) run behaves: query
embedding + deterministic entity pinning (REVIEW_FINDINGS G1, via
ragkit.match_entities) + retrieve() with NO metadata filter. This is
deliberate, not a simplification -- see run_retrieval_case's docstring for why
testing retrieval through the gold set's `expected_filter` (an "oracle"
filter, never actually derived by anything at query time in this mode) would
answer a different, easier question and could hide a real pinning/wiring bug.

The LLM stages are OFF by default and require --backend: this harness must be
runnable with zero network access and zero API spend (the retrieval + prompt-
size report, the primary deliverable, needs only the local embedder/reranker),
and flipping them on always costs real LLM calls against whatever --backend/
--model you pick.

Usage
-----
  # fully offline: retrieval hit-rate + prompt-size report only
  python eval.py --db rag_test.db
  python ragkit.py eval --db rag_test.db          # identical, via the ragkit CLi

  # write the full per-case JSON alongside the printed report
  python eval.py --db rag_test.db --json eval_report.json

  # OPT IN to the LLM stages (costs real API calls against --backend)
  python eval.py --db rag_test.db --backend openrouter --model qwen3-14b
"""

import argparse
import json
import re
import statistics
import sys

import catalogue as cat_mod
import ragkit

DEFAULT_EVAL_SET = "eval_set.json"

# Canonical print order for the per-class table -- eval_set.json's own case
# order groups by class already, but dict iteration would otherwise sort
# alphabetically (analytic, comparison, lookup, negative, parametric, prose),
# which reads worse than the set's own narrative order.
CLASS_ORDER = ["lookup", "parametric", "comparison", "analytic", "prose",
               "robustness", "negative"]


# --------------------------------------------------------------------------- #
# Loading                                                                      #
# --------------------------------------------------------------------------- #

def load_cases(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("cases", [])


# --------------------------------------------------------------------------- #
# Retrieval-stage scoring                                                      #
# --------------------------------------------------------------------------- #

def score_retrieval(case, ctx_rids):
    """(hit, rank) for one case given the ORDERED list of retrieved parent
    rids (ctx_rids[i] is the i-th retrieved passage's source record).

    hit:
      - comparison-class cases (`required_rids` non-empty) are ALL-OF: every
        required rid must appear SOMEWHERE in ctx_rids (order doesn't matter --
        the passages just need to both be in context for the model to compare
        them).
      - every other non-negative case (`expected_rids` non-empty) is ANY-OF:
        hit if at least one expected rid appears anywhere.
      - a negative case (both lists empty) has NOTHING to hit -- retrieval
        can't "fail" a query with no right answer, so hit is None here and
        aggregate() reports these cases separately rather than folding a
        non-answer into the hit-rate denominator (see aggregate_retrieval's
        docstring for exactly how -- "not available" correctness for these
        two cases is instead the answer-stage's job, see score_answer).

    rank: the 1-based position of the FIRST target rid seen in ctx_rids (for
    a comparison, this is whichever of the two required rids the retriever
    happened to rank first -- NOT "the position by which both are present" --
    it's a coarse "how quickly did *something* relevant surface" signal, not
    a correctness check; correctness is `hit`). None if no target rid appears
    in ctx_rids at all (a genuine retrieval miss), or if there's no target
    (negative case)."""
    required = case.get("required_rids") or []
    expected = case.get("expected_rids") or []
    ctx_set = set(ctx_rids)

    if required:
        hit = all(r in ctx_set for r in required)
    elif expected:
        hit = any(r in ctx_set for r in expected)
    else:
        hit = None  # negative case: nothing to hit or miss

    target = set(required) if required else set(expected)
    rank = None
    for i, rid in enumerate(ctx_rids, start=1):
        if rid in target:
            rank = i
            break
    return hit, rank


def run_retrieval_case(con, aliases, case, k, min_rel, cap_tokens,
                       allow_embed_mismatch=False):
    """Run ONE case's retrieval stage, fully offline: embed the query,
    deterministically pin any named entity (REVIEW_FINDINGS G1, via
    ragkit.match_entities), then ragkit.retrieve() with k/min_rel and NO
    metadata filter.

    Deliberately unfiltered: the "honest default" evaluation this harness's
    spec calls for is of the pipeline an actual offline (no-LLM) run
    exercises. There is no model to derive a filter from the query in that
    mode -- auto_filter is off -- so the unfiltered, entity-pinned retrieve()
    call below is exactly what ragkit.answer(auto_filter=False) does, not an
    approximation of it. Retrieving THROUGH each case's `expected_filter`
    instead would test a different, easier thing (that the eligible-set/
    entity-cap machinery, REVIEW_FINDINGS G1-G3, works when handed a correct
    filter -- already exercised by unit-level testing of retrieve() itself)
    and would silently paper over a real entity-pinning wiring bug behind a
    filter nothing at query time actually produces."""
    query = case["query"]
    catalogue = ragkit.load_catalogue(con)
    plan = ragkit.build_query_plan(
        con, query, catalogue=catalogue, aliases=aliases,
        field_aliases=ragkit.load_field_aliases())
    pin = ragkit.match_entities(query, aliases)
    contexts = ragkit.retrieve(con, query, k=k, pin_entities=pin,
                               min_rel=min_rel,
                               allow_embed_mismatch=allow_embed_mismatch)
    ctx_rids = [c["rid"] for c in contexts]
    hit, rank = score_retrieval(case, ctx_rids)
    prompt_capped = ragkit.build_prompt(query, contexts, max_context_tokens=cap_tokens)
    prompt_uncapped = ragkit.build_prompt(query, contexts, max_context_tokens=None)
    return {
        "id": case["id"],
        "class": case["class"],
        "query": query,
        "case": case,
        "contexts": contexts,
        "ctx_rids": ctx_rids,
        "n_pinned": len(pin),
        "plan": plan,
        "hit": hit,
        "rank": rank,
        "prompt_tokens_capped": ragkit._est_tokens(prompt_capped),
        "prompt_tokens_uncapped": ragkit._est_tokens(prompt_uncapped),
    }


# --------------------------------------------------------------------------- #
# Aggregation                                                                  #
# --------------------------------------------------------------------------- #

def _percentile(sorted_vals, pct):
    """Linear-interpolation percentile (same method as catalogue._percentile,
    reimplemented locally rather than reaching into that module's private
    helper for a one-line function -- this harness is meant to be readable
    stand-alone)."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _token_dist(values):
    if not values:
        return {}
    vals = sorted(values)
    return {"min": vals[0], "median": statistics.median(vals),
            "p95": _percentile(vals, 0.95), "max": vals[-1]}


def aggregate_retrieval(results, token_cap):
    """Roll per-case retrieval results up into the report's headline numbers.
    See score_retrieval's docstring for what `hit`/`rank` mean per case,
    including why a negative case's hit is None rather than True/False."""
    scored = [r for r in results if r["hit"] is not None]
    negatives = [r for r in results if r["hit"] is None]
    overall_hit_rate = (sum(1 for r in scored if r["hit"]) / len(scored)
                        if scored else None)

    by_class = {}
    for r in results:
        by_class.setdefault(r["class"], []).append(r)
    per_class = {}
    for cls, rs in by_class.items():
        cls_scored = [r for r in rs if r["hit"] is not None]
        per_class[cls] = {
            "n": len(rs),
            "n_negative": len(rs) - len(cls_scored),
            "hit_rate": (sum(1 for r in cls_scored if r["hit"]) / len(cls_scored)
                        if cls_scored else None),
        }

    ranks = [r["rank"] for r in results if r["rank"] is not None]
    rank_stats = {
        "mean": statistics.mean(ranks) if ranks else None,
        "median": statistics.median(ranks) if ranks else None,
        "n_ranked": len(ranks),
        # scored (non-negative) cases where NO target rid appeared at all --
        # a genuine retrieval miss, distinct from a comparison that surfaced
        # only one of its two required rids (hit=False but rank is still set).
        "n_missed": sum(1 for r in scored if r["rank"] is None),
    }

    capped = [r["prompt_tokens_capped"] for r in results]
    uncapped = [r["prompt_tokens_uncapped"] for r in results]
    prompt_stats = {
        "capped": _token_dist(capped),
        "uncapped": _token_dist(uncapped),
        "cap": token_cap,
        "n_over_cap_uncapped": sum(1 for v in uncapped if v > token_cap),
        "n_cases": len(results),
    }

    return {
        "overall_hit_rate": overall_hit_rate,
        "n_scored": len(scored),
        "n_negative": len(negatives),
        "per_class": per_class,
        "rank_stats": rank_stats,
        "prompt_stats": prompt_stats,
    }


# --------------------------------------------------------------------------- #
# Optional LLM stages (default OFF -- see run_eval's `backend` kwarg)          #
# --------------------------------------------------------------------------- #

def _filter_field_match(exp_cond, got_cond):
    """Lenient STRUCTURAL match between one field of a case's hand-authored
    `expected_filter` and the corresponding (validated, cleaned) field of the
    model-derived filter (REVIEW_FINDINGS F2 spec: "a lenient structural
    match is fine (same field name + same relation), document the matching
    rule" -- this is that rule, spelled out):

      - the field NAME match is the caller's job (this function is only ever
        called for a field name present in `expected_filter`, looked up by
        that same name in the cleaned filter -- see score_filter_extraction);
      - categorical/multi_value (`expected_filter` uses "in"): a match if the
        predicted "in" set and the expected "in" set share AT LEAST ONE
        label -- not exact-set equality. A model that filters to the right
        category plus a spurious extra label (or a subset) still gets useful,
        correctly-directed retrieval; exact-set equality would fail that for
        no practical reason, and validate_filter's own label fuzzy-matching
        (REVIEW_FINDINGS A3) already does the "close enough" label work.
      - numeric (`expected_filter` uses "min"/"max"): a match if the
        predicted condition constrains the SAME direction(s) -- both have
        "min", both have "max", or both have both. This deliberately does
        NOT compare the actual bound VALUES: "right numeric direction" (per
        the spec above) is the field-selection/intent question this harness
        cares about; whether 50 vs 45 is a better threshold is a prompt-
        tuning question for a different, finer-grained eval, and grading it
        here would conflate two different failure modes into one number.

    Returns False if the field is entirely absent from the predicted filter
    (got_cond is None/falsy) -- a missed field never "matches" no matter how
    lenient the rule."""
    if not got_cond:
        return False
    if "in" in exp_cond or "contains" in exp_cond:
        exp_vals = exp_cond.get("in") or exp_cond.get("contains") or []
        got_vals = got_cond.get("in") or got_cond.get("contains") or []
        return bool(_normalized_value_overlaps(exp_vals, got_vals))
    exp_dirs = {d for d in ("min", "max") if d in exp_cond}
    got_dirs = {d for d in ("min", "max") if d in got_cond}
    return bool(exp_dirs) and exp_dirs == got_dirs


_FIELD_EQUIVALENTS = {
    "Platform": {"Fitted to"},
    "Fitted to": {"Platform"},
    "Combat weight": {"Weight", "Mass"},
    "Weight": {"Combat weight", "Mass"},
    "Mass": {"Weight", "Combat weight"},
}


def _equivalent_filter_fields(field, catalogue):
    names = [field]
    for f in _FIELD_EQUIVALENTS.get(field, set()):
        if f in catalogue:
            names.append(f)
    return names


def _normalized_value_overlaps(expected, got):
    exp_norm = {ragkit._normalize_label(v) for v in expected}
    got_norm = {ragkit._normalize_label(v) for v in got}
    overlaps = set(exp_norm & got_norm)
    for e in exp_norm:
        for g in got_norm:
            if e and g and (e in g or g in e):
                overlaps.add(e if len(e) <= len(g) else g)
    return sorted(overlaps)


def _filter_value_overlap(exp_cond, got_cond):
    if not got_cond:
        return {"ok": False, "reason": "missing"}
    exp_vals = exp_cond.get("in") or exp_cond.get("contains") or []
    got_vals = got_cond.get("in") or got_cond.get("contains") or []
    if exp_vals or got_vals:
        overlap = _normalized_value_overlaps(exp_vals, got_vals)
        return {"ok": bool(overlap), "expected": exp_vals, "got": got_vals,
                "overlap": overlap}
    exp_dirs = {d for d in ("min", "max") if d in exp_cond}
    got_dirs = {d for d in ("min", "max") if d in got_cond}
    return {"ok": bool(exp_dirs) and exp_dirs == got_dirs,
            "expected_bounds": {k: exp_cond.get(k) for k in exp_dirs},
            "got_bounds": {k: got_cond.get(k) for k in got_dirs}}


def _extract_filter_like_answer(con, catalogue, query, backend, model, base_url):
    """Mirror ragkit.answer's own single-pass auto_filter field-selection
    logic (REVIEW_FINDINGS E2's DEFAULTS) so this harness scores filter
    extraction under the SAME conditions a real `--auto-filter` run would use
    -- the production filter_min_count/filter_max_fields pruning and
    field_select mode -- rather than against an artificially unpruned,
    un-selected catalogue spec that no real call site ever actually shows the
    model."""
    mode = ragkit.resolve_field_select_mode(con, ragkit.DEFAULTS["field_select"])
    only_fields = None
    if mode == "embed":
        always = cat_mod.partition_fields(
            catalogue, min_count=ragkit.DEFAULTS["filter_min_count"])
        only_fields = ragkit.select_fields(
            con, query, catalogue, k=ragkit.DEFAULTS["field_select_k"],
            always=always)
    return ragkit.extract_filter_ex(
        query, catalogue, backend, model, base_url, only_fields=only_fields,
        min_count=ragkit.DEFAULTS["filter_min_count"],
        max_fields=ragkit.DEFAULTS["filter_max_fields"])


def score_clean_filter(case, catalogue, clean, extraction_status="not_needed"):
    """Score an already validated filter, including deterministic/planned ones."""
    expected = case.get("expected_filter")
    if not expected:
        return None
    field_details = {}
    matched = 0
    for f, cond in expected.items():
        candidates = _equivalent_filter_fields(f, catalogue)
        got_field = next((name for name in candidates if name in clean), None)
        got_cond = clean.get(got_field) if got_field else None
        ok = _filter_field_match(cond, got_cond)
        if ok:
            matched += 1
        field_details[f] = {
            "matched": ok,
            "matched_as": got_field,
            "value": _filter_value_overlap(cond, got_cond),
        }
    precision = (matched / len(clean)) if clean else 0.0
    recall = (matched / len(expected)) if expected else None
    return {
        "expected_fields": sorted(expected), "got_fields": sorted(clean),
        "matched_fields": matched, "precision": precision, "recall": recall,
        "extraction_status": extraction_status, "field_details": field_details,
    }


def score_filter_extraction(case, con, catalogue, backend, model, base_url):
    """Field-level precision/recall of extract_filter_ex against a case's
    `expected_filter` (see _filter_field_match for the lenient match rule).
    None for a case with no `expected_filter` (only the analytic-* cases in
    eval_set.json have one -- lookups/parametrics/comparisons/prose don't
    need a filter to answer correctly, so there's nothing to grade here)."""
    expected = case.get("expected_filter")
    if not expected:
        return None
    raw, info = _extract_filter_like_answer(con, catalogue, case["query"],
                                            backend, model, base_url)
    clean, _errors = ragkit.validate_filter(raw, catalogue)
    return score_clean_filter(case, catalogue, clean, info["status"])


# Approximate, English-only "I don't have that information" phrasing check
# (documented per the F2 spec: "a keyword check ... document it"). Deliberately
# a plain substring list, not an LLM judge -- a negative case (REVIEW_FINDINGS
# G-series doesn't cover this, it's the eval_set's own "negative" class) is
# specifically testing that the model DIDN'T fabricate an answer; a simple,
# auditable keyword list is the right-weight tool for that, not a second LLM
# call whose own hallucination risk would need grading in turn.
_NEGATIVE_KEYWORDS = (
    "don't have", "do not have", "doesn't have", "does not have",
    "not available", "no information", "not in the", "not provided",
    "cannot find", "can't find", "unable to find", "doesn't contain",
    "does not contain", "no record", "not mentioned", "not present",
    "does not mention", "do not mention", "not mention",
    "does not reference", "do not reference", "not reference",
    "unknown based on",
)


_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9])")


def _normalize_numeric_text(s):
    return re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", str(s))


def _expected_answer_hit(expected, reply_low, reply_numeric_norm):
    exp = str(expected)
    if exp.lower() in reply_low:
        return True
    exp_norm = _normalize_numeric_text(exp).lower()
    if exp_norm and exp_norm in reply_numeric_norm:
        return True
    nums = [_normalize_numeric_text(m.group(0)) for m in _NUMBER_RE.finditer(exp)]
    if nums and all(n in reply_numeric_norm for n in nums):
        return True
    return False


def score_answer(case, reply):
    """expected_answer_contains / negative-case checks against a generated
    reply. Both are plain case-insensitive substring checks (documented
    here, not hidden): `expected_answer_contains` cases just need the exact
    fact-string to appear somewhere (e.g. "75" for the AIM-120's range); a
    negative case needs ANY of _NEGATIVE_KEYWORDS to appear (see that
    constant's docstring for why a keyword check, not an LLM judge, is the
    right tool here)."""
    result = {}
    low = (reply or "").lower()
    numeric_norm = _normalize_numeric_text(reply or "").lower()
    expects = case.get("expected_answer_contains") or []
    if expects:
        hits = [s for s in expects if _expected_answer_hit(s, low, numeric_norm)]
        result["answer_contains_ok"] = len(hits) == len(expects)
        result["answer_contains_hits"] = hits
        result["answer_contains_expected"] = expects
    # A `negative` case has no in-corpus record at all; a `robustness` case
    # flagged `expect_refusal` DOES have a record (retrieval should still find
    # it) but asks for a fictional attribute or a false-premise fact the model
    # must decline rather than invent. Both fold into the same "didn't
    # fabricate" keyword check / pass-rate metric.
    if case["class"] == "negative" or case.get("expect_refusal"):
        result["negative_ok"] = any(kw in low for kw in _NEGATIVE_KEYWORDS)
    return result


# [rid] citation pattern (REVIEW_FINDINGS G6): rids in this corpus are short
# numeric-ish strings (e.g. "2006"), so a generous-but-bounded bracket capture
# (up to 40 chars, no nested brackets) is enough to catch a real citation
# without also swallowing an unrelated "[42]"-style footnote from a different
# convention -- if a model cites in some other style entirely, this will just
# report zero citations rather than mis-parsing one, which is the safe
# failure direction for a verification check.
_CITATION_RE = re.compile(r"\[([^\[\]]{1,40})\]")
_RID_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _looks_like_rid_token(tok, ctx_set):
    return tok in ctx_set or any(ch.isdigit() for ch in tok)


def score_citations(reply, ctx_rids):
    """Regex the [rid] citations out of a generated reply and flag any that
    aren't actually among the rids retrieve() put in the prompt's context
    (REVIEW_FINDINGS G6 -- the classic small-model failure is citing a
    plausible-sounding id that was never shown to it)."""
    cited = set()
    ctx_set = set(ctx_rids)
    for m in _CITATION_RE.finditer(reply or ""):
        for tok in _RID_TOKEN_RE.findall(m.group(1)):
            tok = tok.strip()
            if _looks_like_rid_token(tok, ctx_set):
                cited.add(tok)
    cited = sorted(cited)
    hallucinated = [c for c in cited if c not in ctx_set]
    return {"cited": cited, "hallucinated": hallucinated}


def score_prepared_evidence(case, prep):
    """Score optional evidence requirements against the production envelope.

    Gold cases may provide `expected_evidence` with `fields`, `contains`, and
    `required_rids`. This deliberately inspects prepared evidence before answer
    generation, separating planning/context failures from model wording.
    """
    expected = case.get("expected_evidence") or {}
    if not expected:
        return None
    text_parts = [prep.get("prompt") or "", prep.get("deterministic_reply") or ""]
    table = prep.get("table") or {}
    for row in table.get("rows") or []:
        text_parts.append(json.dumps(row, default=str))
    haystack = "\n".join(text_parts).lower()
    fields = [str(x) for x in expected.get("fields") or []]
    contains = [str(x) for x in expected.get("contains") or []]
    required = {str(x) for x in expected.get("required_rids") or []}
    seen_rids = {str(c["rid"]) for c in prep.get("contexts") or []}
    seen_rids.update(str(x["rid"]) for x in prep.get("related") or [])
    seen_rids.update(str(x["rid"]) for x in table.get("rows") or [])
    direct = (prep.get("filter_info") or {}).get("direct_answer")
    if direct:
        seen_rids.add(str(direct["rid"]))
    field_hits = [f for f in fields if f.lower() in haystack]
    contains_hits = [v for v in contains if v.lower() in haystack]
    return {
        "ok": (len(field_hits) == len(fields)
               and len(contains_hits) == len(contains)
               and required <= seen_rids),
        "field_hits": field_hits,
        "contains_hits": contains_hits,
        "missing_rids": sorted(required - seen_rids),
    }


def run_llm_stages(con, catalogue, results, backend, model, base_url,
                   filter_model, max_context_tokens, checkpoint_jsonl=None):
    """Run the three opt-in LLM-dependent checks for every retrieval result
    and return {case_id: {...}}. Costs one filter-extraction call (only for
    cases with `expected_filter`) plus one answer-generation call PER CASE
    against `backend`/`model` -- real API spend/latency, which is exactly why
    this is never called unless the caller passed --backend explicitly (see
    run_eval)."""
    out = {}
    aliases = ragkit.load_aliases(con)
    field_aliases = ragkit.load_field_aliases()
    ck = (open(checkpoint_jsonl, "w", encoding="utf-8")
          if checkpoint_jsonl else None)
    try:
        for r in results:
            case = r["case"]
            entry = {}
            fmodel = filter_model or model
            prep = ragkit.prepare_answer(
                con, case["query"], backend=backend, model=model,
                base_url=base_url, auto_filter=True, filter_model=fmodel,
                max_context_tokens=max_context_tokens)
            finfo = prep["filter_info"]
            entry["plan"] = finfo.get("plan")
            fres = score_clean_filter(
                case, catalogue, finfo.get("applied") or {},
                finfo.get("extraction") or "empty")
            if fres:
                entry["filter"] = fres
            if prep["deterministic_reply"] is not None:
                reply = prep["deterministic_reply"]
                entry["deterministic"] = True
            else:
                reply = ragkit.generate(
                    case["query"], prep["contexts"], backend, model, base_url,
                    digest=prep["digest"], table=prep["table"],
                    related=prep["related"], relations=prep["relations"],
                    max_context_tokens=max_context_tokens)
            entry["reply"] = reply
            entry["answer"] = score_answer(case, reply)
            evidence_score = score_prepared_evidence(case, prep)
            if evidence_score is not None:
                entry["evidence"] = evidence_score
            allowed_rids = {c["rid"] for c in prep["contexts"]}
            allowed_rids.update(x["rid"] for x in prep["related"])
            if prep["table"]:
                allowed_rids.update(x["rid"] for x in prep["table"].get("rows", []))
            direct = finfo.get("direct_answer")
            if direct:
                allowed_rids.add(direct["rid"])
            entry["citations"] = score_citations(reply, sorted(allowed_rids))
            out[r["id"]] = entry
            if ck:
                ck.write(json.dumps({"id": r["id"], "llm": entry}, default=str) + "\n")
                ck.flush()
    finally:
        if ck:
            ck.close()
    return out


def aggregate_llm(llm_by_id):
    entries = list(llm_by_id.values())
    filt = [e["filter"] for e in entries if e.get("filter")]
    precision = [f["precision"] for f in filt]
    recall = [f["recall"] for f in filt if f["recall"] is not None]
    contains = [e["answer"]["answer_contains_ok"] for e in entries
               if "answer_contains_ok" in e.get("answer", {})]
    negatives = [e["answer"]["negative_ok"] for e in entries
                if "negative_ok" in e.get("answer", {})]
    hallucinated_cases = sum(1 for e in entries if e.get("citations", {}).get("hallucinated"))
    hallucinated_total = sum(len(e.get("citations", {}).get("hallucinated") or [])
                             for e in entries)
    return {
        "n_filter_cases": len(filt),
        "filter_precision_mean": statistics.mean(precision) if precision else None,
        "filter_recall_mean": statistics.mean(recall) if recall else None,
        "n_answer_contains_cases": len(contains),
        "answer_contains_pass_rate": (sum(contains) / len(contains)
                                      if contains else None),
        "n_negative_cases": len(negatives),
        "negative_pass_rate": sum(negatives) / len(negatives) if negatives else None,
        "cases_with_hallucinated_citation": hallucinated_cases,
        "hallucinated_citation_total": hallucinated_total,
    }


# --------------------------------------------------------------------------- #
# Reporting                                                                    #
# --------------------------------------------------------------------------- #

def _fmt_pct(x):
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def print_report(results, agg, llm_by_id, llm_agg):
    print("=" * 78)
    print("ragkit eval report (REVIEW_FINDINGS F2)")
    print("=" * 78)
    print(f"cases: {len(results)}  (scored {agg['n_scored']}, "
          f"negative/no-target {agg['n_negative']})")
    print(f"overall retrieval hit-rate: {_fmt_pct(agg['overall_hit_rate'])}")
    print()
    print(f"{'class':<12}{'n':>4}  {'hit-rate':>9}  {'negatives':>9}")
    seen = set()
    for cls in CLASS_ORDER + sorted(agg["per_class"]):
        if cls in seen or cls not in agg["per_class"]:
            continue
        seen.add(cls)
        s = agg["per_class"][cls]
        print(f"{cls:<12}{s['n']:>4}  {_fmt_pct(s['hit_rate']):>9}  {s['n_negative']:>9}")
    print()

    rs = agg["rank_stats"]
    if rs["n_ranked"]:
        print(f"rank of first hit: mean={rs['mean']:.2f}  median={rs['median']:.1f}  "
              f"(n={rs['n_ranked']}, missed={rs['n_missed']} scored cases with no "
              f"target rid retrieved at all)")
    else:
        print("rank of first hit: n/a (no case had a target rid retrieved)")
    print()

    ps = agg["prompt_stats"]
    for label in ("capped", "uncapped"):
        d = ps[label]
        if d:
            print(f"prompt tokens ({label:>8}): min={d['min']:>5}  "
                  f"median={d['median']:>6.0f}  p95={d['p95']:>6.0f}  max={d['max']:>6}")
    print(f"cases exceeding the {ps['cap']}-token cap when UNCAPPED: "
          f"{ps['n_over_cap_uncapped']}/{ps['n_cases']}")
    print()

    if llm_by_id is None:
        print("LLM stages (filter-extraction accuracy / answer check / citation "
              "verification): SKIPPED -- no --backend given, fully offline run.")
        return
    print(f"LLM stages ({len(llm_by_id)} case(s), backend given):")
    if llm_agg["n_filter_cases"]:
        print(f"  filter-extraction: mean precision={llm_agg['filter_precision_mean']:.2f}  "
              f"mean recall={llm_agg['filter_recall_mean']:.2f}  "
              f"(n={llm_agg['n_filter_cases']} case(s) with expected_filter)")
    else:
        print("  filter-extraction: n/a (no case with expected_filter)")
    if llm_agg["n_answer_contains_cases"]:
        print(f"  answer-contains-expected pass rate: "
              f"{_fmt_pct(llm_agg['answer_contains_pass_rate'])} "
              f"(n={llm_agg['n_answer_contains_cases']})")
    if llm_agg["n_negative_cases"]:
        print(f"  negative-case 'not available' pass rate: "
              f"{_fmt_pct(llm_agg['negative_pass_rate'])} "
              f"(n={llm_agg['n_negative_cases']})")
    print(f"  citation verification (REVIEW_FINDINGS G6): "
          f"{llm_agg['cases_with_hallucinated_citation']} case(s) cited a rid "
          f"not in context ({llm_agg['hallucinated_citation_total']} hallucinated "
          f"citation(s) total)")


def _case_report(r, llm_entry):
    """Compact per-case dict for the optional --json report: retrieval fields
    plus the matching LLM-stage entry (if the LLM stages ran), WITHOUT the
    full retrieved passage text (kept in-process for scoring/generation, not
    worth bloating the JSON report with -- ctx_rids/titles already say which
    records were retrieved)."""
    out = {
        "id": r["id"], "class": r["class"], "query": r["query"],
        "ctx_rids": r["ctx_rids"],
        "ctx_titles": [c["title"] for c in r["contexts"]],
        "n_pinned": r["n_pinned"], "hit": r["hit"], "rank": r["rank"],
        "plan": r.get("plan"),
        "prompt_tokens_capped": r["prompt_tokens_capped"],
        "prompt_tokens_uncapped": r["prompt_tokens_uncapped"],
    }
    if llm_entry is not None:
        out["llm"] = {k: v for k, v in llm_entry.items() if k != "reply"}
        out["llm"]["reply"] = llm_entry.get("reply")
    return out


def _select_cases(cases, case_ids=None, classes=None):
    selected = list(cases)
    if case_ids:
        wanted = [c.strip() for c in re.split(r"[,\s]+", case_ids) if c.strip()]
        wanted_set = set(wanted)
        selected = [c for c in selected if c.get("id") in wanted_set]
        missing = [cid for cid in wanted if cid not in {c.get("id") for c in selected}]
        if missing:
            raise RuntimeError(f"case id(s) not found: {', '.join(missing)}")
    if classes:
        cls_set = {c.strip() for c in re.split(r"[,\s]+", classes) if c.strip()}
        selected = [c for c in selected if c.get("class") in cls_set]
    return selected


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #

def run_eval(db="rag_test.db", eval_set=DEFAULT_EVAL_SET, k=None, min_rel=None,
            max_context_tokens=None, backend=None, model=None, base_url=None,
            filter_model=None, json_out=None, limit=None, case_ids=None,
            classes=None, checkpoint_jsonl=None, quiet=False,
            allow_embed_mismatch=False):
    """Run the full harness once and return (report_dict, ok). `ok` is True
    whenever the run itself completed (this is a MEASUREMENT tool, not a
    pass/fail quality gate -- there's no --fail-under threshold, deliberately:
    REVIEW_FINDINGS F2 asks for hit-rate/accuracy VISIBILITY so filtering and
    retrieval changes can be judged, not a hard-coded target this early in the
    corpus's life). A caller that wants a CI-style gate can inspect
    report["aggregate"]["overall_hit_rate"] itself.

    k/min_rel/max_context_tokens default to ragkit.DEFAULTS (REVIEW_FINDINGS
    E2) when None, so this harness measures against the SAME knobs `ragkit.py
    ask`/the bench actually use by default, not a harness-only configuration.

    backend=None (default) skips ALL THREE LLM-dependent stages entirely --
    filter-extraction accuracy, the answer/negative-phrasing check, and
    citation verification -- so the retrieval + prompt-size report (the
    primary deliverable) never requires network access or API spend. Passing
    --backend opts into real generation calls against that backend/model."""
    k = ragkit.DEFAULTS["k"] if k is None else k
    min_rel = ragkit.DEFAULTS["min_rel"] if min_rel is None else min_rel
    cap = ragkit.DEFAULTS["max_context_tokens"] if max_context_tokens is None else max_context_tokens

    cases = load_cases(eval_set)
    cases = _select_cases(cases, case_ids=case_ids, classes=classes)
    if limit:
        cases = cases[:limit]
    if not cases:
        raise RuntimeError(f"no cases loaded from {eval_set!r}")

    con = ragkit.connect_readonly(db)
    ragkit._check_embed_model(con, ragkit.DEFAULT_EMBED_MODEL,
                              allow_embed_mismatch)
    aliases = ragkit.load_aliases(con)
    catalogue = ragkit.load_catalogue(con)

    results = [run_retrieval_case(con, aliases, case, k=k, min_rel=min_rel,
                                  cap_tokens=cap,
                                  allow_embed_mismatch=allow_embed_mismatch)
              for case in cases]
    agg = aggregate_retrieval(results, token_cap=cap)

    llm_by_id, llm_agg = None, None
    if backend:
        llm_by_id = run_llm_stages(con, catalogue, results, backend, model,
                                   base_url, filter_model, cap,
                                   checkpoint_jsonl=checkpoint_jsonl)
        llm_agg = aggregate_llm(llm_by_id)

    if not quiet:
        print_report(results, agg, llm_by_id, llm_agg)

    report = {
        "db": db, "eval_set": eval_set, "k": k, "min_rel": min_rel,
        "prompt_token_cap": cap,
        "backend": backend, "model": model,
        "case_ids": case_ids, "classes": classes,
        "aggregate": agg,
        "llm_aggregate": llm_agg,
        "cases": [_case_report(r, (llm_by_id or {}).get(r["id"])) for r in results],
    }
    if json_out:
        with open(json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        if not quiet:
            print(f"\nfull JSON report written to {json_out}")

    return report, True


def build_arg_parser(parser=None):
    """Shared CLI flag set, used by BOTH this file's own entrypoint (`python
    eval.py ...`) and ragkit.py's `eval` subcommand (`python ragkit.py eval
    ...`) -- passing an existing subparser in via `parser` lets ragkit.py add
    these flags directly onto its own subcommand instead of maintaining a
    second, driftable copy of this flag list."""
    p = parser or argparse.ArgumentParser(
        description="offline ragkit eval harness (REVIEW_FINDINGS F2)")
    p.add_argument("--db", default="rag_test.db")
    p.add_argument("--eval-set", default=DEFAULT_EVAL_SET,
                   help=f"path to the gold eval set JSON (default {DEFAULT_EVAL_SET})")
    p.add_argument("--k", type=int, default=None,
                   help=f"retrieval k (default {ragkit.DEFAULTS['k']}, see DEFAULTS)")
    p.add_argument("--min-rel", type=float, default=None,
                   help=f"relevance floor, REVIEW_FINDINGS G3 (default "
                        f"{ragkit.DEFAULTS['min_rel']}, see DEFAULTS)")
    p.add_argument("--max-context-tokens", type=int, default=None,
                   help="prompt token cap used for the 'capped' stat -- the "
                        "'uncapped' stat always ALSO runs regardless, so the "
                        f"budgeter's effect is visible (default "
                        f"{ragkit.DEFAULTS['max_context_tokens']}, see DEFAULTS)")
    p.add_argument("--json", dest="json_out", default=None,
                   help="write the full per-case JSON report to this path")
    p.add_argument("--limit", type=int, default=None,
                   help="only run the first N cases (debugging)")
    p.add_argument("--case-ids", default=None,
                   help="comma/space separated case id list to run")
    p.add_argument("--class", dest="classes", default=None,
                   help="comma/space separated case class list to run")
    p.add_argument("--checkpoint-jsonl", default=None,
                   help="write one JSONL row per completed LLM case during hosted stages")
    p.add_argument("--backend", choices=("local", "anthropic", "openrouter", "openai"),
                   default=None,
                   help="OPT-IN: also run the LLM-dependent stages (filter-"
                        "extraction accuracy, answer/negative-phrasing check, "
                        "citation verification -- REVIEW_FINDINGS G6). Costs "
                        "real API calls. Omitting this flag (the default) "
                        "runs a fully offline retrieval + prompt-size report.")
    p.add_argument("--model", default=None, help="model for the LLM stages (if --backend given)")
    p.add_argument("--base-url", default=None, help="for --backend openai")
    p.add_argument("--filter-model", default=None,
                   help="separate model for the filter-extraction stage "
                        f"(default: reuse --model; bench's own default is "
                        f"{ragkit.DEFAULTS['filter_model']!r}, see DEFAULTS)")
    p.add_argument("--allow-embed-mismatch", action="store_true",
                   default=ragkit.DEFAULTS["allow_embed_mismatch"],
                   help="unsafe: allow querying an index built with a different "
                        "embedding model name (dimension mismatches still fail)")
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    try:
        _report, ok = run_eval(
            db=args.db, eval_set=args.eval_set, k=args.k, min_rel=args.min_rel,
            max_context_tokens=args.max_context_tokens, backend=args.backend,
            model=args.model, base_url=args.base_url,
            filter_model=args.filter_model, json_out=args.json_out,
            limit=args.limit, case_ids=args.case_ids, classes=args.classes,
            checkpoint_jsonl=args.checkpoint_jsonl,
            allow_embed_mismatch=args.allow_embed_mismatch)
    except ragkit.EmbedModelMismatchError as e:
        print(str(e), file=sys.stderr)
        return 1
    except Exception as e:
        print(f"eval failed: {e}", file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
