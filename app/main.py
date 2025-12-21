from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sqlite3
import os
import requests
import time
import json
import re
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


class NormalizeRequest(BaseModel):
    dry_run: bool = True


# -----------------------------
# DB helpers
# -----------------------------
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(db: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
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

    # Auto-migrate older DBs
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

    # Indexes
    _ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_media_title ON media(title)")
    _ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_media_type ON media(media_type)")
    _ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_media_platform ON media(platform)")
    _ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_media_format ON media(format)")


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
    Returns: title, media_type, source, source_payload_json, lookup_debug_json
    """
    is_isbn10 = len(code_digits) == 10
    is_isbn13 = len(code_digits) == 13 and code_digits.startswith(("978", "979"))
    is_upc = len(code_digits) == 12
    is_ean13_non_isbn = len(code_digits) == 13 and not is_isbn13

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

    if is_upc or is_ean13_non_isbn:
        title, inferred_type, meta, payload = lookup_upc_barcodelookup(code_digits)
        source_payload = json.dumps(payload) if payload else None
        return (title or "Unknown Item"), inferred_type, "barcodelookup", source_payload, json.dumps(meta)

    return "Unknown Title", "unknown", "none", None, json.dumps(
        {"path": "unknown", "provider": None, "message": "Barcode format not recognized"}
    )


# -----------------------------
# Normalization helpers
# -----------------------------
PLATFORM_PATTERNS = [
    (re.compile(r"\bPS5\b", re.I), "PS5"),
    (re.compile(r"\bPS4\b", re.I), "PS4"),
    (re.compile(r"\bPS3\b", re.I), "PS3"),
    (re.compile(r"\bPS2\b", re.I), "PS2"),
    (re.compile(r"\bPS1\b|\bPlayStation\b", re.I), "PS1"),
    (re.compile(r"\bSwitch\b|\bNintendo Switch\b", re.I), "Switch"),
    (re.compile(r"\bWii U\b", re.I), "Wii U"),
    (re.compile(r"\bWii\b", re.I), "Wii"),
    (re.compile(r"\bGameCube\b", re.I), "GameCube"),
    (re.compile(r"\bN64\b|\bNintendo 64\b", re.I), "N64"),
    (re.compile(r"\bSNES\b|\bSuper Nintendo\b", re.I), "SNES"),
    (re.compile(r"\bNES\b", re.I), "NES"),
    (re.compile(r"\bXbox Series X\b|\bXbox Series\b|\bXSX\b", re.I), "Xbox Series X"),
    (re.compile(r"\bXbox One\b", re.I), "Xbox One"),
    (re.compile(r"\bXbox 360\b", re.I), "Xbox 360"),
    (re.compile(r"\bXbox\b", re.I), "Xbox"),
    (re.compile(r"\bPC\b|\bWindows\b", re.I), "PC"),
]

FORMAT_PATTERNS = [
    (re.compile(r"\b4K\b|\bUHD\b|\bUltra HD\b", re.I), "4K"),
    (re.compile(r"\bBlu[- ]?ray\b", re.I), "Blu-ray"),
    (re.compile(r"\bDVD\b", re.I), "DVD"),
    (re.compile(r"\bVHS\b", re.I), "VHS"),
    (re.compile(r"\bDigital\b", re.I), "Digital"),
    (re.compile(r"\bCartridge\b|\bCart\b", re.I), "Cartridge"),
    (re.compile(r"\bDisc\b", re.I), "Disc"),
]

# Used to remove platform/format tokens from title
PLATFORM_TOKEN_RXS = [rx for (rx, _plat) in PLATFORM_PATTERNS]
FORMAT_TOKEN_RXS = [rx for (rx, _fmt) in FORMAT_PATTERNS]


def _strip_suffix_wrapper(title: str) -> tuple[str, str | None]:
    """
    If title ends in "(...)" or "[...]" return (cleaned_title, inner_text)
    else return (title, None)
    """
    t = title.strip()
    m = re.search(r"[\(\[]\s*([^\)\]]+?)\s*[\)\]]\s*$", t)
    if not m:
        return t, None
    inner = m.group(1).strip()
    cleaned = re.sub(r"[\(\[]\s*([^\)\]]+?)\s*[\)\]]\s*$", "", t).rstrip(" -–—:").strip()
    return cleaned, inner


def _strip_prefix_wrapper(title: str) -> tuple[str, str | None]:
    """
    If title begins with "[...]" or "(...)" return (rest, inner_text)
    else return (title, None)
    """
    t = title.strip()
    m = re.match(r"^\s*[\(\[]\s*([^\)\]]+?)\s*[\)\]]\s*(.+)$", t)
    if not m:
        return t, None
    inner = m.group(1).strip()
    rest = m.group(2).strip()
    return rest, inner


def _strip_dash_suffix(title: str) -> tuple[str, str | None]:
    """
    If title ends with "- something" return (cleaned, suffix)
    """
    t = title.strip()
    m = re.search(r"\s*[-–—:]\s*(.+)\s*$", t)
    if not m:
        return t, None
    suffix = m.group(1).strip()
    cleaned = re.sub(r"\s*[-–—:]\s*(.+)\s*$", "", t).strip()
    return cleaned, suffix


def _detect_platform(text: str) -> str | None:
    if not text:
        return None
    for rx, plat in PLATFORM_PATTERNS:
        if rx.search(text):
            return plat
    return None


def _detect_format(text: str) -> str | None:
    if not text:
        return None
    for rx, fmt in FORMAT_PATTERNS:
        if rx.search(text):
            return fmt
    return None


def _remove_known_tokens(title: str, platform: str | None, fmt: str | None) -> str:
    """
    If we inferred platform/format, remove obvious tokens from the title.
    We keep it conservative: remove bracketed/dash suffix markers and obvious occurrences.
    """
    t = title

    # Common cleanup: collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()

    # Remove stray double separators
    t = t.strip(" -–—:")

    # If we inferred platform, try removing platform tokens if they appear in obvious contexts
    if platform:
        # Remove occurrences like " (PS5)" or " - PS5" already handled by wrapper/dash stripping,
        # but also remove leftover platform tokens anywhere that are standalone words.
        for rx in PLATFORM_TOKEN_RXS:
            t = rx.sub("", t)

    if fmt:
        for rx in FORMAT_TOKEN_RXS:
            t = rx.sub("", t)

    # Normalize after token removal
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([:;,\-–—])\s+", r" \1 ", t).strip()
    t = t.strip(" -–—:").strip()

    return t


def normalize_title_move_platform_format(title: str) -> tuple[str, str | None, str | None]:
    """
    Returns (new_title, platform_or_None, format_or_None)

    Priority:
      1) parse suffix wrapper (Title (PS5)) / prefix wrapper ([PS5] Title)
      2) parse dash suffix (Title - PS5)
      3) infer platform/format from title text (conservative)
    """
    if not title:
        return title, None, None

    original = title.strip()

    # Start with wrapper/dash parsing
    t1, inner1 = _strip_suffix_wrapper(original)
    plat = _detect_platform(inner1 or "")
    fmt = _detect_format(inner1 or "")

    # Prefix wrapper if suffix wasn't present
    t2 = t1
    if inner1 is None:
        t2, inner2 = _strip_prefix_wrapper(original)
        plat = plat or _detect_platform(inner2 or "")
        fmt = fmt or _detect_format(inner2 or "")

    # Dash suffix if still no wrapper extraction
    t3 = t2
    if inner1 is None:
        t3, suffix = _strip_dash_suffix(t2)
        plat = plat or _detect_platform(suffix or "")
        fmt = fmt or _detect_format(suffix or "")

    # If still not found, infer conservatively from the title (do not over-strip)
    plat = plat or _detect_platform(original)
    fmt = fmt or _detect_format(original)

    new_title = _remove_known_tokens(t3, plat, fmt)

    # final whitespace normalize
    new_title = re.sub(r"\s+", " ", new_title).strip()

    return new_title or original, plat, fmt


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

    existing = db.execute(MEDIA_SELECT + " WHERE barcode = ?", (normalized,)).fetchone()
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


# ---- Normalization endpoints ----
@app.post("/normalize/{item_id}")
def normalize_item(item_id: int, req: NormalizeRequest):
    db = get_db()
    row = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not Found")

    current = dict(row)

    # Propose changes
    proposed: dict[str, Any] = {}

    new_title, plat, fmt = normalize_title_move_platform_format(current.get("title") or "")

    # Only set platform/format if empty currently (safe default)
    if plat and not current.get("platform"):
        proposed["platform"] = plat
    if fmt and not current.get("format"):
        proposed["format"] = fmt

    # If title changed (and is not empty), propose it
    if new_title and new_title != (current.get("title") or ""):
        proposed["title"] = new_title

    if not proposed:
        return {"status": "no_changes", "current": current, "proposed": {}}

    if req.dry_run:
        return {"status": "dry_run", "current": current, "proposed": proposed}

    # Apply updates
    fields = []
    params = []
    for k, v in proposed.items():
        fields.append(f"{k} = ?")
        params.append(v)
    fields.append("updated_at = CURRENT_TIMESTAMP")
    params.append(item_id)

    db.execute(f"UPDATE media SET {', '.join(fields)} WHERE id = ?", params)
    db.commit()

    updated = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    return {"status": "applied", "proposed": proposed, "item": dict(updated) if updated else {"id": item_id}}


@app.post("/normalize")
def normalize_bulk(req: NormalizeRequest, limit: int = 50):
    """
    Normalize up to `limit` items that look like they need normalization:
      - title contains (...) or [...] or " - PS5" patterns
      - platform/format missing
    """
    db = get_db()

    rows = db.execute(
        MEDIA_SELECT + """
        WHERE
          (platform IS NULL OR platform = '' OR format IS NULL OR format = '')
          AND (
            title LIKE '%(%)%' OR title LIKE '%[%]%' OR title LIKE '% - %' OR title LIKE '% – %' OR title LIKE '% — %'
          )
        ORDER BY id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    results = []
    for r in rows:
        item = dict(r)
        new_title, plat, fmt = normalize_title_move_platform_format(item.get("title") or "")
        proposed = {}
        if plat and not item.get("platform"):
            proposed["platform"] = plat
        if fmt and not item.get("format"):
            proposed["format"] = fmt
        if new_title and new_title != (item.get("title") or ""):
            proposed["title"] = new_title

        if not proposed:
            continue

        if req.dry_run:
            results.append({"id": item["id"], "status": "dry_run", "proposed": proposed})
            continue

        fields = []
        params = []
        for k, v in proposed.items():
            fields.append(f"{k} = ?")
            params.append(v)
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(item["id"])
        db.execute(f"UPDATE media SET {', '.join(fields)} WHERE id = ?", params)
        results.append({"id": item["id"], "status": "applied", "proposed": proposed})

    if not req.dry_run:
        db.commit()

    return {"count": len(results), "results": results}


# -----------------------------
# Static UI at /ui
# -----------------------------
app.mount("/ui", StaticFiles(directory="/static", html=True), name="ui")
