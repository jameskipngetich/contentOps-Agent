#!/usr/bin/env bash
# List available database backups, newest last.
#
# Usage: scripts/list-backups.sh [-d DIR]

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    cat <<'USAGE'
Usage: list-backups.sh [-d DIR]

Lists *.db backups with size and modification time, and counts them.

Options:
  -d DIR    look in DIR instead of BACKUP_DIR/backups
  -h        show this help
USAGE
}

directory="${BACKUP_DIR}"
while getopts ":d:h" opt; do
    case "${opt}" in
        d) directory="${OPTARG}" ;;
        h)
            usage
            exit 0
            ;;
        *) die "unknown option: -${OPTARG} (use -h)" ;;
    esac
done

if [[ ! -d "${directory}" ]]; then
    log "backup directory ${directory} does not exist yet"
    exit 0
fi

mapfile -t files < <(find "${directory}" -maxdepth 1 -name '*.db' -type f | sort)

if [[ ${#files[@]} -eq 0 ]]; then
    log "no backups in ${directory}"
    exit 0
fi

printf '%-58s %8s  %s\n' "FILE" "SIZE" "MODIFIED"
for file in "${files[@]}"; do
    printf '%-58s %8s  %s\n' \
        "$(basename "${file}")" \
        "$(du -h "${file}" | cut -f1)" \
        "$(date -r "${file}" '+%Y-%m-%d %H:%M:%S')"
done

printf '\n%d backup(s) in %s\n' "${#files[@]}" "${directory}"
