#!/usr/bin/env python3
"""Phase 5 Unified Pipeline Orchestrator — Acquire -> Normalize -> Stage."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PIL import Image

from acquire_assets import (
    download_and_stage_asset,
    http_session,
    search_itch,
    search_opengameart,
)
from normalize_sprites import (
    BUCKET_ALPHA_REVIEW,
    BUCKET_ANIM_FRAMES,
    BUCKET_BUILD,
    BUCKET_OVERSIZED,
    GATHER_READY_DIR,
    LOGS_DIR,
    WORKSPACE_ROOT,
    ensure_within_workspace,
    load_manifest,
    process_pack,
    save_manifest,
)
from stage_gather import sync_and_stage

THEMES_PATH = WORKSPACE_ROOT / "config" / "themes.json"
PIPELINE_LOG_PATH = LOGS_DIR / "pipeline.log"


def setup_logging() -> logging.Logger:
    """Configure Phase 5 pipeline logging inside logs/pipeline.log."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("run_pipeline")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(PIPELINE_LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def load_themes() -> dict[str, Any]:
    if not THEMES_PATH.is_file():
        raise FileNotFoundError(f"Missing themes config at {THEMES_PATH}")
    return json.loads(THEMES_PATH.read_text(encoding="utf-8"))


def save_themes(data: dict[str, Any]) -> None:
    ensure_within_workspace(THEMES_PATH)
    tmp_path = THEMES_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, THEMES_PATH)


def select_preset(themes_data: dict[str, Any], preset_id: str | None) -> dict[str, Any]:
    """Pick either a specific preset by ID or the least-recently-run preset."""
    presets = themes_data.get("presets", [])
    if not presets:
        raise ValueError("No presets configured in config/themes.json")

    if preset_id:
        for preset in presets:
            if preset.get("id") == preset_id:
                return preset
        raise ValueError(f"Unknown theme preset ID: {preset_id}")

    return min(presets, key=lambda p: p.get("last_run_at") or "")


def seed_existing_pixel_hashes(logger: logging.Logger) -> set[str]:
    """Hash existing normalized RGBA outputs so new packs deduplicate against curated sprites."""
    hashes: set[str] = set()
    for bucket in (BUCKET_BUILD, BUCKET_ALPHA_REVIEW, BUCKET_ANIM_FRAMES, BUCKET_OVERSIZED):
        bucket_dir = GATHER_READY_DIR / bucket
        if not bucket_dir.is_dir():
            continue
        for png_path in bucket_dir.rglob("*.png"):
            try:
                with Image.open(png_path) as img:
                    hashes.add(hashlib.sha256(img.convert("RGBA").tobytes()).hexdigest())
            except Exception:
                logger.warning("Could not hash existing normalized sprite: %s", png_path)
    return hashes


def run_acquisition(
    queries: list[str],
    source: str,
    limit: int,
    dry_run: bool,
    logger: logging.Logger,
) -> tuple[int, int]:
    """Run Phase 2 searches and download newly verified CC0/CC-BY packs."""
    candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    with http_session() as session:
        for query in queries:
            found: list[dict[str, Any]] = []
            if source in ("opengameart", "all"):
                found.extend(search_opengameart(session, query, limit, logger))
            if source in ("itch", "all"):
                found.extend(search_itch(session, query, limit, logger))

            for item in found:
                if item["source_url"] not in seen_urls:
                    seen_urls.add(item["source_url"])
                    candidates.append(item)

        if dry_run:
            logger.info("[DRY-RUN] Found %d unique license-verified candidate pack(s):", len(candidates))
            for idx, item in enumerate(candidates, 1):
                logger.info(
                    "  [%d] %s | %s | %s | by %s | %s",
                    idx,
                    item["source_type"],
                    item["license"],
                    item["pack_name"],
                    item["author"],
                    item["source_url"],
                )
            return len(candidates), 0

        downloaded = 0
        for item in candidates:
            if download_and_stage_asset(session, item, logger):
                downloaded += 1
        return len(candidates), downloaded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rayneta Gather Sprite Importer — Phase 5 Unified Pipeline Orchestrator"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--theme", type=str, help="Custom search query for an ad-hoc themed sprite drop")
    mode.add_argument("--theme-preset", type=str, help="Preset ID from config/themes.json")
    mode.add_argument(
        "--rotate-next",
        action="store_true",
        help="Select least-recently-run preset from config/themes.json",
    )

    parser.add_argument(
        "--source",
        choices=["opengameart", "itch", "all"],
        default="opengameart",
        help="Repository source (default: opengameart for unattended runs)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max verified packs per query (defaults to preset limit or 3)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=40,
        help="Max PNGs per Upload_Batches/ folder (default: 40)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview searches and counts without writing files",
    )
    return parser


def main() -> int:
    logger = setup_logging()
    args = build_parser().parse_args()

    themes_data: dict[str, Any] | None = None
    selected_preset: dict[str, Any] | None = None

    if args.theme:
        run_label = f"Custom theme: {args.theme}"
        queries = [args.theme]
        limit = args.limit if args.limit is not None else 3
    else:
        themes_data = load_themes()
        selected_preset = select_preset(themes_data, args.theme_preset)
        run_label = f"Preset '{selected_preset['id']}' ({selected_preset['label']})"
        queries = list(selected_preset["queries"])
        limit = args.limit if args.limit is not None else int(selected_preset.get("limit_per_query", 2))

    logger.info("=== Pipeline Start (%s | source=%s | dry_run=%s) ===", run_label, args.source, args.dry_run)

    candidate_count, acquired_count = run_acquisition(queries, args.source, limit, args.dry_run, logger)

    manifest = load_manifest()
    pending_packs = [a for a in manifest.get("assets", []) if a.get("status") == "raw_staged"]

    if args.dry_run:
        logger.info(
            "[DRY-RUN] Summary: %d candidate pack(s) verified, %d existing raw_staged pack(s) pending; "
            "skipping downloads, normalization writes, staging, and theme timestamp update.",
            candidate_count,
            len(pending_packs),
        )
        return 0

    newly_normalized_ready = 0
    if pending_packs:
        seen_hashes = seed_existing_pixel_hashes(logger)
        norm_args = SimpleNamespace(
            mode="auto",
            grid_size=32,
            bridge_gap=1,
            include_all_frames=False,
            include_shadows=False,
            dry_run=False,
        )
        for asset in pending_packs:
            process_pack(asset, norm_args, seen_hashes, logger)
            newly_normalized_ready += int(asset.get("normalized_counts", {}).get("build_tool_ready", 0))
        save_manifest(manifest)
    else:
        logger.info("No new raw_staged packs to normalize; syncing existing curated sprites.")

    stage_args = SimpleNamespace(sync=True, batch_size=args.batch_size, packs=None)
    sync_and_stage(stage_args, logger)

    if themes_data is not None and selected_preset is not None:
        selected_preset["last_run_at"] = datetime.now(timezone.utc).isoformat()
        save_themes(themes_data)
        logger.info("Updated last_run_at for preset '%s'.", selected_preset["id"])

    logger.info(
        "=== Pipeline Complete: acquired=%d pack(s), newly_normalized_ready=%d sprite(s) ===",
        acquired_count,
        newly_normalized_ready,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
