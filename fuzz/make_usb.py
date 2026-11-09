#!/usr/bin/env python3
"""Build USB inputs for the deck's rekordbox-library parser (no key, no disassembly).

Generates an edge-case export.pdb (SQLite, the deck's DJDBCONTENT table with rows that
exercise boundary conditions: oversized string fields, out-of-range BPM values, type
mismatches) plus ANLZ beat-grid sidecars, laid out under PIONEER/rekordbox/ exactly as
the deck expects. Copy the resulting directory onto a FAT32 (Master Boot Record) USB
stick and put it in the deck's top USB port; the deck mounts and parses it.

Scope: the edge-case FIELD VALUES are the interoperability inputs; making the .pdb
mountable and parseable by the specific firmware build is the iteration step (match the
exact schema from the deck's djdb registry). This emits a strong first pass to iterate on.

Usage:
  python3 make_usb.py --out /tmp/cdj-usb --rows 60
  # then: put /tmp/cdj-usb/PIONEER onto a FAT32 MBR stick -> deck top USB port
"""
import argparse, os, random, sqlite3, struct

DJDBCONTENT_TYPES = [1,19,19,19,19,1,1,1,1,5,1,1,5,19,11,11,5,1,1,1,1,19,11,5,1,1,
                     1,19,19,1,5,1,19,1,11,11,19,19,1,19,19,19,19,19,19,19,19,19,19,19,19,19]
def rs(n): return "".join(chr(random.randint(97,122)) for _ in range(n)).encode()

def make_pdb(path, rows):
    db = sqlite3.connect(path)
    c = db.cursor()
    # The deck's track table. Column 8 = BPM (int, stored BPM*100); string columns
    # 2/3/4/13/28 are the string boundary-condition fields.
    c.execute("CREATE TABLE DJDBCONTENT (ID INTEGER, FOLDERPATH TEXT, FILENAMEL TEXT, "
              "FILENAMES TEXT, TITLE TEXT, ARTISTID INT, ALBUMID INT, GENREID INT, "
              "BPM INT, LENGTH INT, TRACKNO INT, BITRATE INT, BITDEPTH INT, COMMNT TEXT, "
              "FILETYPE INT, RATING INT, RELEASEYEAR INT, REMIXERID INT, LABELID INT, "
              "ORGARTISTID INT, KEYID INT, STOCKDATE TEXT, COLORID INT, DJPLAYCOUNT INT, "
              "IMAGEID INT, MASTERDBID INT, MASTERCONTENTID INT, ANALYSISDATAPATH TEXT, "
              "SEARCHSTR TEXT, FILESIZE INT, DISCNO INT, COMPOSERID INT, SUBTITLE TEXT, "
              "SAMPLERATE INT, PROTECTED INT, ANALYSED INT)")
    for i in range(rows):
        mode = i % 4
        if mode == 0:    # oversized string fields: TITLE / SEARCHSTR / COMMNT
            title = rs(0x2000).decode("latin1"); search = rs(0x4000).decode("latin1")
            bpm = 12000
        elif mode == 1:  # BPM out of range (BPM*100 beyond int bounds)
            title = "t"; search = "s"; bpm = random.choice([0x7FFFFFFF, -1, 0x80000000])
        elif mode == 2:  # FILENAMEL oversized + FILENAMES mismatch
            title = "t"; search = "s"; bpm = 12000
            fnl = rs(0x3000).decode("latin1")
            c.execute("INSERT INTO DJDBCONTENT VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (i, "/usb/", fnl, "x.mp3", title, 1, 1, 1, bpm, 200000, i, 320, 16,
                       rs(0x1000).decode("latin1"), 1, 0, 2020, 1, 1, 1, 1, "d", 0, 0, 0, 0, 0,
                       "ANLZ/track%d.anlz" % i, search, 1000000, 0, 1, "sub", 44100, 0, 1))
            continue
        else:            # mixed
            title = rs(0x1000).decode("latin1"); search = rs(0x2000).decode("latin1")
            bpm = 0x7FFFFFFF
        c.execute("INSERT INTO DJDBCONTENT VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (i, "/usb/", "track%d.mp3" % i, "track%d.mp3" % i, title, 1, 1, 1, bpm, 200000, i,
                   320, 16, rs(0x800).decode("latin1"), 1, 0, 2020, 1, 1, 1, 1, "d", 0, 0, 0, 0, 0,
                   "ANLZ/track%d.anlz" % i, search, 1000000, 0, 1, "sub", 44100, 0, 1))
    db.commit(); db.close()

def make_anlz(path, i):
    # malformed beat-grid sidecar: huge point count / negative loop offsets
    mode = i % 3
    if mode == 0:
        body = struct.pack("<I", 0xFFFFFFFF) + rs(64)
    elif mode == 1:
        body = struct.pack("<I", 4) + struct.pack("<i", -1) + struct.pack("<I", 0x7FFFFFFF) \
             + struct.pack("<i", 0) + struct.pack("<I", 0xFFFFFFFF)
    else:
        body = struct.pack("<I", random.randrange(1, 0x10000)) + rs(16)
    open(path, "wb").write(b"ANLZ" + struct.pack("<I", i) + body)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/cdj-usb")
    ap.add_argument("--rows", type=int, default=60)
    ap.add_argument("--seed", type=int, default=0xC0DE)
    a = ap.parse_args()
    random.seed(a.seed)
    root = os.path.join(a.out, "PIONEER")
    os.makedirs(os.path.join(root, "rekordbox"), exist_ok=True)
    os.makedirs(os.path.join(root, "rekordbox", "ANLZ"), exist_ok=True)
    make_pdb(os.path.join(root, "rekordbox", "export.pdb"), a.rows)
    for i in range(min(a.rows, 20)):
        make_anlz(os.path.join(root, "rekordbox", "ANLZ", "track%d.anlz" % i), i)
    print("wrote", root)
    print("layout:")
    for dp, dn, fn in os.walk(a.out):
        for f in fn:
            print("  ", os.path.relpath(os.path.join(dp, f), a.out))
    print("\nNEXT: format a USB stick FAT32 with a MASTER BOOT RECORD partition map "
          "(not GPT), copy the PIONEER/ folder to its root, insert into the deck's "
          "TOP USB port, and watch for a crash / hang / wrong behaviour.")

if __name__ == "__main__":
    main()
