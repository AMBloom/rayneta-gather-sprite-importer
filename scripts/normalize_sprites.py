#!/usr/bin/env python3
"""Phase 3 Image Normalization Pipeline & Visual Curation Gallery for Rayneta Gather."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import math
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
RAW_SPRITES_DIR = WORKSPACE_ROOT / "raw_sprites"
GATHER_READY_DIR = WORKSPACE_ROOT / "gather_ready"
MANIFEST_PATH = WORKSPACE_ROOT / "manifest.json"
LOGS_DIR = WORKSPACE_ROOT / "logs"
LOG_PATH = LOGS_DIR / "normalization.log"
GALLERY_PATH = GATHER_READY_DIR / "preview_gallery.html"

GRID_UNIT_PX = 32
MAX_FOOTPRINT_PX = 256
MAX_FILE_BYTES = 3 * 1024 * 1024
MIN_COMPONENT_DIM = 6
STRIP_HEIGHTS = {16, 32, 48, 64}

BUCKET_BUILD = "Build_Tool_Uploads"
BUCKET_ALPHA_REVIEW = "_needs_alpha_review"
BUCKET_ANIM_FRAMES = "_animation_frames"
BUCKET_OVERSIZED = "_oversized_rejected"


def setup_logging() -> logging.Logger:
    """Configure normalization logging inside logs/normalization.log."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("normalize_sprites")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def ensure_within_workspace(path: Path) -> Path:
    """Enforce strict workspace lock for all reads and writes."""
    resolved = path.resolve()
    if os.path.commonpath([str(WORKSPACE_ROOT), str(resolved)]) != str(WORKSPACE_ROOT):
        raise ValueError(f"Path escapes workspace lock: {resolved}")
    return resolved


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:60] or "pack"


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Missing manifest.json at {MANIFEST_PATH}")
    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_manifest(manifest: dict[str, Any]) -> None:
    ensure_within_workspace(MANIFEST_PATH)
    tmp_path = MANIFEST_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    os.replace(tmp_path, MANIFEST_PATH)


def should_skip_raw_file(
    rel_path: str,
    width: int,
    height: int,
    pack_slug: str,
    include_shadows: bool,
) -> str | None:
    """Return a skip reason string if this raw file should be excluded, else None."""
    name_lower = Path(rel_path).name.lower()
    parts_lower = [p.lower() for p in Path(rel_path).parts]

    if name_lower.startswith(("preview", "sample")) or (width == 918 and height == 515):
        return "marketing/preview banner"

    if ("megapackfree" in pack_slug or "mega_pack_free" in pack_slug) and rel_path.lower().endswith(
        "freeenvironment/freepack.png"
    ):
        return "redundant master sheet (extracted_sprites already staged)"

    if "topdown_shooter" in pack_slug and any(p in ("spritesheet", "tilesheet") for p in parts_lower):
        return "redundant spritesheet/tilesheet (PNG/ individual files already staged)"

    if not include_shadows and name_lower in ("tx shadow.png", "tx shadow plant.png"):
        return "shadow-only layer excluded (use --include-shadows to include)"

    return None


def classify_sheet(rel_path: str, width: int, height: int, forced_mode: str) -> str:
    """Determine slicing mode for an input image."""
    if forced_mode != "auto":
        return forced_mode

    if width <= 96 and height <= 96:
        return "single"

    if width >= 2 * height and height in STRIP_HEIGHTS and (width % height == 0):
        return "strip"

    name_lower = Path(rel_path).name.lower()
    if "tileset" in name_lower or "tx player" in name_lower:
        return "grid"

    return "components"


def slice_strip(img: Image.Image) -> list[tuple[str, Image.Image, bool]]:
    """Slice horizontal animation strip into square frames; flag non-zero frames."""
    w, h = img.size
    frame_size = h
    cols = w // frame_size
    frames: list[tuple[str, Image.Image, bool]] = []
    first_non_empty_seen = False

    for idx in range(cols):
        left = idx * frame_size
        tile = img.crop((left, 0, left + frame_size, frame_size))
        if tile.getbbox() is None:
            continue
        is_extra = first_non_empty_seen
        first_non_empty_seen = True
        frames.append((f"f{idx:02d}", tile, is_extra))
    return frames


def slice_grid(img: Image.Image, grid_size: int) -> list[tuple[str, Image.Image, bool]]:
    """Slice uniform multi-row sheet into grid_size x grid_size non-empty cells."""
    w, h = img.size
    cols = w // grid_size
    rows = h // grid_size
    cells: list[tuple[str, Image.Image, bool]] = []

    for r in range(rows):
        for c in range(cols):
            left = c * grid_size
            top = r * grid_size
            cell = img.crop((left, top, left + grid_size, top + grid_size))
            _, alpha_max = cell.getchannel("A").getextrema()
            if alpha_max == 0:
                continue
            cells.append((f"r{r:02d}_c{c:02d}", cell, False))
    return cells


