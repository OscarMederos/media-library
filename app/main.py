from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sqlite3
import os
import requests
import time
import json
import re
import logging
from typing import Any

# -------------------------------------------------
# Logging (THIS IS IMPORTANT)
# -------------------------------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("media-library")

# -------------------------------------------------
# Config / Env
# -------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "/data/media.db")
BARCODELOOKUP_API_KEY = os.getenv("BARCODELOOKUP_API_KEY")

IGDB_CLIENT_ID = os.getenv("IGDB_CLIENT_ID")
IGDB_CLIENT_SECRET = os.getenv("IGDB_CLIENT_SECRET")

app = FastAPI()

# -------------------------------------------------
# Models
# -------------------------------------------------
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

class NormalizeGameRequest(BaseModel):
    dry_run: bool = True
    igdb_id: int | None = None

# -------------------------------------------------
# DB helpers
# -------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.on_event("startup")
def init_db():
    db = get_db()
    db.execute("""
    CREATE TABLE IF NOT EXISTS media (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        barcode TEXT UNIQUE,
        title TEXT,
        title_raw TEXT,
        media_type TEXT,
        platform TEXT,
        format TEXT,
        release_year INTEGER,
        cover_url TEXT,
        source TEXT,
        source_payload TEXT,
        added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME
    )
    """)
    db.commit()

# -------------------------------------------------
# IGDB helpers
# -------------------------------------------------
_igdb_token: str | None = None
_igdb_token_exp: float = 0.0

def _igdb_get_token() -> str:
    global _igdb_token, _igdb_token_exp

    if not IGDB_CLIENT_ID or not IGDB_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="IGDB_CLIENT_ID or IGDB_CLIENT_SECRET not set"
        )

    now = time.time()
    if _igdb_token and now < (_igdb_token_exp - 60):
        return _igdb_token

    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": IGDB_CLIENT_ID,
            "client_secret": IGDB_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=10,
    )

    if not r.ok:
        raise HTTPException(
            status_code=500,
            detail=f"Twitch token failed HTTP {r.status_code}: {r.text[:300]}"
        )

    data = r.json()
    _igdb_token = data["access_token"]
    _igdb_token_exp = now + int(data["expires_in"])
    return _igdb_token

def _igdb_search_games(title: str):
    token = _igdb_get_token()

    body = f'''
search "{title.replace('"', '')}";
fields id,name,first_release_date,cover.image_id;
limit 5;
'''

    r = requests.post(
        "https://api.igdb.com/v4/games",
        data=body,
        headers={
            "Client-ID": IGDB_CLIENT_ID,
            "Authorization": f"Bearer {token}",
        },
        timeout=10,
    )

    if not r.ok:
        raise HTTPException(
            status_code=500,
            detail=f"IGDB search failed HTTP {r.status_code}: {r.text[:300]}"
        )

    return r.json()

# -------------------------------------------------
# API
# -------------------------------------------------
MEDIA_SELECT = """
SELECT id, barcode, title, title_raw, media_type,
       platform, format, release_year, cover_url,
       source, source_payload
FROM media
"""

@app.get("/media")
def list_media():
    db = get_db()
    rows = db.execute(MEDIA_SELECT).fetchall()
    return [dict(r) for r in rows]

# -------------------------------------------------
# NORMALIZE GAME (WITH SAFE WRAPPER)
# -------------------------------------------------
@app.post("/normalize_game/{item_id}")
def normalize_game(item_id: int, req: NormalizeGameRequest):
    try:
        db = get_db()
        row = db.execute(MEDIA_SELECT + " WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Item not found")

        item = dict(row)
        title = item["title"]

        # ---- Dry run: return IGDB candidates
        games = _igdb_search_games(title)

        if req.dry_run:
            return {
                "status": "dry_run",
                "current": item,
                "candidates": [
                    {
                        "igdb_id": g.get("id"),
                        "name": g.get("name"),
                        "release_year": (
                            time.gmtime(g["first_release_date"]).tm_year
                            if g.get("first_release_date") else None
                        ),
                        "cover_url": (
                            f"https://images.igdb.com/igdb/image/upload/t_cover_big/{g['cover']['image_id']}.jpg"
                            if g.get("cover") else None
                        )
                    }
                    for g in games
                ]
            }

        # ---- Apply
        if not req.igdb_id:
            raise HTTPException(status_code=400, detail="igdb_id required")

        chosen = next((g for g in games if g["id"] == req.igdb_id), None)
        if not chosen:
            raise HTTPException(status_code=404, detail="IGDB candidate not found")

        year = (
            time.gmtime(chosen["first_release_date"]).tm_year
            if chosen.get("first_release_date") else None
        )
        cover = (
            f"https://images.igdb.com/igdb/image/upload/t_cover_big/{chosen['cover']['image_id']}.jpg"
            if chosen.get("cover") else None
        )

        db.execute(
            """
            UPDATE media
            SET title=?, release_year=?, cover_url=?,
                source='igdb',
                source_payload=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                chosen["name"],
                year,
                cover,
                json.dumps({"igdb_id": chosen["id"]}),
                item_id,
            )
        )
        db.commit()

        updated = db.execute(MEDIA_SELECT + " WHERE id=?", (item_id,)).fetchone()
        return {"status": "applied", "item": dict(updated)}

    # ---- THIS IS THE IMPORTANT PART ----
    except HTTPException:
        raise
    except Exception as e:
        log.exception("normalize_game crashed for item_id=%s", item_id)
        raise HTTPException(
            status_code=500,
            detail=f"normalize_game crash: {type(e).__name__}: {e}"
        )

# -------------------------------------------------
# Static UI
# -------------------------------------------------
app.mount("/ui", StaticFiles(directory="/static", html=True), name="ui")
