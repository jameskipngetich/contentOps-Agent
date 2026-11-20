#!/usr/bin/env bash
# Restore the application database from a backup.
#
# Usage: scripts/restore-backup.sh BACKUP_FILE [--yes]
#
# The current database is copied to backups/pre-restore-<timestamp>.db before it
# is overwritten, and the restored file is verified afterwards.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    cat <<'USAGE'
Usage: restore-backup.sh BACKUP_FILE [--yes]

Restores BACKUP_FILE over DATABASE_PATH.

Safety:
  * the file is validated before anything is touched
  * the current database is saved as backups/pre-restore-<timestamp>.db
  * the service should be stopped first, or writes may be lost

Options:
  --yes     do not ask for confirmation (for scripted use)
  -h        show this help
USAGE
}

backup_file=""
for arg in "$@"; do
    case "${arg}" in
        --yes) ASSUME_YES=1 ;;
        -h | --help)
            usage
            exit 0
            ;;
        *) backup_file="${arg}" ;;
    esac
done

[[ -n "${backup_file}" ]] || {
    usage
    exit 2
}
[[ -f "${backup_file}" ]] || die "no such backup file: ${backup_file}"

validate_sqlite "${backup_file}" >/dev/null || die "${backup_file} is not a valid backup"

db="$(resolve_db_path)"
reports="$(validate_sqlite "${backup_file}" | cut -d' ' -f2)"

cat <<INFO
About to restore:
  from: ${backup_file} (${reports} reports)
  to:   ${db}
Stop the service (docker compose stop app, or scale the deployment to 0) before
continuing, otherwise in-flight writes will be lost.
INFO
confirm "Restore this backup?"

mkdir -p "${BACKUP_DIR}" || die "cannot create ${BACKUP_DIR}"
if [[ -f "${db}" ]]; then
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    safety="${BACKUP_DIR}/pre-restore-${stamp}.db"
    sqlite_backup "${db}" "${safety}" >/dev/null || die "could not save the current database"
    log "current database saved to ${safety}"
fi

db_dir="$(dirname "${db}")"
mkdir -p "${db_dir}"

# Remove the target and its WAL/SHM companions first. Copying into an existing
# database leaves its old write-ahead log in place, and a later checkpoint can
# resurrect the state we are trying to replace. The original was already saved
# as pre-restore-<timestamp>.db above, so this is recoverable.
rm -f "${db}" "${db}-wal" "${db}-shm"
sqlite_backup "${backup_file}" "${db}" >/dev/null || die "restore failed"

result="$(validate_sqlite "${db}")" || die "restored database failed validation"
log "restored ${db} (${result#ok } reports)"
log "start the service again and check GET /ready and GET /api/v1/reports"
