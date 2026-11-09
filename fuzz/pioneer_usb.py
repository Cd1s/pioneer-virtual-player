#!/usr/bin/env python3
"""Pioneer USB export format: DeviceSQL `export.pdb`, ANLZ, DEVSETTING.DAT.

This module is the byte-level implementation of the rekordbox USB export that a
CDJ/XDJ reads:

* ``export.pdb`` / ``exportExt.pdb`` -- the DeviceSQL page format (not SQLite):
  fixed 4096-byte pages, a header page naming the tables, per-table index page +
  data pages, rows in a forward heap and row offsets in a backward index with a
  presence bitmap.
* ``PIONEER/USBANLZ/P###/########/ANLZ0000.{DAT,EXT,2EX}`` -- the PMAI-tagged
  analysis bundle (beat grid, preview/detail/colour waveforms, cue lists).
* ``PIONEER/DEVSETTING.DAT`` and ``PIONEER/rekordbox/playlists3*.sync``.

The layouts follow the rekordbox 7 reference in
``rbxport/docs/reference/usb-export-db.md`` and the row encoders in
``rbxport/crates/rbl-pdb``. This file is a from-scratch Python port, and
:class:`Pdb` is a separate reader, so ``pdb_verify.py`` checks the writer
against an independent parser instead of against itself.

No key, no firmware and no hardware are involved: this is the on-media format
only.
"""
from __future__ import annotations

import datetime
import struct
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence

# ---------------------------------------------------------------------------
# DeviceSQL constants
# ---------------------------------------------------------------------------

PAGE_SIZE = 4096
PAGE_HEADER_LEN = 0x28
ROW_GROUP_LEN = 0x24
NO_PAGE = 0x03FF_FFFF

#: ``export.pdb`` table types, in table order.
TABLE_TRACKS = 0
TABLE_GENRES = 1
TABLE_ARTISTS = 2
TABLE_ALBUMS = 3
TABLE_LABELS = 4
TABLE_KEYS = 5
TABLE_COLORS = 6
TABLE_PLAYLIST_TREE = 7
TABLE_PLAYLIST_ENTRIES = 8
TABLE_ARTWORK = 13
TABLE_COLUMNS = 16
TABLE_CATEGORIES = 17
TABLE_SORTS = 18
TABLE_PROPERTY = 19

#: Every table the reference export carries, including the empty ones.
EXPORT_TABLES = [
    TABLE_TRACKS, TABLE_GENRES, TABLE_ARTISTS, TABLE_ALBUMS, TABLE_LABELS,
    TABLE_KEYS, TABLE_COLORS, TABLE_PLAYLIST_TREE, TABLE_PLAYLIST_ENTRIES,
    9, 10, 11, 12, TABLE_ARTWORK, 14, 15, TABLE_COLUMNS, TABLE_CATEGORIES,
    TABLE_SORTS, TABLE_PROPERTY,
]

#: ``exportExt.pdb`` (My Tags) table types.
EXT_TABLES = [0, 1, 2, 3, 4, 5, 6, 7, 8]


def groups_for(rows: int) -> int:
    return (rows + 15) // 16


def max_row_len(page_size: int = PAGE_SIZE) -> int:
    """The longest row that still fits a page (see rbl-pdb ``max_row_len``)."""
    return page_size - (PAGE_HEADER_LEN + ROW_GROUP_LEN + 8)


def u2(b: bytes, at: int) -> int:
    return struct.unpack_from("<H", b, at)[0]


def u4(b: bytes, at: int) -> int:
    return struct.unpack_from("<I", b, at)[0]


def p16(v: int) -> bytes:
    return struct.pack("<H", v & 0xFFFF)


