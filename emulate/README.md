# Perfect emulation: run the factory firmware in QEMU (cdj3k-emu)

Once you have the key and the decrypted factory ISO, cdj3k-emu boots the real deck
firmware in QEMU. That is the "perfect" virtual CDJ-3000 / XDJ-AZ: the genuine UI,
genuine audio path, genuine ProLink endpoint - link it to the Denon unit over the LAN.

## Steps

1. Grab the cdj3k-emu binary for your host (prebuilt, no Go/QEMU install needed for
   the app itself; it bundles what it needs). See `artifacts/` - if the XDJ-AZ build
   is not there yet, cdj3k-emu currently ships cdj3k/cdj3kx/cdj1500x; the XDJ-AZ is
   the next model to add (same LUKS1 + ISO + `ep122` shape).
2. Provision: `cdj3k-emu --provision <firmware.iso-or.upd> --key <keyfile>` (the app
   decrypts the `.UPD` with your key and assembles the initramfs, exactly as the
   deck would).
3. Launch the instance; it comes up as a virtual player on the LAN.
4. Point the Denon (EngineX OS, Pro DJ Link on) at it. The virtual deck serves the
   rekordbox library, waveforms (ANLZ), and full transport - the thing that was
   missing in the no-key simulator.

## Why this is "perfect" vs the no-key simulator

The no-key route (OpenProLink `cdj3000-sim`) only fakes the ProLink protocol; it has
no real UI, no real audio, and no real library parser - which is exactly why waveforms
didn't show on the PRIME 4. cdj3k-emu runs the *actual* firmware, so everything the
real deck does, it does.

## XDJ-AZ note

cdj3k-emu does not list an XDJ-AZ model yet. Because the XDJ-AZ is the same
Rockchip + LUKS1 + ISO + `ep122` family, adding it is a model-spec + firmware task,
not a new decryptor. The key you extract from the physical XDJ-AZ is what unlocks it.
