from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sqlite3
import os
import requests
import time
from typing import Any

DB_PATH = os.getenv("DB_PATH", "/data/media.db")
BARCODELOOKUP_API_KEY = os.getenv("BARCODELOOKUP_API_KEY")  # set in docker-compose

app = FastAPI()


# -----------------------------
# Models
# -----------------------------
class ScanRequest(BaseModel):
    barcode: str


class MediaUpdate(BaseModel):
    title: str | None = None
    media_type: str | None = None
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
    Best-effort column migration (SQLite).
    ddl example: "ALTER TABLE media ADD COLUMN notes TEXT"
    """
    try:
        cols = [r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            db.execute(ddl)
            db.commit()
    except Exception:
        # Don't crash startup due to migration edge-cases
        pass


@app.on_event("startup")
def init_db() -> None:
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            barcode TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            media_type TEXT,
            notes TEXT,
            added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME
        )
        """
    )
    db.commit()

    # If upgrading from older schema, ensure new columns exist
    _ensure_column(db, "media", "notes", "ALTER TABLE media ADD COLUMN notes TEXT")
    _ensure_column(db, "media", "updated_at", "ALTER TABLE media ADD COLUMN updated_at DATETIME")


# -----------------------------
# Metadata lookup
# -----------------------------
def normalize_barcode(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def lookup_isbn_google(isbn: str) -> str | None:
    try:
        r = requests.get(
            f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}",
            timeout=7,
            headers={"User-Agent": "media-library/1.0"},
        )
        if not r.ok:
            return None
        data = r.json()
        if data.get("items"):
            return data["items"][0].get("volumeInfo", {}).get("title")
    except Exception:
        return None
    return None


def lookup_isbn_openlibrary(isbn: str) -> str | None:
    try:
        r = requests.get(
            f"https://openlibrary.org/isbn/{isbn}.json",
            timeout=7,
            headers={"User-Agent": "media-library/1.0"},
        )
        if r.ok:
            return r.json().get("title")
    except Exception:
        return None
    return None


def lookup_upc_barcodelookup(code: str) -> tuple[str | None, str, dict[str, Any]]:
    """
    Returns: (title, media_type, lookup_meta)
    """
    if not BARCODELOOKUP_API_KEY:
        return None, "unknown", {
            "path": "upc/ean",
            "provider": "barcodelookup",
            "http_status": None,
            "message": "BARCODELOOKUP_API_KEY not set",
        }

    try:
        r = requests.get(
            "https://api.barcodelookup.com/v3/products",
            params={"barcode": code, "key": BARCODELOOKUP_API_KEY},
            timeout=10,
            headers={"User-Agent": "media-library/1.0"},
        )
        meta: dict[str, Any] = {
            "path": "upc/ean",
            "provider": "barcodelookup",
            "http_status": r.status_code,
        }

        if not r.ok:
            meta["message"] = f"HTTP {r.status_code}"
            return None, "unknown", meta

        data = r.json()
        products = data.get("products") or []
        if not products:
            meta["message"] = "No products returned"
            return None, "unknown", meta

        p = products[0]
        title = p.get("product_name") or p.get("title") or p.get("name")

        category = (p.get("category") or "").lower()
        description = (p.get("description") or "").lower()

        # Heuristic classification
        text = f"{category} {description}"
        if "video game" in text or "games" in text or "game" in text:
            media_type = "game"
        elif "movie" in text or "blu-ray" in text or "dvd" in text or "video" in text or "film" in text:
            media_type = "movie"
        else:
            media_type = "unknown"

        # Optional: include a few fields for debugging (not huge)
        meta["product_fields"] = {
            "category": p.get("category"),
            "brand": p.get("brand"),
            "manufacturer": p.get("manufacturer"),
        }

        return title, media_type, meta

    except Exception as e:
        return None, "unknown", {
            "path": "upc/ean",
            "provider": "barcodelookup",
            "http_status": None,
            "message": str(e),
        }


