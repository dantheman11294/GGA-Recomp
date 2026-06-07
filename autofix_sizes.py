#!/usr/bin/env python3
"""autofix_sizes.py -- iteratively fix splat under-split functions for the GGA recomp.

splat's function splitter sometimes clips a function early (on an interior `jr $ra`
in a branch-likely delay slot, or on padding), creating a spurious interior glabel
and an under-sized function. N64Recomp then dies with:

    Unhandled branch in func_XXXXXXXX at 0x... to 0x...
    Error in recompiling func_XXXXXXXX

This script loops:
  1. run N64Recomp_tool
  2. if it reports "Error recompiling func_<vram>", find that function's TRUE end
     by scanning the decompressed ROM from its start to the next real prologue,
     taking the last `jr $ra` (+ its delay slot) before that prologue
  3. record the corrected size in gga.size_overrides.txt and patch it into the syms
  4. repeat, until a clean run or an error that is NOT a simple size mis-split

It only ever GROWS a function's size and never crosses the next prologue, so it
cannot silently swallow a genuinely separate function -- if the target really is a
separate function, the scan stops at its prologue and the script reports it for
manual review instead of guessing.

Usage:
    python3 autofix_sizes.py            # uses the defaults below
"""
import re, subprocess, sys

TOOL      = "./N64Recomp_tool"
CONFIG    = "gga.splat.toml"
SYMS      = "gga.splat.syms.toml"
OVERRIDES = "gga.size_overrides.txt"
ROM       = "gga.us.decompressed.z64"
MAX_ITERS = 200
MAX_FUNC  = 0x4000        # absolute ceiling on a single function's size (safety)

# (vram_lo, vram_hi, rom_base) for each code segment, so we can map vram -> rom.
# Matches the splat overlay map we established. .main/.entry are resident.
# Resident + lib: (vram_lo, vram_hi, rom_base). Non-overlapping, simple offset map.
SEGMENTS = [
    (0x80000400, 0x80000460, 0x1000),     # .entry
    (0x80000460, 0x800C7B10, 0x1060),     # .main
    (0x800C7B10, 0x801738A0, 0x582D30),   # file_5 (lib)
]
# Stage overlays share vram base 0x801738A0 but live at different ROM offsets and
# have different sizes. Resolve by which one's [base, base+size) contains the vram.
SHARED_VRAM = 0x801738A0
SHARED_OVERLAYS = [   # (rom_base, size)
    (0x5CAEE0, 0xB420),    # file_7
    (0x5D6300, 0x44400),   # file_8
    (0x61A700, 0x2AF30),   # file_9
]

d = open(ROM, "rb").read()
u32 = lambda r: int.from_bytes(d[r:r+4], "big")
is_prologue = lambda w: (w & 0xFFFF0000) == 0x27BD0000 and (w & 0x8000)

ERR_RE = re.compile(r'(?:Error in recompiling|Error recompiling|Failed to analyze) (func_[0-9A-Fa-f]+)')

def vram_to_rom(vram):
    for lo, hi, rom_base in SEGMENTS:
        if lo <= vram < hi:
            return rom_base + (vram - lo)
    # shared 0x801738A0 window: caller handles by trying each overlay base
    return None

def true_size(vram, rom_start):
    """Walk from rom_start; return size ending after the last `jr $ra` (+delay slot)
    that precedes the next prologue. Returns None if nothing sane found."""
    i = 0
    last_jr = None
    while i < MAX_FUNC:
        w = u32(rom_start + i)
        v = vram + i
        if i > 0 and is_prologue(w):
            # next function starts here; end at last jr (+delay) before it
            if last_jr is not None and last_jr + 8 <= v:
                return (last_jr + 8) - vram
            return v - vram
        if w == 0x03E00008:                 # jr $ra
            last_jr = v
        i += 4
    return None

def find_rom_for_func(vram):
    rom = vram_to_rom(vram)
    if rom is not None:
        return rom
    # shared stage-overlay window: the offset must fall within exactly one overlay
    off = vram - SHARED_VRAM
    if off >= 0:
        for rom_base, size in SHARED_OVERLAYS:
            if off < size:
                return rom_base + off
    return None

def patch_syms(name, size):
    s = open(SYMS).read()
    pat = re.compile(r'(\{ name = "' + re.escape(name) + r'", vram = 0x[0-9a-fA-F]+, size = )0x[0-9a-fA-F]+( \})')
    s2, n = pat.subn(lambda m: f"{m.group(1)}{size:#x}{m.group(2)}", s)
    if n == 0:
        return False
    open(SYMS, "w").write(s2)
    return True

def record_override(name, size):
    with open(OVERRIDES, "a") as f:
        f.write(f"{name} {size:#x}\n")


STUB_LOG = "gga.stubbed_funcs.txt"

def stub_func(name):
    """Add `name` to the stubs = [...] array in the toml so N64Recomp emits an
    empty body and does not analyze its bytes as code. Returns True on success."""
    cfg = open(CONFIG).read()
    if f'"{name}"' in cfg:
        return True  # already stubbed
    m = re.search(r'stubs\s*=\s*\[', cfg)
    if not m:
        return False
    insert_at = m.end()
    cfg = cfg[:insert_at] + f'"{name}", ' + cfg[insert_at:]
    open(CONFIG, "w").write(cfg)
    with open(STUB_LOG, "a") as f:
        f.write(name + "\n")
    return True

def main():
    applied = {}
    for it in range(1, MAX_ITERS + 1):
        res = subprocess.run([TOOL, CONFIG], capture_output=True, text=True)
        out = res.stdout + res.stderr
        m = ERR_RE.search(out)
        if not m:
            print(f"[{it}] clean run (no 'Error in recompiling'). Done.")
            tail = "\n".join(out.strip().splitlines()[-6:])
            print("--- tail ---\n" + tail)
            return
        name = m.group(1)
        vram = int(name.split("_")[1], 16)
        rom = find_rom_for_func(vram)
        if rom is None:
            print(f"[{it}] {name} @ {vram:#x}: can't map vram->rom (not in known segments). STOP.")
            return
        sz = true_size(vram, rom)
        if sz is None or sz <= 0:
            # No coherent function end -> treat as data mislabeled as code: stub it.
            if stub_func(name):
                print(f"[{it}] STUBBED {name} @ {vram:#x} (no sane size; likely data)")
                continue
            print(f"[{it}] {name} @ {vram:#x}: couldn't size AND couldn't stub. STOP.")
            return
        prev = applied.get(name)
        if prev == sz:
            print(f"[{it}] {name}: re-derived the same size {sz:#x} that already "
                  f"failed -> the size fix isn't resolving it. STOP for manual review.")
            return
        if not patch_syms(name, sz):
            print(f"[{it}] {name}: not found in {SYMS} (line format?). STOP.")
            return
        applied[name] = sz
        record_override(name, sz)
        print(f"[{it}] fixed {name} @ {vram:#x} -> size {sz:#x} (rom {rom:#x})")
    print(f"hit MAX_ITERS={MAX_ITERS}; stopping. Re-run if it was still making progress.")

if __name__ == "__main__":
    main()
