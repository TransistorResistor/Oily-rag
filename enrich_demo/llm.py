#!/usr/bin/env python3
"""
llm.py - the single LLM touch-point: ONE claim-extraction call per document.
Mirrors ragkit._openrouter_raw (urllib, no extra deps) but is self-contained so
the demo doesn't import ragkit's heavy embedding stack. Returns neutral claims;
ALL schema mapping/validation happens deterministically downstream.

Model constraint: default is a LOW/MID-tier registry model (google/gemma-3-4b-it,
the cheapest "Edge" model) to honour the hard "must work on cheap models" rule and
to surface cheap-model extraction quirks. Override with --model.
"""

import json
import os
import urllib.request

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemma-3-4b-it"

# short aliases -> OpenRouter slugs (subset of models_registry)
ALIASES = {
    "gemma3-4b": "google/gemma-3-4b-it",
    "phi4-14b": "microsoft/phi-4",
    "qwen3-14b": "qwen/qwen3-14b",
    "mistral-small-24b": "mistralai/mistral-small-3.2-24b-instruct",
    "gemma3-27b": "google/gemma-3-27b-it",
    "qwen3-32b": "qwen/qwen3-32b",
}

SYSTEM = (
    "You extract neutral factual claims from a defence-equipment document. "
    "Return ONLY JSON of the form "
    '{"claims":[{"entity","attribute","value","unit","qualifier","quote"}]}. '
    "Rules: (1) entity = the system/weapon the claim is about, as named in the "
    "text. (2) attribute = the property, lowercase (e.g. 'maximum range', "
    "'operator', 'deployment time', 'alias', or a relation verb like 'fired "
    "from'). (3) value = the bare value with NO unit; put the unit in 'unit' "
    "(e.g. value '380', unit 'km'). (4) qualifier = hedges/conditions verbatim "
    "('up to','estimated','with 40N6 missile') or null. (5) quote = the exact "
    "sentence or phrase from the document that states the fact, copied verbatim. "
    "Extract every distinct fact. Do NOT invent values not present in the text. "
    "Do not add prose outside the JSON."
)

USER_TMPL = (
    "Document title: {title}\n\n"
    "Document text:\n\"\"\"\n{text}\n\"\"\"\n\n"
    "Return the claims JSON now."
)


def _load_key():
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    envf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "key.env")
    if os.path.exists(envf):
        for line in open(envf):
            if line.strip().startswith("OPENROUTER_API_KEY="):
                k = line.strip().split("=", 1)[1]
                os.environ["OPENROUTER_API_KEY"] = k
                return k
    raise RuntimeError("OPENROUTER_API_KEY not set and key.env missing")


def _first_balanced_json(text):
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def _parse(raw):
    text = (raw or "").strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    block = _first_balanced_json(text)
    if block:
        try:
            return json.loads(block)
        except Exception:
            pass
    return None


def extract_claims(title, text, model=DEFAULT_MODEL, timeout=90,
                   max_tokens=1500):
    """One LLM call. Returns (claims_list, usage_dict, raw_response, error)."""
    slug = ALIASES.get(model, model)
    key = _load_key()
    payload = {
        "model": slug,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user",
                      "content": USER_TMPL.format(title=title, text=text)}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        OPENROUTER_URL, data=body,
        headers={"content-type": "application/json",
                 "authorization": f"Bearer {key}",
                 "X-Title": "ragkit-enrich-demo"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    raw = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {}) or {}
    parsed = _parse(raw)
    if parsed is None:
        return [], usage, raw, "unparseable JSON"
    claims = parsed.get("claims") if isinstance(parsed, dict) else None
    if not isinstance(claims, list):
        return [], usage, raw, "no claims array"
    return claims, usage, raw, None
