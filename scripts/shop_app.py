#!/usr/bin/env python3
"""Local Sprite Shop server for browsing, revealing in Finder, and staging Gather picks."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import urllib.parse
import webbrowser
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
GATHER_READY_DIR = (WORKSPACE_ROOT / "gather_ready").resolve()
MY_PICKS_DIR = GATHER_READY_DIR / "_my_picks"
MAX_BATCH_SIZE = 40

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shop_app")


def resolve_safe_sprite_path(rel_path: str) -> Path:
    """Resolve a gallery-relative sprite path strictly inside gather_ready/."""
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise ValueError("Empty sprite path")
    clean_rel = urllib.parse.unquote(rel_path).lstrip("/")
    candidate = (GATHER_READY_DIR / clean_rel).resolve()
    if os.path.commonpath([str(GATHER_READY_DIR), str(candidate)]) != str(GATHER_READY_DIR):
        raise ValueError(f"Path escapes gather_ready/: {rel_path}")
    if not candidate.is_file() or candidate.suffix.lower() != ".png":
        raise FileNotFoundError(f"Sprite PNG not found: {rel_path}")
    return candidate


def reveal_in_file_manager(target: Path) -> None:
    """Reveal a file or open a folder in the OS file manager without invoking a shell."""
    if sys.platform == "darwin":
        cmd = ["open", "-R", str(target)] if target.is_file() else ["open", str(target)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif sys.platform.startswith("win"):
        cmd = ["explorer", f"/select,{target}"] if target.is_file() else ["explorer", str(target)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        folder = target.parent if target.is_file() else target
        subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stage_picked_sprites(rel_paths: list[str]) -> tuple[Path, int, int]:
    """Copy picked PNGs into gather_ready/_my_picks/drop_<ts>/ and return (folder, count, batches)."""
    if not rel_paths:
        raise ValueError("No sprites selected")

    unique_targets: list[Path] = []
    seen: set[Path] = set()
    for rel in rel_paths:
        resolved = resolve_safe_sprite_path(rel)
        if resolved not in seen:
            seen.add(resolved)
            unique_targets.append(resolved)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    drop_dir = (MY_PICKS_DIR / f"drop_{ts}").resolve()
    if os.path.commonpath([str(GATHER_READY_DIR), str(drop_dir)]) != str(GATHER_READY_DIR):
        raise ValueError("Invalid staging destination")
    drop_dir.mkdir(parents=True, exist_ok=True)

    used_names: dict[str, int] = {}
    batches = 1
    if len(unique_targets) <= MAX_BATCH_SIZE:
        for src in unique_targets:
            dest_name = _unique_pick_name(src, used_names)
            shutil.copy2(src, drop_dir / dest_name)
    else:
        batches = (len(unique_targets) + MAX_BATCH_SIZE - 1) // MAX_BATCH_SIZE
        for idx in range(0, len(unique_targets), MAX_BATCH_SIZE):
            chunk = unique_targets[idx : idx + MAX_BATCH_SIZE]
            batch_dir = drop_dir / f"batch_{(idx // MAX_BATCH_SIZE) + 1:02d}"
            batch_dir.mkdir(parents=True, exist_ok=True)
            for src in chunk:
                dest_name = _unique_pick_name(src, used_names)
                shutil.copy2(src, batch_dir / dest_name)

    return drop_dir, len(unique_targets), batches


def _unique_pick_name(src: Path, used_names: dict[str, int]) -> str:
    """Preserve pack context in filename while avoiding collisions inside a picks folder."""
    pack_slug = src.parent.name
    candidate = f"{pack_slug}_{src.name}" if not src.name.startswith(f"{pack_slug}_") else src.name
    count = used_names.get(candidate, 0) + 1
    used_names[candidate] = count
    if count > 1:
        stem, ext = os.path.splitext(candidate)
        return f"{stem}_{count}{ext}"
    return candidate


def build_picks_zip(rel_paths: list[str]) -> tuple[bytes, int]:
    """Bundle picked PNGs into an in-memory ZIP archive (chunked into <=40 batches if large)."""
    unique_targets: list[Path] = []
    seen: set[Path] = set()
    for rel in rel_paths:
        resolved = resolve_safe_sprite_path(rel)
        if resolved not in seen:
            seen.add(resolved)
            unique_targets.append(resolved)

    if not unique_targets:
        raise ValueError("No sprites selected")

    buf = io.BytesIO()
    used_names: dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if len(unique_targets) <= MAX_BATCH_SIZE:
            for src in unique_targets:
                zf.write(src, arcname=f"gather_picks/{_unique_pick_name(src, used_names)}")
        else:
            for idx in range(0, len(unique_targets), MAX_BATCH_SIZE):
                chunk = unique_targets[idx : idx + MAX_BATCH_SIZE]
                batch_name = f"batch_{(idx // MAX_BATCH_SIZE) + 1:02d}"
                for src in chunk:
                    zf.write(src, arcname=f"gather_picks/{batch_name}/{_unique_pick_name(src, used_names)}")
    return buf.getvalue(), len(unique_targets)


class SpriteShopHandler(SimpleHTTPRequestHandler):
    """HTTP handler serving gather_ready/ plus local Finder & Cart API endpoints."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(GATHER_READY_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), format % args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 2 * 1024 * 1024:
            raise ValueError("Invalid request body size")
        data = self.rfile.read(length)
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def do_GET(self) -> None:
        if self.path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/preview_gallery.html")
            self.end_headers()
            return
        if self.path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "mode": "shop_server", "platform": sys.platform})
            return
        super().do_GET()

    def do_POST(self) -> None:
        try:
            body = self._read_json_body()
            if self.path == "/api/reveal":
                target = resolve_safe_sprite_path(body.get("rel_path", ""))
                reveal_in_file_manager(target)
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "revealed": target.relative_to(GATHER_READY_DIR).as_posix()},
                )
                return

            if self.path == "/api/stage-picks":
                rel_paths = body.get("rel_paths", [])
                if not isinstance(rel_paths, list):
                    raise ValueError("rel_paths must be a list")
                drop_dir, count, batches = stage_picked_sprites(rel_paths)
                reveal_in_file_manager(drop_dir)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "count": count,
                        "batches": batches,
                        "folder": drop_dir.relative_to(GATHER_READY_DIR).as_posix(),
                    },
                )
                return

            if self.path == "/api/download-zip":
                rel_paths = body.get("rel_paths", [])
                if not isinstance(rel_paths, list):
                    raise ValueError("rel_paths must be a list")
                zip_bytes, count = build_picks_zip(rel_paths)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename="gather_sprite_picks.zip"')
                self.send_header("Content-Length", str(len(zip_bytes)))
                self.send_header("X-Sprite-Count", str(count))
                self.end_headers()
                self.wfile.write(zip_bytes)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Unknown endpoint"})
        except FileNotFoundError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception as exc:
            logger.exception("Unhandled Sprite Shop API error")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})


def pick_port(preferred: int) -> int:
    """Use preferred localhost port if available, else allocate an ephemeral free port."""
    if preferred > 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", preferred)) != 0:
                return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rayneta Gather Sprite Shop — Local Gallery & Finder Server")
    parser.add_argument("--port", type=int, default=8765, help="Preferred localhost port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Start server without opening a browser tab")
    return parser


def main() -> int:
    if not (GATHER_READY_DIR / "preview_gallery.html").is_file():
        logger.error("Missing %s — run normalize_sprites.py or stage_gather.py first.", GATHER_READY_DIR / "preview_gallery.html")
        return 1

    args = build_parser().parse_args()
    port = pick_port(args.port)
    server = ThreadingHTTPServer(("127.0.0.1", port), SpriteShopHandler)
    url = f"http://127.0.0.1:{port}/preview_gallery.html"
    logger.info("Rayneta Gather Sprite Shop running at %s (Ctrl+C to stop)", url)

    if not args.no_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Sprite Shop server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
