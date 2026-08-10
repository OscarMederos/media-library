from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from typing import Any

import requests
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from igdb_enrich import (
    IgdbClient,
    IgdbConfig,
    _is_game_media_type,
    enrich_media_game_from_igdb,
    ensure_igdb_columns,
)

DB_PATH = os.getenv("DB_PATH", "/data/media.db")

# Migrated from BarcodeLookup -> UPCDatabase
UPCDATABASE_API_KEY = os.getenv("UPCDATABASE_API_KEY")

OMDB_API_KEY = os.getenv("OMDB_API_KEY")
OMDB_BASE_URL = os.getenv("OMDB_BASE_URL", "https://www.omdbapi.com/")

IGDB_CLIENT_ID = os.getenv("IGDB_CLIENT_ID", "").strip()
IGDB_CLIENT_SECRET = os.getenv("IGDB_CLIENT_SECRET", "").strip()

logger = logging.getLogger("media-library")
if not logger.handlers:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI()


@app.middleware("http")
async def no_cache_ui_assets(request, call_next):
    """
    /ui/* is served via StaticFiles, which sets Last-Modified/ETag but no
    Cache-Control. Without an explicit Cache-Control, browsers are allowed to
    heuristically cache these pages and skip the network entirely on reload,
    so a deploy wouldn't show up until the browser's own cache expired.
    Forcing no-store means every request for /ui/* always hits this server.
    """
    response = await call_next(request)
    if request.url.path.startswith("/ui/"):
        response.headers["Cache-Control"] = "no-store"
    return response

igdb_client: IgdbClient | None = None
if IGDB_CLIENT_ID and IGDB_CLIENT_SECRET:
    igdb_client = IgdbClient(IgdbConfig(client_id=IGDB_CLIENT_ID, client_secret=IGDB_CLIENT_SECRET))

def _warn_missing_api_keys() -> None:
    missing = []
    if not UPCDATABASE_API_KEY:
        missing.append("UPCDATABASE_API_KEY (UPC/EAN barcode lookups will return 'Unknown Item')")
    if not OMDB_API_KEY:
        missing.append("OMDB_API_KEY (movie enrichment via /media/{id}/enrich will fail)")
    if not (IGDB_CLIENT_ID and IGDB_CLIENT_SECRET):
        missing.append("IGDB_CLIENT_ID/IGDB_CLIENT_SECRET (game enrichment via /media/{id}/enrich is disabled)")

    if missing:
        for reason in missing:
            logger.warning("Missing configuration: %s", reason)
    else:
        logger.info("All optional API keys configured (UPCDatabase, OMDb, IGDB)")


_warn_missing_api_keys()

# -----------------------------
# DB helpers
# -----------------------------
def _connect() -> sqlite3.Connection:
    # check_same_thread=False: FastAPI's threadpool for sync dependencies can open
    # and close a generator dependency on different worker threads; each connection
    # here is still only ever used within a single request, never shared concurrently.
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def get_db():
    """FastAPI dependency: yields a connection and always closes it after the request."""
    db = _connect()
    try:
        yield db
    finally:
        db.close()

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
    db = _connect()

    db.execute("PRAGMA journal_mode=WAL")

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
    _ensure_index(db, "idx_media_media_type", "CREATE INDEX idx_media_media_type ON media(media_type)")
    _ensure_index(db, "idx_media_author", "CREATE INDEX idx_media_author ON media(author)")
    _ensure_index(db, "idx_media_platform", "CREATE INDEX idx_media_platform ON media(platform)")

    # IGDB auto-migration
    ensure_igdb_columns(db)
    db.commit()

    # developer column only exists after ensure_igdb_columns() runs, so its index goes here
    _ensure_index(db, "idx_media_developer", "CREATE INDEX idx_media_developer ON media(developer)")

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


class ManualCreate(BaseModel):
    title: str
    media_type: str  # "book" | "movie" | "series" | "game"

    author: str | None = None
    platform: str | None = None
    format: str | None = None
    location: str | None = None
    status: str | None = None
    release_year: int | None = None
    cover_url: str | None = None
    notes: str | None = None


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

    omdb_imdb_id: str | None = None

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