def p32(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


def align4(n: int) -> int:
    return (n + 3) & ~3


# ---------------------------------------------------------------------------
# DeviceSQL strings
# ---------------------------------------------------------------------------

def short_ascii(text: str) -> bytes:
    """Short ASCII: ``(len+1)*2+1``, then the text, no terminator."""
    raw = text.encode("ascii")
    return bytes([min((len(raw) + 1) * 2 + 1, 0xFF)]) + raw


def long_ascii(text: str) -> bytes:
    """Long ASCII: ``0x40``, a u16 length counting the 4 header bytes."""
    raw = text.encode("utf-8")
    return b"\x40" + p16(4 + len(raw)) + b"\x00" + raw


def long_utf16le(text: str) -> bytes:
    """UTF-16LE: ``0x90``, a u16 length counting header and text, no NUL."""
    units = text.encode("utf-16-le")
    return b"\x90" + p16(4 + len(units)) + b"\x00" + units


def device_sql_string(text: str) -> bytes:
    """Pick the encoding the deck uses: short, long ASCII, or UTF-16LE."""
    if text.isascii():
        return short_ascii(text) if len(text) < 0x7E else long_ascii(text)
    return long_utf16le(text)


def push_string(row: bytearray, text: str) -> int:
    """Append ``text`` to a row and return its row-relative offset.

    A long ASCII or UTF-16 string starts on a four-byte boundary of the row
    (zero bytes fill the gap), which is how the deck's own rows lay out.
    """
    encoded = device_sql_string(text)
    if encoded[0] in (0x40, 0x90):
        while len(row) % 4:
            row.append(0)
    at = len(row)
    row.extend(encoded)
    return at


# ---------------------------------------------------------------------------
# DeviceSQL pages
# ---------------------------------------------------------------------------

class PageStyle:
    """How a data page is headed at 0x20/0x22 and what its masks say."""

    COUNTED = "counted"
    LAST_ROW_INDEX = "last_row_index"


class PageBuilder:
    """One page: rows grow forward from the header, offsets backward."""

    def __init__(self, page_size: int = PAGE_SIZE, page_index: int = 0,
                 page_type: int = 0, next_page: int = 0,
                 style: str = PageStyle.COUNTED) -> None:
        self.page_size = page_size
        self.page_index = page_index
        self.page_type = page_type
        self.next_page = next_page
        self.style = style
        self.heap = bytearray()
        self.offsets: list[int] = []

    def free_space(self) -> int:
        groups = max(groups_for(len(self.offsets)), 1)
        return (self.page_size - PAGE_HEADER_LEN - align4(len(self.heap))
                - groups * ROW_GROUP_LEN)

    def fits(self, length: int) -> bool:
        if self.style == PageStyle.COUNTED:
            return self.free_space() >= length + 8
        heap = align4(len(self.heap)) + length
        return (PAGE_HEADER_LEN + heap
                + groups_for(len(self.offsets) + 1) * ROW_GROUP_LEN
                <= self.page_size)

    def push_row(self, row: bytes) -> int:
        while len(self.heap) % 4:
            self.heap.append(0)
        at = len(self.heap)
        self.heap.extend(row)
        self.offsets.append(at)
        return at

    def finish(self, sequence: int = 0) -> bytes:
        page = bytearray(self.page_size)
        n = len(self.offsets)
        last_row_marked = self.style == PageStyle.LAST_ROW_INDEX or n > 0xFF

        page[0x04:0x08] = p32(self.page_index)
        page[0x08:0x0C] = p32(self.page_type)
        page[0x0C:0x10] = p32(self.next_page)
        page[0x10:0x14] = p32(sequence)
        packed = (n & 0x1FFF) | ((n & 0x7FF) << 13)
        page[0x18:0x1B] = packed.to_bytes(4, "little")[:3]
        page[0x1B] = 0x24  # ordinary data page
        used = align4(len(self.heap))
        free = (self.page_size - PAGE_HEADER_LEN - used
                - 2 * n - 4 * groups_for(n))
        page[0x1C:0x1E] = p16(free)
        page[0x1E:0x20] = p16(used)
        if last_row_marked:
            page[0x20:0x22] = p16(1)
            page[0x22:0x24] = p16(max(n - 1, 0))
        else:
            page[0x20:0x22] = p16(n)

        end = min(PAGE_HEADER_LEN + len(self.heap), self.page_size)
        page[PAGE_HEADER_LEN:end] = self.heap[:end - PAGE_HEADER_LEN]

        groups = groups_for(n)
        for group in range(groups):
            base = self.page_size - group * ROW_GROUP_LEN
            in_group = 16 if group < groups - 1 else n - group * 16
            present = 0
            for row in range(in_group):
                present |= 1 << row
                off = self.offsets[group * 16 + row] if group * 16 + row < n else 0
                at = base - (6 + 2 * row)
                if at + 2 <= self.page_size:
                    page[at:at + 2] = p16(off)
            if not last_row_marked:
                second = present
            elif group == groups - 1:
                second = 1 << (in_group - 1)
            else:
                second = 0
            for at, word in ((base - 4, present), (base - 2, second)):
                if at + 2 <= self.page_size:
                    page[at:at + 2] = p16(word)
        return bytes(page)


def index_page(page_size: int, page_index: int, page_type: int,
               next_page: int, first_data_page: int | None,
               sequence: int = 1) -> bytes:
    """The page every table must start with (flags 0x64, no rows).

    rekordbox reads a table that starts with a data page as a corrupt
    library, so this page is mandatory even for empty tables.  Layout
    follows a rekordbox 7 export byte for byte: body word 0x2c names the
    first data page (0x03ffffff when there is none), and 1004 copies of
    ``f8 ff ff 1f`` fill the rest from 0x3c.
    """
    page = bytearray(page_size)
    page[0x04:0x08] = p32(page_index)
    page[0x08:0x0C] = p32(page_type)
    page[0x0C:0x10] = p32(next_page)
    page[0x10:0x14] = p32(sequence)
    page[0x1B] = 0x64
    page[0x20:0x22] = p16(0x1FFF)
    page[0x22:0x24] = p16(0x1FFF)
    page[0x24:0x26] = p16(0x03EC)
    body = PAGE_HEADER_LEN
    page[body:body + 4] = p32(page_index)
    page[body + 4:body + 8] = p32(first_data_page if first_data_page is not None
                                   else NO_PAGE)
    page[body + 8:body + 12] = p32(NO_PAGE)
    page[body + 12:body + 16] = p32(0)
    page[body + 16:body + 18] = p16(0)
    page[body + 18:body + 20] = p16(0x1FFF)
    at = body + 20
    for _ in range(1004):
        if at + 4 > page_size:
            break
        page[at:at + 4] = b"\xf8\xff\xff\x1f"
        at += 4
    return bytes(page)


class FileBuilder:
    """A whole DeviceSQL file: header page, then each table's page chain."""

    def __init__(self, page_size: int = PAGE_SIZE) -> None:
        self.page_size = page_size
        self.tables: list[tuple[int, list[bytes]]] = []
        self.write_order: list[int] = []

    def add_table(self, page_type: int, rows: Sequence[bytes],
                  style: str = PageStyle.COUNTED,
                  number=None) -> None:
        """Add a table. ``number(row, slot) -> row`` may stamp the row's slot."""
        pages: list[bytes] = []
        builder = PageBuilder(self.page_size, page_type=page_type, style=style)
        for row in rows:
            if not builder.fits(len(row)):
                pages.append(builder.finish())
                builder = PageBuilder(self.page_size, page_type=page_type,
                                      style=style)
            if number is not None:
                row = number(bytes(row), len(builder.offsets))
            builder.push_row(row)
        pages.append(builder.finish())
        self.tables.append((page_type, pages))

    def _order(self, page_type: int) -> tuple[int, int]:
        """Physical page order: the property table first, then table order."""
        if page_type in self.write_order:
            return (0, self.write_order.index(page_type))
        if not self.write_order and page_type == TABLE_PROPERTY:
            return (0, 0)
        return (1, page_type)

    def finish(self) -> bytes:
        """Header page, then one index/data/candidate page group per table.

        Allocation follows the reference export: for every table in table
        order an index page plus either its first data page or a spare page;
        then the remaining data pages (property table first, as the reference
        does); then one zeroed empty-candidate page per table.  The header
        names those candidates even though the trailing all-zero pages are
        truncated, exactly as rekordbox writes it.
        """
        page_size = self.page_size
        tables = sorted(self.tables, key=lambda item: self._order(item[0]))

        # Pass 1: index page + first data page (or a spare) per table.
        groups: list[dict] = []
        total = 1                                   # page 0 is the header
        for page_type, rows in tables:
            data = [r for r in rows
                    if bytes(r)[0x18:0x1B] != b"\x00\x00\x00"]
            index_no = total
            total += 1
            if data:
                first_data = total
                total += 1
                spare = None
            else:
                first_data = NO_PAGE
                spare = total
                total += 1
            groups.append({"type": page_type, "index": index_no,
                           "first": first_data, "spare": spare,
                           "rest": data[1:] if data else [],
                           "data": data})

        # Pass 2: remaining data pages, in page order.
        for group in groups:
            group["tail"] = []
            for _ in group["rest"]:
                group["tail"].append(total)
                total += 1

        # Pass 3: one empty candidate page per table.
        for group in groups:
            group["candidate"] = total
            total += 1

        pages: dict[int, bytes] = {0: bytes(page_size)}
        sequence = 1
        entries: list[tuple[int, int, int, int]] = []
        for group in groups:
            page_type = group["type"]
            data_nos = ([group["first"]] if group["first"] != NO_PAGE else []) \
                + group["tail"]
            index_no = group["index"]
            pages[index_no] = index_page(
                page_size, index_no, page_type,
                data_nos[0] if data_nos else NO_PAGE,
                data_nos[0] if data_nos else None, sequence)
            sequence += 1
            for at, data_no in enumerate(data_nos):
                last = at + 1 == len(data_nos)
                page = bytearray(group["data"][at])
                page[0x04:0x08] = p32(data_no)
                page[0x08:0x0C] = p32(page_type)
                page[0x0C:0x10] = p32(group["candidate"] if last
                                      else data_nos[at + 1])
                page[0x10:0x14] = p32(sequence)
                pages[data_no] = bytes(page)
                sequence += 1
            if group["spare"] is not None:
                pages[group["spare"]] = bytes(page_size)
            pages[group["candidate"]] = bytes(page_size)
            entries.append((page_type, group["candidate"], index_no,
                            data_nos[-1] if data_nos else index_no))

        # Trailing all-zero pages are not written (the header still names them).
        while total > 1 and not any(pages[total - 1]):
            total -= 1

        out = bytearray(page_size)
        out[0x04:0x08] = p32(page_size)
        out[0x08:0x0C] = p32(len(groups))
        out[0x0C:0x10] = p32(total)
        out[0x10:0x14] = p32(1)
        out[0x14:0x18] = p32(sequence)
        for i, (page_type, candidate, first, last) in enumerate(entries):
            at = 28 + i * 16
            out[at:at + 4] = p32(page_type)
            out[at + 4:at + 8] = p32(candidate)
            out[at + 8:at + 12] = p32(first)
            out[at + 12:at + 16] = p32(last)
        for number in range(1, total):
            out.extend(pages[number])
        return bytes(out)


# ---------------------------------------------------------------------------
# Row encoders
# ---------------------------------------------------------------------------

TRACK_FIXED_LEN = 0x5E
TRACK_STRINGS = 21

SLOT_ISRC = 0
SLOT_HOT_CUE_AUTO_LOAD = 7
SLOT_DATE_CREATED = 10
SLOT_RELEASE_DATE = 11
SLOT_MIX_NAME = 12
SLOT_ANALYZE_PATH = 14
SLOT_DATE_ADDED = 15
SLOT_COMMENT = 16
SLOT_TITLE = 17
SLOT_FILENAME = 19
SLOT_FILE_PATH = 20


def audio_format_code(filename: str) -> int:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {"mp3": 1, "m4a": 4, "mp4": 4, "aac": 4, "flac": 5,
            "wav": 11, "aif": 12, "aiff": 12}.get(ext, 0)


@dataclass
class TrackRow:
    """Everything written into one DJDBCONTENT track row."""

    id: int = 1
    title: str = ""
    filename: str = ""
    file_path: str = ""
    analyze_path: str = ""
    artist_id: int = 0
    album_id: int = 0
    genre_id: int = 0
    key_id: int = 0
    label_id: int = 0
    artwork_id: int = 0
    color_id: int = 0
    rating: int = 0
    bpm_x100: int = 12000
    duration_sec: int = 0
    year: int = 0
    bitrate: int = 320
    sample_rate: int = 44100
    file_size: int = 0
    track_number: int = 0
    play_count: int = 0
    disc_number: int = 0
    sample_depth: int = 16
    master_db_id: int = 0
    master_content_id: int = 0
    content_link: int = 0xC1600 | 0x100
    comment: str = ""
    date_added: str = ""
    date_created: str = ""
    release_date: str = ""
    mix_name: str = ""
    isrc: str = ""
    hot_cue_auto_load: bool = False

    def encode(self, index_shift: int = 0) -> bytes:
        strings = [""] * TRACK_STRINGS
        strings[SLOT_ISRC] = self.isrc
        strings[SLOT_HOT_CUE_AUTO_LOAD] = "ON" if self.hot_cue_auto_load else ""
        strings[SLOT_DATE_CREATED] = self.date_created
        strings[SLOT_RELEASE_DATE] = self.release_date
        strings[SLOT_MIX_NAME] = self.mix_name
        strings[SLOT_ANALYZE_PATH] = self.analyze_path
        strings[SLOT_DATE_ADDED] = self.date_added
        strings[SLOT_COMMENT] = self.comment
        strings[SLOT_TITLE] = self.title
        strings[SLOT_FILENAME] = self.filename
        strings[SLOT_FILE_PATH] = self.file_path

        row = bytearray(TRACK_FIXED_LEN + TRACK_STRINGS * 2)

        def put2(at: int, v: int) -> None:
            row[at:at + 2] = p16(v)

        def put4(at: int, v: int) -> None:
            row[at:at + 4] = p32(v)

        put2(0x00, 0x24)          # track record with 16-bit string offsets
        put2(0x02, index_shift * 32)
        put4(0x04, self.content_link)
        put4(0x08, self.sample_rate)
        put4(0x0C, 0)
        put4(0x10, self.file_size)
        put4(0x14, self.master_content_id)
        put4(0x18, self.master_db_id)
        put4(0x1C, self.artwork_id)
        put4(0x20, self.key_id)
        put4(0x24, 0)
        put4(0x28, self.label_id)
        put4(0x2C, 0)
        put4(0x30, self.bitrate)
        put4(0x34, self.track_number)
        put4(0x38, self.bpm_x100)
        put4(0x3C, self.genre_id)
        put4(0x40, self.album_id)
        put4(0x44, self.artist_id)
        put4(0x48, self.id)
        put2(0x4C, self.disc_number)
        put2(0x4E, self.play_count)
        put2(0x50, self.year)
        put2(0x52, self.sample_depth)
        put2(0x54, self.duration_sec)
        put2(0x56, 0x29)          # trailer: required for the tested CDJ-3000 path
        row[0x58] = self.color_id & 0xFF
        row[0x59] = self.rating & 0xFF
        put2(0x5A, audio_format_code(self.filename))
        put2(0x5C, 3)             # trailer

        offsets = [0] * TRACK_STRINGS
        for slot, text in enumerate(strings):
            offsets[slot] = push_string(row, text)
        for slot, offset in enumerate(offsets):
            put2(TRACK_FIXED_LEN + slot * 2, offset)
        return bytes(row)


def artist_row(id_: int, name: str, index_shift: int = 0) -> bytes:
    row = bytearray(10)
    row[0x00:0x02] = p16(0x60)
    row[0x02:0x04] = p16(index_shift * 32)
    row[0x04:0x08] = p32(id_)
    row[0x08] = 0x03
    at = push_string(row, name)
    row[0x09] = at & 0xFF
    return bytes(row)


def album_row(id_: int, artist_id: int, name: str, index_shift: int = 0) -> bytes:
    row = bytearray(0x16)
    row[0x00:0x02] = p16(0x80)
    row[0x02:0x04] = p16(index_shift * 32)
    row[0x08:0x0C] = p32(artist_id)
    row[0x0C:0x10] = p32(id_)
    row[0x14] = 0x03
    at = push_string(row, name)
    row[0x15] = at & 0xFF
    return bytes(row)


def simple_named_row(id_: int, name: str) -> bytes:
    return p32(id_) + device_sql_string(name)


def key_row(id_: int, name: str) -> bytes:
    return p32(id_) + p32(id_) + device_sql_string(name)


def color_row(id_: int, name: str) -> bytes:
    row = bytearray(8)
    row[4] = id_ & 0xFF
    row[5:7] = p16(id_)
    row.extend(device_sql_string(name))
    return bytes(row)


def playlist_row(id_: int, parent_id: int, sort_order: int, is_folder: bool, name: str) -> bytes:
    row = bytearray()
    row.extend(p32(parent_id))
    row.extend(p32(0))          # the zero word every rekordbox row carries
    row.extend(p32(sort_order))
    row.extend(p32(id_))
    row.extend(p32(1 if is_folder else 0))
    row.extend(device_sql_string(name))
    return bytes(row)


def playlist_entry_row(entry_index: int, track_id: int, playlist_id: int) -> bytes:
    return p32(entry_index) + p32(track_id) + p32(playlist_id)


def artwork_row(id_: int, path: str) -> bytes:
    return p32(id_) + device_sql_string(path)


def property_row(device_name: str, contents: int, created_date: str,
                 background_color: int = 0, db_version: str = "1000",
                 index_shift: int = 0) -> bytes:
    if len(created_date) != 10 or not created_date.isascii():
        raise ValueError("property created_date must be exactly YYYY-MM-DD")
    row = bytearray(b"\x80\x02" + p16(index_shift * 32))
    row.extend(p32(contents))
    row.extend(b"\x00" + bytes([background_color & 0xFF]) + b"\x00\x00")
    row.extend(short_ascii(created_date))
    row.extend(b"\x19\x1e")     # constant carried from the reference
    row.extend(short_ascii(db_version))
    row.extend(device_sql_string(device_name))
    row.extend(bytes(8))
    while len(row) % 4:
        row.append(0)
    return bytes(row)


# ---------------------------------------------------------------------------
# Fixed reference rows (browse columns, category/sort settings)
# ---------------------------------------------------------------------------

def _hex_rows(rows: Sequence[str]) -> list[bytes]:
    return [bytes.fromhex(r) for r in rows]


COLUMNS = _hex_rows([
    "0100800090120000FAFF470045004E0052004500FBFF0000",
    "0200810090140000FAFF410052005400490053005400FBFF",
    "0300820090120000FAFF41004C00420055004D00FBFF0000",
    "0400830090120000FAFF54005200410043004B00FBFF0000",
    "05008500900E0000FAFF420050004D00FBFF0000",
    "0600860090140000FAFF52004100540049004E004700FBFF",
    "0700870090100000FAFF5900450041005200FBFF",
    "0800880090160000FAFF520045004D004900580045005200FBFF0000",
    "0900890090120000FAFF4C004100420045004C00FBFF0000",
    "0A008A0090260000FAFF4F0052004900470049004E0041004C002000410052005400490053005400FBFF0000",
    "0B008B00900E0000FAFF4B0045005900FBFF0000",
    "0C008D00900E0000FAFF430055004500FBFF0000",
    "0D008E0090120000FAFF43004F004C004F005200FBFF0000",
    "0E00920090100000FAFF540049004D004500FBFF",
    "0F00930090160000FAFF4200490054005200410054004500FBFF0000",
    "10009400901A0000FAFF460049004C00450020004E0041004D004500FBFF0000",
    "1100840090180000FAFF50004C00410059004C00490053005400FBFF",
    "1200980090200000FAFF48004F00540020004300550045002000420041004E004B00FBFF",
    "1300950090160000FAFF48004900530054004F0052005900FBFF0000",
    "1400910090140000FAFF530045004100520043004800FBFF",
    "1500960090180000FAFF43004F004D004D0045004E0054005300FBFF",
    "16008C00901C0000FAFF4400410054004500200041004400440045004400FBFF",
    "1700970090220000FAFF44004A00200050004C0041005900200043004F0055004E005400FBFF0000",
    "1800900090140000FAFF46004F004C00440045005200FBFF",
    "1900A10090160000FAFF440045004600410055004C005400FBFF0000",
    "1A00A20090180000FAFF41004C00500048004100420045005400FBFF",
    "1B00AA0090180000FAFF4D00410054004300480049004E004700FBFF",
])

CATEGORIES = _hex_rows([
    "0100010063010000", "0500060005010000", "0600070063010000",
    "0700080063010000", "0800090063010000", "09000A0063010000",
    "0A000B0063010000", "0D000F0063010000", "0E00130004010000",
    "0F00140006010000", "1000150063010000", "1200170063010000",
    "0200020002000100", "0300030003000200", "0400040001000300",
    "0B000C0063000400", "1100050063000500", "1300160063000600",
    "1400120063000700", "1B001A0063020800", "1800110063000900",
    "16001B0063000A00",
])

SORTS = _hex_rows([
    "0100060001000000", "1500070003000000", "0E00080001000000",
    "0800090001000000", "09000A0001000000", "0A000B0001000000",
    "0F000D0001000000", "0D000F0001000000", "1700100001000000",
    "1600110001000000", "1900000000010000", "1A00010000020000",
    "0200020000030000", "0300030000040000", "0500040000050000",
    "0600050000060000", "0B000C0000070000",
])

COLOR_NAMES = ["Pink", "Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple"]


# ---------------------------------------------------------------------------
# DeviceSQL reader / verifier
# ---------------------------------------------------------------------------

class PdbError(Exception):
    pass


@dataclass
class Table:
    type: int
    candidate: int
    first: int
    last: int
    index_page: int = 0


class Pdb:
    """An independent reader, written from the format, not from the writer."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        if len(data) < PAGE_SIZE:
            raise PdbError("file shorter than one page")
        self.page_size = u4(data, 0x04)
        if not (512 <= self.page_size <= 65536) or self.page_size & (self.page_size - 1):
            raise PdbError(f"bad page size {self.page_size}")
        self.table_count = u4(data, 0x08)
        self.next_unused = u4(data, 0x0C)
        self.sequence = u4(data, 0x14)
        self.tables: list[Table] = []
        for i in range(self.table_count):
            at = 28 + i * 16
            if at + 16 > self.page_size:
                raise PdbError("table entry past header page")
            self.tables.append(Table(u4(data, at), u4(data, at + 4),
                                     u4(data, at + 8), u4(data, at + 12)))

    def page(self, index: int) -> bytes:
        at = index * self.page_size
        page = self.data[at:at + self.page_size]
        if len(page) < self.page_size:
            raise PdbError(f"page {index} past end of file")
        return page

    def table(self, page_type: int) -> Table:
        for t in self.tables:
            if t.type == page_type:
                return t
        raise PdbError(f"no table of type {page_type}")

    def table_chains(self, page_type: int) -> list[int]:
        t = self.table(page_type)
        index = self.page(t.first)
        if not (index[0x1B] & 0x40):
            raise PdbError(f"table {page_type} does not start with an index page")
        chain = [t.first]
        if t.first == t.last:
            return chain
        at = u4(index, 0x0C)
        seen = set()
        while True:
            if at in seen or len(seen) > 1_000_000:
                raise PdbError(f"table {page_type} page chain loops")
            seen.add(at)
            chain.append(at)
            if at == t.last:
                break
            page = self.page(at)
            if page[0x1B] & 0x40:
                raise PdbError(f"table {page_type} chain reached a non-data page")
            at = u4(page, 0x0C)
        return chain

    def rows_of_page(self, index: int) -> list[bytes]:
        page = self.page(index)
        if page[0x1B] & 0x40:
            return []
        n = (int.from_bytes(page[0x18:0x1B], "little")) & 0x1FFF
        out = []
        for group in range(groups_for(n)):
            base = self.page_size - group * ROW_GROUP_LEN
            in_group = 16 if group < groups_for(n) - 1 else n - group * 16
            present = u2(page, base - 4)
            for row in range(in_group):
                if not (present >> row) & 1:
                    continue
                off = u2(page, base - (6 + 2 * row))
                at = PAGE_HEADER_LEN + off
                if at >= self.page_size:
                    raise PdbError(f"row offset {off} out of page {index}")
                out.append(page[at:])  # trimmed lazily by the row decoder
        return out

    def rows(self, page_type: int) -> list[bytes]:
        out: list[bytes] = []
        for index in self.table_chains(page_type)[1:]:
            out.extend(self.rows_of_page(index))
        return out

    @staticmethod
    def read_string(row: bytes, at: int) -> str:
        flag = row[at]
        if flag == 0x40:
            n = u2(row, at + 1) - 4
            return row[at + 4:at + 4 + n].decode("utf-8", "replace")
        if flag == 0x90:
            n = u2(row, at + 1) - 4
            return row[at + 4:at + 4 + n].decode("utf-16-le", "replace")
        n = (flag - 1) // 2 - 1
        return row[at + 1:at + 1 + n].decode("ascii", "replace")

    def tracks(self) -> list[dict]:
        out = []
        for row in self.rows(TABLE_TRACKS):
            rec = {
                "subtype": u2(row, 0x00),
                "index_shift": u2(row, 0x02) >> 5,
                "content_link": u4(row, 0x04),
                "sample_rate": u4(row, 0x08),
                "file_size": u4(row, 0x10),
                "master_content_id": u4(row, 0x14),
                "master_db_id": u4(row, 0x18),
                "artwork_id": u4(row, 0x1C),
                "key_id": u4(row, 0x20),
                "label_id": u4(row, 0x28),
                "bitrate": u4(row, 0x30),
                "track_number": u4(row, 0x34),
                "bpm_x100": u4(row, 0x38),
                "genre_id": u4(row, 0x3C),
                "album_id": u4(row, 0x40),
                "artist_id": u4(row, 0x44),
                "id": u4(row, 0x48),
                "disc": u2(row, 0x4C),
                "play_count": u2(row, 0x4E),
                "year": u2(row, 0x50),
                "sample_depth": u2(row, 0x52),
                "duration": u2(row, 0x54),
                "trailer29": u2(row, 0x56),
                "color_id": row[0x58],
                "rating": row[0x59],
                "audio_format": u2(row, 0x5A),
                "trailer3": u2(row, 0x5C),
            }
            offsets = [u2(row, TRACK_FIXED_LEN + s * 2) for s in range(TRACK_STRINGS)]
            rec["strings"] = [self.read_string(row, o) for o in offsets]
            rec["title"] = rec["strings"][SLOT_TITLE]
            rec["file_path"] = rec["strings"][SLOT_FILE_PATH]
            rec["filename"] = rec["strings"][SLOT_FILENAME]
            rec["analyze_path"] = rec["strings"][SLOT_ANALYZE_PATH]
            out.append(rec)
        return out

    def artists(self) -> list[tuple[int, str]]:
        out = []
        for row in self.rows(TABLE_ARTISTS):
            at = row[0x09] if row[0x00:0x02] == p16(0x60) else u2(row, 0x0A)
            out.append((u4(row, 0x04), self.read_string(row, at)))
        return out

    def albums(self) -> list[tuple[int, int, str]]:
        out = []
        for row in self.rows(TABLE_ALBUMS):
            at = row[0x15] if row[0x00:0x02] == p16(0x80) else u2(row, 0x16)
            out.append((u4(row, 0x0C), u4(row, 0x08), self.read_string(row, at)))
        return out

    def named(self, page_type: int) -> list[tuple[int, str]]:
        return [(u4(row, 0), self.read_string(row, 4)) for row in self.rows(page_type)]

    def playlists(self) -> list[dict]:
        out = []
        for row in self.rows(TABLE_PLAYLIST_TREE):
            out.append({"parent": u4(row, 0x00), "sort_order": u4(row, 0x08),
                        "id": u4(row, 0x0C), "folder": u4(row, 0x10),
                        "name": self.read_string(row, 20)})
        return out

    def playlist_entries(self) -> list[tuple[int, int, int]]:
        return [(u4(r, 0), u4(r, 4), u4(r, 8)) for r in self.rows(TABLE_PLAYLIST_ENTRIES)]

    def property(self) -> dict:
        rows = self.rows(TABLE_PROPERTY)
        if not rows:
            return {}
        row = rows[0]
        at = 0x0C
        created = self.read_string(row, at)
        at += len(short_ascii(created)) + 2
        version = self.read_string(row, at)
        at += len(short_ascii(version))
        name = self.read_string(row, at)
        return {"contents": u4(row, 0x04), "background": row[0x09],
                "created_date": created, "db_version": version,
                "device_name": name}

    def verify(self) -> list[str]:
        """Structural checks a deck relies on. Returns a list of problems."""
        issues: list[str] = []
        if self.table_count < 1:
            issues.append("no tables")
        # Header page itself.
        if u4(self.data, 0x10) != 1:
            issues.append("header 0x10 is not 1")
        max_page = (len(self.data) + self.page_size - 1) // self.page_size
        if self.next_unused < max_page:
            issues.append(f"next_unused {self.next_unused} < pages {max_page}")
        seen_types = set()
        for t in self.tables:
            if t.type in seen_types:
                issues.append(f"duplicate table type {t.type}")
            seen_types.add(t.type)
            try:
                index = self.page(t.first)
            except PdbError as exc:
                issues.append(f"table {t.type}: {exc}")
                continue
            if not (index[0x1B] & 0x40):
                issues.append(f"table {t.type} first page is not an index page")
            if u4(index, 0x08) != t.type:
                issues.append(f"table {t.type} index page names type {u4(index, 0x08)}")
            if u4(index, 0x04) != t.first:
                issues.append(f"table {t.type} index page index mismatch")
            chain = self.table_chains(t.type)
            if t.first != t.last and u4(self.page(chain[-1]), 0x0C) != t.candidate:
                issues.append(f"table {t.type} last page does not point at its candidate")
            # Header sequence must be above every page's.
            for index in chain:
                page = self.page(index)
                if u4(page, 0x10) >= self.sequence:
                    issues.append(f"page {index} sequence >= header sequence")
                if u4(page, 0x08) != t.type:
                    issues.append(f"page {index} type mismatch")
                if u4(page, 0x04) != index:
                    issues.append(f"page {index} index field mismatch")
                slots = int.from_bytes(page[0x18:0x1B], "little") & 0x1FFF
                present = int.from_bytes(page[0x18:0x1B], "little") >> 13
                if not (page[0x1B] & 0x40):
                    used = u2(page, 0x1E)
                    index_start = (self.page_size if slots == 0 else
                                   self.page_size - (groups_for(slots) - 1) * ROW_GROUP_LEN
                                   - 6 - 2 * ((slots - 1) % 16))
                    if PAGE_HEADER_LEN + used > index_start:
                        issues.append(f"page {index} heap overlaps row index")
                    # Deleted rows keep their slot, so the presence mask is
                    # sparse: count its bits and compare with the header.
                    seen = 0
                    for group in range(groups_for(slots)):
                        base = self.page_size - group * ROW_GROUP_LEN
                        in_group = min(16, slots - group * 16)
                        mask = u2(page, base - 4)
                        if mask >> in_group:
                            issues.append(
                                f"page {index} group {group} present bits past slot {slots}")
                        for row in range(in_group):
                            if not (mask >> row) & 1:
                                continue
                            seen += 1
                            off = u2(page, base - (6 + 2 * row))
                            if PAGE_HEADER_LEN + off >= self.page_size:
                                issues.append(
                                    f"page {index} row {group * 16 + row} offset {off} out of page")
                    if seen != present:
                        issues.append(
                            f"page {index} has {seen} present rows, header says {present}")
        # Every track row must parse and carry the trailer words.
        for track in self.tracks():
            if track["subtype"] != 0x24:
                issues.append(f"track {track['id']} subtype {track['subtype']:#x}")
            if track["trailer29"] != 0x29 or track["trailer3"] != 3:
                issues.append(f"track {track['id']} trailer words wrong")
            if audio_format_code(track["filename"]) != track["audio_format"]:
                issues.append(f"track {track['id']} audio format mismatch")
            for i, s in enumerate(track["strings"]):
                if s is None:
                    issues.append(f"track {track['id']} string slot {i} unreadable")
        if TABLE_PROPERTY in seen_types:
            prop = self.property()
            if len(prop.get("created_date", "")) != 10:
                issues.append("property created_date is not YYYY-MM-DD")
        return issues


# ---------------------------------------------------------------------------
# ANLZ
# ---------------------------------------------------------------------------

SECTION_FRAME = 12
PMAI_HEADER_EXTRA = bytes([0, 0, 0, 1, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0])


def be16(v: int) -> bytes:
    return struct.pack(">H", v & 0xFFFF)


def be32(v: int) -> bytes:
    return struct.pack(">I", v & 0xFFFFFFFF)


def anlz_section(tag: bytes, header: bytes, payload: bytes) -> bytes:
    return (tag + be32(SECTION_FRAME + len(header))
            + be32(SECTION_FRAME + len(header) + len(payload))
            + header + payload)


def anlz_render(sections: Sequence[bytes]) -> bytes:
    body = b"".join(sections)
    return b"PMAI" + be32(SECTION_FRAME + len(PMAI_HEADER_EXTRA)) \
        + be32(SECTION_FRAME + len(PMAI_HEADER_EXTRA) + len(body)) \
        + PMAI_HEADER_EXTRA + body


def ppth_section(path: str) -> bytes:
    text = path.encode("utf-16-be") + b"\x00\x00"
    return anlz_section(b"PPTH", be32(len(text)), text)


def pvbr_zero_section() -> bytes:
    return anlz_section(b"PVBR", be32(0), bytes(401 * 4))


def pqtz_section(beats: Sequence[tuple[int, int, int]], offset_ms: int = 0) -> bytes:
    header = be32(0) + be32(0x0008_0000 | (offset_ms & 0xFFFF)) + be32(len(beats))
    payload = b"".join(be16(b) + be16(t) + be32(ms) for b, t, ms in beats)
    return anlz_section(b"PQTZ", header, payload)


def preview_section(tag: bytes, data: bytes) -> bytes:
    return anlz_section(tag, be32(len(data)) + be32(0x0001_0000), data)


def scroll_section(tag: bytes, stride: int, data: bytes) -> bytes:
    entries = len(data) // stride if stride else 0
    trailer = {b"PWV5": 0x0096_0305, b"PWV4": 0}.get(tag, 0x0096_0000)
    header = be32(stride) + be32(entries)
    if tag != b"PWV6":
        header += be32(trailer)
    return anlz_section(tag, header, data)


def empty_cue_lists(extended: bool) -> list[bytes]:
    out = []
    for kind in (1, 0):
        out.append(anlz_section(b"PCOB",
                                be32(kind) + be16(0) + be16(0) + be32(0xFFFFFFFF),
                                b""))
    if extended:
        for kind in (1, 0):
            out.append(anlz_section(b"PCO2", be32(kind) + be16(0) + be16(0), b""))
    return out


def pqt2_empty_section() -> bytes:
    header = bytearray(44)
    header[4:8] = be32(0x0100_0002)
    return anlz_section(b"PQT2", bytes(header), b"")


def analysis_path(audio_path: str) -> str:
    """The USBANLZ directory the firmware computes from the audio path."""
    h = 0
    units = []
    raw = audio_path.encode("utf-16-le")
    for i in range(0, len(raw), 2):
        unit = int.from_bytes(raw[i:i + 2], "little")
        if unit == 0:
            break
        units.append(unit)
    for unit in units:
        h = (h * 0x34F5501D + unit * 0x93B6) & 0xFFFFFFFF
    value = h % 0x30D43
    shard = 0
    for bit, src in enumerate((0, 2, 6, 7, 9, 13, 16)):
        shard |= ((value >> src) & 1) << bit
    return f"/PIONEER/USBANLZ/P{shard:03X}/{value:08X}/"


# ---------------------------------------------------------------------------
# DEVSETTING.DAT
# ---------------------------------------------------------------------------

def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def devsetting(version: str = "7.2.11", overview: int = 1,
               waveform_color: int = 2, key_display: int = 1,
               position_marker: int = 0) -> bytes:
    body = bytearray(b"\x78\x56\x34\x12\x01\x00\x00\x00\x01")
    body += bytes([overview & 0xFF, waveform_color & 0xFF, 0x01,
                   key_display & 0xFF, position_marker & 0xFF])
    body += bytes(18)
    assert len(body) == 32
    out = bytearray()
    out += p32(0x60)
    out += b"PIONEER DJ".ljust(32, b"\x00")
    out += b"rekordbox".ljust(32, b"\x00")
    out += version.encode("ascii").ljust(32, b"\x00")
    out += p32(0x20)
    out += body
    out += struct.pack("<H", crc16_xmodem(body))
    out += b"\x00\x00"
    assert len(out) == 140
    return bytes(out)


def sync_record(db_id: int, playlists: Sequence[dict],
                timestamp_ms: int = 0) -> bytes:
    """``playlists3.sync``: UTF-8 XML with CRLF and a blank line after the decl."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "",
             f'<Sync DBID="{db_id}" AutomaticSync="1" AllPlaylists="0" '
             f'IncludeCue="1" ForcedSync="0" Timestamp="{timestamp_ms}">',
             "  <Playlists>",
             '    <NODE Id="0" ParentId="0" Attribute="1" Lib_Type="0" '
             'Dev_ID="0" Timestamp="0" CheckType="2"/>']
    for pl in playlists:
        attribute = 1 if pl.get("folder") else 0
        lines.append(
            f'    <NODE Id="{pl["id"]:X}" ParentId="{pl.get("parent", 0)}" '
            f'Attribute="{attribute}" Lib_Type="0" Dev_ID="1" '
            f'Timestamp="0" CheckType="1"/>')
    lines += ["  </Playlists>", "</Sync>", ""]
    return "\r\n".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# Convenience: build the whole export.pdb table set