def lookup_metadata(code_digits: str) -> tuple[str, str, dict[str, Any]]:
    """
    Returns: (title, media_type, lookup_meta)
    """
    # Identify barcode type
    is_isbn10 = len(code_digits) == 10
    is_isbn13 = len(code_digits) == 13 and code_digits.startswith(("978", "979"))
    is_upc = len(code_digits) == 12
    is_ean13_non_isbn = len(code_digits) == 13 and not is_isbn13

    # ISBN => books
    if is_isbn10 or is_isbn13:
        t0 = time.time()
        title = lookup_isbn_google(code_digits)
        if title:
            return title, "book", {
                "path": "isbn",
                "provider": "google_books",
                "http_status": 200,
                "t_ms": int((time.time() - t0) * 1000),
            }

        title2 = lookup_isbn_openlibrary(code_digits)
        if title2:
            return title2, "book", {
                "path": "isbn",
                "provider": "openlibrary",
                "http_status": 200,
                "t_ms": int((time.time() - t0) * 1000),
            }

        return "Unknown Book", "book", {
            "path": "isbn",
            "provider": "google_books/openlibrary",
            "http_status": 200,
            "t_ms": int((time.time() - t0) * 1000),
            "message": "No matching items",
        }

    # UPC or EAN-13 non-ISBN => BarcodeLookup
    if is_upc or is_ean13_non_isbn:
        title, media_type, meta = lookup_upc_barcodelookup(code_digits)
        return (title or "Unknown Item"), media_type, meta

    return "Unknown Title", "unknown", {
        "path": "unknown",
        "provider": None,
        "http_status": None,
        "message": "Barcode format not recognized",
    }


# -----------------------------
# API routes
# -----------------------------
@app.post("/scan")
def scan_barcode(req: ScanRequest) -> dict[str, Any]:
    t0 = time.time()
    input_barcode = req.barcode
    normalized = normalize_barcode(input_barcode)

    if not normalized:
        raise HTTPException(status_code=400, detail="Empty/invalid barcode")

    db = get_db()
    cur = db.cursor()

    # Exists?
    existing = db.execute(
        "SELECT id, barcode, title, media_type, notes, added_at, updated_at FROM media WHERE barcode = ?",
        (normalized,),
    ).fetchone()

    if existing:
        return {
            "status": "exists",
            "input_barcode": input_barcode,
            "normalized_barcode": normalized,
            "item": dict(existing),
            "lookup": {
                "path": "skipped",
                "provider": None,
                "http_status": None,
                "t_ms": int((time.time() - t0) * 1000),
            },
            "db": {"inserted": False, "id": existing["id"]},
        }

    title, media_type, lookup_meta = lookup_metadata(normalized)

    cur.execute(
        "INSERT INTO media (barcode, title, media_type) VALUES (?, ?, ?)",
        (normalized, title, media_type),
    )
    db.commit()
    new_id = cur.lastrowid

    row = db.execute(
        "SELECT id, barcode, title, media_type, notes, added_at, updated_at FROM media WHERE id = ?",
        (new_id,),
    ).fetchone()

    return {
        "status": "added",
        "input_barcode": input_barcode,
        "normalized_barcode": normalized,
        "item": dict(row) if row else {"id": new_id, "barcode": normalized, "title": title, "media_type": media_type},
        "lookup": lookup_meta | {"t_ms_total": int((time.time() - t0) * 1000)},
        "db": {"inserted": True, "id": new_id},
    }


@app.get("/media")
def list_media(media_type: str | None = None, search: str | None = None) -> list[dict[str, Any]]:
    db = get_db()
    query = "SELECT id, barcode, title, media_type, notes, added_at, updated_at FROM media WHERE 1=1"
    params: list[Any] = []

    if media_type:
        query += " AND media_type = ?"
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
    row = db.execute(
        "SELECT id, barcode, title, media_type, notes, added_at, updated_at FROM media WHERE id = ?",
        (item_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not Found")
    return dict(row)


@app.put("/media/{item_id}")
def update_media(item_id: int, patch: MediaUpdate) -> dict[str, Any]:
    db = get_db()
    existing = db.execute("SELECT id FROM media WHERE id = ?", (item_id,)).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Not Found")

    fields: list[str] = []
    params: list[Any] = []

    if patch.title is not None:
        fields.append("title = ?")
        params.append(patch.title)

    if patch.media_type is not None:
        fields.append("media_type = ?")
        params.append(patch.media_type)

    if patch.notes is not None:
        fields.append("notes = ?")
        params.append(patch.notes)

    if not fields:
        return {"status": "no_changes", "id": item_id}

    fields.append("updated_at = CURRENT_TIMESTAMP")
    query = f"UPDATE media SET {', '.join(fields)} WHERE id = ?"
    params.append(item_id)

    db.execute(query, params)
    db.commit()

    row = db.execute(
        "SELECT id, barcode, title, media_type, notes, added_at, updated_at FROM media WHERE id = ?",
        (item_id,),
    ).fetchone()
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
# IMPORTANT: mount static LAST and at /ui so it never shadows API routes.
app.mount("/ui", StaticFiles(directory="/static", html=True), name="ui")
