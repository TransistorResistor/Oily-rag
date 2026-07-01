#!/usr/bin/env python3
"""
models_registry.py - the model lineup shown in the comparison frontend.

Six models spanning capability tiers, each with a short description and rough
local hardware requirement (VRAM at a sensible quant) so you can weigh "could I
self-host this later" against "how good is the output" while trialing via
OpenRouter. `slug` is the OpenRouter model id used for the actual API call;
`local_hint` fields (params/vram/hardware) are informational only.

Slugs are real OpenRouter ids (verified at startup by compare_server against
OpenRouter's public /models list; unknown ones are flagged and disabled in the
UI). Figures are approximate Q4/Q5 VRAM for weights at short context; real usage
is higher with long context (KV cache). Edit freely as the lineup moves.
"""

MODELS = [
    {
        "id": "gemma3-4b",
        "name": "Gemma 3 4B",
        "tier": "Edge",
        "slug": "google/gemma-3-4b-it",
        "params": "4B dense",
        "vram": "~3 GB (Q4)",
        "hardware": "6 GB GPU / laptop",
        "desc": "Edge-tier, vision-capable. Fine for short grounded answers and "
                "structured output; struggles on multi-record synthesis and long "
                "context. Cheapest/fastest baseline in the ladder.",
    },
    {
        "id": "phi4-14b",
        "name": "Phi-4 14B",
        "tier": "Small-mid",
        "slug": "microsoft/phi-4",
        "params": "14B dense",
        "vram": "~8.5 GB (Q4)",
        "hardware": "12 GB GPU",
        "desc": "Strong STEM and reasoning per parameter. Weaker at tool calling "
                "and long-context retrieval — better for analytic answers than "
                "agentic filtering.",
    },
    {
        "id": "qwen3-14b",
        "name": "Qwen3 14B",
        "tier": "Mid",
        "slug": "qwen/qwen3-14b",
        "params": "14B dense",
        "vram": "~9 GB (Q4)",
        "hardware": "16 GB GPU",
        "desc": "Balanced general model with strong structured-output / JSON "
                "generation. Solid default for grounded RAG answers and a "
                "reliable filter-extraction model.",
    },
    {
        "id": "mistral-small-24b",
        "name": "Mistral Small 3.2 24B",
        "tier": "Mid-high",
        "slug": "mistralai/mistral-small-3.2-24b-instruct",
        "params": "24B dense",
        "vram": "~14 GB (Q4)",
        "hardware": "16-24 GB GPU",
        "desc": "Owns the agentic / JSON-output niche via native function "
                "calling. Vision-capable. Strong pick when filter-extraction "
                "reliability matters.",
    },
    {
        "id": "gemma3-27b",
        "name": "Gemma 3 27B",
        "tier": "High",
        "slug": "google/gemma-3-27b-it",
        "params": "27B dense",
        "vram": "~16 GB (Q4)",
        "hardware": "24 GB GPU",
        "desc": "Top-tier open-weight document understanding and multilingual "
                "reasoning, vision-capable. Strong all-round grounded generation "
                "at a still-self-hostable size.",
    },
    {
        "id": "qwen3-32b",
        "name": "Qwen3 32B",
        "tier": "Flagship",
        "slug": "qwen/qwen3-32b",
        "params": "32B dense",
        "vram": "~20 GB (Q4)",
        "hardware": "24 GB GPU (3090/4090-class)",
        "desc": "Flagship dense reasoner with an optional thinking mode; best "
                "answer fidelity and multi-record synthesis in this lineup, and "
                "the heaviest (but still single-GPU) to self-host.",
    },
]

MODELS_BY_ID = {m["id"]: m for m in MODELS}
