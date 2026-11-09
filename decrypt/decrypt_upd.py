#!/usr/bin/env python3
"""Decrypt a Pioneer LUKS1 .UPD to its ISO payload, given the 32-byte key.

First verifies the key against the header's mk_digest (same logic as
luks_slot_test.py), then XTS-decrypts the whole payload and writes the ISO.

Usage:
  python3 decrypt_upd.py <file.upd> <keyfile-or-hex> -o <out.iso>
"""
import argparse, struct, sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from luks_slot_test import (parse_pioneer_luks1, pbkdf2_sha256,
                            xts_decrypt_block, af_merge, read_key, gf_mul_le)

def derive_master(data, s, keyfile):
    slot_key = pbkdf2_sha256(keyfile, s["salt"], s["iters"], 64)
    dk, tk = slot_key[:32], slot_key[32:]
    km = data[s["kmoff"]*512:(s["kmoff"]*512) + 64*s["stripes"]]
    stripes = [xts_decrypt_block(km[i*64:(i+1)*64], dk, tk, i) for i in range(s["stripes"])]
    return af_merge(stripes)

def decrypt(data, master, out_path):
    hd = parse_pioneer_luks1(data)
    dk, tk = master[:32], master[32:]
    poff = hd["poff"]                     # first payload sector
    payload = data[poff*512:]
    nsec = len(payload)//512
    out = bytearray(nsec*512)
    for i in range(nsec):
        sec = payload[i*512:(i+1)*512]
        out[i*512:(i+1)*512] = xts_decrypt_block(sec, dk, tk, i)
    with open(out_path, "wb") as f:
        f.write(bytes(out))
    return nsec

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("upd"); ap.add_argument("key"); ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args()
    data = open(a.upd, "rb").read()
    hd = parse_pioneer_luks1(data)
    kb = read_key(a.key)
    active = [s for s in hd["slots"] if s["active"]]
    if not active:
        raise SystemExit("no active slot")
    master = derive_master(data, active[0], kb)
    check = pbkdf2_sha256(master, hd["mk_salt"], hd["mk_iter"], 20)
    if check != hd["mk_digest"]:
        raise SystemExit("key does NOT match this .UPD's mk_digest (wrong key or wrong file)")
    n = decrypt(data, master, a.out)
    print(f"OK: key verified, decrypted {n} sectors -> {a.out}")
    print("next: this is an ISO 9660 image. Mount it (or 7z/isoinfo) and take "
          "images/, the kernel, rootfs and the ep122 UI binary + assets.")

if __name__ == "__main__":
    main()
