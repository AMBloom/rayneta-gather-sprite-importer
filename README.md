# Rayneta Gather Sprite Importer

Automated sprite acquisition, pixel-art normalization, visual curation, and Gather v1.39.2 staging pipeline for Rayneta's virtual office.

It supports two workflows:
- **For Teammates (Zero-Python Import):** Clone/pull the repo, browse the offline visual gallery, and drag-and-drop vetted `32×32`-aligned sprite batches into Gather (**Customize → Build → Uploaded**).
- **For Curators / Maintainers:** Run themed web drops (OpenGameArt/Itch) or local pack imports, normalize sprites to Gather v1.39.2 constraints, prune unwanted items, and regenerate upload batches.

---

## Current Staged Library Snapshot

- **Curated Build-Tool-Ready Sprites:** `860` PNGs across `22` upload batches (`gather_ready/Upload_Batches/batch_01` – `batch_22`, max `40` per batch)
- **Visual Gallery Items:** `1,460` sprites across all curation buckets in `gather_ready/preview_gallery.html`
- **Included Packs (`9`):**
  - `MegaPackFree` (`291` staged)
  - `Pixel Art Icon Pack - RPG` (`107` staged)
  - `Pixel Art Top Down - Basic v1.2.3` (`187` staged)
  - `topdown-shooter` (`199` staged — `CC0`, Kenney)
  - `2d-top-down-office-characters` (`2` staged — `CC0`, BunnyHop43)
  - `16x16-indoor-rpg-tileset-the-baseline` (`13` staged — `CC-BY-3.0`, Redshrike)
  - `rpg-indoor-tileset-expansion-1` (`21` staged — `CC-BY-3.0`, Redshrike)
  - `frogs` (`9` staged — local pack)
  - `bufo_core` (`31` staged — `2×2` core static PNGs from `bufo.fun`)

---

## Quick Start for Teammates (Importing into Gather)

No Python environment is required if you only want to browse and upload already-staged sprites:

1. **Clone or pull** the latest `main` branch:
   ```zsh
   git clone https://github.com/AMBloom/rayneta-gather-sprite-importer.git
   cd rayneta-gather-sprite-importer
   ```
2. **Browse visually:** Open `gather_ready/preview_gallery.html` in any browser. Filter by bucket (`Build_Tool_Uploads` is selected by default), pack name, tile footprint (`1x1`, `2x2`, etc.), or filename search.
3. **Check batch checklists:** Open `gather_ready/UPLOAD_GUIDE.md` for per-pack license attribution and batch contents.
4. **Upload to Gather (`Customize → Build → Uploaded`):**
   - **Full batches:** Drag and drop **one `gather_ready/Upload_Batches/batch_XX/` folder at a time** (capped at `40` PNGs per folder to avoid browser upload stalls).
   - **Pick-and-choose:** Drag individual `.png` files directly from `gather_ready/Build_Tool_Uploads/<pack_slug>/`.

