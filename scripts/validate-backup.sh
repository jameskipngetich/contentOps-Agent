#!/usr/bin/env bash
# Verify that a backup file is a usable application database.
#
# Usage: scripts/validate-backup.sh path/to/backup.db
# Exit:  0 valid, 1 invalid

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if [[ $# -ne 1 || "$1" == "-h" ]]; then
    cat <<'USAGE'
Usage: validate-backup.sh BACKUP_FILE

Runs SQLite integrity checks, confirms the reports table exists and prints how
many reports the backup contains.
USAGE
    [[ $# -eq 1 ]] && exit 0
    exit 2
fi

backup_file="$1"
[[ -f "${backup_file}" ]] || die "no such file: ${backup_file}"

if result="$(validate_sqlite "${backup_file}")"; then
    log "valid: ${backup_file} ($(du -h "${backup_file}" | cut -f1), ${result#ok } reports)"
    exit 0
fi

log "invalid backup: ${backup_file}"
exit 1
