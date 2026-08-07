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
- **Retry lookup** — items that failed their initial lookup (stored as "Unknown Book"/"Unknown Item") can be re-looked-up on demand via `POST /media/{id}/relookup` and a "Retry lookup" button in the library UI.
- **Library UI** (`static/library.html`) — search/filter by type, author, platform, developer; paginated with a "Load more" button; inline edit modal; per-item OMDb/IGDB enrich buttons.
- **Reporting dashboard** (`static/utility.html`) — media type breakdown, missing-field charts (author/platform/format/OMDb/IGDB), duplicate barcode detection, CSV export, bulk OMDb/IGDB enrichment with confirmation.
- **Self-migrating SQLite schema** — new columns and indexes are added automatically at startup (`init_db()`, `ensure_igdb_columns()`).
- **WAL mode** enabled for better read/write concurrency, with a per-connection busy timeout.

## Architecture

```
├── app/
│   ├── main.py            # FastAPI app: scan/lookup/CRUD/enrich/report endpoints
│   ├── igdb_enrich.py     # IGDB (Twitch OAuth) client + game enrichment logic
│   └── requirements.txt
├── static/                # served at /ui via StaticFiles
│   ├── scan.html          # camera scan UI (ZXing)
│   ├── library.html       # browse/search/edit/delete/enrich UI (paginated)
│   ├── utility.html       # reporting dashboard (Chart.js) + bulk enrich
│   └── zxing.min.js       # bundled ZXing browser reader
├── scripts/
│   └── backup.sh          # on-demand backup of DB + .env + compose config
├── tests/                 # pytest suite (run on the Pi under Python 3.11)
│   ├── conftest.py        # fixtures: temp DB + FastAPI TestClient
│   └── test_*.py
├── requirements-dev.txt   # test/dev dependencies (pytest, pytest-mock, httpx)
├── pytest.ini
├── Dockerfile
├── docker-compose.yml
└── .gitignore
```

The backend is a single FastAPI app backed by SQLite (`DB_PATH`, default `/data/media.db`). Static pages are served at `/ui/*`; a middleware forces `Cache-Control: no-store` on `/ui/*` responses so browsers always pick up new deploys. The static directory is configurable via the `STATIC_DIR` env var (default `/static`), which also lets the app be imported in a test/CI environment that has no `/static` directory.

## Requirements

