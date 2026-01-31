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
UPCDATABASE_API_KEY = os.getenv("UPCDATABASE_API_KEY")

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
    _ensure_column(
        db,
        "media",
        "omdb_last_fetched_at",
        "ALTER TABLE media ADD COLUMN omdb_last_fetched_at DATETIME",
    )
    _ensure_column(db, "media", "omdb_raw_json", "ALTER TABLE media ADD COLUMN omdb_raw_json TEXT")
    _ensure_column(db, "media", "omdb_hash", "ALTER TABLE media ADD COLUMN omdb_hash TEXT")

    _ensure_index(
        db,
        "idx_media_type",
        "CREATE INDEX IF NOT EXISTS idx_media_type ON media(media_type)",
    )
    _ensure_index(
        db,
        "idx_media_updated_at",
        "CREATE INDEX IF NOT EXISTS idx_media_updated_at ON media(updated_at)",
    )

    # Ensure IGDB columns for game enrichment
    ensure_igdb_columns(db)


init_db()


# -----------------------------
# Models
# -----------------------------
class ScanRequest(BaseModel):
    barcode: str


class MediaItem(BaseModel):
    barcode: str
    title: str
    media_type: str = "unknown"
    author: str | None = None
    platform: str | None = None
    format: str | None = None
    location: str | None = None
    status: str | None = None
    release_year: int | None = None
    cover_url: str | None = None
    notes: str | None = None


class MediaUpdate(BaseModel):
    title: str | None = None
    media_type: str | None = None
    author: str | None = None
    platform: str | None = None
    format: str | None = None
    location: str | None = None
    status: str | None = None
    release_year: int | None = None
    cover_url: str | None = None
    notes: str | None = None


# -----------------------------
# Normalization / lookup helpers
# -----------------------------
def normalize_barcode(raw: str) -> str:
    return "".join(ch for ch in (raw or "").strip() if ch.isdigit())


def lookup_isbn_google(isbn: str) -> tuple[str | None, str | None, dict[str, Any]]:
    meta: dict[str, Any] = {"path": "isbn", "provider": "google_books"}
    try:
        r = requests.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": f"isbn:{isbn}"},
            timeout=10,
            headers={"User-Agent": "media-library/1.0"},
        )
        meta["http_status"] = r.status_code
        if not r.ok:
            meta["message"] = f"HTTP {r.status_code}"
            return None, None, meta

        data = r.json() or {}
        items = data.get("items") or []
        if not items:
            meta["message"] = "No items"
            return None, None, meta

        vi = (items[0] or {}).get("volumeInfo") or {}
        title = (vi.get("title") or "").strip() or None

        authors = vi.get("authors") or []
        author = None
        if isinstance(authors, list) and authors:
            author = (authors[0] or "").strip() or None

        meta["picked"] = {"title": title, "author": author}
        return title, author, meta
    except Exception as e:
        meta["http_status"] = None
        meta["message"] = str(e)
        return None, None, meta


def lookup_isbn_openlibrary(isbn: str) -> tuple[str | None, str | None, dict[str, Any]]:
    meta: dict[str, Any] = {"path": "isbn", "provider": "openlibrary"}
    try:
        r = requests.get(
            "https://openlibrary.org/api/books",
            params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
            timeout=10,
            headers={"User-Agent": "media-library/1.0"},
        )
        meta["http_status"] = r.status_code
        if not r.ok:
            meta["message"] = f"HTTP {r.status_code}"
            return None, None, meta

        data = r.json() or {}
        entry = data.get(f"ISBN:{isbn}") or {}
        title = (entry.get("title") or "").strip() or None

        author_names = []
        for a in entry.get("authors") or []:
            if isinstance(a, dict):
                author_names.append((a.get("name") or "").strip())
        author = ", ".join([n for n in author_names if n]) or None

        meta["picked"] = {"title": title, "author": author}
        return title, author, meta
    except Exception as e:
        meta["http_status"] = None
        meta["message"] = str(e)
        return None, None, meta


