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

IGDB_CLIENT_ID = os.getenv("IGDB_CLIENT_ID")
IGDB_CLIENT_SECRET = os.getenv("IGDB_CLIENT_SECRET")

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

class NormalizeGameRequest(BaseModel):
    dry_run: bool = True
    igdb_id: int | None = None  # required when dry_run=false

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

    _ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_media_title ON media(title)")
    _ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_media_type ON media(media_type)")
    _ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_media_platform ON media(platform)")
    _ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_media_format ON media(format)")

# -----------------------------
# Lookup helpers (existing)
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
# Normalization helpers (movies/books stay)
# -----------------------------
PLATFORM_MARKERS = [
    (re.compile(r"\bPS5\b", re.I), "PS5"),
    (re.compile(r"\bPS4\b", re.I), "PS4"),
    (re.compile(r"\bPS3\b", re.I), "PS3"),
    (re.compile(r"\bPS2\b", re.I), "PS2"),
    (re.compile(r"\bPS1\b|\bPlayStation\b", re.I), "PS1"),
    (re.compile(r"\bNintendo Switch\b|\bSwitch\b", re.I), "Switch"),
    (re.compile(r"\bWii U\b", re.I), "Wii U"),
    (re.compile(r"\bWii\b", re.I), "Wii"),
    (re.compile(r"\bGameCube\b", re.I), "GameCube"),
    (re.compile(r"\bXbox Series X\b|\bXbox Series\b|\bXSX\b", re.I), "Xbox Series X"),
    (re.compile(r"\bXbox One\b", re.I), "Xbox One"),
    (re.compile(r"\bXbox 360\b", re.I), "Xbox 360"),
    (re.compile(r"\bPC\b|\bWindows\b", re.I), "PC"),
]

FORMAT_MARKERS = [
    (re.compile(r"\b4K\b|\bUHD\b|\bUltra HD\b", re.I), "4K"),
    (re.compile(r"\bBlu[- ]?ray\b", re.I), "Blu-ray"),
    (re.compile(r"\bDVD\b", re.I), "DVD"),
    (re.compile(r"\bVHS\b", re.I), "VHS"),
]

def _strip_suffix_wrapper(title: str) -> tuple[str, str | None]:
    t = title.strip()
    m = re.search(r"[\(\[]\s*([^\)\]]+?)\s*[\)\]]\s*$", t)
    if not m:
        return t, None
    inner = m.group(1).strip()
    cleaned = re.sub(r"[\(\[]\s*([^\)\]]+?)\s*[\)\]]\s*$", "", t).rstrip(" -–—:").strip()
    return cleaned, inner

def _strip_prefix_wrapper(title: str) -> tuple[str, str | None]:
    t = title.strip()
    m = re.match(r"^\s*[\(\[]\s*([^\)\]]+?)\s*[\)\]]\s*(.+)$", t)
    if not m:
        return t, None
    return m.group(2).strip(), m.group(1).strip()

def _strip_dash_suffix(title: str) -> tuple[str, str | None]:
    t = title.strip()
    m = re.search(r"\s*[-–—:]\s*(.+)\s*$", t)
    if not m:
        return t, None
    suffix = m.group(1).strip()
    cleaned = re.sub(r"\s*[-–—:]\s*(.+)\s*$", "", t).strip()
    return cleaned, suffix

def _detect_from_marker(text: str, patterns: list[tuple[re.Pattern, str]]) -> str | None:
    if not text:
        return None
    for rx, val in patterns:
        if rx.search(text):
            return val
    return None

def normalize_title_extract_markers_only(title: str) -> tuple[str, str | None, str | None]:
    """
    IMPORTANT: This does NOT infer platform/format from random words inside the title.
    It only extracts when platform/format appear as explicit markers:
      - suffix: "Title (PS5)" or "Title [PS5]"
      - prefix: "[PS5] Title"
      - dash suffix: "Title - PS5"
    """
    if not title:
        return title, None, None

    original = title.strip()
    plat = None
    fmt = None

    t1, inner1 = _strip_suffix_wrapper(original)
    if inner1:
        plat = _detect_from_marker(inner1, PLATFORM_MARKERS)
        fmt = _detect_from_marker(inner1, FORMAT_MARKERS)
        if plat or fmt:
            return t1, plat, fmt

    t2, inner2 = _strip_prefix_wrapper(original)
    if inner2:
        plat = _detect_from_marker(inner2, PLATFORM_MARKERS)
        fmt = _detect_from_marker(inner2, FORMAT_MARKERS)
        if plat or fmt:
            return t2, plat, fmt

    t3, suffix = _strip_dash_suffix(original)
    if suffix:
        plat = _detect_from_marker(suffix, PLATFORM_MARKERS)
        fmt = _detect_from_marker(suffix, FORMAT_MARKERS)
        if plat or fmt:
            return t3, plat, fmt

    return original, None, None

# -----------------------------
# IGDB client (games normalization)
# -----------------------------
_igdb_token: str | None = None
_igdb_token_exp: float = 0.0  # epoch seconds