- Docker + Docker Compose (recommended), or Python 3.11+ locally. **Python 3.10+ is required** — the code uses `str | None` union syntax that older versions (e.g. macOS's built-in Python 3.9) cannot evaluate at runtime.
- API keys (optional; each unlocks a feature, and missing keys are logged as warnings at startup):
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

### Deployment workflow

Edit locally → `git push` → `git pull` on the Pi → `docker compose up -d --build`. Note that test-only changes (`tests/`, `requirements-dev.txt`, `pytest.ini`) and the backup script do **not** require a container rebuild.

### Local (without Docker)

```bash
cd app
pip install -r requirements.txt
export DB_PATH=./media.db
export STATIC_DIR=../static
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## Usage

- Open `https://<host>:8000/ui/scan.html` on a phone, tap **Start Camera**, and scan a barcode. The item is looked up and inserted automatically.
- Open `https://<host>:8000/ui/library.html` to search, edit, delete, retry lookups, or manually trigger OMDb/IGDB enrichment per item.
- Open `https://<host>:8000/ui/utility.html` for reporting, CSV export, and bulk enrichment.

## API Reference

Base URL: `http://<host>:8000`

| Method | Path | Description |
|---|---|---|
| `POST` | `/scan` | Body: `{"barcode": "..."}`. Normalizes barcode, looks up metadata if new, inserts/returns the item. Duplicate scans return `status: "exists"`. |
| `GET` | `/media` | List media, **paginated**. Query: `search`/`q`, `media_type`, `author`, `platform`, `developer`, `limit` (default 100, max 1000), `offset` (default 0). Returns `{items, total, limit, offset}`. |
| `GET` | `/media/{id}` | Fetch a single item. |
| `PATCH` | `/media/{id}` | Partial update (any subset of fields in `MediaUpdate`). |
| `DELETE` | `/media/{id}` | Delete an item. |
| `POST` | `/media/{id}/enrich?force=false` | Fetch/refresh OMDb data for a movie item. |
| `POST` | `/media/{id}/enrich/igdb` | Fetch/fill IGDB data for a game item (fill-missing-only, no overwrite). |
| `POST` | `/media/{id}/relookup` | Re-run metadata lookup for an item's barcode; only overwrites title/author/media_type/source if a real match is found. Returns `status: "updated"` or `"still_unknown"`. |
| `GET` | `/reports/summary` | Media type counts + data-quality counters (dupes, empty titles, unknown type, missing cover). |
| `GET` | `/reports/missing` | Query: `media_type`, `field` (`author`/`platform`/`format`/`cover_url`), `limit`. Lists items missing a field. |

## Database Schema

SQLite table `media` (columns and indexes auto-migrated on startup):

- Core: `id`, `barcode` (unique), `title`, `title_raw`, `media_type`
- Type-specific: `author`, `platform`, `developer`, `format`, `location`, `status`, `release_year`, `cover_url`
- Provenance: `source`, `source_payload`, `notes`, `added_at`, `updated_at`
- OMDb: `omdb_imdb_id`, `omdb_status`, `omdb_last_fetched_at`, `omdb_raw_json`, `omdb_hash`
- IGDB: `igdb_game_id`, `igdb_cover_image_id`, `igdb_last_enriched_at`

Indexes exist on `media_type`, `author`, `platform`, `developer`, and the OMDb columns. `media_type` is normalized case-insensitively; games may appear as `game`, `video_game`, or `video game` depending on source — `_media_type_sql_values()` in `main.py` handles the variants.

## Testing

The test suite uses `pytest` and lives in `tests/`. Because the app requires Python 3.10+, the tests run under Python 3.11 — the same version used in production. They run on the Raspberry Pi, which has native Python 3.11.

The Pi's Python is externally managed (Debian), so install the dev dependencies to your user directory:

```bash
cd /opt/docker/media-library
git pull
python3.11 -m pip install --user --break-system-packages \
    -r app/requirements.txt -r requirements-dev.txt
python3.11 -m pytest
```

This installs only to `~/.local` and does not touch the running app container (which has its own isolated Python). Test-only changes do **not** require a container rebuild.

The suite uses temporary databases and a FastAPI `TestClient`, so it never touches the live `media.db`. Fixtures live in `tests/conftest.py`; `main.py`'s static-mount directory is configurable via the `STATIC_DIR` env var (default `/static`) so the app is importable in a test environment with no `/static` directory.

## Backup & Restore

`scripts/backup.sh` makes an on-demand backup of the things that **cannot** be regenerated from the codebase: the SQLite database, the `.env` file (API keys), and `docker-compose.yml`. Backups are written as timestamped, owner-only-readable `.tar.gz` archives to `/opt/backups`.

The database is captured with SQLite's online `.backup` command rather than a plain file copy — this produces a consistent snapshot even while the app is actively writing (a naive `cp` can miss writes still sitting in the WAL file). The script verifies the snapshot with `PRAGMA integrity_check` and refuses to write a corrupt backup.

### One-time setup

`/opt/backups` must exist and be owned by you (this needs `sudo` once):

```bash
sudo mkdir -p /opt/backups
sudo chown $(id -un):$(id -gn) /opt/backups
chmod 700 /opt/backups
```

### Running a backup

```bash
cd /opt/docker/media-library
./scripts/backup.sh
```

It prints the archive path, size, and contents on success. It warns (but still backs up the database) if `.env` is missing.

### Restoring

Extract an archive to inspect or restore its contents:

```bash
tar -xzf /opt/backups/media-library-backup-YYYY-MM-DD_HHMMSS.tar.gz -C /some/restore/dir
```

To restore the live database: stop the container (`docker compose down`), replace `data/media.db` with the `media.db` from the archive, then start it again (`docker compose up -d`).

**Note:** backups are stored on the same Pi as the live data, which protects against accidental deletion, a bad deploy, or database corruption — but **not** against SD-card failure or loss of the Pi itself. To harden against that, copy the archives off the Pi (USB drive, network share, another machine).

## Notes / Gotchas

- `/scan` treats a barcode already present in the DB as `status: "exists"` and returns the stored item without re-running lookups. Use `POST /media/{id}/relookup` to force a fresh lookup.
- OMDb enrichment only overwrites `release_year`/`cover_url` if they're currently blank; it always refreshes `omdb_status`/`omdb_raw_json`/`omdb_hash`.
- IGDB enrichment is fill-only — it never overwrites existing `developer`, `release_year`, or cover data.
- WAL mode creates `media.db-wal` and `media.db-shm` side files next to `media.db`. These are normal; the backup script handles them correctly by using SQLite's online backup rather than copying the files directly.
- Camera access (`getUserMedia`) requires a secure context — use the bundled self-signed cert, a reverse proxy with valid TLS, or `localhost`.
