#!/bin/sh
# deck-backup.sh - image a player's internal storage before you touch it.
#
# Runs on the player (busybox ash is enough) or on a copy of its storage.
# It writes a self-describing directory you can restore with deck-restore.sh:
#
#   <out>/manifest.txt      model, firmware, date, device, partition table
#   <out>/SHA256SUMS        every image, checked before any restore
#   <out>/part-NN.img       one file per partition  (default)
#   <out>/whole.img         the entire device        (--full)
#
# --device accepts a block device (/dev/mmcblk0) or a regular file (an image
# you already pulled), so the same script can be rehearsed off-device.
#
# Usage:
#   ./deck-backup.sh --list
#   ./deck-backup.sh --out /mnt/usb/backup-$(date +%F)
#   ./deck-backup.sh --out DIR --device /dev/mmcblk0 --full
set -eu

PROG=$(basename "$0")
OUT=""
DEV=""
FULL=0
LIST=0
CHUNK=${CHUNK:-4M}

say() { printf '%s: %s\n' "$PROG" "$*" >&2; }
die() { say "error: $*"; exit 1; }

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        --out)     OUT=${2:?--out needs a directory}; shift 2 ;;
        --device)  DEV=${2:?--device needs a path}; shift 2 ;;
        --full)    FULL=1; shift ;;
        --list)    LIST=1; shift ;;
        --chunk)   CHUNK=${2:?--chunk needs a size}; shift 2 ;;
        -h|--help) usage ;;
        *)         die "unknown argument: $1 (try --help)" ;;
    esac
done

if [ "$LIST" = 1 ]; then
    say "candidate devices (pick the one holding the player's storage):"
    if command -v lsblk >/dev/null 2>&1; then
        lsblk -b -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT 2>/dev/null || true
    else
        cat /proc/partitions
    fi
    say "the device backing / is:"
    df -P / 2>/dev/null | awk 'NR==2 {print "  " $1}'
    exit 0
fi

[ -n "$OUT" ] || die "give --out DIR (try --help)"

# ---- pick the device ------------------------------------------------------
if [ -z "$DEV" ]; then
    root_src=$(df -P / | awk 'NR==2 {print $1}')
    case "$root_src" in
        /dev/*)
            # /dev/mmcblk0p1 -> /dev/mmcblk0 ; /dev/sda2 -> /dev/sda
            DEV=$(printf '%s' "$root_src" | sed -e 's/p[0-9][0-9]*$//' \
                                             -e 's/[0-9][0-9]*$//')
            ;;
        *)  die "cannot tell the device behind / ($root_src); pass --device" ;;
    esac
    say "auto-detected device: $DEV"
fi

[ -e "$DEV" ] || die "no such device: $DEV"

IS_BLOCK=0
[ -b "$DEV" ] && IS_BLOCK=1
if [ "$IS_BLOCK" = 0 ] && [ "$FULL" = 0 ]; then
    die "$DEV is a regular file: use --full (there are no partitions to enumerate)"
fi
if [ "$IS_BLOCK" = 1 ] && [ "$(id -u)" != 0 ]; then
    die "reading $DEV needs root"
fi

# ---- never write the backup onto the device being backed up ---------------
mkdir -p "$OUT"
[ -w "$OUT" ] || die "$OUT is not writable"
if [ "$IS_BLOCK" = 1 ]; then
    out_dev=$(df -P "$OUT" | awk 'NR==2 {print $1}')
    case "$out_dev" in
        "$DEV"|"$DEV"p*) die "$OUT lives on $DEV; pick another filesystem" ;;
    esac
fi

avail_kb=$(df -P "$OUT" | awk 'NR==2 {print $4}')
say "free space at $OUT: $((avail_kb / 1024)) MiB"

# ---- manifest -------------------------------------------------------------
model=unknown
for p in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
    [ -r "$p" ] && model=$(tr -d '\000' < "$p") && break
done
fw=unknown
for p in /etc/version /etc/fw-version /VERSION /etc/os-release; do
    if [ -r "$p" ]; then
        fw=$(head -n 1 "$p" | cut -c1-64)
        break
    fi
done

MANIFEST="$OUT/manifest.txt"
{
    echo "# deck-backup manifest"
    echo "date=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "host=$(hostname 2>/dev/null || echo unknown)"
    echo "model=$model"
    echo "firmware=$fw"
    echo "kernel=$(uname -r 2>/dev/null || echo unknown)"
    echo "device=$DEV"
    echo "bytes=$(wc -c < "$DEV")"
    echo "full=$FULL"
    echo "# partition table"
    if [ "$IS_BLOCK" = 1 ] && command -v sfdisk >/dev/null 2>&1; then
        sfdisk -d "$DEV" 2>/dev/null | sed 's/^/# /' || echo "# sfdisk failed"
    else
        echo "# not read (regular file or no sfdisk)"
    fi
} > "$MANIFEST"
say "manifest: $MANIFEST"

# ---- image the storage ----------------------------------------------------
: > "$OUT/SHA256SUMS"

image() {  # image <device-path> <out-file>
    src=$1
    dst=$2
    size=$(wc -c < "$src")
    if [ -e "$dst" ]; then
        have=$(wc -c < "$dst")
        [ "$have" = "$size" ] || die "$dst exists with $have bytes, need $size"
        say "resuming $dst ($have bytes already)"
        skip=$((have / 1048576))
        dd if="$src" of="$dst" bs=1M skip="$skip" seek="$skip" \
           conv=notrunc,fsync status=none 2>/dev/null || {
            dd if="$src" of="$dst" bs="$CHUNK" skip="$have" seek="$have" \
               conv=notrunc,fsync 2>/dev/null || die "dd failed for $dst"
        }
    else
        say "imaging $src -> $dst ($size bytes)"
        dd if="$src" of="$dst" bs="$CHUNK" conv=fsync status=none \
            || die "dd failed for $dst"
    fi
    ( cd "$OUT" && sha256sum "$(basename "$dst")" ) >> "$OUT/SHA256SUMS"
}

if [ "$FULL" = 1 ]; then
    image "$DEV" "$OUT/whole.img"
else
    for part in "$DEV"p* "$DEV"[0-9]*; do
        [ -b "$part" ] || continue
        [ "$part" = "$DEV" ] && continue
        image "$part" "$OUT/part-$(basename "$part").img"
    done
fi

sync
say "done."
say "  files:     $(ls -1 "$OUT" | wc -l)"
say "  bytes:     $(wc -c < "$OUT/SHA256SUMS" | tr -d ' ')B of checksums"
say "  checksums: $OUT/SHA256SUMS"
say "keep this directory off the device; restore with deck-restore.sh --in $OUT"
