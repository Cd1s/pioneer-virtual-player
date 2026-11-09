#!/bin/sh
# One-shot download of all artifacts (official update containers + community binaries).
# Official .UPD: AlphaTheta firmware feed. Community: GitHub releases.
set -e
cd "$(dirname "$0")/artifacts"
A="https://downloads.support.alphatheta.com/firmwares"
R="https://github.com"
echo "[1/6] XDJ-AZ v2.00 (primary, LUKS family)..."
curl -sSL -o x.zip "$A/all-in-one-dj-systems/XDJ-AZ/XDJAZv200.zip" && unzip -o -q x.zip XDJAZv200.UPD && rm -f x.zip
echo "[2/6] CDJ-3000 v3.22 (reference, LUKS family)..."
curl -sSL -o x.zip "$A/dj-players/CDJ-3000/CDJ3Kv322.zip" && unzip -o -q x.zip CDJ3Kv322.UPD && rm -f x.zip
echo "[3/6] XDJ-XZ v1.26 (different non-LUKS format)..."
curl -sSL -o x.zip "$A/all-in-one-dj-systems/XDJ-XZ/XDJXZ_v126.zip" && unzip -o -q x.zip && rm -f x.zip
[ -f XDJXZ.UPD ] && mv XDJXZ.UPD XDJXZ_v126.UPD
echo "[4/6] cdj3k-emu (faithful QEMU emulator)..."
curl -sSL -o cdj3k-emu-linux-aarch64.AppImage "$R/nsaintot/cdj3k-emu/releases/download/v0.4.1/CDJ3K-Emulator-0.4.1-linux-aarch64.AppImage"
echo "[5/6] OpenProLink (ProLink probe + virtual player)..."
curl -sSL -o OpenProLink-linux-arm64.tar.gz "$R/Cd1s/OpenProLink/releases/latest/download/OpenProLink-v0.1.0-linux-arm64.tar.gz"
echo "[6/6] cdj3k-root service-access package (standalone CDJ-3000)..."
curl -sSL -o CDJ3Kv000.UPD.root "$R/emmaworley/cdj3k-root/releases/download/v3.14/CDJ3Kv000.UPD"
openssl ecparam -name prime256v1 -genkey -noout -out cdj_private.ecdsa
openssl ec -in cdj_private.ecdsa -pubout -out fallback-authorized_keys 2>/dev/null
echo "done. artifacts/ is now populated."
ls -la
