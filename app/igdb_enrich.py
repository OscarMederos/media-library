# igdb_enrich.py
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

log = logging.getLogger(__name__)

IGDB_BASE_URL = "https://api.igdb.com/v4"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IGDB_IMAGE_BASE = "https://images.igdb.com/igdb/image/upload"


# -------------------------
# Public config / client
# -------------------------

@dataclass(frozen=True)
class IgdbConfig:
    client_id: str
    client_secret: str
    timeout_s: float = 10.0
    image_size: str = "cover_big"  # IGDB image "size" preset, e.g. cover_big
    image_ext: str = "png"


class IgdbError(RuntimeError):
    pass


class IgdbClient:
    """
    Minimal IGDB v4 client:
      - Fetches Twitch app access token (client_credentials) and caches it in-memory
      - Executes APICalypse POST queries to IGDB endpoints
    """

    def __init__(self, cfg: IgdbConfig, session: Optional[requests.Session] = None) -> None:
        self._cfg = cfg
        self._session = session or requests.Session()
        self._access_token: Optional[str] = None
        self._expires_at_epoch: float = 0.0  # epoch seconds

    def search_games(self, title: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Returns a list of candidate games matching the title (best match is usually index 0).
        """
        title = title.strip()
        if not title:
            return []

        # Keep fields narrow to your enrichment needs.
        apicalypse = f"""
            search "{_escape_search(title)}";
            fields
                id,
                name,
                first_release_date,
                cover.image_id,
                involved_companies.developer,
                involved_companies.company.name;
            limit {int(limit)};
        """.strip()

        return self._query("/games", apicalypse)

    def build_image_url(self, image_id: str) -> str:
        # Example: https://images.igdb.com/igdb/image/upload/t_cover_big/<image_id>.png
        size = f"t_{self._cfg.image_size}"
        ext = self._cfg.image_ext.lstrip(".")
        return f"{IGDB_IMAGE_BASE}/{size}/{image_id}.{ext}"

    # -------------------------
    # Internal HTTP
    # -------------------------

    def _query(self, endpoint: str, apicalypse: str) -> List[Dict[str, Any]]:
        url = f"{IGDB_BASE_URL}/{endpoint.lstrip('/')}"
        headers = self._headers()

        try:
            resp = self._session.post(
                url,
                headers=headers,
                data=apicalypse.encode("utf-8"),
                timeout=self._cfg.timeout_s,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.HTTPError as e:
            body = getattr(e.response, "text", "")
            raise IgdbError(f"IGDB HTTP error: {e} body={body[:500]}") from e
        except requests.RequestException as e:
            raise IgdbError(f"IGDB request failed: {e}") from e
        except ValueError as e:
            raise IgdbError(f"IGDB JSON parse failed: {e}") from e

        if not isinstance(data, list):
            raise IgdbError(f"Unexpected IGDB response shape (expected list): {type(data)}")
        return data

    def _headers(self) -> Dict[str, str]:
        token = self._get_access_token()
        return {
            "Client-ID": self._cfg.client_id,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < (self._expires_at_epoch - 30):
            return self._access_token

        params = {
            "client_id": self._cfg.client_id,
            "client_secret": self._cfg.client_secret,
            "grant_type": "client_credentials",
        }

        try:
            resp = self._session.post(
                TWITCH_TOKEN_URL,
                params=params,
                timeout=self._cfg.timeout_s,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.HTTPError as e:
            body = getattr(e.response, "text", "")
            raise IgdbError(f"Twitch token HTTP error: {e} body={body[:500]}") from e
        except requests.RequestException as e:
            raise IgdbError(f"Twitch token request failed: {e}") from e
        except ValueError as e:
            raise IgdbError(f"Twitch token JSON parse failed: {e}") from e

        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not token:
            raise IgdbError(f"Unexpected token payload: missing access_token: {payload}")
        if not isinstance(expires_in, int) or expires_in <= 0:
            raise IgdbError(f"Unexpected token payload: invalid expires_in: {payload}")

        self._access_token = token
        self._expires_at_epoch = now + float(expires_in)
        return token


# -------------------------
# DB schema helpers (SQLite)
# -------------------------

def ensure_igdb_columns(db) -> None:
    """
    Ensures the following columns exist on media table:

      - developer TEXT
      - igdb_game_id INTEGER
      - igdb_cover_image_id TEXT
      - igdb_last_enriched_at TEXT

    Additionally:
      - If your table has 'cover_url' already, IGDB enrichment will fill that.
      - If not, this helper will also add 'igdb_cover_url' TEXT so you can still persist the URL.
    """
    required: List[Tuple[str, str]] = [
        ("developer", "TEXT"),
        ("igdb_game_id", "INTEGER"),
        ("igdb_cover_image_id", "TEXT"),
        ("igdb_last_enriched_at", "TEXT"),
    ]

    existing = _sqlite_columns(db, "media")
    for col, typ in required:
        if col not in existing:
            log.info("Adding column media.%s %s", col, typ)
            db.execute(f"ALTER TABLE media ADD COLUMN {col} {typ}")

    # If your schema lacks cover_url, add igdb_cover_url as a fallback storage location.
    existing = _sqlite_columns(db, "media")
    if "cover_url" not in existing and "igdb_cover_url" not in existing:
        log.info("Adding column media.igdb_cover_url TEXT (no cover_url column detected)")
        db.execute("ALTER TABLE media ADD COLUMN igdb_cover_url TEXT")


def _sqlite_columns(db, table: str) -> set[str]:
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    cols: set[str] = set()

    for r in rows:
        # Works with sqlite3.Row or tuples
        try:
            name = r["name"]  # type: ignore[index]
        except Exception:
            try:
                name = r[1]
            except Exception:
                name = None

        if isinstance(name, str):
            cols.add(name)

    return cols


# -------------------------
# Enrichment logic
# -------------------------

def enrich_media_game_from_igdb(
    *,
    db,
    igdb: IgdbClient,
    media_id: int,
    force: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """
    Your chosen behavior:
      - Fill only if missing (no overwrite) — this policy applies regardless
        of `force`.
      - force=True only bypasses the "nothing to do" short-circuit below, so
        a re-search/re-fetch happens even when igdb_game_id is already set
        and no fields look missing. It does NOT cause existing values to be
        overwritten — mirrors omdb_fetch_movie's force semantics in main.py.
      - developer is "all developers joined with ', '"
      - release_year filled from first_release_date (year)
      - cover url updated (cover_url if exists, else igdb_cover_url)
      - also stores igdb_game_id, igdb_cover_image_id, igdb_last_enriched_at

    Returns a small status dict for your API response / logs.
    """
    lg = logger or log

    cols = _sqlite_columns(db, "media")
    cover_col = "cover_url" if "cover_url" in cols else "igdb_cover_url"  # ensured by ensure_igdb_columns

    row = db.execute(
        f"""
        SELECT
            id,
            title,
            media_type,
            release_year,
            developer,
            igdb_game_id,
            igdb_cover_image_id,
            {cover_col}
        FROM media
        WHERE id = ?
        """,
        (media_id,),
    ).fetchone()

    if not row:
        return {"media_id": media_id, "updated": False, "reason": "not found"}

    title = _row_get(row, "title", 1)
    media_type = _row_get(row, "media_type", 2)

    if not _is_game_media_type(media_type):
        return {"media_id": media_id, "updated": False, "reason": f"not a game (media_type={media_type})"}

    title = (str(title).strip() if title is not None else "")
    if not title:
        return {"media_id": media_id, "updated": False, "reason": "missing title"}

    cur_release_year = _row_get(row, "release_year", 3)
    cur_developer = _row_get(row, "developer", 4)
    cur_igdb_game_id = _row_get(row, "igdb_game_id", 5)
    cur_cover_image_id = _row_get(row, "igdb_cover_image_id", 6)
    cur_cover_url = _row_get(row, cover_col, 7)

    needs_release_year = not _has_value(cur_release_year)
    needs_developer = not _has_value(cur_developer)
    needs_cover_image_id = not _has_value(cur_cover_image_id)
    needs_cover_url = not _has_value(cur_cover_url)

    if not force and not (needs_release_year or needs_developer or needs_cover_image_id or needs_cover_url) and _has_value(cur_igdb_game_id):
        return {"media_id": media_id, "updated": False, "reason": "no missing fields"}

    candidates = igdb.search_games(title, limit=5)
    if not candidates:
        return {"media_id": media_id, "updated": False, "reason": "no IGDB match"}

    best = candidates[0]

    updates: Dict[str, Any] = {}
    if not _has_value(cur_igdb_game_id) and isinstance(best.get("id"), int):
        updates["igdb_game_id"] = best["id"]

    if needs_release_year:
        year = _extract_release_year(best)
        if isinstance(year, int):
            updates["release_year"] = year

    if needs_developer:
        developer = _extract_developers_joined(best)
        if developer:
            updates["developer"] = developer

    image_id = _extract_cover_image_id(best)
    if needs_cover_image_id and image_id:
        updates["igdb_cover_image_id"] = image_id

    if needs_cover_url:
        # Prefer the extracted image_id (from best or above) to build a stable URL.
        image_id_for_url = image_id or (str(cur_cover_image_id).strip() if _has_value(cur_cover_image_id) else "")
        if image_id_for_url:
            updates[cover_col] = igdb.build_image_url(image_id_for_url)

    if not updates:
        # We found a match, but none of the fields we care about were extractable.
        # Still update last_enriched_at so you can see it was attempted.
        updates["igdb_last_enriched_at"] = _utc_now_iso()
    else:
        updates["igdb_last_enriched_at"] = _utc_now_iso()

    _apply_media_updates(db, media_id, updates)
    db.commit()

    lg.info("IGDB enrichment updated media_id=%s fields=%s", media_id, sorted(updates.keys()))
    return {
        "media_id": media_id,
        "updated": True,
        "updated_fields": sorted(updates.keys()),
        "igdb_game_id": updates.get("igdb_game_id", cur_igdb_game_id),
    }


def _apply_media_updates(db, media_id: int, updates: Dict[str, Any]) -> None:
    keys = list(updates.keys())
    if not keys:
        return

    set_clause = ", ".join([f"{k} = ?" for k in keys])
    values = [updates[k] for k in keys]
    values.append(media_id)

    db.execute(f"UPDATE media SET {set_clause} WHERE id = ?", tuple(values))


def _extract_cover_image_id(game: Dict[str, Any]) -> Optional[str]:
    cover = game.get("cover")
    if not isinstance(cover, dict):
        return None
    image_id = cover.get("image_id")
    if isinstance(image_id, str) and image_id.strip():
        return image_id.strip()
    return None


def _extract_release_year(game: Dict[str, Any]) -> Optional[int]:
    frd = game.get("first_release_date")
    if not isinstance(frd, int) or frd <= 0:
        return None
    try:
        return datetime.fromtimestamp(frd, tz=timezone.utc).year
    except (OSError, ValueError):
        return None


def _extract_developers_joined(game: Dict[str, Any]) -> Optional[str]:
    involved = game.get("involved_companies")
    if not isinstance(involved, list):
        return None

    devs: List[str] = []
    for entry in involved:
        if not isinstance(entry, dict):
            continue
        if not entry.get("developer"):
            continue
        company = entry.get("company")
        if isinstance(company, dict):
            name = company.get("name")
            if isinstance(name, str) and name.strip():
                devs.append(name.strip())

    if not devs:
        return None

    # de-dupe preserving order, case-insensitive
    seen: set[str] = set()
    uniq: List[str] = []
    for d in devs:
        key = d.casefold()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(d)

    return ", ".join(uniq) if uniq else None


def _is_game_media_type(media_type: Any) -> bool:
    if media_type is None:
        return False
    s = str(media_type).strip().casefold()
    return s in {"game", "video game", "videogame", "games", "video games"}


def _escape_search(s: str) -> str:
    # IGDB search string is quoted; escape backslashes and quotes.
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _has_value(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (int, float)):
        return v != 0
    return True


def _row_get(row: Any, key: str, idx: int) -> Any:
    # Works for sqlite3.Row (mapping-like) and tuple rows
    try:
        return row[key]  # type: ignore[index]
    except Exception:
        try:
            return row[idx]
        except Exception:
            return None