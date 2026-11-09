#!/usr/bin/env python3
"""Build a rekordbox USB export the way a CDJ/XDJ mounts and parses it.

This writes the real on-media format, not a sample:

  Contents/<Artist>/<Album>/<file>.wav        audio (tiny valid WAV)
  PIONEER/rekordbox/export.pdb                DeviceSQL library (20 tables)
  PIONEER/rekordbox/exportExt.pdb             My Tags (9 tables)
  PIONEER/rekordbox/playlists3(.Plus).sync    Sync Manager record
  PIONEER/USBANLZ/P###/########/ANLZ0000.*    beat grid + waveforms + cues
  PIONEER/Artwork/#####/a<id>[_m].jpg         artwork
  PIONEER/DEVSETTING.DAT                      player display preferences

Every row is structurally valid, so a deck mounts the stick and browses it; the
edge cases live in the *field values* (BPM extremes, string-encoding
boundaries, 255+ row pages, color/rating/year sentinels, UTF-16 text), which is
what makes it useful as an interoperability test input.

Usage:
  python3 make_usb.py --out /var/tmp/cdj-usb            # 34 boundary tracks
  python3 make_usb.py --out /var/tmp/cdj-usb --tracks 300   # + bulk, >255-row pages
  python3 pdb_verify.py --root /var/tmp/cdj-usb         # independent check

Copy ``<out>`` onto a FAT32 (MBR) stick and insert it into the deck's top USB
port. The deck writes ``PIONEER/rekordbox/RBFLTR.DAT`` on first browse.
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import struct
import sys

import pioneer_usb as fmt

# A 1x1 baseline JPEG, used when Pillow is unavailable.
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707"
    "070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c23"
    "1c1c2837292c30313434341f27393d38323c2e333432ffc0000b0800010001010111"
    "00ffc4001f0000010501010101010100000000000000000102030405060708090a0b"
    "ffc400b5100002010303020403050504040000017d01020300041105122131410613"
    "516107227114328191a1082342b1c11552d1f02433627282090a161718191a252627"
    "28292a3435363738393a434445464748494a535455565758595a636465666768696a"
    "737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aa"
    "b2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7"
    "e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010000000000"
    "000102030405060708090a0bffc400b5110002010204040304070504040001027700"
    "0102031104052131061241510761711322328108144291a1b1c109233352f0156272"
    "d10a162434e125f11718191a262728292a35363738393a434445464748494a535455"
    "565758595a636465666768696a737475767778797a82838485868788898a92939495"
    "969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3"
    "d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda0008010100003f"
    "00fbfa28a2803fffd9"
)


def sanitize(name: str, limit: int = 48) -> str:
    for ch in '/\\:*?"<>|':
        name = name.replace(ch, "_")
    name = "".join(ch for ch in name if ord(ch) >= 0x20).strip(" .")
    return (name or "Unknown")[:limit]


def wav_silence(frames: int = 1, rate: int = 44100) -> bytes:
    data = b"\x00\x00" * 2 * frames
    header = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
              + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, rate,
                                     rate * 4, 4, 16)
              + b"data" + struct.pack("<I", len(data)))
    return header + data


def artwork_bytes(size: int) -> bytes:
    try:
        from io import BytesIO
        from PIL import Image
        image = Image.new("RGB", (size, size), (24, 28, 36))
        buf = BytesIO()
        image.save(buf, "JPEG", quality=70)
        return buf.getvalue()
    except Exception:
        return TINY_JPEG


class Case:
    """One boundary-condition track."""

    def __init__(self, name: str, title: str, *, bpm: int = 12000,
                 rating: int = 0, color: int = 0, year: int = 0,
                 track_number: int = 0, duration: int = 200,
                 comment: str = "", isrc: str = "", hot_cue: bool = False,
                 artist: str = "Edge Artist", album: str = "Edge Album",
                 genre: str = "Electronic", key: str = "8A",
                 label: str = "Edge Label", file_size: int = 0,
                 disc: int = 0, play_count: int = 0, depth: int = 16):
        self.name = name
        self.title = title
        self.bpm = bpm
        self.rating = rating
        self.color = color
        self.year = year
        self.track_number = track_number
        self.duration = duration
        self.comment = comment
        self.isrc = isrc
        self.hot_cue = hot_cue
        self.artist = artist
        self.album = album
        self.genre = genre
        self.key = key
        self.label = label
        self.file_size = file_size
        self.disc = disc
        self.play_count = play_count
        self.depth = depth


def boundary_cases() -> list[Case]:
    """Deterministic cases, ordered so the first few are the interesting ones."""
    long_cut = fmt.max_row_len() - 512
    cases = [
        Case("plain", "Plain Track"),
        # String-encoding switch: short ASCII below 126 bytes, long ASCII at or
        # above it, UTF-16 for anything non-ASCII.
        Case("len-0", ""),
        Case("len-1", "a"),
        Case("len-124", "s" * 124),
        Case("len-125", "s" * 125),
        Case("len-126", "s" * 126),
        Case("len-127", "s" * 127),
        Case("utf16-umlaut", "Café del Mar"),
        Case("utf16-cjk", "夜のドライブ"),
        Case("utf16-emoji", "Test 🎛️ Track"),
        Case("oversize-title", "T" * min(3000, long_cut)),
        # BPM is u32 and stored x100.
        Case("bpm-0", "BPM zero", bpm=0),
        Case("bpm-1", "BPM one", bpm=1),
        Case("bpm-40", "BPM 40.00", bpm=4000),
        Case("bpm-499", "BPM 499.00", bpm=49900),
        Case("bpm-max", "BPM max u32", bpm=0xFFFFFFFF),
        # Other field sentinels.
        Case("rating-255", "Rating 255", rating=255),
        Case("color-255", "Color 255", color=255),
        Case("year-65535", "Year 65535", year=0xFFFF),
        Case("trackno-max", "Track number max", track_number=0xFFFFFFFF),
        Case("duration-max", "Duration max", duration=0xFFFF),
        Case("filesize-max", "File size max", file_size=0xFFFFFFFF),
        Case("disc-max", "Disc max", disc=0xFFFF),
        Case("playcount-max", "Play count max", play_count=0xFFFF),
        Case("depth-32", "Bit depth 32", depth=32),
        Case("isrc", "With ISRC", isrc="GBAYE0000001"),
        Case("comment-long", "Long comment", comment="C" * 1000),
        Case("hotcue-on", "Hot cue auto load", hot_cue=True),
        Case("artist-utf16", "UTF-16 artist", artist="Ångström 東京"),
        Case("album-long", "Long album", album="A" * 160),
        Case("genre-utf16", "UTF-16 genre", genre="Électronique"),
        Case("key-utf16", "UTF-16 key", key="ラ"),
        Case("label-long", "Long label", label="L" * 180),
        Case("filename-200", "Long filename"),
    ]
    return cases


def build(out: str, tracks: int, seed: int, verify: bool) -> int:
    random.seed(seed)
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)

    cases = boundary_cases()
    if tracks <= len(cases):
        cases = cases[:tracks]
    while len(cases) < tracks:
        i = len(cases)
        cases.append(Case(f"bulk-{i}", f"Bulk track {i}", bpm=10000 + (i * 37) % 20000,
                          rating=i % 6, color=i % 9, year=1990 + i % 40,
                          track_number=i + 1, duration=60 + i % 900))

    library = fmt.Library(created_date="2026-11-09", device_name="CDJ-USB-TEST")
    library.artists = [(1, "Edge Artist"), (2, "Ångström 東京")]
    library.albums = [(1, 1, "Edge Album"), (2, 1, "A" * 160),
                      (3, 2, "Café del Mar (Extended Mix)")]
    library.genres = [(1, "Electronic"), (2, "Électronique")]
    library.labels = [(1, "Edge Label"), (2, "L" * 180)]
    library.keys = [(1, "8A"), (2, "ラ")]
    library.artworks = [(1, "/PIONEER/Artwork/00001/a1.jpg"),
                        (2, "/PIONEER/Artwork/00001/a2.jpg")]

    for i, case in enumerate(cases, start=1):
        safe = sanitize(case.name, 120)
        filename = f"{i:03d}-{safe}.wav"
        if case.name == "filename-200":
            filename = ("f" * 200) + ".wav"
        audio_path = f"/Contents/{sanitize(case.artist)}/{sanitize(case.album)}/{filename}"
        analysis_dir = fmt.analysis_path(audio_path)
        analyze_path = analysis_dir + "ANLZ0000.DAT"
        audio_file = os.path.join(out, audio_path.lstrip("/"))
        os.makedirs(os.path.dirname(audio_file), exist_ok=True)
        with open(audio_file, "wb") as fh:
            fh.write(wav_silence())
        size = os.path.getsize(audio_file)

        track = fmt.TrackRow(
            id=i, title=case.title, filename=filename, file_path=audio_path,
            analyze_path=analyze_path, artist_id=2 if "utf16" in case.name else 1,
            album_id=3 if "utf16" in case.name else (2 if case.album.startswith("A") else 1),
            genre_id=2 if "utf16" in case.name else 1,
            key_id=2 if "utf16" in case.name else 1,
            label_id=2 if case.label.startswith("L") else 1,
            artwork_id=1 + (i % 2), color_id=case.color, rating=case.rating,
            bpm_x100=case.bpm, duration_sec=case.duration, year=case.year,
            bitrate=320, sample_rate=44100, file_size=case.file_size or size,
            track_number=case.track_number, play_count=case.play_count,
            disc_number=case.disc, sample_depth=case.depth,
            master_db_id=library.master_db_id, master_content_id=1000 + i,
            comment=case.comment, date_added="2026-11-09",
            date_created="2026-11-01", release_date="2024",
            mix_name="Extended Mix" if "utf16" in case.name else "",
            isrc=case.isrc, hot_cue_auto_load=case.hot_cue)
        library.tracks.append(track)

        anlz = fmt.make_anlz(audio_path, max(case.bpm, 1),
                             max(case.duration, 1) * 1000)
        anlz_dir = os.path.join(out, analysis_dir.lstrip("/"))
        os.makedirs(anlz_dir, exist_ok=True)
        for ext, data in anlz.items():
            with open(os.path.join(anlz_dir, f"ANLZ0000.{ext}"), "wb") as fh:
                fh.write(data)

    library.playlists = [
        {"id": 1, "parent": 0, "sort": 0, "name": "ALL EDGE CASES"},
        {"id": 2, "parent": 0, "sort": 1, "name": "SETS", "folder": True},
        {"id": 3, "parent": 2, "sort": 0, "name": "BOUNDARY"},
    ]
    library.entries = [(i, i, 1) for i in range(1, len(cases) + 1)]
    library.entries += [(i, i, 3) for i in range(1, min(10, len(cases)) + 1)]

    root = os.path.join(out, "PIONEER")
    os.makedirs(os.path.join(root, "rekordbox"), exist_ok=True)
    with open(os.path.join(root, "rekordbox", "export.pdb"), "wb") as fh:
        fh.write(library.build_export_pdb())
    tags = [(1, "Peak Time", 0), (2, "Warm Up", 0), (3, "Vocals", 1)]
    memberships = [(i, 1 + (i % 2)) for i in range(1, len(cases) + 1)]
    with open(os.path.join(root, "rekordbox", "exportExt.pdb"), "wb") as fh:
        fh.write(library.build_export_ext_pdb(tags, memberships))
    sync = fmt.sync_record(library.master_db_id, library.playlists,
                           timestamp_ms=0)
    for name in ("playlists3.sync", "playlists3Plus.sync"):
        with open(os.path.join(root, "rekordbox", name), "wb") as fh:
            fh.write(sync)
    with open(os.path.join(root, "DEVSETTING.DAT"), "wb") as fh:
        fh.write(fmt.devsetting())

    art_dir = os.path.join(root, "Artwork", "00001")
    os.makedirs(art_dir, exist_ok=True)
    for image_id in (1, 2):
        for prefix in ("a", "b"):
            with open(os.path.join(art_dir, f"{prefix}{image_id}.jpg"), "wb") as fh:
                fh.write(artwork_bytes(80))
            with open(os.path.join(art_dir, f"{prefix}{image_id}_m.jpg"), "wb") as fh:
                fh.write(artwork_bytes(240))

    with open(os.path.join(out, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write(
            "Pioneer virtual player -- edge-case rekordbox USB export\n"
            "========================================================\n\n"
            "Every row is structurally valid; the boundary values are the test\n"
            "input. Mount this directory (or a FAT32 MBR stick holding it) on a\n"
            "CDJ/XDJ and browse ALL EDGE CASES / SETS > BOUNDARY.\n\n"
            "Boundary fields: BPM 0/1/4000/49900/0xffffffff, string-encoding\n"
            "cutovers at 125/126 bytes, UTF-16 titles/artists (/\\u00e9, CJK, emoji),\n"
            "rating 255, color 255, year 65535, track number/size/disc/play count\n"
            "at u32/u16 maxima, 3 kB titles, 1 kB comments, and a playlist long\n"
            "enough to overflow a DeviceSQL page.\n\n"
            "Verify without a deck:  python3 pdb_verify.py --root .\n")

    print(f"wrote {out}")
    print(f"  tracks: {len(library.tracks)}  playlists: {len(library.playlists)}"
          f"  entries: {len(library.entries)}")
    print("  layout:")
    for dirpath, _dirnames, filenames in os.walk(out):
        for name in filenames:
            path = os.path.join(dirpath, name)
            print(f"    {os.path.relpath(path, out)}  ({os.path.getsize(path)} bytes)")

    if verify:
        data = open(os.path.join(root, "rekordbox", "export.pdb"), "rb").read()
        pdb = fmt.Pdb(data)
        issues = pdb.verify()
        print(f"  export.pdb: {len(data)} bytes, {len(pdb.tables)} tables,"
              f" {len(pdb.tracks())} tracks")
        if issues:
            for issue in issues:
                print(f"  VERIFY FAIL: {issue}")
            return 1
        print("  export.pdb structural verification: OK")
    print("\nNEXT: copy this directory onto a FAT32 stick with a MASTER BOOT RECORD "
          "partition map (not GPT), insert into the deck's TOP USB port, and browse "
          "ALL EDGE CASES / SETS > BOUNDARY.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/var/tmp/cdj-usb",
                        help="output directory (default: /var/tmp/cdj-usb; "
                             "avoid /tmp, which is tmpfs/RAM on many hosts)")
    parser.add_argument("--tracks", "--rows", dest="tracks", type=int, default=60,
                        help="number of tracks (300+ forces >255-row pages)")
    parser.add_argument("--seed", type=int, default=0xC0DE)
    parser.add_argument("--no-verify", action="store_true")
    args = parser.parse_args()
    return build(args.out, max(args.tracks, 1), args.seed, not args.no_verify)


if __name__ == "__main__":
    sys.exit(main())
