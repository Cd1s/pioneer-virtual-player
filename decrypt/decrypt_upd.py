#!/usr/bin/env python3
"""Decrypt a Pioneer LUKS1 .UPD to its ISO payload, given the 32-byte key.

The key is checked by cryptsetup's own LUKS1 keyslot code, then the payload
is read from the mapped device.  Using cryptsetup for both steps means the
container is decrypted by the same implementation that built the reference
containers in ``luks_slot_test.py --selftest`` -- there is no second,
home-grown XTS/anti-forensic path that could disagree with it.

Usage:
  python3 decrypt_upd.py <file.upd> <keyfile-or-hex> -o <out.iso>
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from luks_slot_test import parse_pioneer_luks1, read_key  # noqa: E402

def iso_volume_bytes(path: str) -> int | None:
    """Size of the ISO 9660 volume, or None when there is no PVD."""
    with open(path, "rb") as fh:
        fh.seek(0x8000)
        pvd = fh.read(2048)
    if len(pvd) < 88 or pvd[0] != 1 or pvd[1:6] != b"CD001":
        return None
    return int.from_bytes(pvd[80:84], "little") * 2048


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("upd")
    parser.add_argument("key")
    parser.add_argument("-o", "--out", required=True)
    args = parser.parse_args()

    if not shutil.which("cryptsetup"):
        raise SystemExit("cryptsetup is required (system package 'cryptsetup')")

    head = open(args.upd, "rb").read(592)
    header = parse_pioneer_luks1(head)
    active = [i for i, s in enumerate(header["slots"]) if s["active"]]
    print(f"{args.upd}: LUKS1, payload at sector {header['poff']}, "
          f"{header['kbytes'] * 8}-bit key, active slots {active}")

    key = read_key(args.key)
    with tempfile.NamedTemporaryFile(delete=False) as fh:
        fh.write(key)
        keyfile = fh.name
    name = "pvp-" + str(os.getpid())
    try:
        open_proc = subprocess.run(
            ["cryptsetup", "open", "--readonly", "--type", "luks1",
             "--key-file", keyfile, args.upd, name],
            capture_output=True, text=True)
        if open_proc.returncode != 0:
            raise SystemExit("key does NOT open this .UPD "
                             "(wrong key or wrong file):\n" + open_proc.stderr.strip())
        out_dir = os.path.dirname(os.path.abspath(args.out))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        subprocess.run(["dd", f"if=/dev/mapper/{name}", f"of={args.out}",
                        "bs=4M", "status=none"], check=True)
    finally:
        subprocess.run(["cryptsetup", "close", name], check=False,
                       capture_output=True)
        os.unlink(keyfile)

    size = os.path.getsize(args.out)
    volume = iso_volume_bytes(args.out)
    if volume and 0 < volume <= size:
        if volume < size:
            # The .UPD carries an unencrypted trailer (product tag + CRC32)
            # past the payload; cryptsetup exposes it as extra sectors.
            with open(args.out, "r+b") as fh:
                fh.truncate(volume)
            size = volume
        print(f"OK: key verified, decrypted ISO 9660 volume, {size} bytes "
              f"-> {args.out}")
        print("mount it (or use 7z/iso9660fs) and take the kernel, rootfs, "
              "images/ and the ep122 UI assets.")
    else:
        print(f"OK: key verified, decrypted {size} bytes -> {args.out}")
        print("note: no ISO 9660 PVD at sector 16; the payload is still "
              "plaintext, inspect it before mounting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
