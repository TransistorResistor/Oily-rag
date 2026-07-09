#!/usr/bin/env python3
"""Prepare a portable Hugging Face cache for a network-isolated deployment."""

import argparse
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "huggingface_models.json"


def main():
    ap = argparse.ArgumentParser(
        description="Download the pinned RAG model snapshots into a portable cache")
    ap.add_argument("--output", default=str(ROOT / ".hf-cache"),
                    help="portable HF_HOME directory (default: .hf-cache)")
    ap.add_argument("--include-local-generator", action="store_true",
                    help="also cache the optional Qwen local answer model")
    args = ap.parse_args()

    out = Path(args.output).resolve()
    hub = out / "hub"
    hub.mkdir(parents=True, exist_ok=True)
    # Set these before importing huggingface_hub. The resulting layout can be
    # copied as-is and selected later by offline_env.ps1.
    os.environ["HF_HOME"] = str(out)
    os.environ["HF_HUB_CACHE"] = str(hub)

    from huggingface_hub import snapshot_download

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    models = list(manifest["required"])
    if args.include_local_generator:
        models.extend(manifest.get("optional", []))

    print(f"Preparing portable Hugging Face cache: {out}")
    for model in models:
        print(f"  {model['role']}: {model['repo_id']} @ {model['revision']}")
        snapshot_download(
            repo_id=model["repo_id"], revision=model["revision"],
            cache_dir=str(hub), local_files_only=False,
        )

    (out / "ragkit-models.json").write_text(
        json.dumps({"models": models}, indent=2), encoding="utf-8")
    print("Cache complete. Copy this directory with the repository and run:")
    print("  . .\\offline_env.ps1")


if __name__ == "__main__":
    main()