_BOOK_KEYWORDS = (
    "book", "paperback", "hardcover", "hardback", "isbn", "novel",
    "textbook", "audiobook", "graphic novel",
)
_MOVIE_KEYWORDS = (
    "movie", "dvd", "blu-ray", "bluray", "blu ray", "4k uhd", "uhd",
    "digital code", "steelbook", "film",
)
_GAME_KEYWORDS = (
    "game", "playstation", "ps5", "ps4", "ps3", "ps2", "xbox",
    "nintendo", "switch", "wii", "steam", "video game", "videogame",
)


def _infer_one(text: str) -> str:
    t = text.lower()
    if any(k in t for k in _BOOK_KEYWORDS):
        return "book"
    if any(k in t for k in _MOVIE_KEYWORDS):
        return "movie"
    if any(k in t for k in _GAME_KEYWORDS):
        return "game"
    return "unknown"


def _infer_media_type_from_text(*candidates: str | None) -> str:
    """
    Infers media type by checking each candidate text in order (e.g. category,
    then title, then description) and returning the first non-"unknown" match.
    """
    for c in candidates:
        if not c:
            continue
        guess = _infer_one(c)
        if guess != "unknown":
            return guess
    return "unknown"


def lookup_upc_upcdatabase(upc: str) -> tuple[str | None, str | None, str | None, dict[str, Any]]:
    """
    UPCDatabase: https://api.upcdatabase.org/product/{barcode}
    Auth: Authorization: Bearer <token>

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
                "User-Agent": "media-library/1.0",
                "Authorization": f"Bearer {UPCDATABASE_API_KEY}",
            },
        )
        meta["http_status"] = r.status_code

        # Capture rate/limit headers when present
        meta["limits"] = {
            "APILimit-Lookups": r.headers.get("APILimit-Lookups"),
            "APILimit-Search": r.headers.get("APILimit-Search"),
            "APILimit-Currency": r.headers.get("APILimit-Currency"),
            "APILimit-Reset": r.headers.get("APILimit-Reset"),
        }

        if not r.ok:
            meta["message"] = f"HTTP {r.status_code}"
            return None, None, None, meta

        data = r.json() or {}
        meta["raw_success"] = data.get("success")

        # According to docs, "success" can be "true"/"false" (string)
        success_val = str(data.get("success", "")).strip().lower()
        if success_val not in {"true", "1", "yes"}:
            meta["message"] = data.get("message") or "Not found"
            return None, None, None, meta

        title = (data.get("title") or "").strip() or None

        # Try category-like fields if present, else infer from title/description
        category = (data.get("category") or data.get("category_name") or "").strip() or None
        description = (data.get("description") or "").strip() or None
        inferred = _infer_media_type_from_text(category, title, description)

        # Best-effort cover extraction (docs don't guarantee an image field)
        cover_url: str | None = None
        images = data.get("images")
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, str) and first.strip():
                cover_url = first.strip()
        if not cover_url:
            img = data.get("image") or data.get("imageUrl") or data.get("image_url")
            if isinstance(img, str) and img.strip():
                cover_url = img.strip()

        meta["picked"] = {
            "title": title,
            "category": category,
            "cover_url": cover_url,
        }
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
            inferred or "unknown",
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
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


# Retail/packaging noise that appears on scanned or hand-typed titles but never
# in OMDb's canonical title.
_OMDB_TITLE_NOISE = re.compile(
    r"\b("
    r"dvd|blu[\s-]?ray|bluray|4k|uhd|ultra\s?hd|vhs|digital\s?copy|steelbook|"
    r"widescreen|full\s?screen|fullscreen|remastered|complete\s+series|"
    r"(?:special|collector\'?s|deluxe|limited|anniversary|extended|unrated|director\'?s)"
    r"\s+(?:edition|cut)|"
    r"\d+\s*-?\s*disc(?:\s+set)?"
    r")\b",
    re.IGNORECASE,
)

# Placeholder written by lookup_metadata() when no provider matched the barcode.
# Manually corrected rows keep it in title_raw forever, so it must never reach OMDb.
_PLACEHOLDER_TITLES = {"unknown item", "unknown", ""}

# A colon prefix is a last-resort candidate: discs often carry a broadcast
# subtitle OMDb doesn't use ("The Blue Planet: Seas of Life"). Guarded so a
# short prefix can't silently match a different title ("Alien: Covenant").
_PREFIX_STOPWORDS = {"the", "a", "an"}
_MIN_PREFIX_WORDS = 2
_MIN_PREFIX_CHARS = 8

# Ceiling on lookups per enrich: candidates x years, plus one search each.
_MAX_TITLE_CANDIDATES = 3


def _clean_movie_title(raw: str | None) -> str | None:
    if not raw:
        return None
    s = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", str(raw))
    s = _OMDB_TITLE_NOISE.sub(" ", s)
    s = " ".join(s.split()).strip(" -\u2013\u2014:,.")
    return s or None


def _colon_prefix(raw: str | None) -> str | None:
    if not raw or ":" not in str(raw):
        return None
    head = " ".join(str(raw).split(":", 1)[0].split()).strip(" -\u2013\u2014:,.")
    if not head:
        return None
    significant = [w for w in head.split() if w.lower() not in _PREFIX_STOPWORDS]
    if len(significant) < _MIN_PREFIX_WORDS and len(head) < _MIN_PREFIX_CHARS:
        return None
    return head


def _title_candidates(*titles: str | None) -> list[str]:
    """Ordered, de-duplicated candidates, most precise first: as given, then
    noise-stripped, then colon prefixes. Placeholders are dropped."""
    out: list[str] = []
    seen: set[str] = set()

    def push(cand: str | None) -> None:
        if not cand:
            return
        c = " ".join(str(cand).split())
        key = c.lower()
        if not c or key in seen or key in _PLACEHOLDER_TITLES:
            return
        seen.add(key)
        out.append(c)

    for raw in titles:
        push(raw)
    for raw in titles:
        push(_clean_movie_title(raw))
    for raw in titles:
        push(_colon_prefix(raw))
        push(_colon_prefix(_clean_movie_title(raw)))

    return out[:_MAX_TITLE_CANDIDATES]


def _omdb_request(params: dict[str, Any]) -> dict[str, Any]:
    try:
        r = requests.get(
            OMDB_BASE_URL,
            params={**params, "apikey": OMDB_API_KEY, "r": "json"},
            timeout=10,
            headers={"User-Agent": "media-library/1.0"},
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"OMDb request failed: {e}") from e

    if not r.ok:
        raise HTTPException(status_code=502, detail=f"OMDb HTTP {r.status_code}")

    try:
        return r.json() or {}
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"OMDb returned non-JSON: {e}") from e


def _omdb_ok(data: dict[str, Any]) -> bool:
    return str(data.get("Response", "")).lower() == "true"


def _pick_search_result(
    results: list[dict[str, Any]], year: int | None, wanted: str | None = None
) -> dict[str, Any]:
    """Choose among ?s= hits. Position is the weakest signal and is used last:
    a broad search returns dozens of rows and an unrelated one can lead."""
    pool = results

    if wanted:
        w = " ".join(str(wanted).split()).lower()
        exact = [r for r in pool if " ".join(str(r.get("Title") or "").split()).lower() == w]
        if exact:
            pool = exact

    if year:
        for r in pool:
            ry = str(r.get("Year") or "")[:4]
            if ry.isdigit() and int(ry) == int(year):
                return r

    return pool[0]


def omdb_fetch_movie(
    *,
    imdb_id: str | None,
    title: str | None,
    year: int | None,
    title_raw: str | None = None,
    omdb_type: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Resolve a title against OMDb:
      1. imdb_id (exact)
      2. exact title (?t=) with year, then without
      3. noise-stripped / colon-prefix variants, same ladder
      4. fuzzy search (?s=) -> best imdbID -> exact fetch

    omdb_type comes straight from the row's media_type ("movie" or "series")
    and constrains every request. It is never guessed: hardcoding movie hid
    documentaries and box sets OMDb files as series, and trying both doubled
    the request count. A mislabelled row is fixed by changing its media_type.

    Returns (payload, debug) where debug records every attempt.
    """
    if not OMDB_API_KEY:
        raise HTTPException(status_code=500, detail="OMDB_API_KEY not set")

    attempts: list[dict[str, Any]] = []
    last: dict[str, Any] | None = None

    if imdb_id:
        data = _omdb_request({"i": imdb_id, "plot": "short"})
        attempts.append({"strategy": "imdb_id", "value": imdb_id, "ok": _omdb_ok(data)})
        if _omdb_ok(data):
            return data, {"attempts": attempts}
        last = data

    candidates = _title_candidates(title, title_raw)
    if not candidates:
        raise HTTPException(
            status_code=400,
            detail="No usable title available for OMDb lookup (empty or placeholder)",
        )

    years: list[int | None] = [year, None] if year else [None]

    for cand in candidates:
        for y in years:
            params: dict[str, Any] = {"t": cand, "plot": "short"}
            if y:
                params["y"] = int(y)
            if omdb_type:
                params["type"] = omdb_type
            data = _omdb_request(params)
            attempts.append({
                "strategy": "title", "value": cand, "year": y, "type": omdb_type,
                "ok": _omdb_ok(data), "error": data.get("Error"),
            })
            if _omdb_ok(data):
                return data, {"attempts": attempts}
            last = data

    for cand in candidates:
        search_params: dict[str, Any] = {"s": cand}
        if omdb_type:
            search_params["type"] = omdb_type
        search = _omdb_request(search_params)
        results = search.get("Search") or []
        attempts.append({
            "strategy": "search", "value": cand, "type": omdb_type,
            "ok": _omdb_ok(search), "results": len(results), "error": search.get("Error"),
        })
        if not (_omdb_ok(search) and results):
            continue

        best_id = (_pick_search_result(results, year, cand).get("imdbID") or "").strip()
        if not best_id:
            continue

        data = _omdb_request({"i": best_id, "plot": "short"})
        attempts.append({"strategy": "search_imdb_id", "value": best_id, "ok": _omdb_ok(data)})
        if _omdb_ok(data):
            return data, {"attempts": attempts}
        last = data

    return (last or {"Response": "False", "Error": "Movie not found!"}), {"attempts": attempts}


