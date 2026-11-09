# Interoperability inputs: the rekordbox media format

Two generators, both **offline and key-free**:

- `make_usb.py` — builds a complete rekordbox USB export (DeviceSQL database,
  ANLZ analysis bundles, artwork, settings, sync record) whose *field values*
  are the edge cases.
- `prolink_fuzz.py` — sends Pro DJ Link datagrams to a deck on the LAN.

`pioneer_usb.py` is the byte-level format module (writer + an independent
reader); `pdb_verify.py` re-parses the output from the format definition and
checks the invariants a deck relies on.

## Why the database is not SQLite

The deck's *internal* library is a page-based key/value store ("DeviceSQL",
4096-byte pages), not SQLite. A USB export carries the page format in
`PIONEER/rekordbox/export.pdb`. Writing SQLite there produces a file no deck
mounts, which is what an earlier revision of `make_usb.py` did.

## Layout produced

```
Contents/<Artist>/<Album>/<file>.wav        audio, one tree for all playlists
PIONEER/rekordbox/export.pdb                DeviceSQL library (20 tables)
PIONEER/rekordbox/exportExt.pdb             My Tags (9 tables)
PIONEER/rekordbox/playlists3(.Plus).sync    Sync Manager record
PIONEER/USBANLZ/P###/########/ANLZ0000.*    grid, waveforms, cue lists
PIONEER/Artwork/#####/a<id>[_m].jpg         artwork
PIONEER/DEVSETTING.DAT                      display preferences
```

## Format reference

The layouts follow a genuine rekordbox 7 export and the public
[`rbxport` format reference](https://github.com/chrisle/rbxport/blob/main/docs/reference/usb-export-db.md)
(`docs/reference/usb-export-db.md`). Points that matter and are easy to get
wrong:

- page 0 header: page size, table count, next unused page, sequence;
  per table `{type, empty-candidate, first, last}` (16 bytes each);
- every table **must** start with an index page (flags `0x64`, `0x03ec` at
  `0x24`, filler `f8 ff ff 1f` from `0x3c`), or the deck reports the library
  as corrupt and hides the volume;
- the last data page of each table points at a zeroed candidate page; the
  header names candidates even past end of file (trailing zero pages are
  truncated);
- page header `0x18` is a u24: low 13 bits = row slots, top 11 bits = present
  rows. Deleted rows keep their slot, so the presence mask is sparse;
- row offsets are relative to `0x28` and grow backwards from the page end in
  36-byte groups (`base - (6 + 2r)`), with two u16 masks at `base-4`/`base-2`
  (`0x20`/`0x22` are DeviceSQL's last-write record, not row counts);
- track rows are `0x5e` fixed bytes + 21 u16 string offsets relative to the
  row start; strings are short ASCII, long ASCII (`0x40`) or UTF-16 (`0x90`),
  and long/UTF-16 strings start on a 4-byte boundary of the row.

## Verifying without a deck

```sh
cd fuzz
python3 make_usb.py --out /var/tmp/cdj-usb
python3 pdb_verify.py --root /var/tmp/cdj-usb
```

`pdb_verify.py --pdb <file>` checks any DeviceSQL database on its own. Point
it at a genuine `export.pdb` (one written by rekordbox to a stick you own) to
prove the reader agrees with the real writer:

```sh
python3 pdb_verify.py --pdb /media/USB/PIONEER/rekordbox/export.pdb
```

`../selftest.sh` runs both checks, plus the LUKS1 tester's self-test.

## Resource use

`make_usb.py` streams each track's files and keeps only the database rows in
memory: 34 tracks ≈ 14 MB on disk, flat memory. Output defaults to
`/var/tmp/cdj-usb` because `/tmp` is tmpfs (RAM) on many hosts. Use
`--tracks N` to set the exact row count; `N > 34` adds bulk tracks on top of
the 34 boundary cases.

Waveform/beat-grid sizes are clamped (`MAX_BEATS`, `MAX_WAVE_ENTRIES`): a
boundary BPM such as `0xFFFFFFFF` hundredths implies a 0 ms beat step, which
used to loop forever and grow without bound.

## What the field values cover

BPM `0 / 1 / 4000 / 49900 / 0xFFFFFFFF`, string-encoding cutovers at 124/125/
126/127 bytes, UTF-16 (`é`, CJK, emoji), rating 255, colour 255, year 65535,
u32/u16 maxima for track number, file size, disc, play count, 3 kB titles,
1 kB comments, 200-character filenames, and a playlist long enough to need
more than one page.

## Copying to a stick

FAT32 with a **Master Boot Record** partition map (not GPT). Copy the
contents of the output directory to the stick root, insert it into the deck's
top USB port, and browse `ALL EDGE CASES` / `SETS > BOUNDARY`. The deck
creates `PIONEER/rekordbox/RBFLTR.DAT` on first browse, so start from a fresh
copy for each test.
