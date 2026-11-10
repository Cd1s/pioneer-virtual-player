# Service access (cdj3k-root) — standalone CDJ-3000 only

## Back up the deck first (do this before anything else)

Anything you do to the storage of a player you do not own outright (a friend's
unit, a venue's) should start with an image. `deck-backup.sh` writes one file
per partition plus a manifest and a checksum list, and `deck-restore.sh` writes
it back — but only after every checksum matches:

```sh
./deck-backup.sh --list                                   # find the device
./deck-backup.sh --out /mnt/usb/deck-$(date +%F)          # ~= the whole storage
./deck-backup.sh --out DIR --device /dev/mmcblk0 --full   # one image instead

./deck-restore.sh --in DIR --check                        # verify only
./deck-restore.sh --in DIR --device /dev/mmcblk0 --dry-run
./deck-restore.sh --in DIR --device /dev/mmcblk0 --force
```

`test-backup-restore.sh` proves the round-trip on loop devices (build a
two-partition disk, back it up, wipe a partition, restore it, read the files
back, and confirm a tampered backup is refused).

What "restore" does and does not cover:

- **It restores storage, not firmware.** After a restore, run the official
  `.UPD` updater so the player boots a signed image and reports an official
  version again. Whether an updater accepts the version you land on (and
  whether it will go *down* a version) is the vendor's rule, not this tool's.
- **Unit-specific data lives outside the signed image.** Calibration/serial
  style data is not recreated by an official update; if it is lost, only your
  own backup brings it back. That is the reason to image first.
- **It is not a tested rollback path for a modified image.** Restoring the
  partitions of a unit that ran a modified rootfs has not been exercised on
  hardware here. Treat it as best-effort recovery, not as a warranty.
- **None of this extracts a container key.** It is an image/restore tool for
  storage you already have access to.

## With a standalone CDJ-3000

The open-source cdj3k-root project ships a prebuilt service-access update
(`artifacts/CDJ3Kv000.UPD.root`, v3.14-based) that, when installed on a **standalone
CDJ-3000**, provides SSH so you can read the container key from the deck's own update
tooling.

**Model limitation.** It is built for the standalone CDJ-3000 (product magic `XDJ-RR`,
model `CDJ3K-RK3399`). The XDJ-AZ (`XDJ-XZN`) and XDJ-XZ use different product magics
and are not yet in the community toolchain, so they will very likely refuse this
package. Use this path only with a standalone CDJ-3000.

## With a standalone CDJ-3000

1. Format a USB stick FAT32, Master Boot Record map, single partition.
2. Copy `CDJ3Kv000.UPD.root` and `fallback-authorized_keys` (the ECDSA public key) to
   its root. The `.UPD` must be the only `.UPD` on the stick.
3. Insert into the top USB port, run the firmware update (downgrades to a v3.14-based
   image and adds service access - fine on a spare unit).
4. Power cycle, then `ssh -i cdj_private.ecdsa root@<deck-ip>`.
5. Read the update tool's 32-byte container key, and verify:
   `../decrypt/luks_slot_test.py ../artifacts/CDJ3Kv322.UPD <key>` -> `MATCH`.

Keys: `fallback-authorized_keys` (public, on the stick) and `cdj_private.ecdsa`
(private, stays with you) are in `../artifacts/`.

## Tools

| file | what it does |
| --- | --- |
| `deck-backup.sh` | image the player's storage (per partition or whole device) + manifest + SHA256SUMS |
| `deck-restore.sh` | verify checksums, then write the image back (`--check`, `--dry-run`, `--force`) |
| `test-backup-restore.sh` | loop-device round-trip test for the two scripts above |
