#!/usr/bin/env python3
"""overlay_scope.py -- automate N64 overlay discovery for the recomp pipeline.

Given a ROM and the already-mapped .main code range, this tool:
  1. Finds candidate CODE regions via MIPS function-prologue density, filtering out
     data false-positives (isolated hits in graphics/audio).
  2. Searches the ROM for the overlay LOAD TABLE: records of (rom_start, rom_end,
     vram_addr) whose rom range matches a detected code region. This is the mapping
     splat needs (rom offset <-> load vram).
  3. Cross-checks against out-of-range jal targets (overlay entry points the recompiler
     reported) -- each should land inside a discovered overlay's vram span.
  4. Emits ready-to-paste splat segment YAML for each overlay.

No manual hex-parsing required: run it, get segments.
"""
import argparse, re, struct, json, sys

PROLOGUE = re.compile(rb'\x27\xbd[\xf0-\xff]')      # addiu $sp, $sp, -N
JR_RA    = b'\x03\xe0\x00\x08'                      # jr $ra
BLOCK    = 0x10000

def prologue_density(rom):
    blocks = {}
    for m in PROLOGUE.finditer(rom):
        blocks[m.start() // BLOCK] = blocks.get(m.start() // BLOCK, 0) + 1
    return blocks

def code_regions(rom, min_density=15, main_end=0x40000):
    """Contiguous runs of 64KB blocks with >= min_density prologues, past .main.
    min_density filters the 1-2/block data false-positives."""
    blocks = prologue_density(rom)
    hot = sorted(b for b, c in blocks.items() if c >= min_density and b * BLOCK >= main_end)
    regions = []
    for b in hot:
        if regions and b == regions[-1][1] + 1:
            regions[-1][1] = b
        else:
            regions.append([b, b])
    # convert block-ranges to byte ranges, refined to first prologue / last jr-ra
    out = []
    for lo_b, hi_b in regions:
        start = lo_b * BLOCK; end = (hi_b + 1) * BLOCK
        fp = PROLOGUE.search(rom, start, end)
        first = fp.start() if fp else start
        last_jr = rom.rfind(JR_RA, start, end)
        last = (last_jr + 4) if last_jr != -1 else end
        out.append((first, last))
    return out

def find_load_table(rom, regions, main_lo=0x1000, main_hi=0x40000):
    """Scan .main's data for (rom_start, rom_end, vram) triples (big-endian u32) whose
    rom_start matches a detected code region. Returns discovered overlay records."""
    recs = []
    region_starts = {r[0] for r in regions}
    # tolerate the table living anywhere in the first part of the rom
    for off in range(0, min(len(rom) - 12, 0x100000), 4):
        a, b, c = struct.unpack_from('>III', rom, off)
        # a,b look like a rom range into a code region; c looks like a KSEG0 vram
        if a in region_starts and b > a and (b - a) < 0x80000 and 0x80000000 <= c < 0x80800000:
            recs.append({"table_off": off, "rom_start": a, "rom_end": b, "vram": c})
    return recs

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("--main-end", type=lambda x: int(x, 0), default=0x40000)
    ap.add_argument("--min-density", type=int, default=15)
    ap.add_argument("--jal-targets", default="", help="comma-sep out-of-range jal targets to cross-check")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rom = open(args.rom, "rb").read()
    regions = code_regions(rom, args.min_density, args.main_end)
    table = find_load_table(rom, regions)
    targets = [int(t, 0) for t in args.jal_targets.split(",") if t.strip()]

    report = {"rom_size": len(rom), "code_regions": [], "load_table": table, "segments_yaml": []}
    for (start, end) in regions:
        # match a load-table record to this region for the vram AND authoritative end
        rec = next((r for r in table if r["rom_start"] == start), None)
        vram = rec["vram"] if rec else None
        if rec:                                  # load table gives the true rom extent
            end = rec["rom_end"]
        size = end - start
        covered = [hex(t) for t in targets if vram is not None and vram <= t < vram + size]
        report["code_regions"].append({
            "rom_start": hex(start), "rom_end": hex(end), "size": hex(size),
            "vram": hex(vram) if vram else "UNKNOWN (no load-table match)",
            "jal_targets_covered": covered})
        if vram is not None:
            report["segments_yaml"].append(
                f"  - name: ovl_{vram:08x}\n    type: code\n    start: {hex(start)}\n"
                f"    vram: {hex(vram)}\n    subsegments:\n      - [{hex(start)}, asm]")

    if args.json:
        print(json.dumps(report, indent=2)); return
    print("=== detected code regions past .main ===")
    for r in report["code_regions"]:
        print(f"  ROM {r['rom_start']}..{r['rom_end']} (size {r['size']})  vram {r['vram']}"
              + (f"  covers jal targets {r['jal_targets_covered']}" if r['jal_targets_covered'] else ""))
    print(f"\n=== overlay load-table records found: {len(table)} ===")
    for r in table:
        print(f"  @rom 0x{r['table_off']:X}: rom 0x{r['rom_start']:X}..0x{r['rom_end']:X} -> vram 0x{r['vram']:X}")
    print("\n=== ready-to-paste splat segments ===")
    print("\n".join(report["segments_yaml"]) or "  (none -- load table not found; see notes)")

if __name__ == "__main__":
    main()