# ---------------------------------------------------------------------------

@dataclass
class Library:
    tracks: list[TrackRow] = field(default_factory=list)
    artists: list[tuple[int, str]] = field(default_factory=list)
    albums: list[tuple[int, int, str]] = field(default_factory=list)
    genres: list[tuple[int, str]] = field(default_factory=list)
    labels: list[tuple[int, str]] = field(default_factory=list)
    keys: list[tuple[int, str]] = field(default_factory=list)
    playlists: list[dict] = field(default_factory=list)
    entries: list[tuple[int, int, int]] = field(default_factory=list)
    artworks: list[tuple[int, str]] = field(default_factory=list)
    device_name: str = "CDJ-USB"
    created_date: str = ""
    background_color: int = 0
    master_db_id: int = 1

    def rows_by_type(self) -> dict[int, list[bytes]]:
        return {
            TABLE_TRACKS: [t.encode() for t in self.tracks],
            TABLE_GENRES: [simple_named_row(i, n) for i, n in self.genres],
            TABLE_ARTISTS: [artist_row(i, n) for i, n in self.artists],
            TABLE_ALBUMS: [album_row(i, a, n) for i, a, n in self.albums],
            TABLE_LABELS: [simple_named_row(i, n) for i, n in self.labels],
            TABLE_KEYS: [key_row(i, n) for i, n in self.keys],
            TABLE_COLORS: [color_row(i + 1, n) for i, n in enumerate(COLOR_NAMES)],
            TABLE_PLAYLIST_TREE: [
                playlist_row(p["id"], p.get("parent", 0), p.get("sort", 0),
                             p.get("folder", False), p["name"])
                for p in self.playlists],
            TABLE_PLAYLIST_ENTRIES: [playlist_entry_row(e[0], e[1], e[2])
                                     for e in self.entries],
            TABLE_ARTWORK: [artwork_row(i, p) for i, p in self.artworks],
            TABLE_COLUMNS: list(COLUMNS),
            TABLE_CATEGORIES: list(CATEGORIES),
            TABLE_SORTS: list(SORTS),
            TABLE_PROPERTY: [property_row(
                self.device_name, len(self.tracks),
                self.created_date or datetime.date.today().isoformat(),
                self.background_color)],
        }

    def build_export_pdb(self) -> bytes:
        """Every table, in type order, empty ones included."""
        rows = self.rows_by_type()
        builder = FileBuilder()
        for page_type in EXPORT_TABLES:
            builder.add_table(page_type, rows.get(page_type, []))
        return builder.finish()

    def build_export_ext_pdb(self, my_tags: Sequence[tuple[int, str, str]] = (),
                             track_tags: Sequence[tuple[int, int]] = ()) -> bytes:
        """``exportExt.pdb``: type 3 tags, type 4 memberships, type 7 master."""
        builder = FileBuilder()
        builder.write_order = [7, 3]
        tags: list[bytes] = []
        for seq, (tag_id, name, parent) in enumerate(my_tags, start=1):
            encoded = device_sql_string(name)
            row = bytearray()
            row += p16(0x0680)
            row += p16(0)                       # row slot on page, stamped below
            row += bytes(8)
            row += p32(parent)
            row += p32(seq - 1)
            row += p32(tag_id)
            row += bytes(3)
            row.append(1 if parent == 0 else 0)  # 0x1b: category flag
            row.append(0x03)                     # 0x1c: empty short string
            name_at = 0x1F
            row.append(name_at)                  # 0x1d: name offset
            row.append(name_at + len(encoded))   # 0x1e: trailing empty string
            assert len(row) == name_at
            row += encoded
            row += b"\x03"                      # the trailing empty string
            tags.append(bytes(row))

        for page_type in EXT_TABLES:
            rows: list[bytes] = []
            number = None
            style = PageStyle.COUNTED
            if page_type == 3:
                rows = tags

                def number(row: bytes, slot: int) -> bytes:
                    row = bytearray(row)
                    row[2:4] = p16(slot * 32)
                    return bytes(row)
            elif page_type == 4:
                rows = [p32(0) + p32(track_id) + p32(tag_id) + b"\x03\x00\x00\x00"
                        for track_id, tag_id in track_tags]
                style = PageStyle.LAST_ROW_INDEX
            elif page_type == 7:
                row = bytearray(b"\x00\x07\x00\x00")
                row += bytes(20)
                row += p32(self.master_db_id)
                row += b"\x03" + bytes(5)
                while len(row) < 60:
                    row.append(0)
                rows = [bytes(row)]
            builder.add_table(page_type, rows, number=number,
                              style=style)
        return builder.finish()


