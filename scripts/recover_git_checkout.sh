#!/usr/bin/env bash
# Corrupted deployment checkout recovery.
#
# The old checkout is never deleted. A fresh shallow clone is verified first,
# then the old directory is renamed to a timestamped backup.
set -Eeuo pipefail

REPO="/tmp/tybot-src"
BRANCH=""
ASSUME_YES=0

usage() {
    cat <<'EOF'
Usage:
  sudo bash scripts/recover_git_checkout.sh [options]

Options:
  --repo PATH       Corrupted checkout (default: /tmp/tybot-src)
  --branch NAME     Branch to clone (default: current checkout branch)
  --yes             Do not ask for confirmation
  -h, --help        Show this help

The script preserves only a regular, non-symlink .env file. The corrupted
checkout remains next to the restored checkout as PATH.corrupt-TIMESTAMP.
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --repo)
            (($# >= 2)) || die "--repo requires a path"
            REPO=$2
            shift 2
            ;;
        --branch)
            (($# >= 2)) || die "--branch requires a name"
            BRANCH=$2
            shift 2
            ;;
        --yes)
            ASSUME_YES=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

for command in git realpath stat mv cp df; do
    command -v "$command" >/dev/null 2>&1 || die "required command not found: $command"
done

[[ -d "$REPO/.git" ]] || die "not a Git checkout: $REPO"

REPO=$(realpath -e -- "$REPO")
PARENT=$(dirname -- "$REPO")
BASENAME=$(basename -- "$REPO")

# A typo must never rename a filesystem root or the entire temporary directory.
case "$REPO" in
    /|/tmp|/var|/opt|/home)
        die "refusing unsafe repository path: $REPO"
        ;;
esac
[[ "$PARENT" != "$REPO" ]] || die "repository path has no safe parent: $REPO"

REMOTE=$(git -C "$REPO" config --get remote.origin.url || true)
[[ -n "$REMOTE" ]] || die "remote.origin.url is missing"

if [[ -z "$BRANCH" ]]; then
    BRANCH=$(git -C "$REPO" branch --show-current 2>/dev/null || true)
fi
if [[ -z "$BRANCH" ]]; then
    BRANCH=$(git -C "$REPO" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
fi
[[ -n "$BRANCH" ]] || die "cannot determine branch; pass --branch NAME"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
CLEAN="${REPO}.clean-${STAMP}-$$"
BACKUP="${REPO}.corrupt-${STAMP}"
FAILED="${REPO}.failed-${STAMP}"

for path in "$CLEAN" "$BACKUP" "$FAILED"; do
    [[ ! -e "$path" ]] || die "destination already exists: $path"
done

OWNER_GROUP=$(stat -c '%u:%g' -- "$REPO")

printf 'Repository : %s\n' "$REPO"
printf 'Branch     : %s\n' "$BRANCH"
printf 'Backup     : %s\n' "$BACKUP"
printf 'Preserve   : .env only (when it is a regular, non-symlink file)\n'
printf 'Precondition: stop services that execute code directly from this checkout.\n'
printf '\nDisk status:\n'
df -h -- "$PARENT"

if ((ASSUME_YES == 0)); then
    printf '\nContinue with a fresh clone and path swap? [y/N] '
    read -r answer
    [[ "$answer" == "y" || "$answer" == "Y" ]] || die "cancelled"
fi

printf '\n[1/5] Cloning a fresh shallow checkout...\n'
git clone --depth 1 --branch "$BRANCH" -- "$REMOTE" "$CLEAN"

printf '[2/5] Verifying the fresh Git object database...\n'
git -C "$CLEAN" fsck --full
git -C "$CLEAN" rev-parse --verify HEAD >/dev/null
[[ -z $(git -C "$CLEAN" status --porcelain) ]] || die "fresh clone is unexpectedly dirty"

printf '[3/5] Preserving server-local configuration...\n'
if [[ -L "$REPO/.env" ]]; then
    die ".env is a symlink; refusing to copy it automatically"
elif [[ -f "$REPO/.env" ]]; then
    if git -C "$CLEAN" ls-files --error-unmatch .env >/dev/null 2>&1; then
        die "fresh clone tracks .env; refusing to overwrite a tracked file"
    fi
    cp --preserve=mode,ownership,timestamps -- "$REPO/.env" "$CLEAN/.env"
    printf 'Preserved .env without printing its contents.\n'
else
    printf 'No .env file found; nothing copied.\n'
fi

# Running this script through sudo would otherwise turn a tybot-owned checkout
# into a root-owned checkout. Apply ownership only to the new, verified sibling.
if [[ $(id -u) -eq 0 ]]; then
    chown -R -- "$OWNER_GROUP" "$CLEAN"
fi

printf '[4/5] Swapping paths; the corrupted checkout remains as a backup...\n'
cd -- "$PARENT"
mv -- "$REPO" "$BACKUP"
if ! mv -- "$CLEAN" "$REPO"; then
    printf 'Path swap failed; restoring the original checkout path.\n' >&2
    mv -- "$BACKUP" "$REPO"
    die "failed to move fresh checkout into place"
fi

printf '[5/5] Verifying the restored path...\n'
if ! git -C "$REPO" fsck --full; then
    printf 'Final verification failed; rolling the path back.\n' >&2
    mv -- "$REPO" "$FAILED"
    mv -- "$BACKUP" "$REPO"
    die "restored checkout failed fsck; fresh files retained at $FAILED"
fi

COMMIT=$(git -C "$REPO" log -1 --format='%h %s')
printf '\nRecovery completed.\n'
printf 'Current checkout : %s\n' "$REPO"
printf 'Current commit   : %s\n' "$COMMIT"
printf 'Corrupt backup  : %s\n' "$BACKUP"
printf '\nDo not delete the backup until deployment and service checks pass.\n'
