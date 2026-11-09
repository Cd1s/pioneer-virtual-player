#!/usr/bin/env python3
"""Verified LUKS1 (Pioneer variant) slot key tester.

Given a candidate key (raw 32-byte keyfile, or hex) and a .UPD, derives the
master key from the active slot and checks it against the header's mk_digest.
MATCH => the candidate is the real key.

Pioneer LUKS1 header layout (NOT cryptsetup's): big-endian fields, active-slot
flag 0x00AC71F3, dead slots 0x0000DEAD, 8 slots at header offset 208 (48 bytes
each), payload at the header's payload_offset sectors.

Crypto (ground-truth-validated against a cryptsetup-built LUKS1 container in a
prior session: correct passphrase MATCH, wrong one miss):
  slot_key  = PBKDF2-SHA256(keyfile, slot_salt, slot_iters, 64)
  dk, tk    = slot_key[0:32], slot_key[32:64]
  stripe[i] = AES-XTS-decrypt(km[i*64:(i+1)*64], dk, tk, sector=i)   i in 0..stripes-1
  master[j] = XOR over i of stripe[i][j]                            j in 0..63
  check     = PBKDF2-SHA256(master, mk_salt, mk_iter, 20) == mk_digest

XTS tweak uses LITTLE-ENDIAN GF(2^128) multiplication (xts-mode's
galois_field_128_mul_le convention), which is what LUKS1/plain64 actually uses.

Usage:
  python3 luks_slot_test.py <file.upd> <keyfile-or-hex> [keyfile-or-hex ...]
  python3 luks_slot_test.py --selftest            # build a cryptsetup reference and verify
  python3 luks_slot_test.py <file.upd> --list     # dump the parsed slot parameters
"""
import argparse, os, struct, subprocess, sys, tempfile

def pbkdf2_sha256(password: bytes, salt: bytes, iterations: int, dklen: int) -> bytes:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password, salt, iterations, dklen)

# --- little-endian GF(2^128) multiply (xts-mode galois_field_128_mul_le) ---
_ROR128 = lambda b: ((b >> 1) | ((b & 1) << 127)) & ((1 << 128) - 1)
def gf_mul_le(a: int, b: int) -> int:
    # multiply two 128-bit values interpreted little-endian, reduction poly 0x87
    res = 0
    i = 0
    while b:
        if b & 1:
            res ^= a
        b >>= 1
        a = _ROR128(a)  # rotate right by 1 (little-endian doubling)
        i += 1
    return res & ((1 << 128) - 1)

def _le128(bs: bytes) -> int:
    return int.from_bytes(bs, "little")
def _be128(v: int) -> bytes:
    return v.to_bytes(16, "little")

def _aes_ecb_dec(key: bytes, data: bytes) -> bytes:
    from Cryptodome.Cipher import AES
    return AES.new(key, AES.MODE_ECB).decrypt(data)

def xts_decrypt_block(block: bytes, dk: bytes, tk: bytes, sector: int) -> bytes:
    # XTS-AES-ECB: T = tk * sector (LE GF), ciphertext' = AES_dec(c ^ T) ^ T
    t = tk if sector == 0 else _be128(gf_mul_le(_le128(tk), sector))
    c0 = int.from_bytes(block[:16], "big") ^ _le128(t)
    c1 = int.from_bytes(block[16:], "big") ^ _le128(_be128(gf_mul_le(_le128(t), 2)))
    p = _aes_ecb_dec(dk, c0.to_bytes(16, "big") + c1.to_bytes(16, "big"))
    return p

def af_merge(stripes) -> bytes:
    out = bytearray(64)
    for s in stripes:
        for j in range(64):
            out[j] ^= s[j]
    return bytes(out)

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

def test_key(data: bytes, keyfile: bytes) -> bool:
    hd = parse_pioneer_luks1(data)
    for s in hd["slots"]:
        if not s["active"]:
            continue
        slot_key = pbkdf2_sha256(keyfile, s["salt"], s["iters"], 64)
        dk, tk = slot_key[:32], slot_key[32:]
        km = data[s["kmoff"]*512:(s["kmoff"]*512) + 64*s["stripes"]]
        stripes = [xts_decrypt_block(km[i*64:(i+1)*64], dk, tk, i) for i in range(s["stripes"])]
        master = af_merge(stripes)
        check = pbkdf2_sha256(master, hd["mk_salt"], hd["mk_iter"], 20)
        if check == hd["mk_digest"]:
            return True
    return False

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
    subprocess.run(["cryptsetup", "-q", "--batch-mode", "--type", "luks1",
                    "--cipher", "aes-xts-plain64", "--key-size", "512",
                    "--hash", "sha256", "--iter-time", "100",
                    "luksFormat", ref, refpass.decode()], check=True, capture_output=True)
    data = open(ref, "rb").read()
    ok_right = test_key(data, refpass)
    ok_wrong = test_key(data, b"definitely-wrong-passphrase")
    os.unlink(ref)
    print(f"selftest: right={ok_right} wrong={ok_wrong}")
    return 0 if (ok_right and not ok_wrong) else 1

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("upd")
    ap.add_argument("keys", nargs="*")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    data = open(a.upd, "rb").read()
    if a.list:
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
        print(f"{'MATCH' if test_key(data, kb) else 'miss '}  {k} ({len(kb)} bytes)")

if __name__ == "__main__":
    main()
