# Pioneer Virtual Player

A faithful QEMU emulation of Pioneer CDJ / XDJ firmware, plus the tooling to recover
that firmware from a user-owned deck, so the emulator can run the genuine software and
be tested over Pro DJ Link against a Denon unit.

This is compatibility / interoperability work in the spirit of the public `cdj3k-emu`,
`cdj3k-mods`, and `OpenProLink` projects: run the real firmware in an emulator, recover
it from hardware you own, and generate edge-case media / network inputs to test how the
deck's parsers behave. No case opening, no pre-existing key.

## Primary target: XDJ-AZ

The XDJ-AZ is a CDJ-3000 player + DJM-2000 mixer all-in-one. Its firmware
(`artifacts/XDJAZv200.UPD`, v2.00) is a LUKS1 container in the same family as the
standalone CDJ-3000. Recovering its container key gives the full factory firmware: the
CDJ-3000 player, the DJM-2000 mixer, and every UI asset (the four-deck layout, palette,
icons, fonts, `ep122`) — both for the emulator and for the EngineX Pioneer skin.

The XDJ-XZ (`artifacts/XDJXZ_v126.UPD`, v1.26) is the older CDJ-2000NXS2 platform and a
**different container format** (proprietary-encrypted, trailer `XDJ-XZ\0 1.26\0`+CRC32,
not LUKS). It is a separate recovery target; the LUKS tooling here applies to the
XDJ-AZ and CDJ-3000 family.

## How it fits together

1. **Firmware recovery** (`firmware-recovery/`) — recover the container key from the
   user's own deck over its normal network / media interfaces, then decrypt.
2. **Decrypt** (`decrypt/`) — verify the recovered key and expand the factory ISO.
3. **Emulate** (`emulate/`) — boot the genuine firmware in QEMU via cdj3k-emu = the
   faithful virtual player; link it to the Denon over the LAN.
4. **Interoperability inputs** (`fuzz/`) — edge-case rekordbox libraries (USB) and ProLink
   datagrams (LAN) for testing the deck's parsers.
5. **Service access** (`service-access/`) — the open-source cdj3k-root package, for a
   standalone CDJ-3000 if one is available.

## Official update containers (latest, in `artifacts/`)

- XDJ-AZ v2.00 — `XDJAZv200.UPD` (trailer `XDJ-XZN`, LUKS1 aes-xts-plain64/sha256) — primary.
- CDJ-3000 v3.22 — `CDJ3Kv322.UPD` (trailer `XDJ-RR`, LUKS1) — same family, reference.
- XDJ-XZ v1.26 — `XDJXZ_v126.UPD` — different (non-LUKS) format, separate target.

LUKS1 container layout (verified by CRC against the official files):
`[LUKS1 header + encrypted ISO][XDJ-<code><version>\x00][CRC32-LE of all preceding bytes]`.
Pioneer LUKS1 header: big-endian fields, active-slot flag `0x00AC71F3`, dead slots
`0x0000DEAD`, payload at the header's payload offset (sector 4096).

## Layout

```
README.md                    this overview
selftest.sh                  prove the key tester + USB writer without a deck
firmware-recovery/README.md  recover the container key from user-owned hardware
decrypt/luks_slot_test.py    verified container-key tester (MATCH/miss, --selftest)
decrypt/decrypt_upd.py       recovered key -> full factory ISO
fuzz/prolink_fuzz.py         ProLink edge-case input runner (network)
fuzz/make_usb.py             rekordbox USB export builder (media)
fuzz/pioneer_usb.py          DeviceSQL + ANLZ writer, and an independent reader
fuzz/pdb_verify.py           re-parses the export and checks its invariants
fuzz/README.md               the on-media format, and the traps in it
emulate/README.md            cdj3k-emu faithful virtual player
service-access/README.md     cdj3k-root service access (standalone CDJ-3000 only)
artifacts/                   official .UPD containers + community binaries
```

## Verifying without hardware

```sh
./selftest.sh          # LUKS1 tester, then build + verify a 34-track export
```

The USB writer is checked by an independent parser, and that parser is in turn
checked against a genuine rekordbox 7 `export.pdb` (see `fuzz/README.md`), so
the structural claims are testable offline. A deck is still the final word on
whether a given firmware accepts a given edge case.
