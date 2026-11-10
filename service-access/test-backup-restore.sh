#!/bin/sh
# test-backup-restore.sh - prove deck-backup.sh / deck-restore.sh round-trip.
#
# Uses loop devices only. Needs root (mount, losetup) and mkfs.vfat; it never
# touches a disk that is not a loop device it created itself.
#
#   sudo ./test-backup-restore.sh
set -eu
cd "$(dirname "$0")"

WORK=${WORK:-/var/tmp/deck-backup-test}
LOOP=""
MNT=""

MOUNTED=0
cleanup() {
    set +e
    [ "$MOUNTED" = 1 ] && [ -n "$MNT" ] && umount "$MNT" 2>/dev/null
    [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null
    set -e
}
trap cleanup EXIT INT TERM

for tool in losetup sfdisk mkfs.vfat dd sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || { echo "skip: $tool not installed"; exit 0; }
done
[ "$(id -u)" = 0 ] || { echo "skip: needs root for losetup/mount"; exit 0; }

rm -rf "$WORK"
mkdir -p "$WORK/mnt"

echo "== build a two-partition device"
truncate -s 64M "$WORK/disk.img"
printf 'label: dos\n,16M,c\n,32M,c\n' | sfdisk -q "$WORK/disk.img" >/dev/null
LOOP=$(losetup -f --show -P "$WORK/disk.img")
MNT="$WORK/mnt"
mkfs.vfat -F 16 "${LOOP}p1" >/dev/null 2>&1
mkfs.vfat -F 16 "${LOOP}p2" >/dev/null 2>&1
mount "${LOOP}p1" "$MNT"
MOUNTED=1
echo hello > "$MNT/a.txt"
mkdir -p "$MNT/sub"
echo world > "$MNT/sub/b.txt"
umount "$MNT"
MOUNTED=0

echo "== backup"
./deck-backup.sh --device "$LOOP" --out "$WORK/bk" 2>&1 | grep -E "imaging|checksums:"

echo "== destroy partition 1, then restore"
dd if=/dev/zero of="${LOOP}p1" bs=1M count=16 conv=notrunc status=none
./deck-restore.sh --in "$WORK/bk" --device "$LOOP" --force 2>&1 \
    | grep -E "checksums: OK|writing"

echo "== read the files back"
mount "${LOOP}p1" "$MNT"
MOUNTED=1
got=$(cat "$MNT/a.txt" "$MNT/sub/b.txt" | tr '\n' ' ')
umount "$MNT"
MOUNTED=0
[ "$got" = "hello world " ] || { echo "FAIL: got '$got'"; exit 1; }
echo "round-trip: OK ($got)"

echo "== tampered backup must be refused"
printf 'x' | dd of="$WORK/bk/part-$(basename "$LOOP")p1.img" bs=1 seek=100 \
    conv=notrunc status=none
if ./deck-restore.sh --in "$WORK/bk" --device "$LOOP" --check >/dev/null 2>&1; then
    echo "FAIL: tampered backup passed its checksum check"
    exit 1
fi
echo "tamper detection: OK"
