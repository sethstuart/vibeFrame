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
#   VIBEFRAME_BACKUP_KEEP=30 ./vibeframe-backup.sh   # one-off override
set -euo pipefail

# The archive contains .env (which may hold VIBEFRAME_WEB_TOKEN) and the whole
# database. It lands on a share every host on the LAN can mount, so nothing this
# script creates may be group- or world-readable.
umask 077

MOUNT="${VIBEFRAME_MOUNT:-/mnt/vibeFrame}"
# Derived from MOUNT, not independent: the mountpoint guard below must validate
# the same path we actually write to, or it guards nothing.
DEST="${VIBEFRAME_BACKUP_DIR:-$MOUNT/.vibeframe-backup}"
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

# How many snapshots to keep. The number is owned by the app's Settings page so
# there is one visible place to change it; this reads it back out of the same
# `setting` table the web UI writes. An explicit env var still wins, for one-off
# runs and for the tests that prove the validation below actually fires.
KEEP="${VIBEFRAME_BACKUP_KEEP:-}"
KEEP_SOURCE="VIBEFRAME_BACKUP_KEEP"
if [ -z "$KEEP" ]; then
  KEEP="$(docker exec -i "$CONTAINER" python3 - <<'PY' 2>/dev/null | tr -d '[:space:]'
import sqlite3
try:
    c = sqlite3.connect("/var/lib/vibeframe/vibeframe.db")
    row = c.execute('select value from setting where "key" = ?', ("backup_keep",)).fetchone()
    print(row[0] if row else "")
except Exception:
    print("")
PY
)"
  KEEP_SOURCE="Settings page"
fi
if [ -z "$KEEP" ]; then
  # Matches Settings.backup_keep's default; only reached before the user has
  # ever saved settings.
  KEEP=5
  KEEP_SOURCE="built-in default"
fi

# KEEP=0 would make `tail -n +1` list every archive and delete the snapshot we
# just wrote; a non-numeric value would abort mid-run under set -u.
case "$KEEP" in
  '' | *[!0-9]*) die "backup retention must be a whole number, got '$KEEP' (from $KEEP_SOURCE)" ;;
esac
[ "$KEEP" -ge 1 ] || die "backup retention must be >= 1, got '$KEEP' (from $KEEP_SOURCE); 0 would delete every snapshot including the new one"
log "keeping $KEEP snapshot(s) (from $KEEP_SOURCE)"

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
chmod 600 "$STAGE/vibeframe.db"
log "db captured: $COUNTS"

# --- host + app config ----------------------------------------------------
# An absent file is fine and expected (the systemd units do not exist on a first
# run). A file that exists but cannot be copied is a real failure and must not be
# reported as "absent" -- that is how a backup silently ships without its .env.
copy() {
  local src=$1 dst=$2
  if [ ! -e "$src" ]; then
    log "skipped $src (absent)"
    return 0
  fi
  cp "$src" "$dst" || die "$src exists but could not be copied (permissions? full disk?)"
  chmod 600 "$dst"
  log "captured $src"
}

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
       sudo reboot

2. Install docker + nfs-common, add yourself to the docker group, then log out
   and back in (group membership is fixed at login).

3. Restore the vibeFrame line from `etc/fstab` and copy
   `etc/docker-wait-for-nfs.conf` to
   /etc/systemd/system/docker.service.d/, then:

       sudo systemctl daemon-reload && sudo mount -a

4. Clone the repo at the commit in MANIFEST.txt and put `app/.env` back:

       install -m 600 app/.env <repo>/.env

5. Start the stack once so the volume and schema exist, then stop the app and
   replace the database:

       docker compose up -d --build
       docker compose stop

   `docker cp` writes as root:root unless -a is passed, but the app runs as
   USER vibeframe (uid 1000) -- a root-owned DB opens read-only and the
   scheduler dies on its first write with "attempt to write a readonly
   database", leaving the frame stuck on one photo. Copy with -a and then fix
   ownership explicitly:

       docker cp -a vibeframe.db vibeframe:/var/lib/vibeframe/vibeframe.db
       docker exec -u 0 vibeframe chown vibeframe:vibeframe /var/lib/vibeframe/vibeframe.db

   Delete any stale WAL beside it. The backup is already checkpointed, so a
   leftover -wal/-shm from the old database would replay the wrong data:

       docker exec -u 0 vibeframe rm -f /var/lib/vibeframe/vibeframe.db-wal \
                                        /var/lib/vibeframe/vibeframe.db-shm

       docker compose start

6. Reinstall the backup timer (it is not restored by any of the above):

       sudo install -m 755 vibeframe-backup.sh /usr/local/bin/vibeframe-backup
       sudo install -m 644 etc/vibeframe-backup.service etc/vibeframe-backup.timer \
            /etc/systemd/system/
       sudo systemctl daemon-reload
       sudo systemctl enable --now vibeframe-backup.timer

7. Verify the row counts in MANIFEST.txt match -- favourites and history are the
   whole point of this backup:

       docker exec vibeframe python3 -c "import sqlite3;c=sqlite3.connect('/var/lib/vibeframe/vibeframe.db');print({t:c.execute('select count(*) from '+t).fetchone()[0] for t in ('image','favorite','history','setting')})"
EOF
chmod 600 "$STAGE/MANIFEST.txt" "$STAGE/RESTORE.md"

# --- write + rotate -------------------------------------------------------
mkdir -p "$DEST"
chmod 700 "$DEST" 2>/dev/null || true

# Clear any temp left by a previous interrupted run.
rm -f "$DEST"/.tmp-vibeframe-backup-*.tar.gz

# Write to a temp name and rename into place. A rename within one filesystem is
# atomic, so an interrupted run can never leave a truncated archive that the
# rotation glob would count as a valid snapshot, nor destroy the previous
# latest.tar.gz.
ARCHIVE="$DEST/vibeframe-backup-$STAMP.tar.gz"
TMP_ARCHIVE="$DEST/.tmp-vibeframe-backup-$STAMP.tar.gz"
tar -czf "$TMP_ARCHIVE" -C "$WORK" "vibeframe-backup-$STAMP"
tar -tzf "$TMP_ARCHIVE" >/dev/null || die "archive failed verification immediately after writing"
chmod 600 "$TMP_ARCHIVE"
mv -f "$TMP_ARCHIVE" "$ARCHIVE"

TMP_LATEST="$DEST/.tmp-vibeframe-backup-latest.tar.gz"
cp -f "$ARCHIVE" "$TMP_LATEST"
mv -f "$TMP_LATEST" "$DEST/latest.tar.gz"
log "wrote $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

mapfile -t OLD < <(ls -1t "$DEST"/vibeframe-backup-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)))
if [ "${#OLD[@]}" -gt 0 ]; then
  rm -f "${OLD[@]}"
  log "rotated out ${#OLD[@]} old backup(s), keeping $KEEP"
fi

log "done: $(ls -1 "$DEST"/vibeframe-backup-*.tar.gz | wc -l) snapshot(s) in $DEST"
