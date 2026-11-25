## Rayneta Gather Sprite Importer: Antigravity Agent Roadmap & Directive

This document serves as the master development roadmap and system prompt for the Antigravity agent. It establishes the workspace, strict execution guardrails, and phased development lifecycle for the sprite automation pipeline.

### Agent Rules of Engagement

* **Strict Approval Gate:** You (the Antigravity agent) must **never** generate, execute, or commit code without explicit user approval.  
* **Execution Flow:** At the start of each phase below, you will generate a detailed, step-by-step implementation plan outlining the files to be created, scripts to be written, and libraries to be installed. You will pause and wait for the user to reply "Approved" before executing the plan.  
* **Target Workspace:** All project files, raw assets, scripts, and output directories must be scoped exclusively to: `/Users/andrewbloom/Documents/Rayneta/Internal/Assets/Gather`  
* **Licensing Constraint:** Only target assets explicitly labeled as CC0 (Public Domain) or CC-BY.

---

### Phase 1: Workspace Initialization & Environment Setup

**Goal:** Scaffold the local directory structure and prepare the Python environment for the agent's sandboxed runtime.

* **Directory Structure Generation:**  
    
* `/raw_sprites` (Staging for downloaded zip/png files)  
    
* `/gather_ready` (Final normalized PNGs ready for Gather upload)  
    
* `/scripts` (Python processing utilities)  
    
* `/logs` (Execution and error logging)  
    
* **Dependency Management:**  
    
* Define a `requirements.txt` containing `Pillow` (for image processing), `requests` (for HTTP fetching), and `beautifulsoup4` (for scraping CC0 asset hubs).  
    
* **Agent Task:** Draft the initialization plan for creating these directories and installing requirements, then wait for approval.

### Phase 2: Asset Acquisition Module

**Goal:** Build the search and extraction tools to fetch raw tabletop sprite sheets from open-source repositories.

* **Target Sources:** Itch.io (filtering by free/CC0 tabletop assets) and OpenGameArt.org.  
* **Download Handler:** Write a Python module that accepts a URL, downloads the file, and unpacks `.zip` archives into the `/raw_sprites` directory.  
* **Metadata Tracking:** Generate a `manifest.json` in the workspace to track source URLs, author attribution (for CC-BY compliance), and download dates.  
* **Agent Task:** Outline the architecture of the web-scraping and downloading script, detail the target endpoints, and wait for approval.

### Phase 3: Image Normalization Pipeline

**Goal:** Process raw, heterogeneous sprite sheets into Gather-compliant individual assets.

* **Grid Slicing Engine:** Develop a Pillow-based script that takes a defined grid size (e.g., 16x16, 32x32, 48x48) and slices full sprite sheets into individual frame/object files.  
    
* **Gather Specification Scaling:**  
    
* Apply `Image.NEAREST` (Nearest-Neighbor) resampling to scale sprites perfectly to multiples of Gather's **32x32 pixel** grid constraint to preserve crisp pixel art edges.  
    
* **Transparency & Optimization:**  
    
* Trim dead transparency (auto-crop empty alpha space around the sprite).  
    
* Enforce standard RGBA `.png` encoding.  
    
* Implement a file size check to ensure no single output exceeds Gather's **3 MB** hard limit.  
    
* **Agent Task:** Provide the logic flow and pseudo-code for the Pillow image manipulation script. Highlight how alpha channels and nearest-neighbor scaling will be handled, then wait for approval.

### Phase 4: Staging & Gather Ingestion Options

**Goal:** Route the normalized PNGs into Gather for the Rayneta team to use.

* **Option A: Build Tool Staging (Default)**  
    
* Configure the pipeline to output finalized, named assets directly into `/gather_ready/Build_Tool_Uploads`.  
    
* Output a user-facing log file confirming which assets are ready for manual drag-and-drop into the Gather UI (`Customize → Build → Uploaded`).  
    
* **Option B: API Map Injection (Advanced)**  
    
* Develop an optional Python module to interact with the Gather Maps API (`api.gather.town`).  
    
* Implement a `GET` function to back up the current Rayneta office map JSON.  
    
* Implement a function to upload the image to a static URL (e.g., AWS S3 or GitHub Pages) and `POST` the new object coordinates directly to the map array.  
    
* **Agent Task:** Present the structural plan for Option A (and Option B if requested), detailing the output folder routing and API payload structures, then wait for approval.

### Phase 5: Automation & Scheduling

**Goal:** Convert the pipeline into a hands-free, recurring task for fresh seasonal office drops.

* **Cron / Trigger Setup:** Configure an Antigravity schedule trigger to run the Phase 2-4 pipeline bi-weekly.  
* **Thematic Querying:** Parameterize the search queries so the agent can be scheduled to look for specific themes (e.g., "Cyberpunk Office", "Holiday Tavern", "Sci-Fi Desks").  
* **Agent Task:** Draft the configuration for the scheduled trigger and the parameterization scheme for the main execution script, then wait for approval.