def _igdb_get_app_token() -> str:
    """
    Gets/refreshes Twitch app access token for IGDB.
    Uses client credential flow (client_id + client_secret).
    """
    global _igdb_token, _igdb_token_exp

    if not IGDB_CLIENT_ID or not IGDB_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="IGDB_CLIENT_ID/IGDB_CLIENT_SECRET not set")

    now = time.time()
    if _igdb_token and now < (_igdb_token_exp - 60):
        return _igdb_token

    # Twitch token endpoint for app access token (client credentials). :contentReference[oaicite:2]{index=2}
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": IGDB_CLIENT_ID,
            "client_secret": IGDB_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=10,
        headers={"User-Agent": "media-library/1.0"},
    )
    if not r.ok:
        raise HTTPException(status_code=500, detail=f"Failed to get Twitch token: HTTP {r.status_code}")

    data = r.json()
    token = data.get("access_token")
    expires_in = int(data.get("expires_in") or 0)
    if not token or expires_in <= 0:
        raise HTTPException(status_code=500, detail="Invalid Twitch token response")

    _igdb_token = token
    _igdb_token_exp = now + expires_in
    return token

def _igdb_post(endpoint: str, body: str) -> list[dict[str, Any]]:
    token = _igdb_get_app_token()
    # IGDB v4 endpoints are POST with body DSL: `search "..."; fields ...; limit ...;` :contentReference[oaicite:3]{index=3}
    r = requests.post(
        f"https://api.igdb.com/v4/{endpoint}",
        data=body.encode("utf-8"),
        timeout=10,
        headers={
            "Client-ID": IGDB_CLIENT_ID,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "media-library/1.0",
        },
    )
    if not r.ok:
        raise HTTPException(status_code=500, detail=f"IGDB request failed: HTTP {r.status_code}")
    return r.json() if r.text else []

def _igdb_candidates_for_title(title: str, limit: int = 8) -> list[dict[str, Any]]:
    # Escape quotes for IGDB search string
    q = (title or "").replace('"', '\\"').strip()
    if not q:
        return []

    # We fetch platforms.name to show you options; but we won't auto-set owned platform.
    body = f'''
search "{q}";
fields id,name,first_release_date,cover.image_id,platforms.name;
limit {limit};
'''
    games = _igdb_post("games", body)

    out: list[dict[str, Any]] = []
    for g in games:
        image_id = ((g.get("cover") or {}).get("image_id"))
        cover_url = f"https://images.igdb.com/igdb/image/upload/t_cover_big/{image_id}.jpg" if image_id else None
        frd = g.get("first_release_date")
        year = None
        if isinstance(frd, int) and frd > 0:
            year = time.gmtime(frd).tm_year
        plats = []
        for p in (g.get("platforms") or []):
            n = p.get("name")
            if n:
                plats.append(n)
        out.append({
            "igdb_id": g.get("id"),
            "name": g.get("name"),
            "release_year": year,
            "cover_url": cover_url,
            "platforms": plats[:12],
        })
    return out

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

    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO media (barcode, title, title_raw, media_type, source, source_payload)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (normalized, title, title, media_type, source, source_payload),
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

    if patch.title is not None: add("title", patch.title)
    if patch.title_raw is not None: add("title_raw", patch.title_raw)
    if patch.media_type is not None: add("media_type", patch.media_type)

    if patch.platform is not None: add("platform", patch.platform)
    if patch.format is not None: add("format", patch.format)
    if patch.location is not None: add("location", patch.location)
    if patch.status is not None: add("status", patch.status)
    if patch.release_year is not None: add("release_year", patch.release_year)
    if patch.cover_url is not None: add("cover_url", patch.cover_url)

    if patch.notes is not None: add("notes", patch.notes)

    if not fields:
        return {"status": "no_changes", "id": item_id}

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

# -----------------------------
# Normalization endpoints
# -----------------------------
@app.post("/normalize/{item_id}")
def normalize_item_non_game(item_id: int, req: NormalizeRequest):
    """
    For movies/books/unknown: extract ONLY explicit platform/format markers
    (does not infer platform from arbitrary words in title).
    """
    db = get_db()
    row = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not Found")

    current = dict(row)
    if (current.get("media_type") or "unknown") == "game":
        return {"status": "wrong_endpoint", "detail": "Use /normalize_game/{id} for games."}

    proposed: dict[str, Any] = {}
    new_title, plat, fmt = normalize_title_extract_markers_only(current.get("title") or "")

    if new_title and new_title != (current.get("title") or ""):
        proposed["title"] = new_title
    if plat and not (current.get("platform") or "").strip():
        proposed["platform"] = plat
    if fmt and not (current.get("format") or "").strip():
        proposed["format"] = fmt

    if not proposed:
        return {"status": "no_changes", "current": current, "proposed": {}}

    if req.dry_run:
        return {"status": "dry_run", "current": current, "proposed": proposed}

    fields, params = [], []
    for k, v in proposed.items():
        fields.append(f"{k} = ?")
        params.append(v)
    fields.append("updated_at = CURRENT_TIMESTAMP")
    params.append(item_id)
    db.execute(f"UPDATE media SET {', '.join(fields)} WHERE id = ?", params)
    db.commit()

    updated = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    return {"status": "applied", "proposed": proposed, "item": dict(updated) if updated else {"id": item_id}}

