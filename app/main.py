from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sqlite3
import os
import requests
import time
import json
from typing import Any

DB_PATH = os.getenv("DB_PATH", "/data/media.db")
BARCODELOOKUP_API_KEY = os.getenv("BARCODELOOKUP_API_KEY")

app = FastAPI()


# -----------------------------
# Models
# -----------------------------
class ScanRequest(BaseModel):
    barcode: str


class MediaUpdate(BaseModel):
    title: str | None = None
    title_raw: str | None = None
    media_type: str | None = None

    platform: str | None = None
    format: str | None = None
    location: str | None = None
    status: str | None = None
    release_year: int | None = None
    cover_url: str | None = None

    notes: str | None = None


# -----------------------------
# DB helpers
# -----------------------------
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(db: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """
    Best-effort idempotent column add for SQLite.
    """
    cols = [r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        db.execute(ddl)
        db.commit()


def _ensure_index(db: sqlite3.Connection, ddl: str) -> None:
    db.execute(ddl)
    db.commit()


@app.on_event("startup")
def init_db() -> None:
    db = get_db()

    # Base table (includes upgraded columns so fresh installs get everything)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            barcode TEXT UNIQUE NOT NULL,

            title TEXT NOT NULL,
            title_raw TEXT,
            media_type TEXT,

            platform TEXT,
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

    # Auto-migrate older DBs (safe for your existing 400 items)
    _ensure_column(db, "media", "title_raw", "ALTER TABLE media ADD COLUMN title_raw TEXT")
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

    # Indexes for speed
    _ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_media_title ON media(title)")
    _ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_media_type ON media(media_type)")
    _ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_media_platform ON media(platform)")


# -----------------------------
# Lookup helpers
# -----------------------------
def normalize_barcode(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def lookup_isbn_google(isbn: str) -> tuple[str | None, dict[str, Any]]:
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
            return None, meta
        data = r.json()
        items = data.get("items") or []
        if not items:
            meta["message"] = "No items"
            return None, meta
        info = items[0].get("volumeInfo", {})
        title = info.get("title")
        meta["picked"] = {"title": title}
        return title, meta
    except Exception as e:
        meta["http_status"] = None
        meta["message"] = str(e)
        return None, meta


def lookup_isbn_openlibrary(isbn: str) -> tuple[str | None, dict[str, Any]]:
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
            return None, meta
        data = r.json()
        title = data.get("title")
        meta["picked"] = {"title": title}
        return title, meta
    except Exception as e:
        meta["http_status"] = None
        meta["message"] = str(e)
        return None, meta


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
            params={"barcode": code, "key": BARCODELOOKUP_API_KEY},
            timeout=10,
            headers={"User-Agent": "media-library/1.0"},
        )
        meta["http_status"] = r.status_code
        if not r.ok:
            meta["message"] = f"HTTP {r.status_code}"
            return None, "unknown", meta, None

        data = r.json()
        products = data.get("products") or []
        if not products:
            meta["message"] = "No products"
            return None, "unknown", meta, {"products_count": 0}

        p = products[0]
        title = p.get("product_name") or p.get("title") or p.get("name")

        category = (p.get("category") or "").lower()
        description = (p.get("description") or "").lower()
        text = f"{category} {description}"

        if "video game" in text or "games" in text or "game" in text:
            inferred_type = "game"
        elif "movie" in text or "blu-ray" in text or "dvd" in text or "film" in text or "video" in text:
            inferred_type = "movie"
        else:
            inferred_type = "unknown"

        # small payload snippet for debugging/enrichment later (keep it small!)
        payload_snippet = {
            "product_name": title,
            "category": p.get("category"),
            "brand": p.get("brand"),
            "manufacturer": p.get("manufacturer"),
            "images": (p.get("images") or [])[:2],
        }

        meta["picked"] = {"title": title, "inferred_media_type": inferred_type}
        return title, inferred_type, meta, payload_snippet

    except Exception as e:
        meta["http_status"] = None
        meta["message"] = str(e)
        return None, "unknown", meta, None


def lookup_metadata(code_digits: str) -> tuple[str, str, str, str | None, str | None]:
    """
    Returns:
      title, media_type, source, source_payload_json, lookup_debug_json
    """
    is_isbn10 = len(code_digits) == 10
    is_isbn13 = len(code_digits) == 13 and code_digits.startswith(("978", "979"))
    is_upc = len(code_digits) == 12
    is_ean13_non_isbn = len(code_digits) == 13 and not is_isbn13

    # Books
    if is_isbn10 or is_isbn13:
        t0 = time.time()
        title, meta1 = lookup_isbn_google(code_digits)
        if title:
            meta1["t_ms"] = int((time.time() - t0) * 1000)
            return title, "book", "google_books", None, json.dumps(meta1)

        title2, meta2 = lookup_isbn_openlibrary(code_digits)
        meta2["t_ms"] = int((time.time() - t0) * 1000)
        if title2:
            return title2, "book", "openlibrary", None, json.dumps(meta2)

        meta2["message"] = "No matching items"
        return "Unknown Book", "book", "google_books/openlibrary", None, json.dumps(meta2)

    # UPC / EAN
    if is_upc or is_ean13_non_isbn:
        title, inferred_type, meta, payload = lookup_upc_barcodelookup(code_digits)
        source_payload = json.dumps(payload) if payload else None
        return (title or "Unknown Item"), inferred_type, "barcodelookup", source_payload, json.dumps(meta)

    return "Unknown Title", "unknown", "none", None, json.dumps(
        {"path": "unknown", "provider": None, "message": "Barcode format not recognized"}
    )


# -----------------------------
# API
# -----------------------------
MEDIA_SELECT = """
SELECT
  id, barcode,
  title, title_raw, media_type,
  platform, format, location, status, release_year, cover_url,
  notes,
  source, source_payload,
  added_at, updated_at
FROM media
"""


@app.post("/scan")
def scan_barcode(req: ScanRequest) -> dict[str, Any]:
    t0 = time.time()
    input_barcode = req.barcode
    normalized = normalize_barcode(input_barcode)

    if not normalized:
        raise HTTPException(status_code=400, detail="Empty/invalid barcode")

    db = get_db()

    existing = db.execute(
        MEDIA_SELECT + " WHERE barcode = ?",
        (normalized,),
    ).fetchone()

    if existing:
        return {
            "status": "exists",
            "input_barcode": input_barcode,
            "normalized_barcode": normalized,
            "item": dict(existing),
            "lookup": {"path": "skipped", "t_ms_total": int((time.time() - t0) * 1000)},
            "db": {"inserted": False, "id": existing["id"]},
        }

    title, media_type, source, source_payload, lookup_debug = lookup_metadata(normalized)

    # Store raw title separately so you can clean title later without losing original
    title_raw = title

    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO media (
          barcode, title, title_raw, media_type,
          source, source_payload
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (normalized, title, title_raw, media_type, source, source_payload),
    )
    db.commit()
    new_id = cur.lastrowid

    row = db.execute(MEDIA_SELECT + " WHERE id = ?", (new_id,)).fetchone()

    return {
        "status": "added",
        "input_barcode": input_barcode,
        "normalized_barcode": normalized,
        "item": dict(row) if row else {"id": new_id, "barcode": normalized, "title": title, "media_type": media_type},
        "lookup": json.loads(lookup_debug) | {"t_ms_total": int((time.time() - t0) * 1000)},
        "db": {"inserted": True, "id": new_id},
    }


@app.get("/media")
def list_media(media_type: str | None = None, search: str | None = None) -> list[dict[str, Any]]:
    db = get_db()
    query = MEDIA_SELECT + " WHERE 1=1"
    params: list[Any] = []

    if media_type:
        query += " AND COALESCE(media_type,'unknown') = ?"
        params.append(media_type)

    if search:
        query += " AND title LIKE ?"
        params.append(f"%{search}%")

    query += " ORDER BY COALESCE(updated_at, added_at) DESC, id DESC"
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
# Static UI at /ui
# -----------------------------
app.mount("/ui", StaticFiles(directory="/static", html=True), name="ui")