> **Important Gather v1.39.2 Notes:**
> - Sprites uploaded via **Build → Uploaded** are stored in **your personal account library** (not automatically shared across other admins' upload trays until placed on a map).
> - Once uploaded to Gather's Uploaded library, objects **cannot be resized or deleted** from the library UI—inspect `preview_gallery.html` first!

---

## Curator & Developer Setup (Python Pipeline)

Required only if you are acquiring new packs, re-normalizing sprites, or re-syncing batches after deletions.

```zsh
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Dependencies (`requirements.txt`): `Pillow>=10.0.0`, `requests>=2.31.0`, `beautifulsoup4>=4.12.0`, `lxml>=5.0.0`.

> Always run scripts with `.venv/bin/python` from the repository root.

---

## Pipeline Architecture & CLI Reference

The pipeline runs in five stages:

1. **Phase 2 — Acquire (`scripts/acquire_assets.py`)**: Searches OpenGameArt/Itch.io (enforcing `CC0`/`CC-BY` licenses on web sources), downloads verified `.zip`/`.png` payloads with magic-byte and Zip Slip checks, or ingests local packs (`--register-local` / `--local-path`). Stages raw files into `raw_sprites/` and records `raw_staged` entries in `manifest.json`.
2. **Phase 3 — Normalize (`scripts/normalize_sprites.py`)**: Classifies sheets (`single`, `strip`, `grid`, or connected-component flood-fill), scales `<=16px` pixel art `2×` with `Image.Resampling.NEAREST`, pads to `32×32` tile multiples (capped at `256×256 px` / `8×8` tiles, `<3 MB` RGBA PNG), deduplicates by RGBA pixel SHA-256, routes opaque or oversized assets to review buckets, and builds `gather_ready/preview_gallery.html`.
3. **Phase 4 Option A — Stage (`scripts/stage_gather.py`)**: Reconciles manual deletions in `gather_ready/Build_Tool_Uploads/`, updates `manifest.json`, rebuilds `gather_ready/Upload_Batches/batch_XX/`, and generates `gather_ready/UPLOAD_GUIDE.md`.
4. **Phase 4 Option B — Direct Map Injection (`scripts/gather_map_inject.py`, Optional)**: Backs up a Gather map via HTTP Maps v2 API (`GET`) and injects externally hosted sprite placements (`POST`) from a `--placement-json` spec. Never invoked automatically.
5. **Phase 5 — Unified Orchestrator (`scripts/run_pipeline.py`)**: Chains Acquire → Normalize → Stage for a custom query or rotating preset from `config/themes.json`.

### Common Commands

#### Unified Orchestrator (Phase 5)
```zsh
# Preview the next rotating theme without writing files
.venv/bin/python scripts/run_pipeline.py --rotate-next --dry-run

# Execute the next rotating theme (Acquire -> Normalize -> Stage)
.venv/bin/python scripts/run_pipeline.py --rotate-next

# Run a specific preset from config/themes.json or a custom query
.venv/bin/python scripts/run_pipeline.py --theme-preset cozy_office
.venv/bin/python scripts/run_pipeline.py --theme "cyberpunk office" --limit 2
```

#### Individual Phase Commands
- **Acquire (`scripts/acquire_assets.py`):**
  ```zsh
  # Ingest a local .zip archive or PNG folder inside the workspace
  .venv/bin/python scripts/acquire_assets.py --local-path frogs.zip [--license CC0 --author "Name"]

  # Register configured seed folders (MegaPackFree, Pixel Art Icon Pack - RPG, Pixel Art Top Down)
  .venv/bin/python scripts/acquire_assets.py --register-local

  # Search and download verified CC0/CC-BY packs
  .venv/bin/python scripts/acquire_assets.py --search "top down furniture" --source opengameart --limit 3 --download

  # Acquire a specific OpenGameArt/Itch page or direct binary URL
  .venv/bin/python scripts/acquire_assets.py --url "<url>" [--license CC0|CC-BY --author "<name>"]
  ```
- **Normalize (`scripts/normalize_sprites.py`):**
  ```zsh
  # Dry-run all manifest packs
  .venv/bin/python scripts/normalize_sprites.py --all --dry-run

  # Normalize a single pack (preserves manual curation in other packs)
  .venv/bin/python scripts/normalize_sprites.py --pack-id <pack_id>

  # Re-normalize all packs
  .venv/bin/python scripts/normalize_sprites.py --all
  ```
- **Stage & Sync (`scripts/stage_gather.py`):**
  ```zsh
  # Re-sync after deleting unwanted PNGs from gather_ready/Build_Tool_Uploads/
  .venv/bin/python scripts/stage_gather.py --sync --batch-size 40
  ```
- **Optional Gather Maps v2 API Injection (`scripts/gather_map_inject.py`):**
  Requires `GATHER_API_KEY`, `GATHER_SPACE_ID`, and `GATHER_MAP_ID` environment variables (or CLI flags). Always back up and preview before `--apply`:
  ```zsh
  .venv/bin/python scripts/gather_map_inject.py --backup-only
  .venv/bin/python scripts/gather_map_inject.py --placement-json placements.json --dry-run
  .venv/bin/python scripts/gather_map_inject.py --placement-json placements.json --apply
  ```

---

## Directory Layout

| Path | Purpose | Git Tracked |
| :--- | :--- | :--- |
| `gather_ready/Build_Tool_Uploads/` | Vetted RGBA PNGs (`32×32` multiples, `≤256×256 px`, `<3 MB`) | Yes |
| `gather_ready/Upload_Batches/` | Numbered drag-and-drop folders (`batch_01/` … `batch_22/`) | Yes |
| `gather_ready/_needs_alpha_review/` | Opaque sprites (`alpha_min == 255`) needing background removal | Yes |
| `gather_ready/_animation_frames/` | Secondary frames (`Frame 1..N`) from horizontal animation strips | Yes |
| `gather_ready/_oversized_rejected/` | Sprites exceeding `256×256 px` (`8×8` tiles) | Yes |
| `gather_ready/preview_gallery.html` | Self-contained offline HTML curation & preview gallery | Yes |
| `gather_ready/UPLOAD_GUIDE.md` | Gather v1.39.2 import instructions, attribution, & checklists | Yes |
| `manifest.json` | Asset provenance, SHA-256 hashes, counts, and pipeline state | Yes |
| `config/` | Theme rotation presets (`themes.json`) and schedule templates | Yes |
| `scripts/` | Python CLI modules for Phases 2–5 | Yes |
| `skills/gather_sprite_importer/` | Antigravity `SKILL.md` operator definition | Yes |
| `raw_sprites/` | Downloaded `.zip` contents and staged raw pack files | **Ignored** |
| `logs/` | Pipeline logs and timestamped Gather map backups | **Ignored** |
| `.venv/` | Local Python virtual environment | **Ignored** |
| `*.zip`, seed dirs, `repomix-output.xml` | Raw local seed archives/folders and repo bundle | **Ignored** |

---

## Guardrails & Bi-Weekly Scheduling

- **Strict Web Licensing:** Automated web scraping (`--search`, `--url`, `run_pipeline.py`) enforces `CC0` or `CC-BY` (`3.0` / `4.0`) verification and rejects `CC-BY-NC`, `CC-BY-SA`, `CC-BY-ND`, `GPL`, `All Rights Reserved`, or missing licenses. Local packs ingested via `--local-path` without `--license` are tagged `local_unverified`.
- **Gather Spec Enforcement:** Output PNGs are strictly `RGBA`, `32×32`-aligned, `≤256×256 px`, and `<3 MB`, scaled exclusively with `Image.Resampling.NEAREST`.
- **Automated Staging Schedule (Optional):**
  - **macOS `launchd`:** Copy `config/com.rayneta.gather-sprite-importer.plist` (updating paths if cloned elsewhere) to `~/Library/LaunchAgents/` and load via `launchctl load ~/Library/LaunchAgents/com.rayneta.gather-sprite-importer.plist`.
  - **Antigravity Cron:** Schedule spec in `config/antigravity_trigger.json` (`0 9 1,15 * *`). Scheduled runs only stage sprites locally (`stage_only: true`) and never mutate live Gather maps.