# -----------------------------
# Endpoints
# -----------------------------
@app.post("/scan")
def scan_barcode(req: ScanRequest, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    t0 = time.time()
    input_barcode = req.barcode
    normalized = normalize_barcode(input_barcode)

    if not normalized:
        raise HTTPException(status_code=400, detail="Empty/invalid barcode")

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

    try:
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
    except sqlite3.IntegrityError:
        # Lost the race: another request inserted this barcode first.
        logger.info("Duplicate barcode insert raced, returning existing row: %s", normalized)
        row = db.execute(MEDIA_SELECT + " WHERE barcode = ?", (normalized,)).fetchone()
        return {
            "status": "exists",
            "input_barcode": input_barcode,
            "normalized_barcode": normalized,
            "item": dict(row) if row else None,
            "db": {"inserted": False, "id": row["id"] if row else None},
        }

    row = db.execute(MEDIA_SELECT + " WHERE id = ?", (new_id,)).fetchone()

    return {
        "status": "inserted",
        "input_barcode": input_barcode,
        "normalized_barcode": normalized,
        "item": dict(row) if row else {"id": new_id},
        "lookup": json.loads(lookup_debug) if lookup_debug else None,
        "timing_ms": int((time.time() - t0) * 1000),
    }


_MANUAL_MEDIA_TYPES = {"book", "movie", "series", "game"}


@app.post("/media/manual")
def create_media_manual(req: ManualCreate, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """
    Adds an item with no barcode (out-of-print books, homemade/promo items,
    anything a scan can't identify). Gets a synthetic barcode so the existing
    UNIQUE NOT NULL constraint and duplicate-scan logic don't need to change;
    the "MANUAL-" prefix guarantees it can never collide with a real scanned
    barcode (which is always pure digits) and source="manual" tells /relookup
    to skip it rather than run metadata lookup against a fake barcode.
    """
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")

    media_type = (req.media_type or "").strip().lower()
    if media_type not in _MANUAL_MEDIA_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"media_type must be one of: {', '.join(sorted(_MANUAL_MEDIA_TYPES))}",
        )

    barcode = f"MANUAL-{uuid.uuid4().hex[:12].upper()}"

    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO media (
          barcode, title, title_raw, media_type,
          author, platform, format, location, status, release_year, cover_url, notes,
          source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            barcode, title, title, media_type,
            (req.author or None), (req.platform or None), (req.format or None),
            (req.location or None), (req.status or None), req.release_year,
            (req.cover_url or None), (req.notes or None),
            "manual",
        ),
    )
    db.commit()
    new_id = cur.lastrowid

    row = db.execute(MEDIA_SELECT + " WHERE id = ?", (new_id,)).fetchone()
    return {"status": "inserted", "item": dict(row) if row else {"id": new_id}}