def lookup_upc_upcdatabase(upc: str) -> tuple[str | None, str | None, str | None, dict[str, Any]]:
    """
    UPC Database product lookup.

    Returns: (title, inferred_media_type, cover_url, meta)
    """
    meta: dict[str, Any] = {"path": "upc", "provider": "upcdatabase"}

    if not UPCDATABASE_API_KEY:
        meta["message"] = "UPCDATABASE_API_KEY not set"
        return None, None, None, meta

    try:
        r = requests.get(
            f"https://api.upcdatabase.org/product/{upc}",
            timeout=10,
            headers={
                "Authorization": f"Bearer {UPCDATABASE_API_KEY}",
                "Accept": "application/json",
                "User-Agent": "media-library/1.0",
            },
        )
        meta["http_status"] = r.status_code
        if not r.ok:
            meta["message"] = f"HTTP {r.status_code}"
            return None, None, None, meta

        data = r.json() or {}

        # Typical shape: { "success": true/false, ... }
        success = data.get("success", True)
        if isinstance(success, str):
            success = success.strip().lower() == "true"
        if not success:
            err = data.get("error") or {}
            meta["message"] = (err.get("message") or "lookup_failed").strip()
            return None, None, None, meta

        title = (data.get("title") or "").strip() or None

        # Attempt to infer media_type from category text
        cat = (data.get("category") or "").lower()
        inferred = "unknown"
        if "book" in cat:
            inferred = "book"
        elif "movie" in cat or "dvd" in cat or "blu-ray" in cat or "bluray" in cat:
            inferred = "movie"
        elif "game" in cat or "playstation" in cat or "xbox" in cat or "nintendo" in cat:
            inferred = "game"

        images = data.get("images") or []
        cover_url = images[0] if images and isinstance(images[0], str) else None

        meta["picked"] = {"title": title, "category": data.get("category"), "cover_url": cover_url}
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
        title, inferred, cover_url, meta = lookup_upc_upcdatabase(normalized)
        payload: dict[str, Any] | None = None
        if title or cover_url:
            payload = {"title": title, "cover_url": cover_url}

        return (
            title or "Unknown Item",
            inferred,
            "upcdatabase" if UPCDATABASE_API_KEY else "none",
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
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _omdb_get(imdb_id: str | None = None, title: str | None = None, year: int | None = None) -> dict[str, Any]:
    if not OMDB_API_KEY:
        raise RuntimeError("OMDB_API_KEY not set")

    params: dict[str, Any] = {"apikey": OMDB_API_KEY}
    if imdb_id:
        params["i"] = imdb_id
    elif title:
        params["t"] = title
        if year:
            params["y"] = year
    else:
        raise ValueError("Need imdb_id or title")

    r = requests.get(OMDB_BASE_URL, params=params, timeout=15, headers={"User-Agent": "media-library/1.0"})
    r.raise_for_status()
    data = r.json() or {}
    return data


def _omdb_should_fetch(existing_status: str | None, last_fetched_at: str | None) -> bool:
    # Basic throttling: re-fetch at most once per 24h when status is success.
    if not last_fetched_at:
        return True
    if existing_status != "success":
        return True
    try:
        # SQLite timestamp format: "YYYY-MM-DD HH:MM:SS"
        ts = time.mktime(time.strptime(last_fetched_at, "%Y-%m-%d %H:%M:%S"))
        return (time.time() - ts) > 24 * 3600
    except Exception:
        return True


# -----------------------------
# API endpoints
# -----------------------------
@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.post("/scan")
def scan(req: ScanRequest) -> dict[str, Any]:
    barcode = req.barcode
    title, media_type, source, source_payload, lookup_debug, author = lookup_metadata(barcode)

    db = get_db()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        db.execute(
            """
            INSERT INTO media (barcode, title, title_raw, media_type, author, source, source_payload, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(barcode) DO UPDATE SET
                title=excluded.title,
                title_raw=excluded.title_raw,
                media_type=excluded.media_type,
                author=excluded.author,
                source=excluded.source,
                source_payload=excluded.source_payload,
                updated_at=excluded.updated_at
            """,
            (
                normalize_barcode(barcode),
                title,
                title,
                media_type,
                author,
                source,
                source_payload,
                now,
            ),
        )
        db.commit()
    except sqlite3.Error as e:
        logger.exception("DB error on scan")
        raise HTTPException(status_code=500, detail=str(e)) from e

    return {
        "barcode": normalize_barcode(barcode),
        "title": title,
        "media_type": media_type,
        "source": source,
        "source_payload": json.loads(source_payload) if source_payload else None,
        "lookup_debug": json.loads(lookup_debug) if lookup_debug else None,
        "author": author,
    }


@app.get("/api/library")
def library() -> list[dict[str, Any]]:
    db = get_db()
    rows = db.execute(
        """
        SELECT
            id, barcode, title, title_raw, media_type, author, platform, format, location, status, release_year,
            cover_url, notes, source, source_payload,
            omdb_imdb_id, omdb_status, omdb_last_fetched_at, omdb_hash,
            added_at, updated_at,
            igdb_id, igdb_status, igdb_last_fetched_at, igdb_hash
        FROM media
        ORDER BY added_at DESC, id DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


@app.put("/api/media/{item_id}")
def update_media(item_id: int, upd: MediaUpdate) -> dict[str, Any]:
    db = get_db()
    fields: dict[str, Any] = {k: v for k, v in upd.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    fields["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    sets = ", ".join([f"{k}=?" for k in fields.keys()])
    vals = list(fields.values()) + [item_id]

    try:
        cur = db.execute(f"UPDATE media SET {sets} WHERE id=?", vals)
        db.commit()
    except sqlite3.Error as e:
        logger.exception("DB error on update")
        raise HTTPException(status_code=500, detail=str(e)) from e

    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Not found")

    row = db.execute("SELECT * FROM media WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row else {}


@app.post("/api/enrich/omdb/{item_id}")
def enrich_omdb(item_id: int) -> dict[str, Any]:
    db = get_db()
    row = db.execute("SELECT * FROM media WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    if (row["media_type"] or "").lower() != "movie":
        raise HTTPException(status_code=400, detail="OMDb enrichment only supported for movies")

    title = row["title"]
    year = row["release_year"]

    if not title:
        raise HTTPException(status_code=400, detail="Missing title")

    if not _omdb_should_fetch(row["omdb_status"], row["omdb_last_fetched_at"]):
        return {"status": "skipped", "reason": "recently_fetched"}

    try:
        data = _omdb_get(title=title, year=year)
    except Exception as e:
        logger.exception("OMDb fetch failed")
        raise HTTPException(status_code=502, detail=str(e)) from e

    if (data.get("Response") or "").lower() == "false":
        status = "not_found"
    else:
        status = "success"

    h = _sha256_json(data)
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    try:
        db.execute(
            """
            UPDATE media
            SET omdb_status=?,
                omdb_last_fetched_at=?,
                omdb_raw_json=?,
                omdb_hash=?,
                omdb_imdb_id=?,
                updated_at=?
            WHERE id=?
            """,
            (
                status,
                now,
                json.dumps(data),
                h,
                data.get("imdbID"),
                now,
                item_id,
            ),
        )
        db.commit()
    except sqlite3.Error as e:
        logger.exception("DB error on OMDb enrich update")
        raise HTTPException(status_code=500, detail=str(e)) from e

    return {"status": status, "hash": h}


@app.post("/api/enrich/igdb/{item_id}")
def enrich_igdb(item_id: int) -> dict[str, Any]:
    if not igdb_client:
        raise HTTPException(status_code=400, detail="IGDB not configured")

    db = get_db()
    row = db.execute("SELECT * FROM media WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    if (row["media_type"] or "").lower() != "game":
        raise HTTPException(status_code=400, detail="IGDB enrichment only supported for games")

    try:
        updated = enrich_media_game_from_igdb(db, igdb_client, row)
        db.commit()
        return {"status": updated.get("igdb_status"), "hash": updated.get("igdb_hash")}
    except Exception as e:
        logger.exception("IGDB enrich failed")
        raise HTTPException(status_code=502, detail=str(e)) from e


# -----------------------------
# Static front-end
# -----------------------------
app.mount("/", StaticFiles(directory="static", html=True), name="static")