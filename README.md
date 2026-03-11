# WebClipper

A self-hosted browser app for managing raw gameplay or recording files from mounted storage: preview in the browser, trim into clips, remux MKV to MP4, and organize output in a dedicated clips folder.

## Features

- **Source management** — Add mounted folders (e.g. `/mnt/recordings/Elgato`) and scan for recordings
- **Recordings view** — Grid of cards with thumbnails, file type (MP4/MKV), size, date; open editor, remux MKV, bulk delete
- **Preview** — Browser-safe previews (direct, remux, or transcode) so you can play files that wouldn’t play natively
- **Clip editor** — Set in/out points, choose export mode (fast cut vs accurate), audio tracks, export to clips folder
- **Remux** — MKV → MP4 (stream copy), per-file or bulk, with job progress
- **Clips library** — List, play, download, delete exported clips and their metadata sidecars

## Stack

- **Backend:** FastAPI, Uvicorn, FFmpeg/FFprobe
- **Frontend:** Single HTML file, vanilla JS and CSS, no build step
- **Deploy:** Docker / Docker Compose; bind-mount your recordings and optional data volume

## Quick start

### Local (no Docker)

1. Install Python 3.10+, FFmpeg, and FFprobe.
2. Create a data directory (e.g. `./data`) and set `WEBCLIPPER_DATA` to it (default is `/data`).
3. Install and run:

```bash
pip install -r requirements.txt
python run.py
```

Open http://localhost:8765. Add a source (path to a folder with video files), then reload recordings.

### Docker

1. Edit `docker-compose.yml`: set the host path for recordings, e.g.  
   `- /volume1/Recordings:/mnt/recordings`
2. Build and run:

```bash
docker compose up -d
```

Open http://localhost:8765. In Settings, add a source with the **container** path (e.g. `/mnt/recordings/Elgato`), then use Recordings to browse and edit.

## Data layout

Under `WEBCLIPPER_DATA` (default `/data`):

- `config.json` — Sources, clips output folder, auto-refresh settings
- `clips/` — Exported clips and `.meta.json` sidecars
- `thumbnails/` — Generated recording thumbnails
- `preview/` — Cached browser-safe previews (remux/transcode)

## API (high level)

- `GET/POST/DELETE /api/sources` — List, add, remove sources
- `GET/PUT /api/settings` — Settings (clips folder, refresh)
- `POST /api/browse` — Folder browser for picking paths
- `GET /api/recordings` — List recordings (optional `?source=path`)
- `GET /api/recordings/info?path=...` — File + probe info
- `GET /api/recordings/thumbnail?path=...` — Thumbnail image
- `GET /api/recordings/preview?path=...` — Preview strategy and URL
- `DELETE /api/recordings` — Delete files by path
- `POST /api/remux` — Start remux job (MKV paths)
- `GET /api/jobs`, `GET /api/jobs/{id}` — Remux job status
- `POST /api/clips/create` — Create clip (body: source_path, start/end, mode, audio, etc.)
- `GET /api/clips`, `DELETE /api/clips` — List/delete clips
- `GET /api/stream/preview?path=...`, `GET /api/stream/clip?path=...` — Stream preview or clip file

## Limitations

- Remux jobs are in-memory only (lost on restart).
- No authentication; treat as an internal/trusted tool.
- Editor has no waveform or NLE-style timeline; it’s a trim-and-export clipper.

## Publishing to GitHub

1. **Install Git** (if needed): [git-scm.com](https://git-scm.com/download/win) — use the default options so `git` is on your PATH.

2. **Open a terminal in the project folder** (e.g. `C:\Users\nemoh\WebClipper`).

3. **Initialize and commit:**
   ```bash
   git init
   git add .
   git commit -m "Initial commit: WebClipper app"
   ```

4. **Create a new repo on GitHub:**
   - Go to [github.com/new](https://github.com/new).
   - Name it (e.g. `WebClipper`), choose Public, leave “Add a README” **unchecked**.
   - Click **Create repository**.

5. **Push your code** (replace `YOUR_USERNAME` and `WebClipper` with your GitHub username and repo name):
   ```bash
   git remote add origin https://github.com/YOUR_USERNAME/WebClipper.git
   git branch -M main
   git push -u origin main
   ```

   If you use **SSH** instead of HTTPS:
   ```bash
   git remote add origin git@github.com:YOUR_USERNAME/WebClipper.git
   git branch -M main
   git push -u origin main
   ```

   GitHub will prompt you to sign in (HTTPS) or use your SSH key the first time you push.

## License

MIT.