# ---------------------------------------------------------------------------
# ANLZ bundle for one track
# ---------------------------------------------------------------------------

MAX_BEATS = 200_000          # ~2.4 MB of PQTZ payload, bounds a 0xFFFF-BPM track
MAX_WAVE_ENTRIES = 600_000   # ~1.2 MB of PWV* payload, bounds a 18-hour track


def make_anlz(path: str, bpm_x100: int, duration_ms: int,
              overview: bool = True) -> dict[str, bytes]:
    """A DAT/EXT/2EX bundle: PPTH, grid, waveforms, empty cue lists.

    Sizes are clamped: a boundary BPM (e.g. 0xFFFFFFFF hundredths) would
    otherwise make the beat grid step 0 ms and loop forever, and a boundary
    duration would make the waveforms arbitrarily large.
    """
    bpm = max(bpm_x100, 1) / 100.0
    # One beat can never be shorter than 1 ms on the media, and a 0 ms step
    # would make the grid loop forever (0xFFFFFFFF centi-BPM -> 0.000014 ms).
    beat_ms = max(1, int(round(60000.0 / bpm))) if bpm else 500
    beats = []
    at = 0
    n = 0
    while at <= duration_ms and n < MAX_BEATS:
        beats.append(((n % 4) + 1, min(bpm_x100, 0xFFFF), at))
        n += 1
        at += beat_ms
    # Preview waveforms: no column may be 0; PWAV tops at 25, PWV2 at 15.
    pwav = bytes([2 + (i * 7) % 24 for i in range(400)])
    pwv2 = bytes([1 + (i * 3) % 15 for i in range(100)])
    scroll = min(MAX_WAVE_ENTRIES, max(150, duration_ms // 1000 * 150))
    pwv3 = bytes([1 + (i * 3) % 31 for i in range(scroll)])
    pwv5 = b"".join(be16((1 << 13) | ((i * 3) % 8) << 10 | ((i * 5) % 8) << 7 | ((i * 7) % 8) << 3)
                    for i in range(scroll))
    pwv4 = b"".join(bytes([min(127, (i * 5) % 128), 0, 0,
                           (i * 3) % 256, (i * 5) % 256, (i * 7) % 256])
                    for i in range(1200))
    pwv6 = bytes([(i * 3) % 128 for i in range(1200 * 3)])
    pwv7 = bytes([(i * 5) % 128 for i in range(scroll * 3)])

    dat = [ppth_section(path), pvbr_zero_section(), pqtz_section(beats)]
    dat += [preview_section(b"PWAV", pwav), preview_section(b"PWV2", pwv2)]
    dat += empty_cue_lists(False)
    ext = [ppth_section(path), scroll_section(b"PWV3", 1, pwv3)]
    ext += empty_cue_lists(True)
    ext += [scroll_section(b"PWV5", 2, pwv5), scroll_section(b"PWV4", 6, pwv4),
            pqt2_empty_section()]
    two_ex = [ppth_section(path),
              scroll_section(b"PWV7", 3, pwv7),
              scroll_section(b"PWV6", 3, pwv6)]
    return {"DAT": anlz_render(dat), "EXT": anlz_render(ext), "2EX": anlz_render(two_ex)}
