#!/usr/bin/env python3
"""scan_overlay_rodata.py -- find code/rodata boundaries inside GGA overlays and
emit splat subsegment lists.

The overlays (file_5/7/8/9) interleave real MIPS functions with rodata: jump
tables, pointer arrays, float/coefficient blobs, and zero padding. splat's
function splitter mislabels much of that rodata as tiny "functions", which then
break N64Recomp. Rather than stub each one, we detect the data regions directly
from the decompressed ROM and tell splat where code ends and rodata begins.

Method (per overlay):
  * Walk the overlay word-by-word from its known function START.
  * Maintain a position cursor that follows REAL functions: at each function
    start (27bd prologue) we scan to its terminating `jr $ra` (+delay slot) to
    find its end. Everything between a function end and the next prologue is a
    gap -> candidate rodata (only if it isn't immediately another prologue).
  * Coalesce gaps and prologue-led runs into [code]/[rodata] regions, merging
    tiny code islands that are actually mislabeled data (no prologue, low code
    density) into the surrounding rodata.
  * Emit `- [<rom>, asm]` / `- [<rom>, rodata]` subsegment lines for the YAML.

This is a heuristic boundary finder, not a disassembler -- it errs toward marking
ambiguous regions as rodata (safe: splat emits bytes, recompiler ignores) and
prints the regions so they can be eyeballed before committing.
"""
import sys

DEC = "gga.us.decompressed.z64"
d = open(DEC, "rb").read()
u32 = lambda r: int.from_bytes(d[r:r+4], "big")
is_prologue = lambda w: (w & 0xFFFF0000) == 0x27BD0000 and (w & 0x8000)
is_jr_ra    = lambda w: w == 0x03E00008

# Overlays: name -> (vram_base, rom_base, size)
OVERLAYS = {
    "file_5": (0x800C7B10, 0x582D30, 0x481B0),
    "file_7": (0x801738A0, 0x5CAEE0, 0x0B420),
    "file_8": (0x801738A0, 0x5D6300, 0x44400),
    "file_9": (0x801738A0, 0x61A700, 0x2AF30),
}

def looks_like_code_word(w):
    """Very loose: is this plausibly a MIPS instruction (not obviously data)?"""
    op = w >> 26
    # common opcodes: special(0), regimm(1), j/jal(2,3), branches(4-7),
    # immediate ALU(8-15), load/store(32-46), cop(16-19), etc.
    return op in (0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,
                  16,17,18,32,33,34,35,36,37,38,40,41,42,43,44,45,46,49,53,57,61)

def find_func_end(rom_start, limit_rom):
    """From a prologue at rom_start, return rom offset just past the function's
    terminating jr $ra (+delay slot). None if no jr ra before limit."""
    i = rom_start
    last_jr = None
    while i < limit_rom:
        w = u32(i)
        if is_jr_ra(w):
            last_jr = i
            # peek: if the word after the delay slot is a prologue or padding, end here
            nxt = i + 8
            if nxt >= limit_rom or is_prologue(u32(nxt)) or u32(nxt) == 0:
                return nxt
        i += 4
    return (last_jr + 8) if last_jr else None

def scan_overlay(name, vram, rom, size):
    end_rom = rom + size
    regions = []   # list of (kind, rom_start, rom_end)
    cur = rom
    while cur < end_rom:
        w = u32(cur)
        if is_prologue(w):
            fend = find_func_end(cur, end_rom)
            if fend is None or fend <= cur:
                fend = cur + 4
            # extend a code region across consecutive functions
            if regions and regions[-1][0] == "code" and regions[-1][2] == cur:
                regions[-1] = ("code", regions[-1][1], fend)
            else:
                regions.append(("code", cur, fend))
            cur = fend
        else:
            # data run: advance until the next prologue
            start = cur
            while cur < end_rom and not is_prologue(u32(cur)):
                cur += 4
            if regions and regions[-1][0] == "rodata" and regions[-1][2] == start:
                regions[-1] = ("rodata", regions[-1][1], cur)
            else:
                regions.append(("rodata", start, cur))

    # merge tiny code islands (< 0x40, i.e. <16 instrs) surrounded by rodata into rodata:
    # those are almost always mislabeled data that happened to start with a 27bd word.
    merged = []
    for kind, s, e in regions:
        if (kind == "code" and (e - s) < 0x40
                and merged and merged[-1][0] == "rodata"):
            # absorb into preceding rodata; also pull a following rodata in next pass
            merged[-1] = ("rodata", merged[-1][1], e)
        elif merged and merged[-1][0] == kind and merged[-1][2] == s:
            merged[-1] = (kind, merged[-1][1], e)
        else:
            merged.append((kind, s, e))
    # second pass: re-coalesce adjacent same-kind after island absorption
    final = []
    for r in merged:
        if final and final[-1][0] == r[0] and final[-1][2] == r[1]:
            final[-1] = (final[-1][0], final[-1][1], r[2])
        else:
            final.append(r)
    return final

def main():
    for name, (vram, rom, size) in OVERLAYS.items():
        regions = scan_overlay(name, vram, rom, size)
        code_b = sum(e - s for k, s, e in regions if k == "code")
        data_b = sum(e - s for k, s, e in regions if k == "rodata")
        print(f"\n=== {name}  vram {vram:#x} rom {rom:#x} size {size:#x} "
              f"({len(regions)} regions, code {code_b:#x} / rodata {data_b:#x}) ===")
        # print the splat subsegment block (rom offsets), with vram annotations
        for k, s, e in regions:
            v = vram + (s - rom)
            kind = "asm" if k == "code" else "rodata"
            print(f"    - [{s:#x}, {kind}]    # {v:#x}..{vram+(e-rom):#x}  ({e-s:#x})")
    print("\n# Paste each overlay's block as that segment's `subsegments:` list.")
    print("# Boundaries are heuristic -- rodata regions are safe (bytes only);")
    print("# verify any region marked rodata that you expect to be code.")

if __name__ == "__main__":
    main()
