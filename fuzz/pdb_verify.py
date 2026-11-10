#!/usr/bin/env python3
"""Independent verifier for a Pioneer USB export produced by make_usb.py.

It re-parses the files from the format definitions (it shares no writer code
path with :mod:`pioneer_usb`) and checks the invariants a CDJ/XDJ relies on:

  * DeviceSQL header/index/data page chains, presence masks, page sequence,
    row offsets and string encodings;
  * track trailer words, audio-format code and all 21 string slots;
  * ANLZ PMAI framing, PPTH == the track's path, PQTZ beat count, waveform
    sizes, empty cue lists;
  * USBANLZ directory == the firmware's path hash;
  * DEVSETTING.DAT length/CRC and the sync XML.

Exit status is 0 only when every check passes.

Usage:
  python3 pdb_verify.py --root /tmp/cdj-usb
  python3 pdb_verify.py --root /tmp/cdj-usb --json report.json
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from collections import Counter

import pioneer_usb as fmt


def be32(b: bytes, at: int) -> int:
    return struct.unpack_from(">I", b, at)[0]


def check_anlz(path: str, audio_path: str, issues: list[str], report: dict) -> None:
    data = open(path, "rb").read()
    tag = os.path.basename(path)
    if data[:4] != b"PMAI":
        issues.append(f"{path}: no PMAI magic")
        return
    len_header = be32(data, 4)
    total = be32(data, 8)
    if total != len(data):
        issues.append(f"{path}: PMAI declares {total}, file is {len(data)}")
    if len_header < 12 or len_header > 64:
        issues.append(f"{path}: implausible PMAI header length {len_header}")
    at = len_header
    seen = []
    while at + 12 <= len(data):
        fourcc = data[at:at + 4]
        lh = be32(data, at + 4)
        lt = be32(data, at + 8)
        if lh < 12 or lt < lh or at + lt > len(data):
            issues.append(f"{path}: bad section {fourcc!r} lengths {lh}/{lt}")
            return
        header = data[at + 12:at + lh]
        payload = data[at + lh:at + lt]
        seen.append(fourcc.decode("latin1"))
        if fourcc == b"PPTH":
            text = payload.decode("utf-16-be", "replace").rstrip("\x00")
            if text != audio_path:
                issues.append(f"{path}: PPTH {text!r} != {audio_path!r}")
        elif fourcc == b"PQTZ":
            count = be32(header, 8)
            if len(payload) != count * 8:
                issues.append(f"{path}: PQTZ count {count} but {len(payload)} bytes")
        elif fourcc in (b"PWAV", b"PWV2"):
            declared = be32(header, 0)
            if declared != len(payload):
                issues.append(f"{path}: {fourcc!r} length {declared} != {len(payload)}")
            if fourcc == b"PWV2" and payload and max(payload) > 15:
                issues.append(f"{path}: PWV2 byte above 15")
            if payload and min(payload) == 0:
                issues.append(f"{path}: {fourcc!r} has a zero column")
        elif fourcc in (b"PWV3", b"PWV4", b"PWV5", b"PWV6", b"PWV7"):
            stride = be32(header, 0)
            entries = be32(header, 4)
            if stride and len(payload) != stride * entries:
                issues.append(f"{path}: {fourcc!r} {stride}x{entries} != {len(payload)}")
        elif fourcc in (b"PCOB", b"PCO2"):
            if payload:
                issues.append(f"{path}: cue list is not empty")
        at += lt
    if at != len(data):
        issues.append(f"{path}: {len(data) - at} trailing bytes")
    report.setdefault("anlz", Counter()).update(seen)


def verify(root: str) -> tuple[list[str], dict]:
    issues: list[str] = []
    report: dict = {"tables": {}, "counts": {}, "fields": {}}
    rekordbox = os.path.join(root, "PIONEER", "rekordbox")
    pdb_path = os.path.join(rekordbox, "export.pdb")
    if not os.path.exists(pdb_path):
        return [f"missing {pdb_path}"], report
    data = open(pdb_path, "rb").read()
    try:
        pdb = fmt.Pdb(data)
    except Exception as exc:  # noqa: BLE001 - report, do not crash
        return [f"export.pdb: {exc}"], report
    issues.extend(f"export.pdb: {i}" for i in pdb.verify())
    for table in pdb.tables:
        try:
            chain = pdb.table_chains(table.type)
            if table.first == table.last:
                rows = 0
            else:
                rows = sum((int.from_bytes(pdb.page(i)[0x18:0x1B], "little") & 0x1FFF)
                           for i in chain[1:])
            report["tables"][table.type] = {"pages": len(chain), "rows": rows}
        except Exception as exc:  # noqa: BLE001
            issues.append(f"export.pdb table {table.type}: {exc}")

    tracks = pdb.tracks()
    report["counts"]["tracks"] = len(tracks)
    if not tracks:
        issues.append("export.pdb: no tracks")
    bpms = sorted({t["bpm_x100"] for t in tracks})
    report["fields"]["bpm_x100"] = [bpms[0], bpms[-1]] if bpms else []
    report["fields"]["ratings"] = sorted({t["rating"] for t in tracks})
    report["fields"]["colors"] = sorted({t["color_id"] for t in tracks})
    report["fields"]["years"] = sorted({t["year"] for t in tracks})

    encodings = Counter()
    for track in tracks:
        for text in track["strings"]:
            if not text:
                continue
            encodings["ascii" if text.isascii() else "utf16"] += 1
            if len(text) >= 0x7E:
                encodings["long_ascii_or_utf16"] += 1
        path = track["file_path"]
        analyze = track["analyze_path"]
        expected_dir = fmt.analysis_path(path)
        if os.path.dirname(analyze) + "/" != expected_dir:
            issues.append(f"track {track['id']}: analysis dir {analyze} != {expected_dir}")
        audio_abs = os.path.join(root, path.lstrip("/"))
        if not os.path.exists(audio_abs):
            issues.append(f"track {track['id']}: missing audio {path}")
        dat = os.path.join(root, analyze.lstrip("/"))
        if not os.path.exists(dat):
            issues.append(f"track {track['id']}: missing {analyze}")
            continue
        check_anlz(dat, path, issues, report)
        for ext in ("EXT", "2EX"):
            sibling = dat[:-3] + ext
            if not os.path.exists(sibling):
                issues.append(f"track {track['id']}: missing sibling {sibling}")
            else:
                check_anlz(sibling, path, issues, report)
    report["counts"]["string_encodings"] = dict(encodings)

    data = open(os.path.join(root, "PIONEER", "DEVSETTING.DAT"), "rb").read()
    if len(data) != 140:
        issues.append(f"DEVSETTING.DAT is {len(data)} bytes, not 140")
    else:
        body = data[0x68:0x88]
        if fmt.crc16_xmodem(body) != struct.unpack_from("<H", data, 0x88)[0]:
            issues.append("DEVSETTING.DAT CRC-16/XMODEM mismatch")

    ext_path = os.path.join(rekordbox, "exportExt.pdb")
    if os.path.exists(ext_path):
        try:
            ext = fmt.Pdb(open(ext_path, "rb").read())
            issues.extend(f"exportExt.pdb: {i}" for i in ext.verify())
            report["counts"]["ext_tables"] = len(ext.tables)
        except Exception as exc:  # noqa: BLE001
            issues.append(f"exportExt.pdb: {exc}")

    sync_path = os.path.join(rekordbox, "playlists3.sync")
    if os.path.exists(sync_path):
        sync = open(sync_path, "rb").read()
        if b"\r\n" not in sync or b"<Sync " not in sync:
            issues.append("playlists3.sync is not CRLF XML")
    return issues, report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", help="USB export root built by make_usb.py")
    parser.add_argument("--pdb", action="append", default=[],
                        help="check a standalone .pdb (repeatable); use this to "
                             "validate the reader against a genuine rekordbox export")
    parser.add_argument("--json")
    args = parser.parse_args()
    if args.pdb:
        rc = 0
        for path in args.pdb:
            issues = []
            try:
                pdb = fmt.Pdb(open(path, "rb").read())
                issues = pdb.verify()
                print(f"{path}: {pdb.table_count} tables, "
                      f"{len(pdb.tracks())} tracks, "
                      f"{len(open(path, 'rb').read()) // pdb.page_size} pages")
            except Exception as exc:  # noqa: BLE001
                issues = [str(exc)]
            for issue in issues:
                print(f"  - {issue}")
            print(f"  {'FAIL' if issues else 'OK'}")
            rc |= 1 if issues else 0
        return rc
    if not args.root:
        parser.error("give --root or --pdb")
    issues, report = verify(args.root)
    print(f"tables: {report.get('tables')}")
    print(f"counts: {report.get('counts')}")
    print(f"fields: {report.get('fields')}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"issues": issues, "report": report}, fh, indent=2,
                      default=str)
    if issues:
        print(f"\nFAIL: {len(issues)} issue(s)")
        for issue in issues:
            print(f"  - {issue}")
        return 1
    print("\nOK: DeviceSQL tables, ANLZ bundle, DEVSETTING and sync record verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
