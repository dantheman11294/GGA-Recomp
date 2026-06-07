#!/usr/bin/env python3
"""splat_to_syms.py -- convert splat's disassembly (.s files) into an N64Recomp
symbols TOML.

Two modes:

  * single-section (default, back-compatible):
        splat_to_syms.py ASM_DIR --section .main --out gga.syms.toml
    All discovered functions go into one [[section]].

  * segment-aware (pass --yaml CONFIG):
        splat_to_syms.py ASM_DIR --yaml splat_gga/goemonsgreatadv.yaml \
                         --out gga.splat.syms.toml
    Emits one [[section]] per `type: code` segment in the splat YAML
    (resident `main` -> ".main", plus each overlay e.g. file_5/file_7/...).
    Each function is assigned to the segment whose ROM range [start, next_start)
    contains it, and sizes are computed *within* a section -- which is required
    once overlays exist, because mutually-exclusive overlays share a VRAM window
    (e.g. file_7/8/9 all at 0x801738A0) and a global sort would size them against
    one another.

How functions are read:
  splat emits `glabel <name>` per function (overlay names may carry a `_<rom>`
  suffix to disambiguate colliding vrams, e.g. func_801738A0_5CAEE0). Each
  instruction line carries `/* <rom_hex> <vram_hex> <word_hex> */`. A function's
  VRAM and ROM are taken from its FIRST instruction line (authoritative -- we do
  NOT parse vram out of the label name). A function spans from its glabel to the
  next glabel; its last instruction line + 4 fixes its end, clamped so it never
  crosses the next function in the same section.
"""
import argparse, glob, os, re, sys

# `/* <rom_hex> <vram_hex> <word_hex> */`
INSTR_RE  = re.compile(r'/\*\s*([0-9A-Fa-f]+)\s+([0-9A-Fa-f]{8})\s+[0-9A-Fa-f]{8}\s*\*/')
# `glabel <name>` (allow _<rom> suffixes and any label chars)
GLABEL_RE = re.compile(r'^\s*glabel\s+(\S+)\s*$')


def parse_functions(asm_dir):
    """Return list of [start_vram, start_rom, name, last_instr_vram] for every
    function found under asm_dir. VRAM/ROM come from each function's first
    instruction line."""
    funcs = []
    for path in sorted(glob.glob(os.path.join(asm_dir, "**", "*.s"), recursive=True)):
        pending = None      # glabel name awaiting its first instruction
        cur = None          # index in `funcs` of the function being extended
        for line in open(path, errors="replace"):
            g = GLABEL_RE.match(line)
            if g:
                pending = g.group(1)        # a new glabel ends the previous function
                cur = None
                continue
            m = INSTR_RE.search(line)
            if not m:
                continue                    # internal label / blank / macro / .word
            rom = int(m.group(1), 16)
            vram = int(m.group(2), 16)
            if pending is not None:         # first instruction of this function
                funcs.append([vram, rom, pending, vram])
                cur = len(funcs) - 1
                pending = None
            if cur is not None and vram > funcs[cur][3]:
                funcs[cur][3] = vram        # extend end to this instruction
    if not funcs:
        sys.exit(f"no glabel functions found under {asm_dir} (is this splat asm output?)")
    return funcs


def load_code_segments(yaml_path):
    """Read the splat config and return code segments with their ROM ranges.
    Each returned dict: name, rom, vram, rom_end, overlay, exclusive_ram_id."""
    import yaml
    d = yaml.safe_load(open(yaml_path))
    segs = d["segments"]

    def start_of(s):
        if isinstance(s, dict):
            return s.get("start")
        if isinstance(s, list) and s:
            return s[0]                     # bare end-marker, e.g. [0x1000000]
        return None

    starts = sorted(x for x in (start_of(s) for s in segs) if x is not None)

    def rom_end(start):
        for s in starts:
            if s > start:
                return s
        return None

    SKIP = {"header", "ipl3"}               # entry is emitted as ".entry" (holds recomp_entrypoint)
    out = []
    for s in segs:
        if not isinstance(s, dict) or s.get("type") != "code":
            continue
        if s.get("name") in SKIP:
            continue
        out.append({
            "name": s["name"],
            "rom": s["start"],
            "vram": s.get("vram"),
            "rom_end": rom_end(s["start"]),
            "overlay": bool(s.get("overlay")),
            "exclusive_ram_id": s.get("exclusive_ram_id"),
        })
    return out


def load_overrides(path):
    """Parse 'func_NAME 0xSIZE' lines. Dedupe identical (name,size); error on
    conflicting sizes for the same name. Returns {name: size}."""
    if not path or not os.path.exists(path):
        return {}
    seen = {}
    with open(path) as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) != 2:
                sys.exit(f"{path}:{lineno}: expected 'func_NAME 0xSIZE', got: {raw!r}")
            name, size_s = parts
            try:
                size = int(size_s, 0)
            except ValueError:
                sys.exit(f"{path}:{lineno}: bad size {size_s!r} for {name}")
            if size <= 0:
                sys.exit(f"{path}:{lineno}: non-positive size {size_s} for {name}")
            if name in seen and seen[name] != size:
                sys.exit(f"{path}:{lineno}: conflicting size for {name}: "
                         f"{seen[name]:#x} vs {size:#x}")
            seen[name] = size
    return seen


