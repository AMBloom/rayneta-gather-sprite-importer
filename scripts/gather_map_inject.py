#!/usr/bin/env python3
"""Phase 4 Option B — Safe Gather HTTP Maps API Backup & Object Placement Injector."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from PIL import Image

from normalize_sprites import GRID_UNIT_PX, LOGS_DIR, WORKSPACE_ROOT, ensure_within_workspace

MAP_BACKUPS_DIR = LOGS_DIR / "map_backups"
STAGING_LOG_PATH = LOGS_DIR / "staging.log"
GATHER_API_BASE = "https://api.gather.town/api/v2"
REQUEST_TIMEOUT = 20


def setup_logging() -> logging.Logger:
    """Configure logging inside logs/staging.log."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("gather_map_inject")
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


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:50] or "space"


def resolve_credentials(args: argparse.Namespace) -> tuple[str | None, str | None, str | None]:
    """Read Gather credentials strictly from environment variables or explicit CLI flags."""
    api_key = args.api_key or os.environ.get("GATHER_API_KEY")
    space_id = args.space_id or os.environ.get("GATHER_SPACE_ID")
    map_id = args.map_id or os.environ.get("GATHER_MAP_ID")
    return api_key, space_id, map_id


def map_endpoint(space_id: str, map_id: str) -> str:
    return f"{GATHER_API_BASE}/spaces/{quote(space_id, safe='')}/maps/{quote(map_id, safe='')}"


