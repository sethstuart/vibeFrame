#!/usr/bin/env bash
# Snapshot the irreplaceable vibeFrame state to the NAS.
#
# What is worth saving is small: the SQLite DB (favourites, history, and any
# settings changed through the web UI) plus the handful of host files that took
# real effort to work out. Photos already live on the NAS and the render cache
# regenerates itself, so neither is included.
#
# Runs as the invoking user -- deliberately NOT root. The NAS export uses
# root_squash, so a root-run backup lands as uid 65534 (nobody); because the
# share's top directory is sticky (drwxrwxrwt), those files could then never be
# deleted by the rotation pass, and the backup dir would grow forever.
#
#   ./vibeframe-backup.sh              # write a snapshot, rotate old ones
#   VIBEFRAME_BACKUP_KEEP=30 ./vibeframe-backup.sh
set -euo pipefail

DEST="${VIBEFRAME_BACKUP_DIR:-/mnt/vibeFrame/.vibeframe-backup}"
MOUNT="${VIBEFRAME_MOUNT:-/mnt/vibeFrame}"
KEEP="${VIBEFRAME_BACKUP_KEEP:-14}"
REPO="${VIBEFRAME_REPO:-$HOME/Documents/github/vibeFrame}"
CONTAINER="${VIBEFRAME_CONTAINER:-vibeframe}"
DB_IN_CONTAINER=/var/lib/vibeframe/vibeframe.db

log() { printf '[vibeframe-backup] %s\n' "$*"; }
die() { printf '[vibeframe-backup] FAIL: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run as your normal user, not root (NFS root_squash would make this backup unreadable and undeletable)"

# Writing into an unmounted mountpoint would silently fill the SD card with
# files that vanish the moment NFS mounts over them.
mountpoint -q "$MOUNT" || die "$MOUNT is not mounted; refusing to write into the bare mountpoint"

docker inspect "$CONTAINER" >/dev/null 2>&1 || die "container '$CONTAINER' not found (is the stack up?)"

STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/vibeframe-backup-$STAMP"
mkdir -p "$STAGE/etc" "$STAGE/boot" "$STAGE/app"

# --- database -------------------------------------------------------------
# The DB runs in WAL mode: vibeframe.db alone is stale, because recent commits
# live in vibeframe.db-wal until a checkpoint. sqlite3's online backup API
# consolidates the WAL and is safe against a concurrently writing app, which a
# plain cp is not. Run it inside the container -- the state lives in a docker
# volume that only root can read from the host, but the container user owns it.
log "backing up database (online, WAL-safe)"
COUNTS="$(docker exec -i "$CONTAINER" python3 - <<PY
import sqlite3
src = sqlite3.connect("$DB_IN_CONTAINER")
dst = sqlite3.connect("/tmp/vibeframe-backup.db")
with dst:
    src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
if ok != "ok":
    raise SystemExit("integrity_check failed: " + ok)
counts = {t: dst.execute("select count(*) from " + t).fetchone()[0]
          for t in ("image", "favorite", "history", "setting")}
dst.close(); src.close()
print(" ".join(f"{k}={v}" for k, v in counts.items()))
PY
)"
docker cp "$CONTAINER:/tmp/vibeframe-backup.db" "$STAGE/vibeframe.db" >/dev/null
docker exec "$CONTAINER" rm -f /tmp/vibeframe-backup.db
log "db captured: $COUNTS"

# --- host + app config ----------------------------------------------------
copy() { [ -e "$1" ] && cp "$1" "$2" && log "captured $1" || log "skipped $1 (absent)"; }

copy "$REPO/.env"                                                    "$STAGE/app/.env"
copy /etc/fstab                                                      "$STAGE/etc/fstab"
copy /boot/firmware/config.txt                                       "$STAGE/boot/config.txt"
copy /etc/systemd/system/docker.service.d/wait-for-vibeframe-nfs.conf "$STAGE/etc/docker-wait-for-nfs.conf"
copy /etc/systemd/system/vibeframe-backup.service                    "$STAGE/etc/vibeframe-backup.service"
copy /etc/systemd/system/vibeframe-backup.timer                      "$STAGE/etc/vibeframe-backup.timer"
# Ship the script inside its own backup so a restore is self-contained.
copy "${BASH_SOURCE[0]}"                                             "$STAGE/vibeframe-backup.sh"

GIT_COMMIT="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
IMAGE_ID="$(docker inspect -f '{{.Image}}' "$CONTAINER" 2>/dev/null || echo unknown)"

{
  echo "host:        $(hostname)"
  echo "created:     $(date -Is)"
  echo "created_by:  $(id -un) (uid $(id -u))"
  echo "git_commit:  $GIT_COMMIT"
  echo "image:       $IMAGE_ID"
  echo "nfs_source:  $(findmnt -no SOURCE "$MOUNT" 2>/dev/null || echo unknown)"
  echo "db_rows:     $COUNTS"
  echo
  echo "sha256:"
  (cd "$STAGE" && find . -type f ! -name MANIFEST.txt -exec sha256sum {} +)
} > "$STAGE/MANIFEST.txt"

cat > "$STAGE/RESTORE.md" <<'EOF'
# Restoring vibeFrame

Order matters: the NFS mount must exist before Docker starts, or the container
bind-mounts an empty directory and keeps it (docker bind mounts are rprivate).

1. Fresh Raspberry Pi OS, then enable the panel's interfaces. Both ship
   disabled, and `spi0-0cs` is required or the panel write aborts with
   "Chip Select: (line 8, GPIO8) currently claimed by spi0 CS0":

       sudo raspi-config nonint do_spi 0
       sudo raspi-config nonint do_i2c 0
       echo 'dtoverlay=spi0-0cs' | sudo tee -a /boot/firmware/config.txt

2. Install docker + nfs-common, add yourself to the docker group, re-login.

3. Restore `etc/fstab`'s vibeFrame line and `etc/docker-wait-for-nfs.conf`
   (to /etc/systemd/system/docker.service.d/), then:

       sudo systemctl daemon-reload && sudo mount -a

4. Clone the repo at the commit in MANIFEST.txt, drop `app/.env` in place.

5. Restore the database BEFORE first start, so the app doesn't create an empty
   one. Copy `vibeframe.db` into the state volume as uid 1000, e.g. start the
   stack once, `docker compose stop`, then:

       docker cp vibeframe.db vibeframe:/var/lib/vibeframe/vibeframe.db
       docker compose restart

   Delete any stale `vibeframe.db-wal` / `-shm` beside it -- a WAL from a
   different database will be rejected or replay the wrong data.

6. `docker compose up -d --build`, then verify the row counts in MANIFEST.txt
   match: favourites and history are the whole point of this backup.
EOF

# --- write + rotate -------------------------------------------------------
mkdir -p "$DEST"
ARCHIVE="$DEST/vibeframe-backup-$STAMP.tar.gz"
tar -czf "$ARCHIVE" -C "$WORK" "vibeframe-backup-$STAMP"
cp -f "$ARCHIVE" "$DEST/latest.tar.gz"
log "wrote $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

mapfile -t OLD < <(ls -1t "$DEST"/vibeframe-backup-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)))
if [ "${#OLD[@]}" -gt 0 ]; then
  rm -f "${OLD[@]}"
  log "rotated out ${#OLD[@]} old backup(s), keeping $KEEP"
fi

log "done: $(ls -1 "$DEST"/vibeframe-backup-*.tar.gz | wc -l) snapshot(s) in $DEST"
