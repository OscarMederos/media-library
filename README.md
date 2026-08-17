# Media Library

A self-hosted barcode scanner and inventory tracker for books, movies, TV series, and games. Scan a barcode with a phone camera, and the app looks up metadata (title/author for books via Google Books/OpenLibrary, title/type via UPCDatabase for UPC/EAN items), stores it in SQLite, and lets you browse/edit/enrich the collection from a mobile-friendly web UI.

## Features

- **Barcode scanning** in-browser via ZXing (`static/scan.html`), using the device camera — no native app required.
- **Manual entry** (`static/add.html`) for items with no barcode or no lookup hit, via `POST /media/manual`.
- **Automatic metadata lookup**
  - ISBN-10/13 → Google Books, falls back to OpenLibrary (title + author).
  - UPC/EAN-12/13 → UPCDatabase (title, inferred media type, cover image).
- **Enrichment** — one endpoint, provider selected explicitly or inferred from the item's `media_type`:
  - Movies and series: OMDb (poster, release year, IMDb ID, full raw JSON stored).
  - Games: IGDB (developer, release year, cover image, IGDB game ID) via Twitch OAuth client-credentials flow.
- **Retry lookup** — items that failed their initial lookup (stored as "Unknown Book"/"Unknown Item") can be re-looked-up on demand via `POST /media/{id}/relookup` and a "Retry lookup" button in the library UI.
- **Personal rating** — 1–5 per item, set from the library edit modal, shown as stars in the list. Explicitly clearable.
- **Library UI** (`static/library.html`) — search/filter by type, author, platform, developer; paginated with a "Load more" button; inline edit modal; per-item OMDb/IGDB enrich buttons.
- **Random pick** (`static/random.html`) — one button, one random item from the collection. Frontend-only: no dedicated endpoint.
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
│   ├── add.html           # manual entry form
│   ├── library.html       # browse/search/edit/delete/enrich UI (paginated)
│   ├── random.html        # random pick page
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

Because `/ui` is a `StaticFiles` mount with `html=True`, **any new `.html` file dropped into `static/` is served automatically** — no route registration needed. It does still require a container rebuild, since `static/` is `COPY`ed into the image at build time.

## Requirements

