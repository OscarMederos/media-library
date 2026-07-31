# Media Library

A self-hosted barcode scanner and inventory tracker for books, movies, and games. Scan a barcode with a phone camera, and the app looks up metadata (title/author for books via Google Books/OpenLibrary, title/type via UPCDatabase for UPC/EAN items), stores it in SQLite, and lets you browse/edit/enrich the collection from a mobile-friendly web UI.

## Features

- **Barcode scanning** in-browser via ZXing (`static/scan.html`), using the device camera — no native app required.
- **Automatic metadata lookup**
  - ISBN-10/13 → Google Books, falls back to OpenLibrary (title + author).
  - UPC/EAN-12/13 → UPCDatabase (title, inferred media type, cover image).
- **Enrichment**
  - Movies: OMDb (poster, release year, IMDb ID, full raw JSON stored).
  - Games: IGDB (developer, release year, cover image, IGDB game ID) via Twitch OAuth client-credentials flow.
- **Library UI** (`static/library.html`) — search/filter by type, author, platform, developer; inline edit modal; per-item OMDb/IGDB enrich buttons; offline-capable via `localStorage` cache + service worker.
- **Reporting dashboard** (`static/utility.html`) — media type breakdown, missing-field charts (author/platform/format/OMDb/IGDB), duplicate barcode detection, CSV export, bulk OMDb/IGDB enrichment with confirmation.
- **Offline support** via a service worker (`media/sq.js`) that caches UI assets and does network-first caching of `/media`.
- **Self-migrating SQLite schema** — new columns are added automatically at startup (`init_db()`, `ensure_igdb_columns()`).

## Architecture
```
├── app/
│ ├── main.py # FastAPI app: scan/lookup/CRUD/enrich/report endpoints
│ ├── igdb_enrich.py # IGDB (Twitch OAuth) client + game enrichment logic
│ └── requirements.txt
├── static/ # served at /ui via StaticFiles
│ ├── scan.html # camera scan UI (ZXing)
│ ├── library.html # browse/search/edit/delete/enrich UI
│ ├── utility.html # reporting dashboard (Chart.js) + bulk enrich
│ └── zxing.min.js # bundled ZXing browser reader
├── media/sq.js # PWA service worker (cache UI + /media)
├── Dockerfile
├── docker-compose.yml
└── .gitignore
```

The backend is a single FastAPI app backed by SQLite (`DB_PATH`, default `/data/media.db`). Static pages are served at `/ui/*`; the service worker path referenced by the pages is `/ui/sq.js` (ensure `media/sq.js` is copied into `static/` at build time, or adjust the Dockerfile `COPY` step accordingly).

## Requirements

- Docker + Docker Compose (recommended), or Python 3.11+ locally.
- API keys (optional but required for full functionality):
  - `UPCDATABASE_API_KEY` — UPC/EAN lookups.
  - `OMDB_API_KEY` — movie enrichment.
  - `IGDB_CLIENT_ID` / `IGDB_CLIENT_SECRET` — game enrichment (Twitch developer app credentials).

## Setup

### Docker (recommended)

1. Create a `.env` file in the repo root:

```env
UPCDATABASE_API_KEY=your_upcdatabase_key
OMDB_API_KEY=your_omdb_key
IGDB_CLIENT_ID=your_twitch_client_id
IGDB_CLIENT_SECRET=your_twitch_client_secret
LOG_LEVEL=INFO
```

2. Build and start:

```bash
docker compose up -d --build
```

The app listens on `https://<host>:8000` (the Dockerfile self-signs a TLS cert at build time — a browser camera prompt over `getUserMedia` requires HTTPS or `localhost`).

3. Data persists to `./data/media.db` on the host (bind-mounted to `/data`).

### Local (without Docker)

```bash
cd app
pip install -r requirements.txt
export DB_PATH=./media.db
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Static files are referenced from `/static` in `main.py` — run from a working directory where `../static` resolves to the repo's `static/` folder, or adjust `StaticFiles(directory=...)`.

## Usage

- Open `https://<host>:8000/ui/scan.html` on a phone, tap **Start Camera**, and scan a barcode. The item is looked up and inserted automatically.
- Open `https://<host>:8000/ui/library.html` to search, edit, delete, or manually trigger OMDb/IGDB enrichment per item.
- Open `https://<host>:8000/ui/utility.html` for reporting, CSV export, and bulk enrichment.

## API Reference

Base URL: `http://<host>:8000`

| Method | Path | Description |
|---|---|---|
| `POST` | `/scan` | Body: `{"barcode": "..."}`. Normalizes barcode, looks up metadata if new, inserts/returns the item. |
| `GET` | `/media` | List media. Query: `search`/`q`, `media_type`, `author`, `platform`, `developer`, `limit` (default 5000). |
| `GET` | `/media/{id}` | Fetch a single item. |
| `PATCH` | `/media/{id}` | Partial update (any subset of fields in `MediaUpdate`). |
| `DELETE` | `/media/{id}` | Delete an item. |
| `POST` | `/media/{id}/enrich?force=false` | Fetch/refresh OMDb data for a movie item. |
| `POST` | `/media/{id}/enrich/igdb` | Fetch/fill IGDB data for a game item (fill-missing-only, no overwrite). |
| `GET` | `/reports/summary` | Media type counts + data-quality counters (dupes, empty titles, unknown type, missing cover). |
| `GET` | `/reports/missing` | Query: `media_type`, `field` (`author`/`platform`/`format`/`cover_url`), `limit`. Lists items missing a field. |

## Database Schema

SQLite table `media` (columns auto-migrated on startup):

- Core: `id`, `barcode` (unique), `title`, `title_raw`, `media_type`
- Type-specific: `author`, `platform`, `developer`, `format`, `location`, `status`, `release_year`, `cover_url`
- Provenance: `source`, `source_payload`, `notes`, `added_at`, `updated_at`
- OMDb: `omdb_imdb_id`, `omdb_status`, `omdb_last_fetched_at`, `omdb_raw_json`, `omdb_hash`
- IGDB: `igdb_game_id`, `igdb_cover_image_id`, `igdb_last_enriched_at` (and `igdb_cover_url` fallback if `cover_url` doesn't exist)

`media_type` is normalized case-insensitively; games may appear as `game`, `video_game`, or `video game` depending on source — `_media_type_sql_values()` in `main.py` handles the variants.

## Notes / Gotchas

- `/scan` treats a barcode already present in the DB as `status: "exists"` and returns the stored item without re-running lookups.
- OMDb enrichment only overwrites `release_year`/`cover_url` if they're currently blank; it always refreshes `omdb_status`/`omdb_raw_json`/`omdb_hash`.
- IGDB enrichment is fill-only — it never overwrites existing `developer`, `release_year`, or cover data.
- `static/library.html` references `/ui/sq.js`; confirm the service worker is actually served at that path in your deployment.
- Camera access (`getUserMedia`) requires a secure context — use the bundled self-signed cert, a reverse proxy with valid TLS, or `localhost`.