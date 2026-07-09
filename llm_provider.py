"""
HOSTED-LLM SWAP POINT
=====================

This file is the ONLY place the hosted-LLM transport lives. The OpenRouter
implementation below is a PLACEHOLDER. To adapt this project to another
provider or deployment, reimplement ``chat()`` and ``chat_with_usage()`` here
while keeping their existing contracts.

Where the prompts live (prompt changes belong there, not in this adapter):
- Answer generation: ``ragkit.system_prompt_for`` / ``ragkit.build_prompt``
- Filter extraction: the system prompt inside ``ragkit.extract_filter_ex``
- Claim extraction: ``enrich_demo/llm.py`` ``SYSTEM`` / ``USER_TMPL``
"""

import json
import os
import urllib.error
import urllib.request


BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT = 60


def load_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "key.env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                if line.strip().startswith("OPENROUTER_API_KEY="):
                    key = line.strip().split("=", 1)[1]
                    os.environ["OPENROUTER_API_KEY"] = key
                    return key
    raise RuntimeError(
        "OPENROUTER_API_KEY not set and key.env missing; "
        "set OPENROUTER_API_KEY to use the hosted LLM"
    )


def _http_json(req, timeout):
    """Fetch and parse JSON, surfacing readable network and HTTP errors."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (TimeoutError, urllib.error.URLError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(e, urllib.error.HTTPError):
            try:
                reason = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code}: {reason}") from None
        raise RuntimeError(f"request failed after {timeout}s: {reason}") from None


def chat_with_usage(system, user, model, *, max_tokens=None, temperature=0.1,
                    timeout=DEFAULT_TIMEOUT, response_format=None,
                    extra_headers=None) -> tuple:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if response_format:
        payload["response_format"] = response_format
    headers = {
        "authorization": f"Bearer {load_api_key()}",
        "content-type": "application/json",
    }
    headers.update(extra_headers or {})
    req = urllib.request.Request(
        BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(), headers=headers,
    )
    data = _http_json(req, timeout)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"provider returned no choices; usage={data.get('usage') or {}}")
    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content)
    if content is None:
        finish = choice.get("finish_reason")
        raise RuntimeError(
            "provider returned null message content "
            f"(finish_reason={finish!r}, usage={data.get('usage') or {}})"
        )
    text = str(content).strip()
    return text, data.get("usage", {}) or {}


def chat(system, user, model, *, max_tokens=None, temperature=0.1,
         timeout=DEFAULT_TIMEOUT, response_format=None,
         extra_headers=None) -> str:
    text, _usage = chat_with_usage(
        system, user, model, max_tokens=max_tokens, temperature=temperature,
        timeout=timeout, response_format=response_format,
        extra_headers=extra_headers,
    )
    return text


def list_models(timeout=DEFAULT_TIMEOUT) -> dict:
    req = urllib.request.Request(
        BASE_URL.rstrip("/") + "/models",
        headers={"user-agent": "ragkit-bench"},
    )
    return _http_json(req, timeout)
