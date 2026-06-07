#!/usr/bin/env python3
"""Remove non-word-aligned function entries from gga.syms.toml"""
import re

path = "/home/bazzite/recomp/gga.syms.toml"
lines = open(path).readlines()
out = []
skipped = 0

for line in lines:
    m = re.match(r'\s*\{ name = ".*", vram = (0x[0-9A-Fa-f]+)', line)
    if m:
        vram = int(m.group(1), 16)
        if vram % 4 != 0:
            skipped += 1
            continue
    out.append(line)

open(path, "w").writelines(out)
print(f"Removed {skipped} unaligned entries, {len(out)} lines remain")