def backup_map(
    api_key: str,
    space_id: str,
    map_id: str,
    logger: logging.Logger,
) -> tuple[dict[str, Any], Path]:
    """GET current map JSON from Gather v2 API and save timestamped backup before any write."""
    url = map_endpoint(space_id, map_id)
    logger.info("Fetching Gather map backup from %s ...", url)
    resp = requests.get(url, headers={"apiKey": api_key}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    map_data = resp.json()

    MAP_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_name = f"{slugify(space_id)}_{slugify(map_id)}_{ts}.json"
    backup_path = ensure_within_workspace(MAP_BACKUPS_DIR / backup_name)
    backup_path.write_text(json.dumps(map_data, indent=2) + "\n", encoding="utf-8")
    logger.info("Saved map backup -> %s", backup_path.relative_to(WORKSPACE_ROOT))
    return map_data, backup_path


def validate_hosted_png(url: str, logger: logging.Logger) -> None:
    """Verify hosted_url responds 200 OK with image/png content type."""
    resp = requests.head(url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 405:
        resp = requests.get(url, stream=True, allow_redirects=True, timeout=REQUEST_TIMEOUT)
        resp.close()
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "").lower()
    if "image/png" not in ctype:
        raise ValueError(f"Hosted URL {url} returned Content-Type {ctype!r} (expected image/png)")
    logger.info("Verified hosted PNG URL (%d): %s", resp.status_code, url)


def build_placement_objects(
    placement_path: Path,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """Load placement JSON, measure local PNG tile sizes via Pillow, and validate hosted URLs."""
    resolved = ensure_within_workspace(placement_path)
    specs = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(specs, list) or not specs:
        raise ValueError("Placement JSON must be a non-empty array of placement objects.")

    objects_to_add: list[dict[str, Any]] = []
    for idx, spec in enumerate(specs):
        sprite_rel = spec["sprite_file"]
        sprite_path = Path(sprite_rel)
        if not sprite_path.is_absolute():
            sprite_path = WORKSPACE_ROOT / sprite_path
        sprite_path = ensure_within_workspace(sprite_path)
        if not sprite_path.is_file():
            raise FileNotFoundError(f"Placement [{idx}] sprite_file not found: {sprite_rel}")

        with Image.open(sprite_path) as img:
            px_w, px_h = img.size
        if px_w % GRID_UNIT_PX != 0 or px_h % GRID_UNIT_PX != 0:
            raise ValueError(
                f"Placement [{idx}] {sprite_rel} has non-grid dimensions {px_w}x{px_h}; normalize in Phase 3 first."
            )

        hosted_url = spec["hosted_url"]
        validate_hosted_png(hosted_url, logger)

        obj = {
            "id": spec.get("id") or f"rayneta_{sprite_path.stem}_{idx}",
            "normal": hosted_url,
            "highlighted": spec.get("highlighted_url", hosted_url),
            "x": int(spec["x"]),
            "y": int(spec["y"]),
            "width": px_w // GRID_UNIT_PX,
            "height": px_h // GRID_UNIT_PX,
            "type": int(spec.get("type", 0)),
            "distThreshold": int(spec.get("distThreshold", 1)),
        }
        if "properties" in spec:
            obj["properties"] = spec["properties"]
        objects_to_add.append(obj)
        logger.info(
            "Prepared object '%s' at (%d, %d) [%dx%d tiles] -> %s",
            obj["id"],
            obj["x"],
            obj["y"],
            obj["width"],
            obj["height"],
            hosted_url,
        )

    return objects_to_add


def inject_objects(
    api_key: str,
    space_id: str,
    map_id: str,
    map_data: dict[str, Any],
    new_objects: list[dict[str, Any]],
    logger: logging.Logger,
) -> None:
    """POST updated objects array to Gather HTTP Maps v2 API."""
    existing_objects = list(map_data.get("objects") or [])
    updated_objects = existing_objects + new_objects
    url = map_endpoint(space_id, map_id)
    payload = {"objects": updated_objects}

    resp = requests.post(
        url,
        headers={"apiKey": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    logger.info(
        "Successfully injected %d object(s) into map '%s' (total objects: %d).",
        len(new_objects),
        map_id,
        len(updated_objects),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rayneta Gather Sprite Importer — Phase 4 Option B Gather Maps v2 API Injector"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate placements and preview injection without mutating the map (default)",
    )
    mode.add_argument(
        "--backup-only",
        action="store_true",
        help="Only GET and save a timestamped map JSON backup",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Back up map via GET, then POST new objects to Gather Maps API",
    )

    parser.add_argument("--placement-json", type=Path, help="Path to JSON array of object placements")
    parser.add_argument("--api-key", type=str, help="Gather API key (defaults to GATHER_API_KEY env var)")
    parser.add_argument("--space-id", type=str, help="Gather Space ID (defaults to GATHER_SPACE_ID env var)")
    parser.add_argument("--map-id", type=str, help="Gather Map ID (defaults to GATHER_MAP_ID env var)")
    return parser


def main() -> int:
    logger = setup_logging()
    args = build_parser().parse_args()
    api_key, space_id, map_id = resolve_credentials(args)

    if args.backup_only:
        if not (api_key and space_id and map_id):
            logger.error("Missing GATHER_API_KEY, GATHER_SPACE_ID, or GATHER_MAP_ID for --backup-only.")
            return 1
        backup_map(api_key, space_id, map_id, logger)
        return 0

    if not args.placement_json:
        logger.error("--placement-json is required unless --backup-only is specified.")
        return 1

    new_objects = build_placement_objects(args.placement_json, logger)

    if not args.apply:
        logger.info("[DRY-RUN] Validated %d object(s); no remote map changes applied.", len(new_objects))
        if api_key and space_id and map_id:
            backup_map(api_key, space_id, map_id, logger)
        else:
            logger.info("[DRY-RUN] Skipping remote GET backup (credentials not set in environment).")
        return 0

    if not (api_key and space_id and map_id):
        logger.error("Refusing --apply: GATHER_API_KEY, GATHER_SPACE_ID, and GATHER_MAP_ID must be set.")
        return 1

    map_data, backup_path = backup_map(api_key, space_id, map_id, logger)
    if not backup_path.is_file():
        logger.error("Backup verification failed; aborting --apply.")
        return 1

    inject_objects(api_key, space_id, map_id, map_data, new_objects, logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