@app.post("/normalize_game/{item_id}")
def normalize_game(item_id: int, req: NormalizeGameRequest):
    """
    Games-only normalization via IGDB.
    - dry_run: return IGDB candidates + proposed changes for best guess (first candidate)
    - apply: requires igdb_id. Applies canonical title + cover_url + release_year + source metadata.
    Also extracts explicit platform/format markers from your existing title and moves them out.
    """
    db = get_db()
    row = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not Found")
    current = dict(row)

    if (current.get("media_type") or "unknown") != "game":
        return {"status": "wrong_type", "detail": "This endpoint is for games only."}

    # Step 1: remove explicit markers from title (platform/format) but DO NOT infer
    stripped_title, marker_platform, marker_format = normalize_title_extract_markers_only(current.get("title") or "")
    search_title = stripped_title or (current.get("title") or "")

    # Step 2: IGDB candidates based on stripped title
    candidates = _igdb_candidates_for_title(search_title, limit=8)

    if req.dry_run:
        proposed: dict[str, Any] = {}
        if stripped_title and stripped_title != (current.get("title") or ""):
            proposed["title"] = stripped_title
        if marker_platform and not (current.get("platform") or "").strip():
            proposed["platform"] = marker_platform
        if marker_format and not (current.get("format") or "").strip():
            proposed["format"] = marker_format

        # Best-guess: first candidate (user will pick in UI)
        if candidates:
            best = candidates[0]
            # We do NOT auto-set platform from IGDB platforms list
            if best.get("name") and best["name"] != (proposed.get("title") or current.get("title")):
                proposed["title"] = best["name"]
            if best.get("release_year") and not current.get("release_year"):
                proposed["release_year"] = best["release_year"]
            if best.get("cover_url") and not current.get("cover_url"):
                proposed["cover_url"] = best["cover_url"]

        return {
            "status": "dry_run",
            "current": current,
            "stripped_title": stripped_title,
            "candidates": candidates,
            "proposed_best_guess": proposed,
        }

    # Apply requires chosen igdb_id
    if not req.igdb_id:
        raise HTTPException(status_code=400, detail="igdb_id is required when dry_run=false")

    chosen = next((c for c in candidates if c.get("igdb_id") == req.igdb_id), None)
    # If candidate list doesn't contain it (rare, if title changed), fetch by id
    if not chosen:
        body = f"fields id,name,first_release_date,cover.image_id,platforms.name; where id = {int(req.igdb_id)}; limit 1;"
        got = _igdb_post("games", body)
        if not got:
            raise HTTPException(status_code=404, detail="IGDB game not found")
        g = got[0]
        image_id = ((g.get("cover") or {}).get("image_id"))
        cover_url = f"https://images.igdb.com/igdb/image/upload/t_cover_big/{image_id}.jpg" if image_id else None
        frd = g.get("first_release_date")
        year = time.gmtime(frd).tm_year if isinstance(frd, int) and frd > 0 else None
        plats = [p.get("name") for p in (g.get("platforms") or []) if p.get("name")]
        chosen = {"igdb_id": g.get("id"), "name": g.get("name"), "release_year": year, "cover_url": cover_url, "platforms": plats}

    proposed: dict[str, Any] = {}

    # Apply marker extraction first (safe)
    if stripped_title and stripped_title != (current.get("title") or ""):
        proposed["title"] = stripped_title
    if marker_platform and not (current.get("platform") or "").strip():
        proposed["platform"] = marker_platform
    if marker_format and not (current.get("format") or "").strip():
        proposed["format"] = marker_format

    # Apply IGDB canonical fields
    if chosen.get("name"):
        proposed["title"] = chosen["name"]
    if chosen.get("release_year"):
        proposed["release_year"] = chosen["release_year"]
    if chosen.get("cover_url"):
        proposed["cover_url"] = chosen["cover_url"]

    # Source metadata
    proposed["source"] = "igdb"
    proposed["source_payload"] = json.dumps({
        "igdb_id": chosen.get("igdb_id"),
        "platforms": chosen.get("platforms", [])[:20],
    })

    # Write
    fields, params = [], []
    for k, v in proposed.items():
        fields.append(f"{k} = ?")
        params.append(v)
    fields.append("updated_at = CURRENT_TIMESTAMP")
    params.append(item_id)

    db.execute(f"UPDATE media SET {', '.join(fields)} WHERE id = ?", params)
    db.commit()

    updated = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    return {"status": "applied", "chosen": chosen, "proposed": proposed, "item": dict(updated) if updated else {"id": item_id}}

# -----------------------------
# Static UI at /ui
# -----------------------------
app.mount("/ui", StaticFiles(directory="/static", html=True), name="ui")
