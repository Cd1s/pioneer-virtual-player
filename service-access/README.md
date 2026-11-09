# Service access (cdj3k-root) — standalone CDJ-3000 only

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
