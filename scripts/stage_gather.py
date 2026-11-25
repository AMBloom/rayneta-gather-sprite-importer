#!/usr/bin/env python3
"""Phase 4 Option A — Build Tool Staging, Curation Sync & Upload Guide Generator."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from normalize_sprites import (
    BUCKET_BUILD,
    GATHER_READY_DIR,
    GRID_UNIT_PX,
    LOGS_DIR,
    WORKSPACE_ROOT,
    collect_gallery_items_from_disk,
    ensure_within_workspace,
    generate_preview_gallery,
    load_manifest,
    save_manifest,
    slugify,
)

UPLOAD_BATCHES_DIR = GATHER_READY_DIR / "Upload_Batches"
UPLOAD_GUIDE_PATH = GATHER_READY_DIR / "UPLOAD_GUIDE.md"
STAGING_LOG_PATH = LOGS_DIR / "staging.log"


def setup_logging() -> logging.Logger:
    """Configure Phase 4 logging inside logs/staging.log."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stage_gather")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(STAGING_LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def resolve_pack_slug(asset: dict[str, Any]) -> str:
    """Resolve the on-disk bucket folder slug for a manifest pack entry."""
    out_dir = asset.get("output_dir", "")
    if out_dir:
        return Path(out_dir).name
    return slugify(asset["pack_name"])


def inspect_curated_pack(
    asset: dict[str, Any],
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """Scan Build_Tool_Uploads/<pack_slug>/ on disk and collect sprite metadata."""
    pack_slug = resolve_pack_slug(asset)
    pack_dir = ensure_within_workspace(GATHER_READY_DIR / BUCKET_BUILD / pack_slug)
    sprites: list[dict[str, Any]] = []

    if not pack_dir.is_dir():
        logger.warning("Build_Tool_Uploads folder missing for '%s': %s", asset["pack_name"], pack_dir)
        return sprites

    for png_path in sorted(pack_dir.glob("*.png")):
        with Image.open(png_path) as img:
            w, h = img.size
        size_bytes = png_path.stat().st_size
        tiles_w = max(1, w // GRID_UNIT_PX)
        tiles_h = max(1, h // GRID_UNIT_PX)
        sprites.append(
            {
                "path": png_path,
                "filename": png_path.name,
                "pack_id": asset["id"],
                "pack_name": asset["pack_name"],
                "pack_slug": pack_slug,
                "license": asset.get("license", "unknown"),
                "author": asset.get("author", "unknown"),
                "width": w,
                "height": h,
                "tiles_w": tiles_w,
                "tiles_h": tiles_h,
                "tile_label": f"{tiles_w}x{tiles_h}",
                "size_kb": round(size_bytes / 1024.0, 2),
            }
        )
    return sprites


def stage_batches(
    sprites: list[dict[str, Any]],
    batch_size: int,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """Copy curated sprites into Upload_Batches/batch_XX/ prefixed with <pack_slug>_."""
    batches_root = ensure_within_workspace(UPLOAD_BATCHES_DIR)
    if batches_root.exists():
        shutil.rmtree(batches_root)
    batches_root.mkdir(parents=True, exist_ok=True)

    if batch_size < 1:
        raise ValueError("--batch-size must be >= 1")

    batches: list[dict[str, Any]] = []
    for idx in range(0, len(sprites), batch_size):
        chunk = sprites[idx : idx + batch_size]
        batch_num = (idx // batch_size) + 1
        batch_name = f"batch_{batch_num:02d}"
        batch_dir = ensure_within_workspace(batches_root / batch_name)
        batch_dir.mkdir(parents=True, exist_ok=True)

        staged_items: list[dict[str, Any]] = []
        used_names: set[str] = set()
        total_kb = 0.0

        for item in chunk:
            dest_name = f"{item['pack_slug']}_{item['filename']}"
            if dest_name in used_names:
                stem = Path(dest_name).stem
                suffix = Path(dest_name).suffix
                counter = 2
                while f"{stem}_{counter}{suffix}" in used_names:
                    counter += 1
                dest_name = f"{stem}_{counter}{suffix}"
            used_names.add(dest_name)

            dest_path = ensure_within_workspace(batch_dir / dest_name)
            shutil.copy2(item["path"], dest_path)
            total_kb += item["size_kb"]
            staged_items.append({**item, "batch_filename": dest_name})

        batches.append(
            {
                "batch_name": batch_name,
                "count": len(staged_items),
                "total_kb": round(total_kb, 2),
                "packs": sorted({it["pack_name"] for it in staged_items}),
                "items": staged_items,
            }
        )
        logger.info("Staged %s (%d PNGs, %.1f KB)", batch_name, len(staged_items), total_kb)

    return batches


def generate_upload_guide(
    pack_summaries: list[dict[str, Any]],
    batches: list[dict[str, Any]],
    batch_size: int,
    logger: logging.Logger,
) -> None:
    """Write gather_ready/UPLOAD_GUIDE.md with Gather v1.39.2 instructions and checklists."""
    ensure_within_workspace(UPLOAD_GUIDE_PATH)
    total_sprites = sum(b["count"] for b in batches)
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = [
        "# Rayneta Gather Sprite Upload Guide (v1.39.2)",
        "",
        f"*Generated: {now_utc} · Total Staged Sprites: **{total_sprites}** across **{len(batches)}** batch(es) (max {batch_size} per batch)*",
        "",
        "## Gather v1.39.2 Import Instructions (`Customize → Build → Uploaded`)",
        "",
        "1. Open your Rayneta Gather Space and select **Customize** (hammer/paintbrush icon) → **Build** → **Uploaded**.",
        "2. Open Finder at `gather_ready/Upload_Batches/`.",
        "3. Drag and drop **one `batch_XX/` folder's contents at a time** into the Gather Uploaded dropzone (batched at "
        f"≤{batch_size} PNGs to avoid browser upload stalls; filenames are prefixed with `<pack_slug>_` to prevent collisions).",
        "4. Review visual previews beforehand in `gather_ready/preview_gallery.html` — delete unwanted PNGs from "
        "`gather_ready/Build_Tool_Uploads/` and re-run `.venv/bin/python scripts/stage_gather.py --sync` before uploading.",
        "",
        "> **Important Gather Build Tool Notes:**",
        "> - Objects uploaded via **Build → Uploaded** are **personal to the uploader's account library** (not automatically shared across admins' upload trays until placed on the map).",
        "> - Once uploaded to Gather's Uploaded library, sprites **cannot be resized or deleted** from the library UI — verify footprints and cleanliness locally first.",
        "",
        "## Pack Summary & License Attribution",
        "",
        "| Pack | License | Author Attribution | Staged Sprites | Tile Footprints |",
        "| :--- | :--- | :--- | ---: | :--- |",
    ]

    for ps in pack_summaries:
        fp_text = ", ".join(f"`{k}`: {v}" for k, v in sorted(ps["footprints"].items())) or "—"
        lines.append(
            f"| {ps['pack_name']} | `{ps['license']}` | {ps['author']} | {ps['count']} | {fp_text} |"
        )

    lines.extend(
        [
            "",
            "## Upload Batch Overview",
            "",
            "| Batch | Sprites | Total Size | Packs Included |",
            "| :--- | ---: | ---: | :--- |",
        ]
    )
    for b in batches:
        packs_str = ", ".join(b["packs"])
        lines.append(f"| `{b['batch_name']}` | {b['count']} | {b['total_kb']:.1f} KB | {packs_str} |")

    lines.extend(["", "## Itemized Batch Checklists", ""])
    for b in batches:
        lines.append(f"### `{b['batch_name']}` ({b['count']} sprites · {b['total_kb']:.1f} KB)")
        lines.append("")
        for it in b["items"]:
            lines.append(
                f"- [ ] `{it['batch_filename']}` — `{it['tile_label']}` tiles (`{it['width']}×{it['height']} px`, "
                f"{it['size_kb']:.2f} KB, *{it['pack_name']}*)"
            )
        lines.append("")

    UPLOAD_GUIDE_PATH.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Generated %s", UPLOAD_GUIDE_PATH.relative_to(WORKSPACE_ROOT))


def sync_and_stage(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Reconcile Build_Tool_Uploads/, refresh manifest + gallery, batch, and write guide."""
    manifest = load_manifest()
    assets = manifest.get("assets", [])
    requested_packs = (
        {p.strip().lower() for p in args.packs.split(",") if p.strip()} if args.packs else None
    )

    all_staged_sprites: list[dict[str, Any]] = []
    pack_summaries: list[dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for asset in assets:
        pack_slug = resolve_pack_slug(asset)
        sprites = inspect_curated_pack(asset, logger)
        curated_count = len(sprites)

        asset["curated_count"] = curated_count
        if isinstance(asset.get("normalized_counts"), dict):
            asset["normalized_counts"]["build_tool_ready"] = curated_count
        asset["status"] = "staged_for_upload"
        asset["staged_at"] = now_iso

        include_pack = (
            requested_packs is None
            or pack_slug.lower() in requested_packs
            or asset["id"].lower() in requested_packs
        )
        if include_pack:
            all_staged_sprites.extend(sprites)
            fp_counts = Counter(s["tile_label"] for s in sprites)
            pack_summaries.append(
                {
                    "pack_name": asset["pack_name"],
                    "license": asset.get("license", "unknown"),
                    "author": asset.get("author", "unknown"),
                    "count": curated_count,
                    "footprints": dict(fp_counts),
                }
            )
            logger.info("Synced '%s' (%s): %d curated PNGs", asset["pack_name"], pack_slug, curated_count)
        else:
            logger.info(
                "Reconciled '%s' (%s): %d PNGs (excluded from batches by --packs)",
                asset["pack_name"],
                pack_slug,
                curated_count,
            )

    save_manifest(manifest)

    gallery_items = collect_gallery_items_from_disk(manifest)
    generate_preview_gallery(gallery_items, logger)

    batches = stage_batches(all_staged_sprites, args.batch_size, logger)
    generate_upload_guide(pack_summaries, batches, args.batch_size, logger)

    logger.info(
        "Phase 4 Option A complete: %d sprites staged into %d batch(es) under %s",
        len(all_staged_sprites),
        len(batches),
        UPLOAD_BATCHES_DIR.relative_to(WORKSPACE_ROOT),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rayneta Gather Sprite Importer — Phase 4 Option A Staging CLI")
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Reconcile local curations, refresh gallery, stage batches, and generate UPLOAD_GUIDE.md",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=40,
        help="Max PNGs per Upload_Batches/batch_XX folder (default: 40)",
    )
    parser.add_argument(
        "--packs",
        type=str,
        default=None,
        help="Optional comma-separated pack slugs or IDs to include in Upload_Batches/",
    )
    return parser


def main() -> int:
    logger = setup_logging()
    args = build_parser().parse_args()
    if not args.sync:
        logger.info("Running sync by default (pass --sync explicitly in automated runs).")
    return sync_and_stage(args, logger)


if __name__ == "__main__":
    raise SystemExit(main())
