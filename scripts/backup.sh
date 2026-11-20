#!/usr/bin/env bash
# Create a consistent copy of the application database.
#
# Usage: scripts/backup.sh [-o OUTPUT_DIR]
# Env:   DATABASE_PATH, BACKUP_DIR

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    cat <<'USAGE'
Usage: backup.sh [-o OUTPUT_DIR]

Backs up the SQLite database to backups/contentops-<UTC timestamp>.db and verifies
the copy. Safe to run while the service is running.

Options:
  -o DIR    write the backup to DIR instead of BACKUP_DIR/backups
  -h        show this help
USAGE
}

output_dir="${BACKUP_DIR}"
while getopts ":o:h" opt; do
    case "${opt}" in
        o) output_dir="${OPTARG}" ;;
        h)
            usage
            exit 0
            ;;
        *) die "unknown option: -${OPTARG} (use -h)" ;;
    esac
done

db="$(require_db)"
mkdir -p "${output_dir}" || die "cannot create backup directory ${output_dir}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="${output_dir}/contentops-${stamp}.db"

log "backing up ${db}"
sqlite_backup "${db}" "${target}" >/dev/null || die "backup failed"

result="$(validate_sqlite "${target}")" || die "backup verification failed for ${target}"
reports="${result#ok }"
size="$(du -h "${target}" | cut -f1)"

log "created ${target} (${size}, ${reports} reports, verified)"
