#!/bin/sh
# deck-restore.sh - write a deck-backup.sh image back, checksums first.
#
# Nothing is written until every checksum in SHA256SUMS matches, and writing a
# block device additionally requires --force.  Restoring the storage is not
# the same as restoring the firmware: after this, run the official .UPD
# updater so the signed image (and its version number) is what the player
# reports again.
#
# Usage:
#   ./deck-restore.sh --in DIR --check                  # verify only
#   ./deck-restore.sh --in DIR --device /dev/mmcblk0 --dry-run
#   ./deck-restore.sh --in DIR --device /dev/mmcblk0 --force
#   ./deck-restore.sh --in DIR --device /dev/mmcblk0 --force --full
set -eu

PROG=$(basename "$0")
IN=""
DEV=""
CHECK=0
DRY=0
FORCE=0
FULL=0

say() { printf '%s: %s\n' "$PROG" "$*" >&2; }
die() { say "error: $*"; exit 1; }

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        --in)     IN=${2:?--in needs the backup directory}; shift 2 ;;
        --device) DEV=${2:?--device needs a path}; shift 2 ;;
        --check)  CHECK=1; shift ;;
        --dry-run) DRY=1; shift ;;
        --force)  FORCE=1; shift ;;
        --full)   FULL=1; shift ;;
        -h|--help) usage ;;
        *)        die "unknown argument: $1 (try --help)" ;;
    esac
done

[ -n "$IN" ] || die "give --in DIR (try --help)"
[ -d "$IN" ] || die "no such directory: $IN"
[ -f "$IN/SHA256SUMS" ] || die "$IN/SHA256SUMS is missing; refusing to restore"
[ -f "$IN/manifest.txt" ] || die "$IN/manifest.txt is missing"

say "manifest:"
sed 's/^/  /' "$IN/manifest.txt" | head -n 20

# ---- 1. checksums ---------------------------------------------------------
say "verifying checksums (this can take a while)"
( cd "$IN" && sha256sum -c SHA256SUMS ) || die "checksum mismatch: the backup is damaged or was modified"
say "checksums: OK"
[ "$CHECK" = 1 ] && exit 0

[ -n "$DEV" ] || die "give --device to restore (or use --check)"

# ---- 2. target sanity -----------------------------------------------------
[ -e "$DEV" ] || die "no such device: $DEV"
IS_BLOCK=0
[ -b "$DEV" ] && IS_BLOCK=1
[ "$IS_BLOCK" = 1 ] && [ "$(id -u)" != 0 ] && die "writing $DEV needs root"

want=$(sed -n 's/^bytes=//p' "$IN/manifest.txt" | head -n 1)
have=$(wc -c < "$DEV")
[ -n "$want" ] || die "manifest has no device size; refusing to guess"
if [ "$want" != "$have" ]; then
    say "size differs: backup=$want target=$have"
    [ "$FORCE" = 1 ] || die "refusing to write a different-sized target without --force"
fi

if [ "$IS_BLOCK" = 1 ] && [ "$FORCE" = 0 ] && [ "$DRY" = 0 ]; then
    die "refusing to write a block device without --force"
fi

# ---- 3. plan --------------------------------------------------------------
if [ "$FULL" = 1 ] || [ -f "$IN/whole.img" ]; then
    src="$IN/whole.img"
else
    src=""
fi

write_one() {  # write_one <image> <target>
    img=$1
    tgt=$2
    if [ "$DRY" = 1 ]; then
        say "would write $img ($(wc -c < "$img") bytes) -> $tgt"
        return 0
    fi
    say "writing $img -> $tgt"
    dd if="$img" of="$tgt" bs=4M conv=fsync,notrunc status=none \
        || die "dd failed writing $tgt"
}

if [ -n "$src" ]; then
    write_one "$src" "$DEV"
else
    found=0
    for img in "$IN"/part-*.img; do
        [ -f "$img" ] || continue
        found=1
        base=$(basename "$img" .img)          # part-mmcbblk0p1
        suffix=${base#part-}                  # mmcblk0p1
        if [ "$IS_BLOCK" = 1 ]; then
            tgt="/dev/$suffix"
            [ -b "$tgt" ] || die "$img maps to $tgt, which is not a block device"
        else
            tgt="$DEV"
            say "note: $img will be written at offset 0 of $DEV (regular-file target)"
        fi
        write_one "$img" "$tgt"
    done
    [ "$found" = 1 ] || die "no part-*.img and no whole.img in $IN"
fi

[ "$DRY" = 1 ] && { say "dry run: nothing written"; exit 0; }

sync
say "restore written."
say ""
say "next:"
say "  1. re-run the official .UPD updater for this model, so the player"
say "     boots the signed image and reports the official version again;"
say "  2. check the version the player shows against the firmware in"
say "     $IN/manifest.txt;"
say "  3. re-run your checks. If the player does not boot, restore again"
say "     from this backup before changing anything else."
