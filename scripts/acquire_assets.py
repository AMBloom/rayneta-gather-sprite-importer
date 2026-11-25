#!/usr/bin/env python3
"""Phase 2 Asset Acquisition & Local Pack Registration CLI for Rayneta Gather."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
RAW_SPRITES_DIR = WORKSPACE_ROOT / "raw_sprites"
MANIFEST_PATH = WORKSPACE_ROOT / "manifest.json"
LOGS_DIR = WORKSPACE_ROOT / "logs"
LOG_PATH = LOGS_DIR / "acquisition.log"

USER_AGENT = "RaynetaGatherSpriteImporter/1.0 (+https://github.com/rayneta)"
REQUEST_TIMEOUT = 30
ZIP_MAGIC = b"PK\x03\x04"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

ALLOWED_LICENSES = {"CC0", "CC-BY-3.0", "CC-BY-4.0"}
DISALLOWED_LICENSE_PATTERNS = (
    r"\bcc[\s-]*by[\s-]*nc\b",
    r"\bcc[\s-]*by[\s-]*sa\b",
    r"\bcc[\s-]*by[\s-]*nd\b",
    r"\ball\s+rights\s+reserved\b",
    r"\bgpl(?:v?[23])?\b",
    r"\blgpl\b",
    r"\boga[\s-]*by\b",
)

LOCAL_SEED_SPECS: list[dict[str, Any]] = [
    {
        "source_dir": "MegaPackFree",
        "slug": "mega_pack_free",
        "pack_name": "MegaPackFree",
        "include_roots": ("CatMegaFree", "FreeAnimalPack", "FreeEnvironment"),
        "exclude_dirs": ("UIBundleFree", "__pycache__"),
    },
    {
        "source_dir": "Pixel Art Icon Pack - RPG",
        "slug": "pixel_art_icon_pack_rpg",
        "pack_name": "Pixel Art Icon Pack - RPG",
        "include_roots": (
            "Equipment",
            "Food",
            "Material",
            "Misc",
            "Monster Part",
            "Ore & Gem",
            "Potion",
            "Weapon & Tool",
        ),
        "exclude_dirs": (),
    },
    {
        "source_dir": "Pixel Art Top Down - Basic v1.2.3",
        "slug": "pixel_art_top_down_basic_v1_2_3",
        "pack_name": "Pixel Art Top Down - Basic v1.2.3",
        "include_roots": ("Texture",),
        "exclude_dirs": (),
    },
]


def setup_logging() -> logging.Logger:
    """Configure file and console logging inside workspace logs/."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("acquire_assets")
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


