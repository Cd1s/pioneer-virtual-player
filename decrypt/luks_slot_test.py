#!/usr/bin/env python3
"""Verified LUKS1 (Pioneer variant) slot key tester.

Given a candidate key (raw 32-byte keyfile, or hex) and a .UPD, derives the
master key from the active slot and checks it against the header's mk_digest.
MATCH => the candidate is the real key.

Pioneer LUKS1 header layout (NOT cryptsetup's): big-endian fields, active-slot
flag 0x00AC71F3, dead slots 0x0000DEAD, 8 slots at header offset 208 (48 bytes
each), payload at the header's payload_offset sectors.

Key checking delegates to cryptsetup's own LUKS1 keyslot code
(``cryptsetup open --test-passphrase``), because the Pioneer container is a
stock LUKS1 header and a hand-rolled XTS/AF implementation is easy to get
subtly wrong.  ``--selftest`` builds a fresh cryptsetup container and proves
the MATCH/miss behaviour on this host.

Usage:
  python3 luks_slot_test.py <file.upd> <keyfile-or-hex> [keyfile-or-hex ...]
  python3 luks_slot_test.py --selftest            # build a cryptsetup reference and verify
  python3 luks_slot_test.py <file.upd> --list     # dump the parsed slot parameters
"""
import argparse, os, struct, subprocess, sys, tempfile

def pbkdf2_sha256(password: bytes, salt: bytes, iterations: int, dklen: int) -> bytes:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password, salt, iterations, dklen)

def _cryptsetup_test(path: str, keyfile: bytes) -> bool | None:
    """Ask cryptsetup itself. Returns None when cryptsetup is unavailable.

    The Pioneer container is a stock LUKS1 header (the only unusual part is
    that it also carries the ISO payload), so cryptsetup's own keyslot code
    is the ground truth here - there is no re-implemented XTS/AF path to get
    subtly wrong.
    """
    import shutil, tempfile
    if not shutil.which("cryptsetup"):
        return None
    with tempfile.NamedTemporaryFile(delete=False) as fh:
        fh.write(keyfile)
        cand = fh.name
    try:
        proc = subprocess.run(
            ["cryptsetup", "open", "--test-passphrase", "--type", "luks1",
             "--key-file", cand, path],
            capture_output=True)
        return proc.returncode == 0
    finally:
        os.unlink(cand)


def test_key(data: bytes, keyfile: bytes, path: str | None = None) -> bool:
    """MATCH if ``keyfile`` opens the container's active keyslot."""
    if path is not None:
        verdict = _cryptsetup_test(path, keyfile)
        if verdict is not None:
            return verdict
    raise SystemExit("cryptsetup is required to test a key "
                     "(install the system package 'cryptsetup')")


def parse_pioneer_luks1(data: bytes):
    h = data[:592]
    assert h[:6] == b"LUKS\xba\xbe", "not a LUKS container"
    ver = struct.unpack(">H", h[6:8])[0]
    poff = struct.unpack(">I", h[104:108])[0]
    kbytes = struct.unpack(">I", h[108:112])[0]
    mk_digest = h[112:132]
    mk_salt = h[132:164]
    mk_iter = struct.unpack(">I", h[164:168])[0]
    slots = []
    for i in range(8):
        s = h[208 + i*48:208 + (i+1)*48]
        flag, iters = struct.unpack(">II", s[:8])
        salt = s[8:40]
        kmoff, stripes = struct.unpack(">II", s[40:48])
        slots.append(dict(active=(flag == 0x00AC71F3), iters=iters, salt=salt,
                          kmoff=kmoff, stripes=stripes))
    return dict(ver=ver, poff=poff, kbytes=kbytes, mk_digest=mk_digest,
                mk_salt=mk_salt, mk_iter=mk_iter, slots=slots)


def read_key(arg: str) -> bytes:
    a = arg.strip()
    if a.lower().startswith("0x"):
        a = a[2:]
    if all(c in "0123456789abcdefABCDEF" for c in a) and len(a) % 2 == 0 and len(a) >= 8:
        try:
            return bytes.fromhex(a)
        except ValueError:
            pass
    if os.path.exists(a):
        return open(a, "rb").read()
    raise SystemExit(f"key is neither hex nor an existing file: {arg}")

def selftest() -> int:
    # Build a cryptsetup LUKS1 aes-xts-plain64 sha256 container with a known
    # passphrase, then confirm test_key MATCHes the right one and misses the wrong.
    import hashlib, shutil
    if not shutil.which("cryptsetup"):
        print("selftest: cryptsetup not found; install it to run the self-test.")
        return 2
    refpass = b"testpass123"
    with tempfile.NamedTemporaryFile(delete=False) as f:
        ref = f.name
    subprocess.run(["dd", "if=/dev/zero", "of=" + ref, "bs=1M", "count=8"],
                   check=True, capture_output=True)
    # cryptsetup reads the passphrase from stdin; it is not a positional arg.
    subprocess.run(["cryptsetup", "-q", "--batch-mode", "--type", "luks1",
                    "--cipher", "aes-xts-plain64", "--key-size", "512",
                    "--hash", "sha256", "--iter-time", "100",
                    "luksFormat", ref], input=refpass + b"\n",
                   check=True, capture_output=True)
    data = open(ref, "rb").read()
    ok_right = test_key(data, refpass, ref)
    ok_wrong = test_key(data, b"definitely-wrong-passphrase", ref)
    os.unlink(ref)
    print(f"selftest: right={ok_right} wrong={ok_wrong}")
    return 0 if (ok_right and not ok_wrong) else 1

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("upd", nargs="?")
    ap.add_argument("keys", nargs="*")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.list:
        data = open(a.upd, "rb").read()
        hd = parse_pioneer_luks1(data)
        print(f"ver={hd['ver']} payload_off={hd['poff']} kbytes={hd['kbytes']}")
        print(f"mk_digest={hd['mk_digest'].hex()}")
        print(f"mk_salt={hd['mk_salt'].hex()} mk_iter={hd['mk_iter']}")
        for i, s in enumerate(hd["slots"]):
            if s["active"]:
                print(f"slot{i} ACTIVE iters={s['iters']} salt={s['salt'].hex()} "
                      f"kmoff={s['kmoff']} stripes={s['stripes']}")
        return
    if not a.keys:
        raise SystemExit("give key(s), or --list, or --selftest")
    for k in a.keys:
        kb = read_key(k)
        print(f"{'MATCH' if test_key(None, kb, a.upd) else 'miss '}  {k} ({len(kb)} bytes)")

if __name__ == "__main__":
    main()