- Docker + Docker Compose (recommended), or Python 3.11+ locally. **Python 3.10+ is required** — the code uses `str | None` union syntax that older versions (e.g. macOS's built-in Python 3.9) cannot evaluate at runtime.
- API keys (optional; each unlocks a feature, and missing keys are logged as warnings at startup):
  - `UPCDATABASE_API_KEY` — UPC/EAN lookups. Without it, UPC/EAN scans store "Unknown Item".
  - `OMDB_API_KEY` — movie and series enrichment.
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

The app listens on `https://<host>:8000` (the Dockerfile self-signs a TLS cert at build time — a browser camera prompt over `getUserMedia` requires HTTPS or `localhost`). Plain `http://` is not served and will return "Empty reply from server".

3. Data persists to `./data/media.db` on the host (bind-mounted to `/data`).

### Deployment workflow

Edit locally → `git push` → `git pull` on the Pi → `docker compose up -d --build`.

| Change | Rebuild needed? |
|---|---|
| `app/*.py` | Yes |
| `static/*` (HTML/JS) | Yes — `static/` is `COPY`ed at build time |
| `app/requirements.txt` | Yes (and the pip layer is reinstalled, so it's slow) |
| `tests/`, `requirements-dev.txt`, `pytest.ini` | No |
| `scripts/backup.sh` | No |

### Local (without Docker)

```bash
cd app
pip install -r requirements.txt
export DB_PATH=./media.db
export STATIC_DIR=../static
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## Usage

- `https://<host>:8000/ui/scan.html` — tap **Start Camera** and scan a barcode. The item is looked up and inserted automatically.
- `https://<host>:8000/ui/add.html` — add an item by hand.
- `https://<host>:8000/ui/library.html` — search, edit, rate, delete, retry lookups, or trigger OMDb/IGDB enrichment per item.
- `https://<host>:8000/ui/random.html` — pick a random item.
- `https://<host>:8000/ui/utility.html` — reporting, CSV export, and bulk enrichment.

## API Reference

Base URL: `https://<host>:8000` (HTTPS only; use `curl -k` against the self-signed cert).

| Method | Path | Description |
|---|---|---|
| `POST` | `/scan` | Body: `{"barcode": "..."}`. Normalizes barcode, looks up metadata if new, inserts/returns the item. Duplicate scans return `status: "exists"` without re-running lookups. |
| `POST` | `/media/manual` | Body: `{"title", "media_type", ...}`. Creates an item without a barcode lookup. `media_type` must be one of `book`, `movie`, `series`, `game`. Also accepts `author`, `platform`, `format`, `location`, `status`, `release_year`, `cover_url`, `notes`. |
| `GET` | `/media` | List media, **paginated**. Query: `search`/`q`, `media_type`, `author`, `platform`, `developer`, `limit` (default 100, max 1000), `offset` (default 0). Returns `{items, total, limit, offset}`. |
| `GET` | `/media/{id}` | Fetch a single item. |
| `PATCH` | `/media/{id}` | Partial update (any subset of `MediaUpdate`). Omitting a field leaves it unchanged. For nullable fields (`rating`, `omdb_imdb_id`, `igdb_*`), sending explicit `null` **clears** the stored value. |
| `DELETE` | `/media/{id}` | Delete an item. |
| `POST` | `/media/{id}/enrich` | Query: `provider` (`omdb` or `igdb`; inferred from the item's `media_type` if omitted), `force` (default `false`). Returns `{status, reason, item}` where `status` is `ok`, `not_found`, `skipped`, or `error`. A `provider` that contradicts the item's type returns `skipped` with `provider_mismatch`. |
| `POST` | `/media/{id}/relookup` | Re-run metadata lookup for an item's barcode; only overwrites title/author/media_type/source if a real match is found. Returns `status: "updated"` or `"still_unknown"`. |
| `GET` | `/reports/summary` | Media type counts + data-quality counters (dupes, empty titles, unknown type, missing cover). |
| `GET` | `/reports/missing` | Query: `media_type`, `field` (`author`/`platform`/`format`/`cover_url`), `limit`. Lists items missing a field. |

### Random pick without an endpoint

`random.html` deliberately adds no backend surface. Since `GET /media` returns `total` on every page, a uniform pick is two requests:

1. `GET /media?limit=1&offset=0` — read `total`.
2. `GET /media?limit=1&offset=<random 0..total-1>` — fetch that row.

Use a random **offset**, not a random **id**: ids have gaps wherever rows were deleted, so an id-based pick would 404 often and skew toward surviving ids. Offsets are dense in `[0, total-1]`, so every row is equally likely.

## Database Schema

SQLite table `media` (columns and indexes auto-migrated on startup by `_ensure_column()` / `_ensure_index()`):

- Core: `id`, `barcode` (unique, not null), `title` (not null), `title_raw`, `media_type`
- Type-specific: `author` (books), `platform` (games), `developer` (games), `format`, `location`, `status`, `rating`, `release_year`, `cover_url`
- Provenance: `source`, `source_payload`, `notes`, `added_at`, `updated_at`
- OMDb: `omdb_imdb_id`, `omdb_status`, `omdb_last_fetched_at`, `omdb_raw_json`, `omdb_hash`
- IGDB: `igdb_game_id`, `igdb_cover_image_id`, `igdb_last_enriched_at`

Indexes exist on `media_type`, `author`, `platform`, `developer`, `omdb_imdb_id`, and `omdb_status`. The `developer` index is created after `ensure_igdb_columns()`, since the column doesn't exist before that runs.

**`media_type`** is one of `book`, `movie`, `series`, `game`. It is matched case-insensitively and with aliases via `_media_type_sql_values()` — `books`, `movies`, `tv`/`show`/`shows` → `series`, and `video_game`/`video game`/`videogame` variants → `game`, since older rows were stored with whichever spelling the source supplied.

**`series` is a first-class media type**, not a flag. This matters for enrichment: OMDb requires a `type` constraint on every request, and that constraint comes straight from the row's `media_type`. Hardcoding `movie` hid documentaries and box sets that OMDb files as series, and trying both types doubled the request count. A mislabelled row is fixed by changing its `media_type`, not by adding a parallel column.

**`rating`** is an integer 1–5 or `NULL`. The API rejects `0` and `6+` with a 422 before reaching SQL.

**`status`** is a free-text column; the library UI offers `owned`, `backlog`, `in_progress`, `completed`, `loaned`.

**`title_raw`** holds the provider-supplied title when it differs from the curated `title`. It is not redundant — it legitimately diverges on alternate spellings, mojibake, and original-language titles, and `_title_candidates()` uses it as a fallback for OMDb lookups.

## OMDb Lookup Ladder

`omdb_fetch_movie()` escalates rather than making one request, and stops at the first hit:

1. `imdb_id` exact fetch (`?i=`), if the row has one.
2. Exact title (`?t=`) with year, then without, for each title candidate.
3. Noise-stripped and colon-prefix title variants, same ladder.
4. Fuzzy search (`?s=`) → best `imdbID` → exact fetch.

Every attempt is recorded in the returned debug payload. Two cost notes, against OMDb's 1,000 requests/day free tier:

- A bulk fill-missing pass is roughly 40 requests for ~20 rows.
- A `force=true` re-fetch of all movies is 200+ requests per run. Check the budget before running it.
- Leave `release_year` blank when unsure. A wrong year costs an extra request, and OMDb backfills the correct one anyway.

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

This installs only to `~/.local` and does not touch the running app container (which has its own isolated Python). Test-only changes do **not** require a container rebuild, which makes `pytest` the cheapest pre-deploy check available — the Mac's Python 3.9 cannot even import the app.

The suite uses temporary databases and a FastAPI `TestClient`, so it never touches the live `media.db`. Fixtures live in `tests/conftest.py`.

**Known failure:** one test fails and is expected to. The `("Steelbook", "movie")` case in `test_infer_one_recognizes_each_keyword_family` contradicts `test_book_wins_over_movie_within_one_string`. The current behaviour is correct — "steelbook" resolves to `book` because the book keyword family is checked first — so the parametrized case is the thing that's wrong, not the code.

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

It prints the archive path, size, and contents on success. It warns (but still backs up the database) if `.env` is missing. Run it before any schema-changing deploy.

### Restoring

Extract an archive to inspect or restore its contents:

```bash
tar -xzf /opt/backups/media-library-backup-YYYY-MM-DD_HHMMSS.tar.gz -C /some/restore/dir
```

To restore the live database: stop the container (`docker compose down`), replace `data/media.db` with the `media.db` from the archive, then start it again (`docker compose up -d`).

**Note:** backups are stored on the same Pi as the live data, which protects against accidental deletion, a bad deploy, or database corruption — but **not** against SD-card failure or loss of the Pi itself. To harden against that, copy the archives off the Pi (USB drive, network share, another machine).

## Notes / Gotchas

- **`from __future__ import annotations` hides broken Pydantic models until runtime.** Annotations are stored as unevaluated strings, so a field whose type name doesn't resolve produces a class that imports cleanly, starts cleanly, serves `GET` requests fine, and only raises `PydanticUserError` the first time a request body is validated. The symptom is a 500 on one verb (e.g. `PATCH`) with a healthy-looking app. Check `__pydantic_complete__` on the model to confirm.
- **Read the container log first on a 500.** `docker compose logs | grep -A 30 Traceback` truncates before the exception line, because FastAPI's stack is deeper than 30 frames. Use `-A 60 | tail -30`, or `grep -E "main\.py|Error"` to get just the app frames and the exception.
- `/scan` treats a barcode already present in the DB as `status: "exists"` and returns the stored item without re-running lookups. Use `POST /media/{id}/relookup` to force a fresh lookup.
- OMDb enrichment only overwrites `release_year`/`cover_url` if they're currently blank; it always refreshes `omdb_status`/`omdb_raw_json`/`omdb_hash`.
- IGDB enrichment is fill-only — it never overwrites existing `developer`, `release_year`, or cover data.
- `PATCH` uses `model_fields_set` rather than an `is not None` check for genuinely nullable fields, so an explicit `null` clears them. Fields using the `is not None` pattern cannot be cleared through the API.
- WAL mode creates `media.db-wal` and `media.db-shm` side files next to `media.db`. These are normal; the backup script handles them correctly by using SQLite's online backup rather than copying the files directly.
- Camera access (`getUserMedia`) requires a secure context — use the bundled self-signed cert, a reverse proxy with valid TLS, or `localhost`.
- `media/sq.js` is a dead service worker from an earlier version. Nothing serves it; `scan.html`, `add.html`, and `library.html` each unregister any leftover service worker and clear stale caches on load, so returning devices self-heal.
