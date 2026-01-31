from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from igdb_enrich import IgdbClient, IgdbConfig, enrich_media_game_from_igdb, ensure_igdb_columns

DB_PATH = os.getenv("DB_PATH", "/data/media.db")
BARCODELOOKUP_API_KEY = os.getenv("BARCODELOOKUP_API_KEY")

OMDB_API_KEY = os.getenv("OMDB_API_KEY")
OMDB_BASE_URL = os.getenv("OMDB_BASE_URL", "https://www.omdbapi.com/")

IGDB_CLIENT_ID = os.getenv("IGDB_CLIENT_ID", "").strip()
IGDB_CLIENT_SECRET = os.getenv("IGDB_CLIENT_SECRET", "").strip()

logger = logging.getLogger("media-library")
if not logger.handlers:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI()

igdb_client: IgdbClient | None = None
if IGDB_CLIENT_ID and IGDB_CLIENT_SECRET:
    igdb_client = IgdbClient(IgdbConfig(client_id=IGDB_CLIENT_ID, client_secret=IGDB_CLIENT_SECRET))


# -----------------------------
# DB helpers
# -----------------------------
def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def _ensure_column(db: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = [r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        db.execute(ddl)
        db.commit()


def _ensure_index(db: sqlite3.Connection, index_name: str, ddl: str) -> None:
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    if not exists:
        db.execute(ddl)
        db.commit()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = get_db()

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            barcode TEXT UNIQUE NOT NULL,

            title TEXT NOT NULL,
            title_raw TEXT,
            media_type TEXT,

            author TEXT,              -- books
            platform TEXT,            -- games
            format TEXT,
            location TEXT,
            status TEXT,
            release_year INTEGER,
            cover_url TEXT,

            notes TEXT,

            source TEXT,
            source_payload TEXT,

            omdb_imdb_id TEXT,
            omdb_status TEXT,
            omdb_last_fetched_at DATETIME,
            omdb_raw_json TEXT,
            omdb_hash TEXT,

            added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME
        )
        """
    )
    db.commit()

    # Auto-migrate older DBs
    _ensure_column(db, "media", "title_raw", "ALTER TABLE media ADD COLUMN title_raw TEXT")
    _ensure_column(db, "media", "media_type", "ALTER TABLE media ADD COLUMN media_type TEXT")
    _ensure_column(db, "media", "author", "ALTER TABLE media ADD COLUMN author TEXT")
    _ensure_column(db, "media", "platform", "ALTER TABLE media ADD COLUMN platform TEXT")
    _ensure_column(db, "media", "format", "ALTER TABLE media ADD COLUMN format TEXT")
    _ensure_column(db, "media", "location", "ALTER TABLE media ADD COLUMN location TEXT")
    _ensure_column(db, "media", "status", "ALTER TABLE media ADD COLUMN status TEXT")
    _ensure_column(db, "media", "release_year", "ALTER TABLE media ADD COLUMN release_year INTEGER")
    _ensure_column(db, "media", "cover_url", "ALTER TABLE media ADD COLUMN cover_url TEXT")
    _ensure_column(db, "media", "notes", "ALTER TABLE media ADD COLUMN notes TEXT")
    _ensure_column(db, "media", "source", "ALTER TABLE media ADD COLUMN source TEXT")
    _ensure_column(db, "media", "source_payload", "ALTER TABLE media ADD COLUMN source_payload TEXT")
    _ensure_column(db, "media", "updated_at", "ALTER TABLE media ADD COLUMN updated_at DATETIME")

    _ensure_column(db, "media", "omdb_imdb_id", "ALTER TABLE media ADD COLUMN omdb_imdb_id TEXT")
    _ensure_column(db, "media", "omdb_status", "ALTER TABLE media ADD COLUMN omdb_status TEXT")
    _ensure_column(db, "media", "omdb_last_fetched_at", "ALTER TABLE media ADD COLUMN omdb_last_fetched_at DATETIME")
    _ensure_column(db, "media", "omdb_raw_json", "ALTER TABLE media ADD COLUMN omdb_raw_json TEXT")
    _ensure_column(db, "media", "omdb_hash", "ALTER TABLE media ADD COLUMN omdb_hash TEXT")

    _ensure_index(db, "idx_media_omdb_imdb_id", "CREATE INDEX idx_media_omdb_imdb_id ON media(omdb_imdb_id)")
    _ensure_index(db, "idx_media_omdb_status", "CREATE INDEX idx_media_omdb_status ON media(omdb_status)")

    # IGDB auto-migration
    ensure_igdb_columns(db)
    db.commit()

    db.close()


init_db()

MEDIA_SELECT = """
SELECT
  id, barcode,
  title, title_raw, media_type,
  author, platform, format, location, status, release_year, cover_url,
  developer,
  notes,
  source, source_payload,
  omdb_imdb_id, omdb_status, omdb_last_fetched_at, omdb_raw_json, omdb_hash,
  igdb_game_id, igdb_cover_image_id, igdb_last_enriched_at,
  added_at, updated_at
FROM media
"""


# -----------------------------
# Models
# -----------------------------
class ScanRequest(BaseModel):
    barcode: str


class MediaUpdate(BaseModel):
    # All optional; "missing" means do not change
    title: str | None = None
    title_raw: str | None = None
    media_type: str | None = None

    author: str | None = None
    platform: str | None = None
    format: str | None = None
    location: str | None = None
    status: str | None = None
    release_year: int | None = None
    cover_url: str | None = None
    developer: str | None = None

    igdb_game_id: int | None = None
    igdb_cover_image_id: int | None = None
    igdb_last_enriched_at: str | None = None

    notes: str | None = None


# -----------------------------
# Lookup helpers
# -----------------------------
def normalize_barcode(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def _join_authors(auths: Any) -> str | None:
    if not auths:
        return None
    if isinstance(auths, str):
        return auths.strip() or None
    if isinstance(auths, list):
        cleaned = [str(a).strip() for a in auths if str(a).strip()]
        return ", ".join(cleaned) if cleaned else None
    return str(auths).strip() or None


def lookup_isbn_google(isbn: str) -> tuple[str | None, str | None, dict[str, Any]]:
    """
    Returns: (title, author, meta)
    """
    meta: dict[str, Any] = {"path": "isbn", "provider": "google_books"}
    try:
        r = requests.get(
            f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}",
            timeout=7,
            headers={"User-Agent": "media-library/1.0"},
        )
        meta["http_status"] = r.status_code
        if not r.ok:
            meta["message"] = f"HTTP {r.status_code}"
            return None, None, meta
        data = r.json()
        items = data.get("items") or []
        if not items:
            meta["message"] = "No items"
            return None, None, meta
        info = items[0].get("volumeInfo", {}) or {}
        title = info.get("title")
        author = _join_authors(info.get("authors"))
        meta["picked"] = {"title": title, "author": author}
        return title, author, meta
    except Exception as e:
        meta["http_status"] = None
        meta["message"] = str(e)
        return None, None, meta


def lookup_isbn_openlibrary(isbn: str) -> tuple[str | None, str | None, dict[str, Any]]:
    """
    Returns: (title, author, meta)
    """
    meta: dict[str, Any] = {"path": "isbn", "provider": "openlibrary"}
    try:
        r = requests.get(
            f"https://openlibrary.org/isbn/{isbn}.json",
            timeout=7,
            headers={"User-Agent": "media-library/1.0"},
        )
        meta["http_status"] = r.status_code
        if not r.ok:
            meta["message"] = f"HTTP {r.status_code}"
            return None, None, meta

        data = r.json()
        title = data.get("title")

        # openlibrary returns authors as keys -> resolve names
        author_names: list[str] = []
        for a in (data.get("authors") or []):
            key = (a or {}).get("key")
            if not key:
                continue
            try:
                ar = requests.get(
                    f"https://openlibrary.org{key}.json",
                    timeout=7,
                    headers={"User-Agent": "media-library/1.0"},
                )
                if ar.ok:
                    aname = (ar.json() or {}).get("name")
                    if aname:
                        author_names.append(str(aname).strip())
            except Exception:
                continue

        author = ", ".join([n for n in author_names if n]) or None
        meta["picked"] = {"title": title, "author": author}
        return title, author, meta
    except Exception as e:
        meta["http_status"] = None
        meta["message"] = str(e)
        return None, None, meta


def lookup_upc_barcodelookup(upc: str) -> tuple[str | None, str | None, str | None, dict[str, Any]]:
    """
    Returns: (title, inferred_media_type, cover_url, meta)
    """
    meta: dict[str, Any] = {"path": "upc", "provider": "barcodelookup"}
    if not BARCODELOOKUP_API_KEY:
        meta["message"] = "BARCODELOOKUP_API_KEY not set"
        return None, None, None, meta

    try:
        r = requests.get(
            "https://api.barcodelookup.com/v3/products",
            params={"barcode": upc, "formatted": "y", "key": BARCODELOOKUP_API_KEY},
            timeout=10,
            headers={"User-Agent": "media-library/1.0"},
        )
        meta["http_status"] = r.status_code
        if not r.ok:
            meta["message"] = f"HTTP {r.status_code}"
            return None, None, None, meta

        data = r.json() or {}
        products = data.get("products") or []
        if not products:
            meta["message"] = "No products"
            return None, None, None, meta

        p0 = products[0] or {}
        title = (p0.get("title") or "").strip() or None

        # Attempt to infer media_type from category or title
        cat = (p0.get("category") or "").lower()
        inferred = "unknown"
        if "book" in cat:
            inferred = "book"
        elif "movie" in cat or "dvd" in cat or "blu-ray" in cat:
            inferred = "movie"
        elif "game" in cat or "playstation" in cat or "xbox" in cat or "nintendo" in cat:
            inferred = "game"

        images = p0.get("images") or []
        cover_url = images[0] if images and isinstance(images[0], str) else None

        meta["picked"] = {"title": title, "category": p0.get("category"), "cover_url": cover_url}
        return title, inferred, cover_url, meta
    except Exception as e:
        meta["http_status"] = None
        meta["message"] = str(e)
        return None, None, None, meta


def lookup_metadata(barcode: str) -> tuple[str, str, str, str | None, str, str | None]:
    """
    Returns: (title, media_type, source, source_payload_json, lookup_debug_json, author)
    """
    normalized = normalize_barcode(barcode)

    # ISBN-10/13 for books
    if len(normalized) in (10, 13) and normalized.startswith(("978", "979", "0", "1")):
        title, author, meta = lookup_isbn_google(normalized)
        if title:
            return (
                title,
                "book",
                "google_books",
                json.dumps({"title": title, "author": author}),
                json.dumps(meta),
                author,
            )

        title, author, meta2 = lookup_isbn_openlibrary(normalized)
        if title:
            return (
                title,
                "book",
                "openlibrary",
                json.dumps({"title": title, "author": author}),
                json.dumps(meta2),
                author,
            )

        meta3 = {"path": "isbn", "provider": "none", "message": "No ISBN match"}
        return "Unknown Book", "book", "none", None, json.dumps(meta3), None

    # UPC/EAN for general items (movies/games/etc)
    if len(normalized) in (12, 13):
        title, inferred, cover_url, meta = lookup_upc_barcodelookup(normalized)
        payload: dict[str, Any] | None = None
        if title or cover_url:
            payload = {"title": title, "cover_url": cover_url}

        return (
            title or "Unknown Item",
            inferred,
            "barcodelookup" if BARCODELOOKUP_API_KEY else "none",
            json.dumps(payload) if payload else None,
            json.dumps(meta),
            None,
        )

    # Unknown format
    meta = {"path": "unknown", "provider": "none", "message": "Unrecognized barcode length"}
    return "Unknown Item", "unknown", "none", None, json.dumps(meta), None


# -----------------------------
# OMDb helpers
# -----------------------------
def _sha256_json(data: dict[str, Any]) -> str:
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def omdb_fetch_movie(*, imdb_id: str | None, title: str | None, year: int | None) -> dict[str, Any]:
    if not OMDB_API_KEY:
        raise HTTPException(status_code=500, detail="OMDB_API_KEY not set")

    params: dict[str, Any] = {"apikey": OMDB_API_KEY, "type": "movie", "r": "json", "plot": "short"}

    if imdb_id:
        params["i"] = imdb_id
    else:
        if not title or not title.strip():
            raise HTTPException(status_code=400, detail="No title available for OMDb lookup")
        params["t"] = title.strip()
        if year:
            params["y"] = int(year)

    try:
        r = requests.get(
            OMDB_BASE_URL,
            params=params,
            timeout=10,
            headers={"User-Agent": "media-library/1.0"},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OMDb request failed: {e}")

    if not r.ok:
        raise HTTPException(status_code=502, detail=f"OMDb HTTP {r.status_code}")

    data = r.json() or {}
    return data


# -----------------------------
# Endpoints
# -----------------------------
@app.post("/scan")
def scan_barcode(req: ScanRequest) -> dict[str, Any]:
    t0 = time.time()
    input_barcode = req.barcode
    normalized = normalize_barcode(input_barcode)

    if not normalized:
        raise HTTPException(status_code=400, detail="Empty/invalid barcode")

    db = get_db()

    existing = db.execute(MEDIA_SELECT + " WHERE barcode = ?", (normalized,)).fetchone()
    if existing:
        return {
            "status": "exists",
            "input_barcode": input_barcode,
            "normalized_barcode": normalized,
            "item": dict(existing),
            "db": {"inserted": False, "id": existing["id"]},
        }

    title, media_type, source, source_payload, lookup_debug, author = lookup_metadata(normalized)

    # Store raw title separately so you can clean title later without losing original
    title_raw = title

    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO media (
          barcode, title, title_raw, media_type,
          author,
          source, source_payload
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (normalized, title, title_raw, media_type, author, source, source_payload),
    )
    db.commit()
    new_id = cur.lastrowid

    row = db.execute(MEDIA_SELECT + " WHERE id = ?", (new_id,)).fetchone()

    return {
        "status": "inserted",
        "input_barcode": input_barcode,
        "normalized_barcode": normalized,
        "item": dict(row) if row else {"id": new_id},
        "lookup": json.loads(lookup_debug) if lookup_debug else None,
        "timing_ms": int((time.time() - t0) * 1000),
    }


@app.get("/media")
def list_media(
    # Back-compat: library.html uses "search"; older versions used "q"
    q: str | None = Query(None, description="Search title/title_raw/barcode (alias of 'search')"),
    search: str | None = Query(None, description="Search title/title_raw/barcode"),
    media_type: str | None = Query(None, description="Filter by media type (book/movie/game)"),
    author: str | None = Query(None, description="Filter by author (books)"),
    platform: str | None = Query(None, description="Filter by platform (games)"),
    developer: str | None = Query(None, description="Filter by developer (games)"),
    limit: int = Query(5000, ge=1, le=20000),
) -> list[dict[str, Any]]:
    db = get_db()

    where: list[str] = []
    params: list[Any] = []

    # search (prefer 'search' if provided)
    s = (search if (search and search.strip()) else q) or None
    if s and s.strip():
        like = f"%{s.strip()}%"
        where.append("(title LIKE ? OR title_raw LIKE ? OR barcode LIKE ?)")
        params.extend([like, like, like])

    if media_type and media_type.strip():
        where_mt, mt_params = _media_type_sql_values(media_type)
        where.append(where_mt)
        params.extend(mt_params)

    def add_like(field: str, value: str | None) -> None:
        if value and value.strip():
            where.append(f"({field} IS NOT NULL AND {field} LIKE ?)")
            params.append(f"%{value.strip()}%")

    add_like("author", author)
    add_like("platform", platform)
    add_like("developer", developer)

    sql = MEDIA_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += " ORDER BY added_at DESC, id DESC LIMIT ?"
    params.append(limit)

    rows = db.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/media/{item_id}")
def get_media(item_id: int) -> dict[str, Any]:
    db = get_db()
    row = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not Found")
    return dict(row)


@app.patch("/media/{item_id}")
def update_media(item_id: int, patch: MediaUpdate) -> dict[str, Any]:
    db = get_db()
    exists = db.execute("SELECT id FROM media WHERE id = ?", (item_id,)).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="Not Found")

    fields: list[str] = []
    params: list[Any] = []

    def add(field: str, value: Any) -> None:
        fields.append(f"{field} = ?")
        params.append(value)

    if patch.title is not None:
        add("title", patch.title)
    if patch.title_raw is not None:
        add("title_raw", patch.title_raw)
    if patch.media_type is not None:
        add("media_type", patch.media_type)

    if patch.author is not None:
        add("author", patch.author)
    if patch.platform is not None:
        add("platform", patch.platform)
    if patch.format is not None:
        add("format", patch.format)
    if patch.location is not None:
        add("location", patch.location)
    if patch.status is not None:
        add("status", patch.status)
    if patch.release_year is not None:
        add("release_year", patch.release_year)
    if patch.cover_url is not None:
        add("cover_url", patch.cover_url)
    if patch.developer is not None:
        add("developer", patch.developer)

    # IGDB fields: allow explicit null to clear values (distinguish unset vs set-to-null)
    if "igdb_game_id" in patch.model_fields_set:
        add("igdb_game_id", patch.igdb_game_id)
    if "igdb_cover_image_id" in patch.model_fields_set:
        add("igdb_cover_image_id", patch.igdb_cover_image_id)
    if "igdb_last_enriched_at" in patch.model_fields_set:
        add("igdb_last_enriched_at", patch.igdb_last_enriched_at)

    if patch.notes is not None:
        add("notes", patch.notes)

    if not fields:
        row = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
        return {"status": "noop", "item": dict(row) if row else {"id": item_id}}

    fields.append("updated_at = CURRENT_TIMESTAMP")
    params.append(item_id)

    db.execute(f"UPDATE media SET {', '.join(fields)} WHERE id = ?", params)
    db.commit()

    row = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    return {"status": "updated", "item": dict(row) if row else {"id": item_id}}


@app.delete("/media/{item_id}")
def delete_media(item_id: int) -> dict[str, Any]:
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM media WHERE id = ?", (item_id,))
    db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Not Found")
    return {"status": "deleted", "id": item_id}


@app.post("/media/{item_id}/enrich")
def enrich_media_movie(item_id: int, force: bool = Query(False)) -> dict[str, Any]:
    db = get_db()
    row = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not Found")

    item = dict(row)
    media_type = (item.get("media_type") or "").strip().lower()

    if media_type != "movie":
        return {"status": "skipped", "reason": "not_movie", "item": item}

    if not force and item.get("omdb_status") == "ok" and item.get("omdb_raw_json"):
        return {"status": "skipped", "reason": "already_ok", "item": item}

    imdb_id = (item.get("omdb_imdb_id") or "").strip() or None
    title = (item.get("title_raw") or item.get("title") or "").strip() or None
    year = item.get("release_year")
    try:
        year_i = int(year) if year is not None else None
    except Exception:
        year_i = None

    data = omdb_fetch_movie(imdb_id=imdb_id, title=title, year=year_i)

    status = "error"
    if str(data.get("Response", "")).lower() == "true":
        status = "ok"
    else:
        err = (data.get("Error") or "").strip().lower()
        status = "not_found" if "not found" in err else "error"

    omdb_hash = _sha256_json(data)
    new_imdb = (data.get("imdbID") or "").strip() or imdb_id

    fields: list[str] = [
        "omdb_status = ?",
        "omdb_last_fetched_at = CURRENT_TIMESTAMP",
        "omdb_raw_json = ?",
        "omdb_hash = ?",
    ]
    params: list[Any] = [status, json.dumps(data), omdb_hash]

    if new_imdb:
        fields.append("omdb_imdb_id = ?")
        params.append(new_imdb)

    # Fill-blanks-only policy for selected display fields
    if status == "ok":
        if item.get("release_year") is None:
            y = (data.get("Year") or "").strip()
            try:
                y_i = int(y[:4]) if y else None
            except Exception:
                y_i = None
            if y_i:
                fields.append("release_year = ?")
                params.append(y_i)

        if _is_blank(item.get("cover_url")):
            poster = (data.get("Poster") or "").strip()
            if poster and poster.upper() != "N/A":
                fields.append("cover_url = ?")
                params.append(poster)

    fields.append("updated_at = CURRENT_TIMESTAMP")

    query = f"UPDATE media SET {', '.join(fields)} WHERE id = ?"
    params.append(item_id)

    db.execute(query, params)
    db.commit()

    row2 = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    return {"status": status, "item": dict(row2) if row2 else item}


@app.post("/media/{item_id}/enrich/igdb")
def enrich_media_igdb(item_id: int) -> dict[str, Any]:
    if igdb_client is None:
        raise HTTPException(status_code=500, detail="IGDB not configured (missing IGDB_CLIENT_ID/IGDB_CLIENT_SECRET)")

    db = get_db()
    try:
        return enrich_media_game_from_igdb(db=db, igdb=igdb_client, media_id=item_id, logger=logger)
    except Exception as e:
        logger.exception("IGDB enrich failed media_id=%s", item_id)
        raise HTTPException(status_code=500, detail=str(e)) from e


def _media_type_sql_values(media_type: str) -> tuple[str, list[Any]]:
    mt = (media_type or "").strip().lower()
    if mt in {"book", "books"}:
        return "LOWER(TRIM(media_type)) = 'book'", []
    if mt in {"movie", "movies"}:
        return "LOWER(TRIM(media_type)) = 'movie'", []
    if mt in {"game", "games", "video_game", "video game", "video games", "videogame", "videogames"}:
        return "LOWER(TRIM(media_type)) IN ('game', 'video game', 'videogame')", []
    # fallback: exact match provided
    return "LOWER(TRIM(media_type)) = ?", [mt]


@app.get("/reports/summary")
def report_summary() -> dict[str, Any]:
    db = get_db()

    rows = db.execute(
        "SELECT COALESCE(NULLIF(TRIM(media_type), ''), 'unknown') AS mt, COUNT(*) AS n "
        "FROM media GROUP BY COALESCE(NULLIF(TRIM(media_type), ''), 'unknown') "
        "ORDER BY n DESC, mt ASC"
    ).fetchall()
    media_type_counts = {r["mt"]: r["n"] for r in rows}

    dup_rows = db.execute(
        "SELECT barcode, COUNT(*) AS n "
        "FROM media "
        "WHERE barcode IS NOT NULL AND TRIM(barcode) <> '' "
        "GROUP BY barcode "
        "HAVING COUNT(*) > 1"
    ).fetchall()
    duplicate_barcodes = len(dup_rows)

    empty_titles = db.execute(
        "SELECT COUNT(*) AS n FROM media WHERE title IS NULL OR TRIM(title) = ''"
    ).fetchone()["n"]

    unknown_media_type = db.execute(
        "SELECT COUNT(*) AS n "
        "FROM media "
        "WHERE media_type IS NULL OR TRIM(media_type) = '' OR LOWER(TRIM(media_type)) = 'unknown'"
    ).fetchone()["n"]

    missing_cover_url = db.execute(
        "SELECT COUNT(*) AS n FROM media WHERE cover_url IS NULL OR TRIM(cover_url) = ''"
    ).fetchone()["n"]

    return {
        "media_type_counts": media_type_counts,
        "data_quality": {
            "duplicate_barcodes": duplicate_barcodes,
            "empty_titles": empty_titles,
            "unknown_media_type": unknown_media_type,
            "missing_cover_url": missing_cover_url,
        },
    }


@app.get("/reports/missing")
def report_missing(
    media_type: str = Query(..., description="book | movie | game | video_game"),
    field: str = Query(..., description="author | platform | format | cover_url"),
    limit: int = Query(25, ge=1, le=500),
) -> list[dict[str, Any]]:
    allowed_fields = {"author", "platform", "format", "cover_url"}
    if field not in allowed_fields:
        raise HTTPException(status_code=400, detail=f"field must be one of: {sorted(allowed_fields)}")

    where_mt, mt_params = _media_type_sql_values(media_type)

    db = get_db()

    sql = (
        MEDIA_SELECT
        + f" WHERE {where_mt} AND ({field} IS NULL OR TRIM({field}) = '')"
        + " ORDER BY updated_at DESC, added_at DESC, id DESC"
        + " LIMIT ?"
    )
    params: list[Any] = [*mt_params, limit]
    rows = db.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# -----------------------------
# Static UI at /ui
# -----------------------------
app.mount("/ui", StaticFiles(directory="/static", html=True), name="ui")