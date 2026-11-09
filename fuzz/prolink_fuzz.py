#!/usr/bin/env python3
"""Network ProLink fuzzer for a live XDJ-AZ / CDJ-3000 (no key, no disassembly).

Joins the LAN, sends edge-case ProLink UDP datagrams to the target, and after each
one sends a benign probe to see whether the deck still answers. A deck that stops
answering has hit a parser edge case -> that datagram is the one to study.

This is a first-pass interoperability input runner. Studying the parser path it reveals
and reading the container key the deck already holds is the firmware-recovery step,
see ../firmware-recovery/README.md.

Usage:
  python3 prolink_fuzz.py --target 192.168.1.50 --port 50000 --count 40 --interval 3
  python3 prolink_fuzz.py --target 192.168.1.50 --probe-only      # baseline: does it answer?
"""
import argparse, socket, struct, sys, time, random

def u16(v): return struct.pack("<H", v & 0xFFFF)
def rand_str(n): return "".join(chr(random.randint(97,122)) for _ in range(n)).encode()

# documented ProLink types
T_KEEPALIVE, T_STATUS, T_MIXER, T_BEAT, T_POS, T_ONAIR = 0x05, 0x0a, 0x29, 0x28, 0x0b, 0x03
T_MASTER_OFF, T_MASTER_ON, T_SYNC = 0x26, 0x27, 0x2a

def crafted(idx):
    mode = idx % 4
    if mode == 0:   # status with huge length
        plen = random.choice([0xFFFF, 0x10000, 0xFFFFFFFF])
        return f"status-len-{plen:#x}", bytes([T_STATUS, idx & 0xFF]) + u16(plen) + rand_str(16)
    if mode == 1:   # beat with huge count
        return "beat-count-0xffff", bytes([T_BEAT, idx & 0xFF]) + u16(0xFFFF) + rand_str(8)
    if mode == 2:   # unknown type
        t = random.choice([0x00, 0x01, 0x02, 0x07, 0x08, 0x09, 0x0c, 0x0d, 0xff])
        return f"unknown-type-{t:#x}", bytes([t, idx & 0xFF]) + u16(4) + rand_str(4)
    return "rogue-master-0x63", bytes([T_MASTER_ON, 0x63]) + u16(2) + u16(3)

def sendto(sock, target, port, data):
    sock.sendto(data, (target, port))

def probe(sock, target, port, timeout=2.0):
    """Send a benign keepalive-ish probe; return True if the deck answers anything."""
    sock.settimeout(timeout)
    # a minimal keepalive as player number 0x71 (a free slot)
    sendto(sock, target, port, bytes([T_KEEPALIVE, 0x71]) + u16(0))
    try:
        sock.recvfrom(2048)
        return True
    except socket.timeout:
        return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="XDJ-AZ / CDJ IP")
    ap.add_argument("--port", type=int, default=50000)
    ap.add_argument("--count", type=int, default=40)
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--probe-only", action="store_true")
    ap.add_argument("--seed", type=int, default=0xBEEF)
    a = ap.parse_args()
    random.seed(a.seed)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    if a.probe_only:
        ok = probe(sock, a.target, a.port)
        print(f"probe-only: deck {a.target}:{a.port} {'answers' if ok else 'does NOT answer (down or wrong IP/port)'}")
        return

    print(f"fuzzing {a.target}:{a.port} with {a.count} crafted packets, probe interval {a.interval}s")
    down_at = None
    for i in range(a.count):
        name, pkt = crafted(i)
        sendto(sock, a.target, a.port, pkt)
        time.sleep(1.0)
        alive = probe(sock, a.target, a.port)
        tag = "alive" if alive else "*** DOWN ***"
        print(f"[{i:03d}] sent {name:24s} -> {tag}")
        if not alive and down_at is None:
            down_at = (i, name)
            print(f"      deck stopped answering after packet [{i:03d}] {name}")
            print("      waiting 20s for possible auto-recovery...")
            time.sleep(20)
            if probe(sock, a.target, a.port):
                print("      recovered (rebooted) -> crash confirmed, candidate =", name)
            else:
                print("      still down -> hard crash / needs power cycle; candidate =", name)
            break
        time.sleep(a.interval - 1.0)
    if down_at is None:
        print("no crash in this batch. Increase --count, try --port 50001/50002, or the dbserver TCP path.")

if __name__ == "__main__":
    main()