def apply_overrides(funcs, overrides):
    """For each override, force the named function's extent to [vram, vram+size)
    and DROP any other symbol whose vram falls strictly inside that range.
    Runs on the raw [vram, rom, name, last] list before section sizing."""
    if not overrides:
        return funcs
    by_name = {f[2]: f for f in funcs}
    drop_vrams = set()
    for name, size in overrides.items():
        tgt = by_name.get(name)
        if tgt is None:
            print(f"  [override] WARNING: {name} not found in disassembly; ignoring")
            continue
        vram = tgt[0]
        new_end = vram + size
        tgt[3] = new_end - 4
        swallowed = [f for f in funcs if f is not tgt and vram < f[0] < new_end]
        for f in swallowed:
            drop_vrams.add(f[0])
            print(f"  [override] {name}: size -> {size:#x}; merging out "
                  f"{f[2]} @ {f[0]:#x}")
        if not swallowed:
            print(f"  [override] {name}: size -> {size:#x} (no symbols merged out)")
    return [f for f in funcs if f[0] not in drop_vrams]


def size_within_section(funcs_sorted):
    """funcs_sorted: list of [vram, rom, name, last_vram] sorted by vram.
    Returns [(vram, rom, name, size)] sized within this section only."""
    out = []
    for i, (vram, rom, name, last) in enumerate(funcs_sorted):
        size = last + 4 - vram
        if i + 1 < len(funcs_sorted):
            size = min(size, funcs_sorted[i + 1][0] - vram)
        if size <= 0:
            sys.exit(f"non-positive size for {name} @ {vram:#x} -> {size:#x}")
        out.append((vram, rom, name, size))
    return out


def render_section(sec_name, sec_rom, sec_vram, funcs_sorted, overlay=False, eram=None):
    sized = size_within_section(funcs_sorted)
    last_vram, _, _, last_size = sized[-1]
    sec_size = (last_vram + last_size) - sec_vram
    L = []
    if overlay:
        L.append(f"# overlay segment (exclusive_ram_id = {eram}) -- shares its VRAM")
        L.append(f"# window with other overlays of the same id; resolved at runtime.")
    L.append("[[section]]")
    L.append(f'name = "{sec_name}"')
    L.append(f"rom = {sec_rom:#x}")
    L.append(f"vram = {sec_vram:#x}")
    L.append(f"size = {sec_size:#x}")
    L.append("functions = [")
    for vram, rom, name, size in sized:
        L.append(f'  {{ name = "{name}", vram = {vram:#x}, size = {size:#x} }},')
    L.append("]")
    return "\n".join(L), len(sized)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("asm_dir")
    ap.add_argument("--section", default=".main",
                    help="section name in single-section mode (default .main)")
    ap.add_argument("--yaml",
                    help="splat config path; enables one [[section]] per code segment")
    ap.add_argument("--out", default="gga.syms.toml")
    ap.add_argument("--overrides",
                    help="size-overrides file (func_NAME 0xSIZE per line)")
    args = ap.parse_args()

    funcs = parse_functions(args.asm_dir)
    funcs = apply_overrides(funcs, load_overrides(args.overrides))

    # ---- single-section (back-compatible) ----
    if not args.yaml:
        funcs.sort(key=lambda f: f[0])
        body, n = render_section(args.section, funcs[0][1], funcs[0][0], funcs)
        with open(args.out, "w") as f:
            f.write("# Generated by splat_to_syms.py (single-section)\n\n" + body + "\n")
        print(f"wrote {args.out}: 1 section ({args.section}), {n} functions")
        return

    # ---- segment-aware ----
    segs = load_code_segments(args.yaml)
    blocks = ["# Generated by splat_to_syms.py (segment-aware) from splat disassembly.\n"]
    nsec = total = 0
    for seg in segs:
        lo, hi = seg["rom"], seg["rom_end"]
        sel = [f for f in funcs if lo <= f[1] and (hi is None or f[1] < hi)]
        if not sel:
            print(f"  (skip {seg['name']}: no functions in rom "
                  f"{lo:#x}..{'end' if hi is None else hex(hi)})")
            continue
        sel.sort(key=lambda f: f[0])
        if seg["name"] == "main":
            sec_name = ".main"
        elif seg["name"] == "entry":
            sec_name = ".entry"
            # N64Recomp locates the start of execution by the function literally
            # named "recomp_entrypoint" at the entrypoint vram. Rename the entry
            # function (splat calls it "entrypoint") so the tool finds it.
            ev = seg["vram"] if seg["vram"] is not None else sel[0][0]
            for fn in sel:
                if fn[0] == ev:
                    fn[2] = "recomp_entrypoint"
                    break
        else:
            sec_name = seg["name"]
        sec_vram = seg["vram"] if seg["vram"] is not None else sel[0][0]
        body, n = render_section(sec_name, seg["rom"], sec_vram, sel,
                                 overlay=seg["overlay"], eram=seg["exclusive_ram_id"])
        blocks.append(body)
        nsec += 1
        total += n
        tag = f"  [overlay:{seg['exclusive_ram_id']}]" if seg["overlay"] else ""
        print(f"  {sec_name:16} {n:>4} funcs  vram {sec_vram:#010x}  rom {seg['rom']:#x}{tag}")

    with open(args.out, "w") as f:
        f.write("\n\n".join(blocks) + "\n")
    print(f"wrote {args.out}: {nsec} sections, {total} functions total")


if __name__ == "__main__":
    main()
