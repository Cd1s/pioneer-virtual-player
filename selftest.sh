#!/bin/sh
# Self-check everything that can be proven without a deck.
#
#   1. the LUKS1 key tester against a freshly built cryptsetup container
#   2. the rekordbox USB export writer against the independent verifier
#   3. (optional) the DeviceSQL reader against a genuine export.pdb
#
# Usage:
#   ./selftest.sh                        # 1 + 2
#   PIONEER_REFERENCE_PDB=/path/export.pdb ./selftest.sh   # also 3
#
# Output goes to $OUT (default /var/tmp/pioneer-selftest) on the disk, and the
# generator runs under a 1 GiB address-space cap: /tmp is tmpfs on many hosts
# and an unbounded generator there takes the whole machine down with it.
set -e
cd "$(dirname "$0")"

OUT=${OUT:-/var/tmp/pioneer-selftest}
TRACKS=${TRACKS:-34}
LIMIT=${LIMIT:-1073741824}

echo "== 1/3 LUKS1 key tester (cryptsetup reference container)"
python3 decrypt/luks_slot_test.py --selftest

echo "== 2/3 rekordbox USB export + independent verifier ($TRACKS tracks)"
rm -rf "$OUT"
mkdir -p "$OUT"
(
    cd fuzz
    if command -v prlimit >/dev/null 2>&1; then
        prlimit --as="$LIMIT" -- python3 make_usb.py --out "$OUT" --tracks "$TRACKS"
    else
        python3 make_usb.py --out "$OUT" --tracks "$TRACKS"
    fi
    python3 pdb_verify.py --root "$OUT"
)
du -sh "$OUT"

if [ -n "${PIONEER_REFERENCE_PDB:-}" ]; then
    echo "== 3/3 reader vs a genuine rekordbox export"
    (cd fuzz && python3 pdb_verify.py --pdb "$PIONEER_REFERENCE_PDB")
else
    echo "== 3/3 reader vs a genuine rekordbox export: skipped"
    echo "   point PIONEER_REFERENCE_PDB at any export.pdb written by rekordbox"
    echo "   (e.g. from a USB stick you exported) to run this check."
fi

echo "selftest: OK"
