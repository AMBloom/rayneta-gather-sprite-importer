---
name: gather-sprite-importer
description: Operates the Rayneta Gather Sprite Importer pipeline inside /Users/andrewbloom/Documents/Rayneta/Internal/Assets/Gather to acquire CC0/CC-BY 2D tabletop sprites, normalize them to Gather v1.39.2 32x32 pixel-art specs, curate via preview_gallery.html, stage Upload_Batches/, and optionally back up or inject Gather Maps API objects.
---

# Rayneta Gather Sprite Importer

## Workspace & Environment Rules
- **Workspace Root:** `/Users/andrewbloom/Documents/Rayneta/Internal/Assets/Gather`
- **Python Runtime:** Always invoke `.venv/bin/python` (never global Python).
- **Workspace Lock:** Keep all downloads, scripts, configs, backups, and logs strictly inside the workspace root.

## Hard Constraints & Guardrails
1. **Strict Licensing:** Only acquire web assets explicitly verified as **CC0** or **CC-BY** (`3.0` / `4.0`). Reject `All Rights Reserved`, `CC-BY-NC`, `CC-BY-SA`, `CC-BY-ND`, `GPL`, or missing license metadata.
2. **Gather v1.39.2 Spec:**
   - Canvas dimensions must be multiples of `32×32 px`, capped at `256×256 px` (`8×8` tiles).
   - Output encoding must be `RGBA` PNG under `3 MB`.
   - Use `Image.Resampling.NEAREST` exclusively for pixel-art scaling (`<=16 px` sprites scaled `2×`).
   - Route opaque images (`alpha_min == 255`) to `gather_ready/_needs_alpha_review/` and secondary strip frames (`Frame 1..N`) to `gather_ready/_animation_frames/`.
3. **Human-in-the-Loop Safety:** Automated/scheduled runs must only stage sprites into `gather_ready/Upload_Batches/` and `gather_ready/preview_gallery.html`. Never run `gather_map_inject.py --apply` on a schedule.

## Core Workflows

### 1. Run a Themed Sprite Drop (End-to-End)
Preview next rotating theme from `config/themes.json`:
```zsh
.venv/bin/python scripts/run_pipeline.py --rotate-next --dry-run
```
Execute rotating theme drop (or pass `--theme "<query>"` / `--theme-preset <id>`):
```zsh
.venv/bin/python scripts/run_pipeline.py --rotate-next
```

### 2. Re-Sync After Visual Curation
1. Open `gather_ready/preview_gallery.html` in a browser to inspect normalized sprites.
2. Delete unwanted PNGs from `gather_ready/Build_Tool_Uploads/<pack_slug>/`.
3. Reconcile `manifest.json`, refresh the gallery, rebuild `Upload_Batches/`, and regenerate `UPLOAD_GUIDE.md`:
```zsh
.venv/bin/python scripts/stage_gather.py --sync --batch-size 40
```

### 3. Manual Acquisition & Normalization (Individual Phases)
- Register local seed packs: `.venv/bin/python scripts/acquire_assets.py --register-local`
- Search/download a verified pack: `.venv/bin/python scripts/acquire_assets.py --search "<query>" --source opengameart --limit 3 --download`
- Acquire a specific asset page or binary URL: `.venv/bin/python scripts/acquire_assets.py --url "<url>" [--license CC0|CC-BY --author "<name>"]`
- Re-normalize packs: `.venv/bin/python scripts/normalize_sprites.py --all`

### 4. Gather v1.39.2 Import & Optional Maps API Injection
- **Default (Build Tool):** Drag and drop one `gather_ready/Upload_Batches/batch_XX/` folder at a time into Gather (`Customize → Build → Uploaded`). Remind users that Build Tool uploads are personal to the uploader's library and cannot be resized or deleted once uploaded.
- **Optional Maps API (`gather_map_inject.py`):** Requires `GATHER_API_KEY`, `GATHER_SPACE_ID`, and `GATHER_MAP_ID` env vars. Always back up first and preview with `--dry-run` before `--apply`:
```zsh
.venv/bin/python scripts/gather_map_inject.py --backup-only
.venv/bin/python scripts/gather_map_inject.py --placement-json placements.json --dry-run
.venv/bin/python scripts/gather_map_inject.py --placement-json placements.json --apply
```