def slugify(text: str) -> str:
    """Convert arbitrary titles or pack names into safe lowercase slugs."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:60] or "asset_pack"


def ensure_within_workspace(path: Path) -> Path:
    """Enforce strict workspace lock for all filesystem operations."""
    resolved = path.resolve()
    if os.path.commonpath([str(WORKSPACE_ROOT), str(resolved)]) != str(WORKSPACE_ROOT):
        raise ValueError(f"Path escapes workspace lock: {resolved}")
    return resolved


def load_manifest() -> dict[str, Any]:
    """Load manifest.json, preserving baseline schema keys."""
    if MANIFEST_PATH.exists():
        with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = {
            "schema_version": "1.0",
            "target_gather_version": "1.39.2",
            "grid_unit_px": 32,
            "assets": [],
        }
    data.setdefault("assets", [])
    return data


def save_manifest(manifest: dict[str, Any]) -> None:
    """Atomically write manifest.json inside workspace."""
    ensure_within_workspace(MANIFEST_PATH)
    tmp_path = MANIFEST_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    os.replace(tmp_path, MANIFEST_PATH)


def read_image_dimensions(path: Path) -> tuple[int, int] | None:
    """Read PNG dimensions via Pillow without decoding full pixel buffers."""
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None


def collect_raw_file_metadata(directory: Path, logger: logging.Logger) -> list[dict[str, Any]]:
    """Scan a staged directory for PNG files and collect relative paths + dimensions."""
    entries: list[dict[str, Any]] = []
    for file_path in sorted(directory.rglob("*")):
        if not file_path.is_file() or file_path.suffix.lower() != ".png":
            continue
        dims = read_image_dimensions(file_path)
        if dims is None:
            logger.warning("Skipping unreadable PNG: %s", file_path.relative_to(WORKSPACE_ROOT))
            continue
        rel_path = file_path.relative_to(RAW_SPRITES_DIR).as_posix()
        entries.append({"path": rel_path, "width": dims[0], "height": dims[1]})
    return entries


def normalize_license(raw_text: str) -> str | None:
    """Map license text/URLs strictly to CC0, CC-BY-3.0, CC-BY-4.0, or None if disallowed."""
    text = raw_text.strip()
    if not text:
        return None
    lower = text.lower()

    for pattern in DISALLOWED_LICENSE_PATTERNS:
        if re.search(pattern, lower):
            return None

    if "publicdomain/zero/1.0" in lower or re.search(r"\bcc0\b", lower) or "public domain" in lower:
        return "CC0"
    if "licenses/by/4.0" in lower or re.search(r"\bcc[\s-]*by[\s-]*4\.0\b", lower):
        return "CC-BY-4.0"
    if "licenses/by/3.0" in lower or re.search(r"\bcc[\s-]*by[\s-]*3\.0\b", lower):
        return "CC-BY-3.0"
    if re.fullmatch(r"cc[\s-]*by", lower):
        return "CC-BY-4.0"
    if re.search(r"\bcreative commons attribution\b", lower) and not any(
        tag in lower for tag in ("noncommercial", "sharealike", "noderivs", "-nc", "-sa", "-nd")
    ):
        return "CC-BY-4.0"
    return None


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_pack_sha256(files: list[tuple[str, Path]]) -> str:
    """Compute deterministic digest across sorted (relative_path, file_hash) pairs."""
    h = hashlib.sha256()
    for rel_path, path in sorted(files, key=lambda item: item[0]):
        h.update(rel_path.encode("utf-8"))
        h.update(compute_file_sha256(path).encode("ascii"))
    return h.hexdigest()


def manifest_has_sha256(manifest: dict[str, Any], sha256: str) -> bool:
    return any(asset.get("sha256") == sha256 for asset in manifest.get("assets", []))


def register_local_packs(logger: logging.Logger) -> None:
    """Copy approved sprite folders from workspace root packs into raw_sprites/local_packs/."""
    manifest = load_manifest()
    RAW_SPRITES_DIR.mkdir(parents=True, exist_ok=True)
    local_root = ensure_within_workspace(RAW_SPRITES_DIR / "local_packs")
    local_root.mkdir(parents=True, exist_ok=True)

    registered_count = 0
    for spec in LOCAL_SEED_SPECS:
        src_pack_dir = WORKSPACE_ROOT / spec["source_dir"]
        if not src_pack_dir.is_dir():
            logger.warning("Local pack folder missing, skipping: %s", src_pack_dir)
            continue

        collected: list[tuple[str, Path]] = []
        for sub_name in spec["include_roots"]:
            sub_dir = src_pack_dir / sub_name
            if not sub_dir.is_dir():
                logger.warning("Expected subfolder missing in %s: %s", spec["pack_name"], sub_name)
                continue
            for file_path in sorted(sub_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                if any(excl in file_path.parts for excl in spec["exclude_dirs"]):
                    continue
                if file_path.name == ".DS_Store" or file_path.suffix.lower() != ".png":
                    continue
                rel_within_pack = file_path.relative_to(src_pack_dir).as_posix()
                collected.append((rel_within_pack, file_path))

        if not collected:
            logger.warning("No eligible PNG files found in %s", spec["pack_name"])
            continue

        pack_sha = compute_pack_sha256(collected)
        if manifest_has_sha256(manifest, pack_sha):
            logger.info("Local pack already registered (sha256=%s): %s", pack_sha[:12], spec["pack_name"])
            continue

        dest_pack_dir = ensure_within_workspace(local_root / spec["slug"])
        for rel_within_pack, src_file in collected:
            dest_file = ensure_within_workspace(dest_pack_dir / rel_within_pack)
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)

        raw_files = collect_raw_file_metadata(dest_pack_dir, logger)
        entry = {
            "id": f"{spec['slug']}-{pack_sha[:8]}",
            "pack_name": spec["pack_name"],
            "source_type": "local_seed",
            "source_url": f"local://{spec['source_dir']}",
            "license": "local_unverified",
            "author": "unknown",
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "sha256": pack_sha,
            "raw_files": raw_files,
            "status": "raw_staged",
        }
        manifest["assets"].append(entry)
        save_manifest(manifest)
        registered_count += 1
        logger.info(
            "Registered local pack '%s' (%d PNGs) -> %s",
            spec["pack_name"],
            len(raw_files),
            dest_pack_dir.relative_to(WORKSPACE_ROOT),
        )

    logger.info("Local pack registration finished (%d newly registered).", registered_count)


def ingest_local_path(
    local_path: Path,
    cli_license: str | None,
    cli_author: str | None,
    logger: logging.Logger,
) -> str | None:
    """Ingest a local .zip archive or folder of PNGs into raw_sprites/ and manifest.json."""
    target = local_path if local_path.is_absolute() else (WORKSPACE_ROOT / local_path)
    target = ensure_within_workspace(target)
    if not target.exists():
        raise FileNotFoundError(f"Local path does not exist: {target}")

    license_val = "local_unverified"
    if cli_license:
        normalized_lic = normalize_license(cli_license)
        if normalized_lic not in ALLOWED_LICENSES:
            raise ValueError(f"License {cli_license!r} is not CC0 or CC-BY.")
        license_val = normalized_lic

    manifest = load_manifest()
    RAW_SPRITES_DIR.mkdir(parents=True, exist_ok=True)
    pack_name = target.stem if target.is_file() else target.name
    pack_slug = slugify(pack_name)
    rel_source = target.relative_to(WORKSPACE_ROOT).as_posix()

    if target.is_file():
        if target.suffix.lower() != ".zip":
            raise ValueError(f"Unsupported local file type (expected .zip or directory): {target.name}")
        payload = target.read_bytes()
        if not payload[:16].startswith(ZIP_MAGIC):
            raise ValueError(f"Invalid ZIP archive magic bytes: {target.name}")
        sha256 = hashlib.sha256(payload).hexdigest()
        if manifest_has_sha256(manifest, sha256):
            logger.info("Local pack already registered (sha256=%s): %s", sha256[:12], pack_name)
            return None
        dest_dir = ensure_within_workspace(RAW_SPRITES_DIR / f"{pack_slug}_{sha256[:8]}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_extract_zip(payload, dest_dir, logger)
    elif target.is_dir():
        collected: list[tuple[str, Path]] = []
        for file_path in sorted(target.rglob("*")):
            if not file_path.is_file() or file_path.name == ".DS_Store" or file_path.suffix.lower() != ".png":
                continue
            collected.append((file_path.relative_to(target).as_posix(), file_path))
        if not collected:
            logger.warning("No valid PNG files found in local directory: %s", rel_source)
            return None
        sha256 = compute_pack_sha256(collected)
        if manifest_has_sha256(manifest, sha256):
            logger.info("Local pack already registered (sha256=%s): %s", sha256[:12], pack_name)
            return None
        dest_dir = ensure_within_workspace(RAW_SPRITES_DIR / f"{pack_slug}_{sha256[:8]}")
        for rel_within_pack, src_file in collected:
            dest_file = ensure_within_workspace(dest_dir / rel_within_pack)
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)
    else:
        raise ValueError(f"Unsupported local path: {target}")

    raw_files = collect_raw_file_metadata(dest_dir, logger)
    if not raw_files:
        logger.warning("Local path '%s' yielded 0 valid PNG files; cleaning up empty folder.", rel_source)
        shutil.rmtree(dest_dir, ignore_errors=True)
        return None

    pack_id = f"{pack_slug}-{sha256[:8]}"
    entry = {
        "id": pack_id,
        "pack_name": pack_name,
        "source_type": "local_pack",
        "source_url": f"local://{rel_source}",
        "license": license_val,
        "author": cli_author or "unknown",
        "acquired_at": datetime.now(timezone.utc).isoformat(),
        "sha256": sha256,
        "raw_files": raw_files,
        "status": "raw_staged",
    }
    manifest["assets"].append(entry)
    save_manifest(manifest)
    logger.info(
        "Staged local pack '%s' [%s] (%d PNGs) -> %s",
        pack_name,
        license_val,
        len(raw_files),
        dest_dir.relative_to(WORKSPACE_ROOT),
    )
    return pack_id


def http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def safe_extract_zip(zip_bytes: bytes, dest_dir: Path, logger: logging.Logger) -> int:
    """Extract ZIP safely with strict Zip Slip path-traversal protection."""
    dest_resolved = ensure_within_workspace(dest_dir)
    dest_resolved.mkdir(parents=True, exist_ok=True)
    extracted = 0

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            member_name = info.filename
            if member_name.startswith("__MACOSX/") or Path(member_name).name == ".DS_Store":
                continue

            target_path = (dest_resolved / member_name).resolve()
            if os.path.commonpath([str(dest_resolved), str(target_path)]) != str(dest_resolved):
                raise ValueError(f"Blocked Zip Slip path traversal entry: {member_name}")

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted += 1

    logger.info("Safely extracted %d files into %s", extracted, dest_resolved.relative_to(WORKSPACE_ROOT))
    return extracted


def validate_binary_payload(resp: requests.Response, logger: logging.Logger) -> str | None:
    """Validate Content-Type and magic bytes; returns 'zip', 'png', or None if HTML/invalid."""
    content_type = resp.headers.get("Content-Type", "").lower()
    data = resp.content
    head = data[:16]

    if head.startswith(ZIP_MAGIC):
        return "zip"
    if head.startswith(PNG_MAGIC):
        return "png"

    stripped = head.lstrip().lower()
    if "text/html" in content_type or stripped.startswith(b"<!doctype") or stripped.startswith(b"<html"):
        logger.warning(
            "Received HTML instead of binary asset at %s (Content-Type: %s). "
            "If this is an Itch.io pay-what-you-want/token page, download manually in browser or use a direct file URL.",
            resp.url,
            content_type,
        )
        return None

    logger.warning(
        "Unrecognized binary format at %s (Content-Type: %s, magic=%r); skipping.",
        resp.url,
        content_type,
        head[:8],
    )
    return None


def parse_opengameart_asset(session: requests.Session, url: str, logger: logging.Logger) -> dict[str, Any] | None:
    """Parse an OpenGameArt asset page for title, author, verified license, and file URLs."""
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    title_el = soup.select_one("h1#page-title, h1.title, h2.node-title, h1")
    title = title_el.get_text(strip=True) if title_el else Path(urlparse(url).path).name

    author_el = soup.select_one(".field-name-field-art-author .field-item, .submitted .username, .username")
    author = author_el.get_text(strip=True) if author_el else "unknown"

    license_texts: list[str] = []
    for node in soup.select(".field-name-field-art-licenses a, .field-name-field-art-licenses .field-item"):
        license_texts.append(node.get_text(" ", strip=True))
        if node.get("href"):
            license_texts.append(node["href"])
        img = node.find("img")
        if img and img.get("alt"):
            license_texts.append(img["alt"])

    detected_licenses = [normalize_license(txt) for txt in license_texts]
    valid_licenses = [lic for lic in detected_licenses if lic in ALLOWED_LICENSES]
    if not valid_licenses:
        logger.warning(
            "Rejected OpenGameArt asset '%s' (%s): license not CC0/CC-BY (%s)",
            title,
            url,
            "; ".join(license_texts) or "missing",
        )
        return None

    file_urls: list[str] = []
    for link in soup.select(".field-name-field-art-files a[href], a[href*='/sites/default/files/']"):
        href = urljoin(url, link["href"])
        path_lower = urlparse(href).path.lower()
        if path_lower.endswith((".zip", ".png")) and href not in file_urls:
            file_urls.append(href)

    if not file_urls:
        logger.warning("OpenGameArt asset '%s' (%s) has no .zip or .png file links.", title, url)
        return None

    return {
        "pack_name": title,
        "source_type": "opengameart",
        "source_url": url,
        "license": valid_licenses[0],
        "author": author,
        "file_urls": file_urls,
    }


def search_opengameart(session: requests.Session, query: str, limit: int, logger: logging.Logger) -> list[dict[str, Any]]:
    """Search OpenGameArt.org for 2D CC0/CC-BY assets."""
    search_url = "https://opengameart.org/art-search-advanced"
    logger.info("Searching OpenGameArt.org for %r (limit=%d)...", query, limit)
    form_resp = session.get(search_url, timeout=REQUEST_TIMEOUT)
    form_resp.raise_for_status()
    form_soup = BeautifulSoup(form_resp.text, "lxml")

    params: list[tuple[str, str]] = [("keys", query), ("sort_by", "count"), ("sort_order", "DESC")]

    for inp in form_soup.select("input[name='field_art_type_tid[]']"):
        label = form_soup.find("label", attrs={"for": inp.get("id", "")})
        if label and "2d art" in label.get_text(" ", strip=True).lower() and inp.get("value"):
            params.append(("field_art_type_tid[]", inp["value"]))

    for inp in form_soup.select("input[name='field_art_licenses_tid[]']"):
        label = form_soup.find("label", attrs={"for": inp.get("id", "")})
        label_txt = label.get_text(" ", strip=True).lower() if label else ""
        if inp.get("value") and (
            "cc0" in label_txt
            or "cc-by 3.0" in label_txt
            or "cc-by 4.0" in label_txt
        ):
            params.append(("field_art_licenses_tid[]", inp["value"]))

    res_resp = session.get(search_url, params=params, timeout=REQUEST_TIMEOUT)
    res_resp.raise_for_status()
    res_soup = BeautifulSoup(res_resp.text, "lxml")

    candidate_urls: list[str] = []
    for link in res_soup.select(".view-art .views-row a[href*='/content/'], .art-preview-title a[href*='/content/']"):
        full_url = urljoin("https://opengameart.org", link["href"])
        if full_url not in candidate_urls:
            candidate_urls.append(full_url)

    verified: list[dict[str, Any]] = []
    for asset_url in candidate_urls:
        if len(verified) >= limit:
            break
        parsed = parse_opengameart_asset(session, asset_url, logger)
        if parsed:
            verified.append(parsed)
    return verified


def parse_itch_asset(session: requests.Session, url: str, logger: logging.Logger) -> dict[str, Any] | None:
    """Parse an Itch.io project page for explicit CC0/CC-BY license and downloadable links."""
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    title_el = soup.select_one("h1.game_title, h1")
    title = title_el.get_text(strip=True) if title_el else Path(urlparse(url).path).name

    author_el = soup.select_one(".game_author a, .user_profile_widget .profile_link")
    author = author_el.get_text(strip=True) if author_el else "unknown"

    license_candidates: list[str] = []
    for node in soup.select(".game_info_panel_widget tr, .info_panel_wrapper a[href*='creativecommons.org']"):
        txt = node.get_text(" ", strip=True)
        if "license" in txt.lower() or "creativecommons" in (node.get("href") or "").lower() or "cc0" in txt.lower() or "cc-by" in txt.lower():
            license_candidates.append(txt)
            if node.get("href"):
                license_candidates.append(node["href"])

    desc_el = soup.select_one(".formatted_description")
    if desc_el and not license_candidates:
        for line in desc_el.get_text("\n", strip=True).splitlines():
            if any(tok in line.lower() for tok in ("license", "cc0", "cc-by", "cc by", "creative commons", "all rights reserved")):
                license_candidates.append(line)

    detected = [normalize_license(item) for item in license_candidates]
    valid = [lic for lic in detected if lic in ALLOWED_LICENSES]
    if not valid:
        logger.warning(
            "Rejected Itch.io asset '%s' (%s): missing or disallowed license (%s)",
            title,
            url,
            "; ".join(license_candidates) or "none found",
        )
        return None

    download_urls: list[str] = []
    for link in soup.select("a.download_btn[href], a[href*='/download/'], .upload_list_widget a[href]"):
        href = urljoin(url, link["href"])
        if href not in download_urls:
            download_urls.append(href)

    if not download_urls:
        logger.warning(
            "Itch.io asset '%s' (%s) verified as %s, but no direct download link was found (likely requires interactive browser/token download).",
            title,
            url,
            valid[0],
        )
        return None

    return {
        "pack_name": title,
        "source_type": "itch",
        "source_url": url,
        "license": valid[0],
        "author": author,
        "file_urls": download_urls,
    }


def search_itch(session: requests.Session, query: str, limit: int, logger: logging.Logger) -> list[dict[str, Any]]:
    """Search Itch.io free 2D game assets and filter strictly for CC0/CC-BY."""
    search_url = "https://itch.io/game-assets/free/tag-2d"
    logger.info("Searching Itch.io for %r (limit=%d)...", query, limit)
    resp = session.get(search_url, params={"q": query}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    candidate_urls: list[str] = []
    for link in soup.select(".game_cell a.title.game_link[href]"):
        href = link["href"]
        if href not in candidate_urls:
            candidate_urls.append(href)

    verified: list[dict[str, Any]] = []
    for asset_url in candidate_urls:
        if len(verified) >= limit:
            break
        parsed = parse_itch_asset(session, asset_url, logger)
        if parsed:
            verified.append(parsed)
    return verified


def download_and_stage_asset(session: requests.Session, meta: dict[str, Any], logger: logging.Logger) -> bool:
    """Download verified asset files, validate magic bytes, safely unpack, and update manifest.json."""
    manifest = load_manifest()
    pack_slug = slugify(meta["pack_name"])

    downloaded_payloads: list[tuple[str, str, bytes]] = []
    combined_hash = hashlib.sha256()

    for file_url in meta["file_urls"]:
        logger.info("Fetching %s ...", file_url)
        resp = session.get(file_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        kind = validate_binary_payload(resp, logger)
        if kind is None:
            continue
        combined_hash.update(resp.content)
        filename = Path(urlparse(resp.url).path).name or f"{pack_slug}.{kind}"
        downloaded_payloads.append((kind, filename, resp.content))

    if not downloaded_payloads:
        logger.warning("No valid binary payloads downloaded for '%s' (%s).", meta["pack_name"], meta["source_url"])
        return False

    sha256 = combined_hash.hexdigest()
    if manifest_has_sha256(manifest, sha256):
        logger.info("Duplicate SHA-256 (%s) skipped for '%s'.", sha256[:12], meta["pack_name"])
        return False

    dest_dir = ensure_within_workspace(RAW_SPRITES_DIR / f"{pack_slug}_{sha256[:8]}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    for kind, filename, payload in downloaded_payloads:
        if kind == "zip":
            safe_extract_zip(payload, dest_dir, logger)
        elif kind == "png":
            out_png = ensure_within_workspace(dest_dir / Path(filename).name)
            out_png.write_bytes(payload)
            logger.info("Saved PNG to %s", out_png.relative_to(WORKSPACE_ROOT))

    raw_files = collect_raw_file_metadata(dest_dir, logger)
    if not raw_files:
        logger.warning("Downloaded '%s' yielded 0 valid PNG files; cleaning up empty folder.", meta["pack_name"])
        shutil.rmtree(dest_dir, ignore_errors=True)
        return False

    entry = {
        "id": f"{pack_slug}-{sha256[:8]}",
        "pack_name": meta["pack_name"],
        "source_type": meta["source_type"],
        "source_url": meta["source_url"],
        "license": meta["license"],
        "author": meta["author"],
        "acquired_at": datetime.now(timezone.utc).isoformat(),
        "sha256": sha256,
        "raw_files": raw_files,
        "status": "raw_staged",
    }
    manifest["assets"].append(entry)
    save_manifest(manifest)
    logger.info(
        "Staged '%s' [%s] (%d PNGs) -> %s",
        meta["pack_name"],
        meta["license"],
        len(raw_files),
        dest_dir.relative_to(WORKSPACE_ROOT),
    )
    return True


def handle_direct_url(
    session: requests.Session,
    url: str,
    cli_license: str | None,
    cli_author: str | None,
    logger: logging.Logger,
) -> None:
    """Acquire an asset from a webpage or direct binary URL with strict license checks."""
    host = urlparse(url).netloc.lower()
    if "opengameart.org" in host and "/sites/default/files/" not in url:
        meta = parse_opengameart_asset(session, url, logger)
        if meta:
            download_and_stage_asset(session, meta, logger)
        return
    if "itch.io" in host and not urlparse(url).path.lower().endswith((".zip", ".png")):
        meta = parse_itch_asset(session, url, logger)
        if meta:
            download_and_stage_asset(session, meta, logger)
        return

    if not cli_license:
        logger.error("Rejected direct URL %s: --license (CC0 or CC-BY) is required for direct file URLs.", url)
        return
    normalized_lic = normalize_license(cli_license)
    if normalized_lic not in ALLOWED_LICENSES:
        logger.error("Rejected direct URL %s: license %r is not CC0 or CC-BY.", url, cli_license)
        return

    filename = Path(urlparse(url).path).name or "direct_asset"
    meta = {
        "pack_name": Path(filename).stem,
        "source_type": "direct_url",
        "source_url": url,
        "license": normalized_lic,
        "author": cli_author or "unknown",
        "file_urls": [url],
    }
    download_and_stage_asset(session, meta, logger)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rayneta Gather Sprite Importer — Phase 2 Asset Acquisition CLI")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--register-local", action="store_true", help="Copy and catalog existing local seed packs into raw_sprites/")
    group.add_argument("--local-path", type=Path, help="Local .zip archive or folder of PNGs inside workspace to stage")
    group.add_argument("--search", type=str, help="Search query for CC0/CC-BY 2D tabletop sprites")
    group.add_argument("--url", type=str, help="Direct asset page or binary (.zip/.png) URL to acquire")

    parser.add_argument("--source", choices=["opengameart", "itch", "all"], default="all", help="Repository to search")
    parser.add_argument("--limit", type=int, default=5, help="Max verified results per source")
    parser.add_argument("--download", action="store_true", help="Download and stage verified search results")
    parser.add_argument("--license", type=str, help="Explicit license for direct binary URLs or local paths (CC0, CC-BY, CC-BY-3.0, CC-BY-4.0)")
    parser.add_argument("--author", type=str, help="Author attribution for direct URLs or local paths (for CC-BY compliance)")
    return parser


def main() -> int:
    logger = setup_logging()
    args = build_parser().parse_args()

    if args.register_local:
        register_local_packs(logger)
        return 0

    if args.local_path:
        ingest_local_path(args.local_path, args.license, args.author, logger)
        return 0

    with http_session() as session:
        if args.search:
            results: list[dict[str, Any]] = []
            if args.source in ("opengameart", "all"):
                results.extend(search_opengameart(session, args.search, args.limit, logger))
            if args.source in ("itch", "all"):
                results.extend(search_itch(session, args.search, args.limit, logger))

            logger.info("Found %d license-verified asset(s) matching %r.", len(results), args.search)
            for idx, item in enumerate(results, 1):
                logger.info(
                    "  [%d] %s | %s | %s | by %s | %s",
                    idx,
                    item["source_type"],
                    item["license"],
                    item["pack_name"],
                    item["author"],
                    item["source_url"],
                )
                if args.download:
                    download_and_stage_asset(session, item, logger)
            return 0

        if args.url:
            handle_direct_url(session, args.url, args.license, args.author, logger)
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