def slice_components(img: Image.Image, bridge_gap: int) -> list[tuple[str, Image.Image, bool]]:
    """Extract connected non-transparent alpha islands via iterative DFS with gap bridging."""
    rgba = img.convert("RGBA")
    w, h = rgba.size
    alpha = rgba.getchannel("A").tobytes()
    src_bytes = rgba.tobytes()
    visited = bytearray(w * h)

    radius = 1 + max(0, bridge_gap)
    offsets = [
        (dx, dy)
        for dy in range(-radius, radius + 1)
        for dx in range(-radius, radius + 1)
        if dx != 0 or dy != 0
    ]

    raw_components: list[tuple[int, int, int, int, list[int]]] = []

    for start_idx, a_val in enumerate(alpha):
        if a_val == 0 or visited[start_idx]:
            continue

        visited[start_idx] = 1
        stack = [start_idx]
        comp_pixels: list[int] = []
        min_x = max_x = start_idx % w
        min_y = max_y = start_idx // w

        while stack:
            idx = stack.pop()
            comp_pixels.append(idx)
            x = idx % w
            y = idx // w
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y

            for dx, dy in offsets:
                nx = x + dx
                ny = y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    n_idx = ny * w + nx
                    if alpha[n_idx] > 0 and not visited[n_idx]:
                        visited[n_idx] = 1
                        stack.append(n_idx)

        cw = max_x - min_x + 1
        ch = max_y - min_y + 1
        if cw < MIN_COMPONENT_DIM and ch < MIN_COMPONENT_DIM:
            continue
        raw_components.append((min_y, min_x, max_x, max_y, comp_pixels))

    raw_components.sort(key=lambda item: (item[0], item[1]))
    results: list[tuple[str, Image.Image, bool]] = []

    for idx, (min_y, min_x, max_x, max_y, comp_pixels) in enumerate(raw_components):
        cw = max_x - min_x + 1
        ch = max_y - min_y + 1
        out_buf = bytearray(cw * ch * 4)
        for p_idx in comp_pixels:
            px = (p_idx % w) - min_x
            py = (p_idx // w) - min_y
            src_off = p_idx * 4
            dst_off = (py * cw + px) * 4
            out_buf[dst_off : dst_off + 4] = src_bytes[src_off : src_off + 4]
        comp_img = Image.frombytes("RGBA", (cw, ch), bytes(out_buf))
        results.append((f"obj{idx:03d}", comp_img, False))

    return results


def normalize_sprite(
    sprite: Image.Image,
    is_anim_extra: bool,
    include_all_frames: bool,
) -> tuple[Image.Image, str, str] | None:
    """Normalize sprite to Gather v1.39.2 RGBA spec and determine target bucket."""
    rgba = sprite.convert("RGBA")
    bbox = rgba.getbbox()
    if bbox is None:
        return None

    trimmed = rgba.crop(bbox)
    tw, th = trimmed.size

    if max(tw, th) <= 16:
        trimmed = trimmed.resize((tw * 2, th * 2), Image.Resampling.NEAREST)
        tw, th = trimmed.size

    alpha_min, _ = trimmed.getchannel("A").getextrema()
    needs_alpha_review = alpha_min == 255

    target_w = max(GRID_UNIT_PX, math.ceil(tw / GRID_UNIT_PX) * GRID_UNIT_PX)
    target_h = max(GRID_UNIT_PX, math.ceil(th / GRID_UNIT_PX) * GRID_UNIT_PX)

    canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    offset_x = (target_w - tw) // 2
    offset_y = target_h - th
    canvas.paste(trimmed, (offset_x, offset_y))

    if target_w > MAX_FOOTPRINT_PX or target_h > MAX_FOOTPRINT_PX:
        bucket = BUCKET_OVERSIZED
    elif is_anim_extra and not include_all_frames:
        bucket = BUCKET_ANIM_FRAMES
    elif needs_alpha_review:
        bucket = BUCKET_ALPHA_REVIEW
    else:
        bucket = BUCKET_BUILD

    pixel_hash = hashlib.sha256(canvas.tobytes()).hexdigest()
    return canvas, bucket, pixel_hash


def clean_pack_buckets(pack_slug: str) -> None:
    """Remove prior normalized outputs for pack_slug so re-runs stay idempotent."""
    for bucket in (BUCKET_BUILD, BUCKET_ALPHA_REVIEW, BUCKET_ANIM_FRAMES, BUCKET_OVERSIZED):
        target = ensure_within_workspace(GATHER_READY_DIR / bucket / pack_slug)
        if target.exists():
            shutil.rmtree(target)


def build_output_stem(rel_path: str, pack_slug: str, suffix: str) -> str:
    """Create a readable, collision-free output filename stem."""
    rel = Path(rel_path)
    parts = list(rel.parts)
    if len(parts) >= 2 and parts[0] == "local_packs":
        parts = parts[2:]
    elif len(parts) >= 2 and slugify(parts[0]).startswith(pack_slug[:12]):
        parts = parts[1:]

    clean_parts = [slugify(p) for p in parts[:-1]] + [slugify(rel.stem)]
    base = "_".join(p for p in clean_parts if p)
    if suffix:
        base = f"{base}_{suffix}"
    return base[:110] or "sprite"


def process_pack(
    asset: dict[str, Any],
    args: argparse.Namespace,
    seen_hashes: set[str],
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """Process one manifest pack entry and return gallery metadata items."""
    pack_id = asset["id"]
    pack_name = asset["pack_name"]
    pack_slug = slugify(pack_name)
    pack_license = asset.get("license", "unknown")

    logger.info("Processing pack '%s' (%s)...", pack_name, pack_id)
    if not args.dry_run:
        clean_pack_buckets(pack_slug)

    counts = {
        "build_tool_ready": 0,
        "needs_alpha_review": 0,
        "animation_frames": 0,
        "oversized_rejected": 0,
        "duplicates_skipped": 0,
        "raw_files_skipped": 0,
    }
    gallery_items: list[dict[str, Any]] = []
    used_names: dict[str, int] = {}

    for raw_entry in asset.get("raw_files", []):
        rel_path = raw_entry["path"]
        w = raw_entry["width"]
        h = raw_entry["height"]

        skip_reason = should_skip_raw_file(rel_path, w, h, pack_slug, args.include_shadows)
        if skip_reason:
            counts["raw_files_skipped"] += 1
            logger.debug("Skipped %s (%s)", rel_path, skip_reason)
            continue

        src_path = ensure_within_workspace(RAW_SPRITES_DIR / rel_path)
        if not src_path.is_file():
            logger.warning("Missing raw file on disk: %s", rel_path)
            continue

        mode = classify_sheet(rel_path, w, h, args.mode)
        with Image.open(src_path) as raw_img:
            rgba_src = raw_img.convert("RGBA")
            if mode == "single":
                slices = [("", rgba_src, False)]
            elif mode == "strip":
                slices = slice_strip(rgba_src)
            elif mode == "grid":
                slices = slice_grid(rgba_src, args.grid_size)
            else:
                slices = slice_components(rgba_src, args.bridge_gap)

        for suffix, slice_img, is_anim_extra in slices:
            norm = normalize_sprite(slice_img, is_anim_extra, args.include_all_frames)
            if norm is None:
                continue
            canvas, bucket, pixel_hash = norm

            if pixel_hash in seen_hashes:
                counts["duplicates_skipped"] += 1
                continue
            seen_hashes.add(pixel_hash)

            cw, ch = canvas.size
            stem = build_output_stem(rel_path, pack_slug, suffix)
            key = f"{bucket}/{stem}"
            used_names[key] = used_names.get(key, 0) + 1
            if used_names[key] > 1:
                stem = f"{stem}_{used_names[key]}"

            dim_tag = f"{cw}x{ch}"
            filename = f"{stem}_{dim_tag}.png"
            out_rel = Path(bucket) / pack_slug / filename
            out_abs = ensure_within_workspace(GATHER_READY_DIR / out_rel)

            if bucket == BUCKET_OVERSIZED:
                logger.warning(
                    "Oversized output (%dx%d > %dx%d) routed to %s: %s",
                    cw,
                    ch,
                    MAX_FOOTPRINT_PX,
                    MAX_FOOTPRINT_PX,
                    BUCKET_OVERSIZED,
                    out_rel.as_posix(),
                )

            file_size_bytes = 0
            if not args.dry_run:
                out_abs.parent.mkdir(parents=True, exist_ok=True)
                canvas.save(out_abs, format="PNG", optimize=True)
                file_size_bytes = out_abs.stat().st_size
                if file_size_bytes >= MAX_FILE_BYTES:
                    logger.error(
                        "File exceeds 3 MB Gather limit (%d bytes); deleting %s",
                        file_size_bytes,
                        out_rel.as_posix(),
                    )
                    out_abs.unlink(missing_ok=True)
                    continue

            if bucket == BUCKET_BUILD:
                counts["build_tool_ready"] += 1
            elif bucket == BUCKET_ALPHA_REVIEW:
                counts["needs_alpha_review"] += 1
            elif bucket == BUCKET_ANIM_FRAMES:
                counts["animation_frames"] += 1
            elif bucket == BUCKET_OVERSIZED:
                counts["oversized_rejected"] += 1

            tiles_w = cw // GRID_UNIT_PX
            tiles_h = ch // GRID_UNIT_PX
            gallery_items.append(
                {
                    "rel_path": out_rel.as_posix(),
                    "filename": filename,
                    "pack_name": pack_name,
                    "pack_slug": pack_slug,
                    "license": pack_license,
                    "bucket": bucket,
                    "width": cw,
                    "height": ch,
                    "tile_label": f"{tiles_w}x{tiles_h}",
                    "size_kb": round(file_size_bytes / 1024.0, 2),
                }
            )

    logger.info(
        "Pack '%s' summary%s: build_tool_ready=%d, needs_alpha_review=%d, animation_frames=%d, "
        "oversized_rejected=%d, duplicates_skipped=%d, raw_files_skipped=%d",
        pack_name,
        " [DRY-RUN]" if args.dry_run else "",
        counts["build_tool_ready"],
        counts["needs_alpha_review"],
        counts["animation_frames"],
        counts["oversized_rejected"],
        counts["duplicates_skipped"],
        counts["raw_files_skipped"],
    )

    if not args.dry_run:
        asset["status"] = "normalized"
        asset["output_dir"] = f"{BUCKET_BUILD}/{pack_slug}"
        asset["normalized_counts"] = counts
        asset["normalized_at"] = datetime.now(timezone.utc).isoformat()

    return gallery_items


def collect_gallery_items_from_disk(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Scan all buckets under gather_ready/ so preview_gallery.html reflects full current state."""
    pack_meta = {slugify(a["pack_name"]): a for a in manifest.get("assets", [])}
    items: list[dict[str, Any]] = []

    for bucket in (BUCKET_BUILD, BUCKET_ALPHA_REVIEW, BUCKET_ANIM_FRAMES, BUCKET_OVERSIZED):
        bucket_dir = GATHER_READY_DIR / bucket
        if not bucket_dir.is_dir():
            continue
        for png_path in sorted(bucket_dir.rglob("*.png")):
            rel = png_path.relative_to(GATHER_READY_DIR).as_posix()
            parts = png_path.relative_to(bucket_dir).parts
            pack_slug = parts[0] if len(parts) > 1 else "unknown"
            meta = pack_meta.get(pack_slug, {})
            with Image.open(png_path) as img:
                cw, ch = img.size
            size_kb = round(png_path.stat().st_size / 1024.0, 2)
            items.append(
                {
                    "rel_path": rel,
                    "filename": png_path.name,
                    "pack_name": meta.get("pack_name", pack_slug),
                    "pack_slug": pack_slug,
                    "license": meta.get("license", "unknown"),
                    "bucket": bucket,
                    "width": cw,
                    "height": ch,
                    "tile_label": f"{cw // GRID_UNIT_PX}x{ch // GRID_UNIT_PX}",
                    "size_kb": size_kb,
                }
            )
    return items


def generate_preview_gallery(items: list[dict[str, Any]], logger: logging.Logger) -> None:
    """Generate self-contained offline HTML visual curation gallery."""
    ensure_within_workspace(GALLERY_PATH)
    GATHER_READY_DIR.mkdir(parents=True, exist_ok=True)

    packs = sorted({item["pack_name"] for item in items})
    buckets = [BUCKET_BUILD, BUCKET_ALPHA_REVIEW, BUCKET_ANIM_FRAMES, BUCKET_OVERSIZED]
    tile_sizes = sorted({item["tile_label"] for item in items})

    cards_html: list[str] = []
    for item in items:
        e_path = html.escape(item["rel_path"], quote=True)
        e_name = html.escape(item["filename"], quote=True)
        e_pack = html.escape(item["pack_name"], quote=True)
        e_lic = html.escape(item["license"], quote=True)
        e_bucket = html.escape(item["bucket"], quote=True)
        e_tiles = html.escape(item["tile_label"], quote=True)
        dims = f"{item['width']}×{item['height']}"

        cards_html.append(
            f'<div class="card" data-bucket="{e_bucket}" data-pack="{e_pack}" '
            f'data-tiles="{e_tiles}" data-name="{e_name.lower()}" '
            f'data-path="{e_path}" data-kb="{item["size_kb"]:.2f}">'
            f'<div class="card-top">'
            f'<label class="pick-label"><input type="checkbox" class="pick-checkbox" value="{e_path}"><span>Pick</span></label>'
            f'<span class="badge tile">{e_tiles} ({dims})</span>'
            f'</div>'
            f'<div class="thumb"><img src="{e_path}" alt="{e_name}" loading="lazy" draggable="false"></div>'
            f'<div class="meta">'
            f'<div class="fname" title="{e_name}">{e_name}</div>'
            f'<div class="badges">'
            f'<span class="badge bucket-{e_bucket}">{e_bucket}</span>'
            f'</div>'
            f'<div class="sub">{e_pack} · {e_lic} · {item["size_kb"]:.2f} KB</div>'
            f'<div class="card-actions">'
            f'<a class="action-btn" href="{e_path}" download="{e_name}">↓ PNG</a>'
            f'<button type="button" class="action-btn btn-reveal" data-path="{e_path}" disabled title="Launch scripts/shop_app.py to reveal in Finder">Finder</button>'
            f'</div>'
            f'</div></div>'
        )

    pack_opts = "\n".join(f'<option value="{html.escape(p, quote=True)}">{html.escape(p)}</option>' for p in packs)
    tile_opts = "\n".join(f'<option value="{html.escape(t, quote=True)}">{html.escape(t)} tiles</option>' for t in tile_sizes)
    bucket_btns = "\n".join(
        f'<button type="button" class="filter-btn{" active" if b == BUCKET_BUILD else ""}" data-bucket="{html.escape(b, quote=True)}">{html.escape(b)}</button>'
        for b in ["ALL", *buckets]
    )

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rayneta Gather Sprite Shop & Curation Gallery</title>
<style>
  :root {{ --bg: #0f172a; --panel: #1e293b; --border: #334155; --text: #f1f5f9; --muted: #94a3b8; --accent: #38bdf8; --ok: #22c55e; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding-bottom: 84px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }}
  header {{ position: sticky; top: 0; z-index: 10; background: var(--panel); border-bottom: 1px solid var(--border); padding: 14px 20px; }}
  h1 {{ margin: 0 0 10px 0; font-size: 18px; }}
  .controls {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
  input, select {{ background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 7px 10px; font-size: 13px; }}
  .bucket-bar {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .filter-btn, .util-btn {{ background: var(--bg); color: var(--muted); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; cursor: pointer; font-size: 12px; }}
  .filter-btn.active {{ background: var(--accent); color: #082f49; border-color: var(--accent); font-weight: 600; }}
  .util-btn {{ color: var(--text); }}
  .util-btn:hover:not(:disabled) {{ border-color: var(--accent); }}
  .count {{ margin-left: auto; font-size: 13px; color: var(--muted); }}
  .mode-banner {{ margin-top: 10px; padding: 8px 12px; border-radius: 6px; font-size: 12px; background: #0f172a; border: 1px solid var(--border); color: var(--muted); display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
  .mode-banner.is-connected {{ border-color: #166534; color: #bbf7d0; }}
  .mode-banner code {{ color: var(--accent); font-family: ui-monospace, SFMono-Regular, monospace; }}
  main {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; padding: 18px 20px; }}
  .card {{
    --card-bg: var(--panel);
    --card-border: var(--border);
    background: var(--card-bg);
    border: 2px solid var(--card-border);
    border-radius: 8px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transition: border-color 140ms ease, background-color 140ms ease;
  }}
  .card:nth-child(n+25) {{
    content-visibility: auto;
    contain-intrinsic-size: auto none auto 270px;
  }}
  .card:has(.pick-checkbox:checked) {{
    --card-bg: #172554;
    --card-border: var(--accent);
  }}
  @supports not selector(:has(*)) {{
    .card.is-selected {{
      --card-bg: #172554;
      --card-border: var(--accent);
    }}
  }}
  .card.hidden {{ display: none; }}
  .card-top {{ display: flex; align-items: center; justify-content: space-between; padding: 2px 10px 2px 6px; border-bottom: 1px solid var(--border); gap: 6px; }}
  .pick-label {{ display: inline-flex; align-items: center; gap: 6px; min-block-size: 44px; min-inline-size: 44px; padding: 0 4px; cursor: pointer; font-size: 12px; color: var(--text); user-select: none; }}
  .pick-checkbox {{ accent-color: var(--accent); inline-size: 1.15em; aspect-ratio: 1; margin: 0; cursor: pointer; }}
  .pick-checkbox:focus-visible, .action-btn:focus-visible, .filter-btn:focus-visible, .util-btn:focus-visible, .cart-btn:focus-visible {{
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }}
  .thumb {{ height: 132px; display: flex; align-items: center; justify-content: center; padding: 10px; cursor: pointer;
    background-color: #cbd5e1;
    background-image: linear-gradient(45deg, #94a3b8 25%, transparent 25%),
      linear-gradient(-45deg, #94a3b8 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, #94a3b8 75%),
      linear-gradient(-45deg, transparent 75%, #94a3b8 75%);
    background-size: 16px 16px;
    background-position: 0 0, 0 8px, 8px -8px, -8px 0; }}
  .thumb img {{ max-width: 100%; max-height: 100%; image-rendering: pixelated; }}
  .meta {{ padding: 10px; font-size: 12px; display: flex; flex-direction: column; gap: 6px; flex: 1; }}
  .fname {{ font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .badges {{ display: flex; flex-wrap: wrap; gap: 4px; }}
  .badge {{ padding: 2px 6px; border-radius: 4px; font-size: 11px; background: #0f172a; border: 1px solid var(--border); }}
  .badge.tile {{ color: #7dd3fc; }}
  .badge.bucket-Build_Tool_Uploads {{ color: #86efac; }}
  .badge.bucket-_needs_alpha_review {{ color: #fde047; }}
  .badge.bucket-_animation_frames {{ color: #c4b5fd; }}
  .badge.bucket-_oversized_rejected {{ color: #fca5a5; }}
  .sub {{ color: var(--muted); font-size: 11px; }}
  .card-actions {{ display: flex; gap: 6px; margin-top: auto; padding-top: 4px; }}
  .action-btn {{ flex: 1; text-align: center; text-decoration: none; background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; font-size: 11px; cursor: pointer; font-weight: 500; }}
  .action-btn:hover:not(:disabled) {{ border-color: var(--accent); color: var(--accent); }}
  .action-btn:disabled {{ opacity: 0.45; cursor: not-allowed; }}
  .cart-bar {{ position: fixed; inset: auto 0 0 0; z-index: 20; background: #0b1220; border-top: 1px solid var(--border); padding: 10px 20px; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
  .cart-summary {{ font-size: 13px; font-weight: 600; }}
  .cart-hint {{ font-size: 12px; color: var(--muted); }}
  .cart-hint.warn {{ color: #fde047; }}
  .cart-actions {{ margin-left: auto; display: flex; flex-wrap: wrap; gap: 8px; }}
  .cart-btn {{ background: var(--panel); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; font-size: 12px; font-weight: 600; cursor: pointer; }}
  .cart-btn.primary {{ background: var(--accent); color: #082f49; border-color: var(--accent); }}
  .cart-btn:disabled {{ opacity: 0.45; cursor: not-allowed; }}
  .toast-region {{ position: fixed; top: 16px; right: 16px; z-index: 50; pointer-events: none; }}
  .shop-toast {{ pointer-events: auto; position: fixed; inset: auto 20px 72px auto; margin: 0; background: #0f172a; color: var(--text); border: 1px solid var(--accent); border-radius: 8px; padding: 10px 14px; font-size: 13px; max-width: 420px; box-shadow: 0 10px 30px rgba(0,0,0,0.45); display: none; gap: 12px; align-items: center; opacity: 0; transition: opacity 160ms ease; transition-behavior: allow-discrete; }}
  .shop-toast:is(:popover-open, .\\:popover-open, .is-open) {{ display: flex; opacity: 1; }}
  @supports not selector(:popover-open) {{
    [popover]:not(.\\:popover-open):not(.is-open) {{ display: none; }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .card, .shop-toast {{ transition: none; }}
  }}
  .toast-close {{ background: transparent; color: var(--muted); border: 1px solid var(--border); border-radius: 4px; padding: 4px 8px; cursor: pointer; font-size: 11px; }}
  .sr-only {{ position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }}
</style>
</head>
<body>
<header>
  <h1>Rayneta Gather Sprite Shop & Curation Gallery (v1.39.2 · 32px Grid)</h1>
  <div class="controls">
    <div class="bucket-bar">{bucket_btns}</div>
    <input id="search" type="search" placeholder="Search filename or pack...">
    <select id="packFilter"><option value="ALL">All Packs</option>{pack_opts}</select>
    <select id="tileFilter"><option value="ALL">All Tile Sizes</option>{tile_opts}</select>
    <button type="button" class="util-btn" id="selectVisibleBtn">Pick Visible (≤40)</button>
    <button type="button" class="util-btn" id="clearCartBtn">Clear Picks</button>
    <span class="count" id="visibleCount"></span>
  </div>
  <div class="mode-banner" id="modeBanner">
    <span><strong>How to import into Gather:</strong> Browsers block dragging <code>&lt;img&gt;</code> tags between tabs. Click <strong>↓ PNG</strong> to download a single sprite, or run <code>python3 scripts/shop_app.py</code> for 1-click <strong>Reveal in Finder</strong>, <strong>Stage Picks &amp; Open Folder</strong>, and <strong>Download Cart (.zip)</strong>.</span>
  </div>
</header>
<main id="grid">{"".join(cards_html)}</main>
<div class="cart-bar" id="cartBar">
  <span class="cart-summary" id="cartSummary">Cart: 0 sprites (0.0 KB)</span>
  <span class="cart-hint" id="cartHint">Pick sprites above to stage a Finder folder or download a ZIP.</span>
  <div class="cart-actions">
    <button type="button" class="cart-btn" id="downloadZipBtn" disabled>Download Cart (.zip)</button>
    <button type="button" class="cart-btn primary" id="stagePicksBtn" disabled>Stage Picks &amp; Open Finder</button>
  </div>
</div>
<div class="toast-region" role="region" aria-label="Notifications">
  <div id="shopToast" class="shop-toast" popover="manual">
    <span id="toastMsg"></span>
    <button type="button" class="toast-close" popovertarget="shopToast" popovertargetaction="hide" id="toastCloseBtn">Close</button>
  </div>
</div>
<p class="sr-only">Screen reader announcements</p>
<div id="ariaLiveFallback" class="sr-only" aria-live="polite"></div>
<script>
(function() {{
  let activeBucket = "{BUCKET_BUILD}";
  let serverConnected = false;
  let toastTimer = null;
  const STORAGE_KEY = "rayneta_gather_cart_v1";
  const hasCssSelectorSupport = typeof CSS !== "undefined" && CSS.supports("selector(:has(*))");
  const hasPopoverSupport = Object.hasOwn(HTMLElement.prototype, "popover");

  const cards = Array.from(document.querySelectorAll(".card"));
  const searchEl = document.getElementById("search");
  const packEl = document.getElementById("packFilter");
  const tileEl = document.getElementById("tileFilter");
  const countEl = document.getElementById("visibleCount");
  const modeBanner = document.getElementById("modeBanner");
  const cartSummary = document.getElementById("cartSummary");
  const cartHint = document.getElementById("cartHint");
  const stagePicksBtn = document.getElementById("stagePicksBtn");
  const downloadZipBtn = document.getElementById("downloadZipBtn");
  const selectVisibleBtn = document.getElementById("selectVisibleBtn");
  const clearCartBtn = document.getElementById("clearCartBtn");
  const toastEl = document.getElementById("shopToast");
  const toastMsg = document.getElementById("toastMsg");
  const toastCloseBtn = document.getElementById("toastCloseBtn");
  const ariaLiveEl = document.getElementById("ariaLiveFallback");

  const cardByPath = new Map();
  for (const c of cards) {{
    cardByPath.set(c.dataset.path, c);
  }}

  const cart = new Set();
  try {{
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    if (Array.isArray(saved)) {{
      for (const p of saved) {{
        if (cardByPath.has(p)) cart.add(p);
      }}
    }}
  }} catch (_) {{}}

  function saveCart() {{
    try {{
      localStorage.setItem(STORAGE_KEY, JSON.stringify(Array.from(cart)));
    }} catch (_) {{}}
  }}

  function announce(text) {{
    if (typeof document.ariaNotify === "function") {{
      document.ariaNotify(text);
    }} else {{
      ariaLiveEl.textContent = "";
      requestAnimationFrame(() => {{ ariaLiveEl.textContent = text; }});
    }}
  }}

  function showToast(text) {{
    toastMsg.textContent = text;
    if (hasPopoverSupport) {{
      if (!toastEl.matches(":popover-open")) toastEl.showPopover();
    }} else {{
      toastEl.classList.add("is-open");
    }}
    announce(text);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {{
      if (document.activeElement === toastCloseBtn) return;
      hideToast();
    }}, 4500);
  }}

  function hideToast() {{
    clearTimeout(toastTimer);
    if (hasPopoverSupport) {{
      if (toastEl.matches(":popover-open")) toastEl.hidePopover();
    }} else {{
      toastEl.classList.remove("is-open");
    }}
  }}

  toastCloseBtn.addEventListener("click", () => {{
    if (!hasPopoverSupport) hideToast();
  }});

  function syncCardUI(card, checked) {{
    const cb = card.querySelector(".pick-checkbox");
    cb.checked = checked;
    if (!hasCssSelectorSupport) {{
      card.classList.toggle("is-selected", checked);
    }}
  }}

  function updateCartUI() {{
    let totalKb = 0;
    for (const p of cart) {{
      const c = cardByPath.get(p);
      if (c) totalKb += parseFloat(c.dataset.kb || "0");
    }}
    const count = cart.size;
    cartSummary.textContent = "Cart: " + count + " sprite" + (count === 1 ? "" : "s") + " (" + totalKb.toFixed(1) + " KB)";
    if (count === 0) {{
      cartHint.textContent = "Pick sprites above to stage a Finder folder or download a ZIP.";
      cartHint.classList.remove("warn");
    }} else if (count > 40) {{
      cartHint.textContent = "Note: >40 sprites will be split into 40-file batch subfolders for Gather.";
      cartHint.classList.add("warn");
    }} else {{
      cartHint.textContent = "Ready to drag straight into Gather Customize → Build → Uploaded.";
      cartHint.classList.remove("warn");
    }}
    stagePicksBtn.disabled = !serverConnected || count === 0;
    downloadZipBtn.disabled = !serverConnected || count === 0;
    saveCart();
  }}

  for (const card of cards) {{
    const path = card.dataset.path;
    const cb = card.querySelector(".pick-checkbox");
    if (cart.has(path)) syncCardUI(card, true);

    cb.addEventListener("change", () => {{
      if (cb.checked) cart.add(path);
      else cart.delete(path);
      syncCardUI(card, cb.checked);
      updateCartUI();
    }});

    card.querySelector(".thumb").addEventListener("click", () => {{
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event("change"));
    }});
  }}

  function applyFilters() {{
    const q = searchEl.value.trim().toLowerCase();
    const pack = packEl.value;
    const tile = tileEl.value;
    let shown = 0;
    for (const c of cards) {{
      const okBucket = activeBucket === "ALL" || c.dataset.bucket === activeBucket;
      const okPack = pack === "ALL" || c.dataset.pack === pack;
      const okTile = tile === "ALL" || c.dataset.tiles === tile;
      const okSearch = !q || c.dataset.name.includes(q) || c.dataset.pack.toLowerCase().includes(q);
      const visible = okBucket && okPack && okTile && okSearch;
      c.classList.toggle("hidden", !visible);
      if (visible) shown++;
    }}
    countEl.textContent = "Showing " + shown + " of " + cards.length + " sprites";
  }}

  document.querySelectorAll(".filter-btn").forEach(btn => {{
    btn.addEventListener("click", () => {{
      document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      activeBucket = btn.dataset.bucket;
      applyFilters();
    }});
  }});
  searchEl.addEventListener("input", applyFilters);
  packEl.addEventListener("change", applyFilters);
  tileEl.addEventListener("change", applyFilters);

  selectVisibleBtn.addEventListener("click", () => {{
    let added = 0;
    for (const c of cards) {{
      if (c.classList.contains("hidden")) continue;
      if (added >= 40) break;
      const p = c.dataset.path;
      if (!cart.has(p)) {{
        cart.add(p);
        syncCardUI(c, true);
        added++;
      }}
    }}
    updateCartUI();
    showToast(added > 0 ? ("Added " + added + " visible sprite(s) to cart.") : "All visible sprites (up to 40) are already in cart.");
  }});

  clearCartBtn.addEventListener("click", () => {{
    cart.clear();
    for (const c of cards) syncCardUI(c, false);
    updateCartUI();
    showToast("Cleared sprite cart.");
  }});

  document.getElementById("grid").addEventListener("click", async (e) => {{
    const btn = e.target.closest(".btn-reveal");
    if (!btn || btn.disabled) return;
    try {{
      const res = await fetch("/api/reveal", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ rel_path: btn.dataset.path }})
      }});
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Reveal failed");
      showToast("Revealed in Finder: " + data.revealed.split("/").pop());
    }} catch (err) {{
      showToast("Reveal failed: " + err.message);
    }}
  }});

  stagePicksBtn.addEventListener("click", async () => {{
    if (!serverConnected || cart.size === 0) return;
    stagePicksBtn.disabled = true;
    try {{
      const res = await fetch("/api/stage-picks", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ rel_paths: Array.from(cart) }})
      }});
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Staging failed");
      showToast("Staged " + data.count + " sprite(s) into gather_ready/" + data.folder + " and opened Finder.");
    }} catch (err) {{
      showToast("Staging failed: " + err.message);
    }} finally {{
      updateCartUI();
    }}
  }});

  downloadZipBtn.addEventListener("click", async () => {{
    if (!serverConnected || cart.size === 0) return;
    downloadZipBtn.disabled = true;
    try {{
      const res = await fetch("/api/download-zip", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ rel_paths: Array.from(cart) }})
      }});
      if (!res.ok) {{
        const data = await res.json().catch(() => ({{}}));
        throw new Error(data.error || "ZIP download failed");
      }}
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "gather_sprite_picks.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      showToast("Downloaded gather_sprite_picks.zip (" + cart.size + " sprites).");
    }} catch (err) {{
      showToast("ZIP failed: " + err.message);
    }} finally {{
      updateCartUI();
    }}
  }});

  async function detectShopServer() {{
    if (window.location.protocol !== "http:" && window.location.protocol !== "https:") return;
    try {{
      const res = await fetch("/api/health", {{ cache: "no-store" }});
      if (!res.ok) return;
      const info = await res.json();
      if (info && info.ok) {{
        serverConnected = true;
        document.querySelectorAll(".btn-reveal").forEach(b => {{
          b.disabled = false;
          b.title = "Reveal PNG in Finder";
        }});
        modeBanner.classList.add("is-connected");
        modeBanner.innerHTML = "<span><strong>Sprite Shop connected:</strong> Click <strong>Finder</strong> on any card to highlight its PNG, or pick multiple sprites and click <strong>Stage Picks &amp; Open Finder</strong> to drag straight into Gather.</span>";
        updateCartUI();
      }}
    }} catch (_) {{}}
  }}

  applyFilters();
  updateCartUI();
  detectShopServer();
}})();
</script>
</body>
</html>
"""
    GALLERY_PATH.write_text(page, encoding="utf-8")
    logger.info("Generated visual preview gallery (%d sprites) -> %s", len(items), GALLERY_PATH.relative_to(WORKSPACE_ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rayneta Gather Sprite Importer — Phase 3 Normalization CLI")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="Normalize all packs registered in manifest.json")
    target.add_argument("--pack-id", type=str, help="Normalize a single pack ID from manifest.json")

    parser.add_argument(
        "--mode",
        choices=["auto", "single", "grid", "strip", "components"],
        default="auto",
        help="Slicing classification strategy (default: auto)",
    )
    parser.add_argument("--grid-size", type=int, default=32, help="Cell size in px for grid mode (default: 32)")
    parser.add_argument("--bridge-gap", type=int, default=1, help="Pixel gap bridge for component flood-fill (default: 1)")
    parser.add_argument("--include-all-frames", action="store_true", help="Include Frame 1..N of animation strips in Build_Tool_Uploads/")
    parser.add_argument("--include-shadows", action="store_true", help="Include shadow-only layers (TX Shadow*.png)")
    parser.add_argument("--dry-run", action="store_true", help="Preview classification and output counts without writing files")
    return parser


def main() -> int:
    logger = setup_logging()
    args = build_parser().parse_args()
    manifest = load_manifest()

    assets = manifest.get("assets", [])
    if args.pack_id:
        selected = [a for a in assets if a.get("id") == args.pack_id]
        if not selected:
            logger.error("Pack ID not found in manifest.json: %s", args.pack_id)
            return 1
    else:
        selected = assets

    seen_hashes: set[str] = set()
    for asset in selected:
        process_pack(asset, args, seen_hashes, logger)

    if not args.dry_run:
        save_manifest(manifest)
        gallery_items = collect_gallery_items_from_disk(manifest)
        generate_preview_gallery(gallery_items, logger)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
