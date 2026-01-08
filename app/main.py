from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB_PATH = os.getenv("DB_PATH", "/data/media.db")
BARCODELOOKUP_API_KEY = os.getenv("BARCODELOOKUP_API_KEY")

app = FastAPI()


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

    db.close()


init_db()

MEDIA_SELECT = """
SELECT
  id, barcode,
  title, title_raw, media_type,
  author, platform, format, location, status, release_year, cover_url,
  notes,
  source, source_payload,
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

        author = _join_authors(author_names)
        meta["picked"] = {"title": title, "author": author}
        return title, author, meta
    except Exception as e:
        meta["http_status"] = None
        meta["message"] = str(e)
        return None, None, meta


def lookup_upc_barcodelookup(code: str) -> tuple[str | None, str, dict[str, Any], dict[str, Any] | None]:
    """
    Returns: (title, inferred_media_type, meta, payload_snippet)
    """
    meta: dict[str, Any] = {"path": "upc/ean", "provider": "barcodelookup"}
    if not BARCODELOOKUP_API_KEY:
        meta["http_status"] = None
        meta["message"] = "BARCODELOOKUP_API_KEY not set"
        return None, "unknown", meta, None

    try:
        r = requests.get(
            "https://api.barcodelookup.com/v3/products",
            params={"barcode": code, "formatted": "y", "key": BARCODELOOKUP_API_KEY},
            timeout=10,
            headers={"User-Agent": "media-library/1.0"},
        )
        meta["http_status"] = r.status_code
        if not r.ok:
            meta["message"] = f"HTTP {r.status_code}"
            return None, "unknown", meta, None

        data = r.json() or {}
        products = data.get("products") or []
        if not products:
            meta["message"] = "No products"
            return None, "unknown", meta, None

        p0 = products[0] or {}
        title = p0.get("product_name") or p0.get("title") or p0.get("description")
        category = (p0.get("category") or "").lower()
        meta["picked"] = {"title": title, "category": p0.get("category")}

        inferred = "unknown"
        if "book" in category:
            inferred = "book"
        elif "video game" in category or "game" in category:
            inferred = "game"
        elif "dvd" in category or "blu-ray" in category or "movie" in category:
            inferred = "movie"

        payload_snippet = {
            "product_name": p0.get("product_name"),
            "title": p0.get("title"),
            "category": p0.get("category"),
            "brand": p0.get("brand"),
            "manufacturer": p0.get("manufacturer"),
            "images": p0.get("images"),
        }
        return title, inferred, meta, payload_snippet
    except Exception as e:
        meta["http_status"] = None
        meta["message"] = str(e)
        return None, "unknown", meta, None


def lookup_metadata(code_digits: str) -> tuple[str, str, str, str | None, str, str | None]:
    """
    Returns:
      title, media_type, source, source_payload_json, lookup_debug_json, author
    """
    is_isbn10 = len(code_digits) == 10
    is_isbn13 = len(code_digits) == 13 and code_digits.startswith(("978", "979"))
    is_upc = len(code_digits) == 12
    is_ean13_non_isbn = len(code_digits) == 13 and not is_isbn13

    # Books (ISBN)
    if is_isbn10 or is_isbn13:
        t0 = time.time()

        title, author, meta1 = lookup_isbn_google(code_digits)
        if title:
            meta1["t_ms"] = int((time.time() - t0) * 1000)
            return title, "book", "google_books", None, json.dumps(meta1), author

        title2, author2, meta2 = lookup_isbn_openlibrary(code_digits)
        meta2["t_ms"] = int((time.time() - t0) * 1000)
        if title2:
            return title2, "book", "openlibrary", None, json.dumps(meta2), author2

        # Fall back to unknown title but mark type as book (still an ISBN)
        meta2["message"] = meta2.get("message") or "No title found"
        return "Unknown Book", "book", "none", None, json.dumps(meta2), None

    # UPC/EAN for movies/games/unknown
    if is_upc or is_ean13_non_isbn:
        title, inferred, meta, payload = lookup_upc_barcodelookup(code_digits)
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
        "status": "added",
        "input_barcode": input_barcode,
        "normalized_barcode": normalized,
        "item": dict(row) if row else {"id": new_id, "barcode": normalized, "title": title, "media_type": media_type, "author": author},
        "lookup": json.loads(lookup_debug) | {"t_ms_total": int((time.time() - t0) * 1000)},
        "db": {"inserted": True, "id": new_id},
    }


@app.get("/media")
def list_media(
    media_type: str | None = None,
    search: str | None = None,
    author: str | None = None,
    platform: str | None = None,
) -> list[dict[str, Any]]:
    db = get_db()
    query = MEDIA_SELECT + " WHERE 1=1"
    params: list[Any] = []

    if media_type:
        query += " AND COALESCE(media_type,'unknown') = ?"
        params.append(media_type)

    if search:
        query += " AND title LIKE ?"
        params.append(f"%{search}%")

    if author:
        query += " AND author LIKE ?"
        params.append(f"%{author}%")

    if platform:
        query += " AND platform LIKE ?"
        params.append(f"%{platform}%")

    query += " ORDER BY added_at DESC, id DESC"
    rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/media/{item_id}")
def get_media(item_id: int) -> dict[str, Any]:
    db = get_db()
    row = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not Found")
    return dict(row)


@app.put("/media/{item_id}")
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
    if patch.notes is not None:
        add("notes", patch.notes)

    if not fields:
        return {"status": "no_changes", "id": item_id}

    fields.append("updated_at = CURRENT_TIMESTAMP")
    query = f"UPDATE media SET {', '.join(fields)} WHERE id = ?"
    params.append(item_id)

    db.execute(query, params)
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



# -----------------------------
# Reporting endpoints
# -----------------------------
def _media_type_sql_values(media_type: str) -> tuple[str, list[str]]:
    mt = (media_type or '').strip().lower()
    if mt in ('game', 'games', 'video_game'):
        return "(COALESCE(media_type,'unknown') IN (?, ?))", ['game', 'video_game']
    return "(COALESCE(media_type,'unknown') = ?)", [mt]


@app.get('/reports/summary')
def report_summary() -> dict[str, Any]:
    db = get_db()
    # media type counts
    mt_rows = db.execute(
        "SELECT COALESCE(media_type,'unknown') AS k, COUNT(*) AS n "
        "FROM media "
        "GROUP BY COALESCE(media_type,'unknown') "
        "ORDER BY n DESC"
    ).fetchall()
    media_type_counts = [{'media_type': r['k'], 'count': r['n']} for r in mt_rows]

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
    ).fetchone()['n']

    unknown_media_type = db.execute(
        "SELECT COUNT(*) AS n "
        "FROM media "
        "WHERE media_type IS NULL OR TRIM(media_type) = '' OR LOWER(TRIM(media_type)) = 'unknown'"
    ).fetchone()['n']

    missing_cover_url = db.execute(
        "SELECT COUNT(*) AS n FROM media WHERE cover_url IS NULL OR TRIM(cover_url) = ''"
    ).fetchone()['n']

    return {
        'media_type_counts': media_type_counts,
        'data_quality': {
            'duplicate_barcodes': duplicate_barcodes,
            'empty_titles': empty_titles,
            'unknown_media_type': unknown_media_type,
            'missing_cover_url': missing_cover_url,
        },
    }


@app.get('/reports/missing')
def report_missing(
    media_type: str = Query(..., description='book | movie | game | video_game'),
    field: str = Query(..., description='author | platform | format | cover_url'),
    limit: int = Query(25, ge=1, le=500),
) -> list[dict[str, Any]]:
    allowed_fields = {'author', 'platform', 'format', 'cover_url'}
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