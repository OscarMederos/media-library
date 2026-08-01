#!/usr/bin/env bash
#
# On-demand backup for media-library.
#
# Backs up the things that CANNOT be regenerated from the codebase:
#   - the SQLite database (data/media.db) — captured via sqlite3's online
#     .backup so it's a consistent snapshot even if the app is mid-write
#   - the .env file (API keys)
#   - docker-compose.yml (how the app is wired)
#
# Output: a timestamped, owner-only-readable tar.gz in /opt/backups.
#
# Run on demand:  ./scripts/backup.sh
# No sudo required (assuming /opt/backups is already owned by you — see the
# one-time setup in the repo docs/comments).

set -euo pipefail

# --- Resolve paths relative to this script, so it works regardless of CWD ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

DB_PATH="${REPO_DIR}/data/media.db"
ENV_PATH="${REPO_DIR}/.env"
COMPOSE_PATH="${REPO_DIR}/docker-compose.yml"

BACKUP_DIR="/opt/backups"
TIMESTAMP="$(date +%Y-%m-%d_%H%M%S)"
STAGING="$(mktemp -d)"
ARCHIVE="${BACKUP_DIR}/media-library-backup-${TIMESTAMP}.tar.gz"

# --- Always clean up the staging dir, even on error ---
cleanup() { rm -rf "${STAGING}"; }
trap cleanup EXIT

# --- Preflight checks: fail loudly and early if something's missing ---
if [[ ! -d "${BACKUP_DIR}" ]]; then
  echo "ERROR: ${BACKUP_DIR} does not exist. Run the one-time setup first:" >&2
  echo "  sudo mkdir -p ${BACKUP_DIR} && sudo chown \$(id -un):\$(id -gn) ${BACKUP_DIR} && chmod 700 ${BACKUP_DIR}" >&2
  exit 1
fi

if [[ ! -w "${BACKUP_DIR}" ]]; then
  echo "ERROR: ${BACKUP_DIR} is not writable by $(id -un). Fix ownership/permissions and retry." >&2
  exit 1
fi

if [[ ! -f "${DB_PATH}" ]]; then
  echo "ERROR: database not found at ${DB_PATH}" >&2
  exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "ERROR: sqlite3 CLI not found. Install with: sudo apt install sqlite3" >&2
  exit 1
fi

# --- 1. Consistent DB snapshot via sqlite3 online backup (WAL-safe) ---
# Using .backup (not cp) guarantees a coherent copy even under concurrent writes.
echo "Backing up database (consistent snapshot)..."
sqlite3 "${DB_PATH}" ".backup '${STAGING}/media.db'"

# Sanity-check the snapshot is a valid, non-empty SQLite file before proceeding.
if ! sqlite3 "${STAGING}/media.db" "PRAGMA integrity_check;" | grep -q '^ok$'; then
  echo "ERROR: backup snapshot failed integrity check — aborting, NOT writing a bad backup." >&2
  exit 1
fi

# --- 2. Copy the secrets/config files (if present) ---
if [[ -f "${ENV_PATH}" ]]; then
  cp "${ENV_PATH}" "${STAGING}/.env"
  echo "Included .env"
else
  echo "WARNING: no .env found at ${ENV_PATH} — skipping (backup will lack API keys)." >&2
fi

if [[ -f "${COMPOSE_PATH}" ]]; then
  cp "${COMPOSE_PATH}" "${STAGING}/docker-compose.yml"
  echo "Included docker-compose.yml"
fi

# --- 3. Bundle into a timestamped archive ---
# -C staging so the archive contents are flat (media.db, .env, ...) not nested.
tar -czf "${ARCHIVE}" -C "${STAGING}" .

# --- 4. Lock down the archive: it contains secrets, owner read/write only ---
chmod 600 "${ARCHIVE}"

# --- 5. Report ---
SIZE="$(du -h "${ARCHIVE}" | cut -f1)"
echo ""
echo "Backup complete:"
echo "  ${ARCHIVE}  (${SIZE})"
echo "  contents:"
tar -tzf "${ARCHIVE}" | sed 's/^/    /'
