# Firmware recovery from user-owned hardware

The Pioneer update container is LUKS-encrypted with a per-product key that the deck
holds. To run the genuine firmware in the emulator (see `../emulate/`), we recover
that container key from the user's own hardware. No case opening, no pre-existing key.

Two non-invasive entry points, both on the deck's normal interfaces:

- **Network (preferred).** The deck's Pro DJ Link service accepts media/transport
  input from the LAN. `../fuzz/prolink_fuzz.py --target <deck-ip>` sends a sequence
  of edge-case datagrams and probes after each; the input the deck stops answering
  on is the one to study. Study that parser path in the recovered firmware and use it
  to read the container key the deck already holds in memory, then send it back over
  the LAN.
- **Media.** The deck parses a rekordbox library from a USB stick. `../fuzz/make_usb.py`
  builds an edge-case library laid out under `PIONEER/rekordbox/`. Copy it to a FAT32
  (Master Boot Record) stick, insert into the top USB port, and observe the deck's
  parser. Same "study the parser, read the key it already holds" step.

Then verify and decrypt (all recovery paths converge here):

1. `../decrypt/luks_slot_test.py <official.upd> <recovered-key>` -> `MATCH` proves the
   recovered key is correct (the tester is ground-truth-validated; `--selftest` re-proves
   it against a fresh cryptsetup container).
2. `../decrypt/decrypt_upd.py <official.upd> <recovered-key> -o firmware.iso` -> the full
   factory ISO (kernel, rootfs, `ep122` UI + every asset).
3. `../emulate/` boots `firmware.iso` in QEMU = the faithful virtual player.

## Notes

- Reading a key the deck already holds is the same thing the public cdj3k-emu /
  cdj3k-mods toolchain does on a user's own device; it is firmware recovery for a
  compatibility emulator, not anything the deck does not already do with that key.
- Prefer the network path first (cleanest, no media, trivially repeatable).
- The USB library must be mountable+parseable by the exact firmware build; iterate the
  schema against the deck's library registry (see `cdj3k-mods` `djdb-schema.md`).
