#!/usr/bin/env bash
# Shared helpers for the operational scripts. Source this file, do not run it.
# shellcheck shell=bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/backups}"
DEFAULT_DB="${REPO_ROOT}/data/contentops.db"
NAMESPACE="${NAMESPACE:-contentops}"
DEPLOYMENT="${DEPLOYMENT:-contentops-agent}"
# 127.0.0.1 rather than localhost: localhost may resolve to IPv6 first while the
# service (or a Docker port binding) only listens on IPv4.
APP_URL="${APP_URL:-http://127.0.0.1:8080}"

log() { printf '%s %s\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$*"; }
warn() { printf '%s WARN  %s\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$*" >&2; }
die() {
    printf '%s ERROR %s\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$*" >&2
    exit 1
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

# Resolve DATABASE_PATH against the repository root, matching how the service is
# started from this directory.
resolve_db_path() {
    local path="${DATABASE_PATH:-${DEFAULT_DB}}"
    case "$path" in
        /*) printf '%s\n' "$path" ;;
        *) printf '%s/%s\n' "$REPO_ROOT" "$path" ;;
    esac
}

require_db() {
    local db
    db="$(resolve_db_path)"
    [[ -f "$db" ]] || die "database not found at ${db} (start the service or set DATABASE_PATH)"
    printf '%s\n' "$db"
}

confirm() {
    local prompt="$1"
    if [[ "${ASSUME_YES:-0}" == "1" ]]; then
        return 0
    fi
    local reply
    read -r -p "${prompt} [y/N] " reply || die "no answer, aborted"
    [[ "${reply}" =~ ^[Yy]$ ]] || die "aborted by user"
}

# Copy a SQLite database through the backup API, which is consistent even while
# the application has the file open.
sqlite_backup() {
    local source="$1" destination="$2"
    require_cmd python3
    python3 - "${source}" "${destination}" <<'PY'
import sqlite3
import sys

import os

source_path, destination_path = sys.argv[1], sys.argv[2]
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
destination = sqlite3.connect(destination_path)
with destination:
    source.backup(destination)
    destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
destination.close()
source.close()

# Leave a single portable file behind, not an empty -wal/-shm pair.
for suffix in ("-wal", "-shm"):
    sidecar = f"{destination_path}{suffix}"
    if os.path.exists(sidecar):
        os.remove(sidecar)
print(destination_path)
PY
}

# Print "ok <report_count>" when the database passes integrity checks, otherwise
# print the problem and exit non-zero.
validate_sqlite() {
    local path="$1"
    require_cmd python3
    [[ -f "$path" ]] || die "no such database file: ${path}"
    python3 - "${path}" <<'PY'
import sqlite3
import sys

path = sys.argv[1]
try:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        print(f"integrity_check failed: {result}")
        raise SystemExit(1)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "reports" not in tables:
        print(f"missing reports table (found: {sorted(tables) or 'none'})")
        raise SystemExit(1)
    count = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    conn.close()
except sqlite3.DatabaseError as exc:
    print(f"not a usable sqlite database: {exc}")
    raise SystemExit(1) from exc

print(f"ok {count}")
PY
}