@app.get("/media")
def list_media(
    q: str | None = Query(None, description="Search title/title_raw/barcode (alias of 'search')"),
    search: str | None = Query(None, description="Search title/title_raw/barcode"),
    media_type: str | None = Query(None, description="Filter by media type (book/movie/game)"),
    author: str | None = Query(None, description="Filter by author (books)"),
    platform: str | None = Query(None, description="Filter by platform (games)"),
    developer: str | None = Query(None, description="Filter by developer (games)"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:

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

    where_sql = ""
    if where:
        where_sql = " WHERE " + " AND ".join(where)

    total = db.execute(f"SELECT COUNT(*) FROM media{where_sql}", params).fetchone()[0]

    sql = MEDIA_SELECT + where_sql + " ORDER BY added_at DESC, id DESC LIMIT ? OFFSET ?"
    rows = db.execute(sql, params + [limit, offset]).fetchall()

    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/media/{item_id}")
def get_media(item_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    row = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not Found")
    return dict(row)


@app.patch("/media/{item_id}")
def update_media(
    item_id: int, patch: MediaUpdate, db: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
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

    if "omdb_imdb_id" in patch.model_fields_set:
        add("omdb_imdb_id", (patch.omdb_imdb_id or "").strip() or None)

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
def delete_media(item_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    cur = db.cursor()
    cur.execute("DELETE FROM media WHERE id = ?", (item_id,))
    db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Not Found")
    return {"status": "deleted", "id": item_id}


# -----------------------------
# Unified enrichment
# -----------------------------
# Every enrich path returns the same envelope, always with all three keys
# present (never a conditionally-omitted "reason"):
#   {"status": "ok" | "not_found" | "skipped" | "error", "reason": str | None, "item": {...full row...}}
_PROVIDERS = {"omdb", "igdb"}


# OMDb covers both films and episodic titles; "series" is a media_type in its
# own right so the same value that classifies the shelf also constrains the
# lookup, instead of a second provider-specific enum shadowing it.
_OMDB_MEDIA_TYPES = {"movie", "series"}


def _is_omdb_media_type(media_type: str | None) -> bool:
    return (media_type or "").strip().lower() in _OMDB_MEDIA_TYPES


def _infer_provider(media_type: str | None) -> str | None:
    mt = (media_type or "").strip().lower()
    if _is_omdb_media_type(mt):
        return "omdb"
    if _is_game_media_type(mt):
        return "igdb"
    return None


def _enrich_via_omdb(
    db: sqlite3.Connection, item: dict[str, Any], item_id: int, force: bool
) -> dict[str, Any]:
    if not force and item.get("omdb_status") == "ok" and item.get("omdb_raw_json"):
        return {"status": "skipped", "reason": "already_ok", "item": item}

    imdb_id = (item.get("omdb_imdb_id") or "").strip() or None
    # Prefer the curated title: title_raw is a scan-time snapshot and holds the
    # "Unknown Item" placeholder on every row corrected by hand after a failed scan.
    title = (item.get("title") or "").strip() or None
    title_raw = (item.get("title_raw") or "").strip() or None
    year = item.get("release_year")
    try:
        year_i = int(year) if year is not None else None
    except Exception:
        year_i = None

    data, omdb_debug = omdb_fetch_movie(
        imdb_id=imdb_id,
        title=title,
        year=year_i,
        title_raw=title_raw,
        omdb_type=(item.get("media_type") or "").strip().lower() or None,
    )

    if str(data.get("Response", "")).lower() == "true":
        status = "ok"
        reason = None
    else:
        err = (data.get("Error") or "").strip().lower()
        status = "not_found" if "not found" in err else "error"
        reason = "omdb_not_found" if status == "not_found" else "omdb_error"

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

    # Fill-blanks-only policy for selected display fields (unaffected by force).
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

    if status != "ok":
        logger.info("OMDb unresolved media_id=%s attempts=%s", item_id, omdb_debug["attempts"])

    row2 = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    result: dict[str, Any] = {"status": status, "reason": reason, "item": dict(row2) if row2 else item}
    if status != "ok":
        result["omdb_debug"] = omdb_debug
    return result


def _enrich_via_igdb(
    db: sqlite3.Connection, item: dict[str, Any], item_id: int, force: bool
) -> dict[str, Any]:
    if igdb_client is None:
        raise HTTPException(status_code=500, detail="IGDB not configured (missing IGDB_CLIENT_ID/IGDB_CLIENT_SECRET)")

    try:
        result = enrich_media_game_from_igdb(
            db=db, igdb=igdb_client, media_id=item_id, force=force, logger=logger
        )
    except Exception as e:
        logger.exception("IGDB enrich failed media_id=%s", item_id)
        raise HTTPException(status_code=500, detail=str(e)) from e

    if result.get("updated"):
        status, reason = "ok", None
    else:
        raw_reason = result.get("reason") or "not_updated"
        # "no IGDB match" is the provider equivalent of OMDb's not-found; the
        # rest (missing title, nothing to do) are genuine skips, not misses.
        status = "not_found" if raw_reason == "no IGDB match" else "skipped"
        reason = raw_reason

    row2 = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    return {"status": status, "reason": reason, "item": dict(row2) if row2 else item}


@app.post("/media/{item_id}/enrich")
def enrich_media(
    item_id: int,
    provider: str | None = Query(
        None, description="'omdb' or 'igdb'; inferred from the item's media_type if omitted"
    ),
    force: bool = Query(False, description="Re-fetch even if already enriched; still fill-blanks-only"),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    row = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not Found")

    item = dict(row)
    media_type = (item.get("media_type") or "").strip().lower()

    prov = (provider or "").strip().lower() or None
    if prov is not None and prov not in _PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider '{provider}' (expected 'omdb' or 'igdb')")

    inferred = _infer_provider(media_type)
    if prov is None:
        prov = inferred
    elif inferred is not None and prov != inferred:
        return {
            "status": "skipped",
            "reason": f"provider_mismatch: item media_type is '{media_type or 'unknown'}'",
            "item": item,
        }

    if prov is None:
        return {"status": "skipped", "reason": "no_provider", "item": item}
    if prov == "omdb":
        return _enrich_via_omdb(db, item, item_id, force)
    return _enrich_via_igdb(db, item, item_id, force)

_UNKNOWN_TITLES = {"unknown book", "unknown item"}


@app.post("/media/{item_id}/relookup")
def relookup_media(item_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """
    Re-runs metadata lookup for an item's barcode (e.g. after an initial scan
    failed to find a match, or a provider's data has since improved). Only
    overwrites title/author/media_type/source if the new lookup actually finds
    something — a repeated miss leaves the existing row untouched.
    """
    row = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not Found")

    item = dict(row)

    if (item.get("source") or "") == "manual":
        raise HTTPException(status_code=400, detail="Item was added manually and has no barcode to look up")

    barcode = (item.get("barcode") or "").strip()
    if not barcode:
        raise HTTPException(status_code=400, detail="Item has no barcode to look up")

    title, media_type, source, source_payload, lookup_debug, author = lookup_metadata(barcode)

    if (title or "").strip().lower() in _UNKNOWN_TITLES:
        return {
            "status": "still_unknown",
            "item": item,
            "lookup": json.loads(lookup_debug) if lookup_debug else None,
        }

    db.execute(
        """
        UPDATE media
        SET title = ?, title_raw = ?, media_type = ?, author = ?,
            source = ?, source_payload = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (title, title, media_type, author, source, source_payload, item_id),
    )
    db.commit()

    row2 = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
    return {
        "status": "updated",
        "item": dict(row2) if row2 else item,
        "lookup": json.loads(lookup_debug) if lookup_debug else None,
    }

def _media_type_sql_values(media_type: str) -> tuple[str, list[Any]]:
    mt = (media_type or "").strip().lower()
    if mt in {"book", "books"}:
        return "LOWER(TRIM(media_type)) = 'book'", []
    if mt in {"movie", "movies"}:
        return "LOWER(TRIM(media_type)) = 'movie'", []
    if mt in {"series", "tv", "show", "shows"}:
        return "LOWER(TRIM(media_type)) = 'series'", []
    if mt in {"game", "games", "video_game", "video game", "video games", "videogame", "videogames"}:
        return "LOWER(TRIM(media_type)) IN ('game', 'video game', 'videogame')", []
    # fallback: exact match provided
    return "LOWER(TRIM(media_type)) = ?", [mt]


@app.get("/reports/summary")
def report_summary(db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:

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
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict[str, Any]]:
    allowed_fields = {"author", "platform", "format", "cover_url"}
    if field not in allowed_fields:
        raise HTTPException(status_code=400, detail=f"field must be one of: {sorted(allowed_fields)}")

    where_mt, mt_params = _media_type_sql_values(media_type)

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
# Directory is configurable so tests/CI (which have no /static) can point this
# elsewhere; production keeps the /static default set by the Dockerfile.
STATIC_DIR = os.getenv("STATIC_DIR", "/static")
app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")