# GGA-Recomp

Work-in-progress static recompilation of **Goemon's Great Adventure** (Nintendo 64, US) using [N64Recomp](https://github.com/N64Recomp/N64Recomp).

This repo contains the GGA-specific tooling, configuration, and reverse-engineering work developed to extend the existing [Goemon64Recomp](https://github.com/klorfmorf/Goemon64Recomp) project (which targets Mystical Ninja Starring Goemon, "MNSG") to support GGA. The two games share Konami's N64 engine and SDK build, which is what makes cross-referencing MNSG practical.

This project is a means to an end. The longer-term goal is to explore how much of the ROM-to-EXE recomp process can be automated. Getting one game working end-to-end first is the practical starting point.

**No game code or assets are distributed by this project.** You must supply your own legally-obtained GGA ROM. The build generates recompiled C from *your* ROM locally; the generated code is never committed or shipped (see `.gitignore`).

---

## Current Status

The recompiled binary **builds, links, and boots into live recompiled game code**, running through the OS/libultra layer and into the engine's asset-loading phase.

- ✅ Decompressed ROM produced (`gga.us.decompressed.z64`); the recompiler config targets it directly
- ✅ Symbol map: **2633 functions across 6 sections** (resident `.entry` + `.main`, plus 4 overlay-region sections) via splat + `splat_to_syms.py`
- ✅ Recompiles clean and links to a native binary
- ✅ **Boots** through: CPU init → FPU enable → OS/libultra layer → thread creation → VI init → start of asset loading
- ✅ **libultra interception**: 38 functions byte-identified against MNSG's same-engine libultra and renamed so N64Recomp routes them to the runtime's implementations (`osInitialize`, thread/message/timer ops, cache/TLB ops, `osVi*`, `osSpTask*`, `osCont*`, etc.). See `gga.reimpl_names.txt`.
- 🔄 **Current frontier — the engine's overlay/asset loader.** The game's file loader reaches the runtime's `osEPiRawStartDma` guard. GGA's overlay system differs from MNSG's (no byte match) and GGA has **no decompilation**, so wiring GGA's loader to the runtime's `recomp_load_overlays` / `overlay_apply_relocations` API is the next phase (reverse-engineering + a game-side `RECOMP_PATCH`, and/or guidance from the recomp dev community).
- ⚠️ **~44% of GGA's code (1165 of 2639 functions) lives in runtime-loaded overlays.** Overlay loading is the gate to executing that code, which is why it is the next major milestone rather than a side issue.

### Known debt (tracked for GitHub issues)
- A small set of functions are temporarily **stubbed** with justification: `__osViInit` (runtime owns VI via RT64), a PI domain-config read and a DOM2/64DD absent-device probe (both vestigial under the runtime), COP0/cache privileged ops, and one function hitting an N64Recomp label-emission bug. Each is documented in `GGA_RECOMP_STATUS.md` and intended to be replaced with proper handling.
- One vendored-runtime change (tolerating the CU1 FPU-enable Status bit) is kept as a tracked patch in `runtime_patches/` rather than committed into the upstream tree.

---

## Repo Contents

| File/Dir | Description |
|---|---|
| `decompress_gga.py` | Dual-format Nisitenma-Ichigo decompressor (zlib + LZKN64); produces `gga.us.decompressed.z64` |
| `splat_to_syms.py` | Converts splat disassembly → N64Recomp `.syms.toml`; applies size overrides, the reserved-`main` rename, and the libultra renames from `gga.reimpl_names.txt` |
| `fix_zero_loads.py` | Post-processes recompiled C: rewrites illegal `0 = MEM_x(...)` (from `lw $zero`) to `(void)(...)`, and asserts none remain |
| `find_bad_labels.py` | Scans generated C for `goto L_X` without a matching `L_X:` (N64Recomp label-emission failures) |
| `gga.reimpl_names.txt` | GGA `func_<addr>` → libultra name map (the 38 renames the runtime provides), byte-verified vs MNSG |
| `gga.libultra_names.txt` | Further byte-matched libultra/library names held for a later batch (cosmetic / not-yet-needed) |
| `gga.size_overrides.txt` | Manual function-size corrections for mis-split functions |
| `gga.splat.toml` | N64Recomp recompiler config (targets the decompressed ROM) |
| `gga.splat.syms.toml` | Generated symbol map (2633 functions) |
| `runtime_patches/` | Tracked diffs applied to the vendored N64ModernRuntime (e.g. CU1 status tolerance) — not committed into the upstream tree |
| `splat_gga/goemonsgreatadv.yaml` | splat disassembly config for GGA |
| `splat_gga/symbol_addrs.txt` | Named symbols for splat (GGA addresses) |
| `GGA_RECOMP_STATUS.md` | Detailed session notes and current findings |
| `ghidra_scripts/` | Ghidra helper scripts used during analysis |

The recompiler host project lives at [Goemon64Recomp](https://github.com/klorfmorf/Goemon64Recomp) and is cloned separately (not duplicated here).

---

## How It Works

GGA uses Konami's **Nisitenma-Ichigo** file system (same as MNSG). Files are compressed with either zlib (`78 DA` magic) or LZKN64 (bit 31 of the file-table entry set), with some stored raw. `decompress_gga.py` produces a fully decompressed ROM, which the recompiler then consumes.

The recompiler translates each MIPS function to C. Generic, cross-game layers (CPU, and libultra/OS functions, which are byte-identical across same-SDK games) are handled by **naming** GGA's anonymous `func_<addr>` symbols with their libultra names so N64Recomp's `reimplemented_funcs` mechanism routes them to the runtime's implementations instead of recompiling raw hardware-register access. These names were derived by byte-matching GGA's functions against MNSG's named, same-engine libultra (verified against GGA's own ROM bytes — reloc-masked comparison for functions with game-specific addresses, exact comparison to disambiguate register-distinguished primitives).

Game-specific systems — most importantly the **overlay/asset loader** — are not shared and must be handled per-game (the reference MNSG port did this with a full decompilation; GGA has none, so this is active reverse-engineering work).

---

## Prerequisites

- [N64Recomp](https://github.com/N64Recomp/N64Recomp) — static recompiler tool
- [splat](https://github.com/ethteck/splat) — N64 ROM disassembler/splitter
- [Goemon64Recomp](https://github.com/klorfmorf/Goemon64Recomp) — recompiler host project (clone separately)
- Python 3.10+, `lzkn64` pip package
- A US copy of Goemon's Great Adventure (`gga.us.z64`) — **you provide your own; no ROM or ROM-derived content is distributed here**

---

## Credits

This project stands entirely on the shoulders of:

- **[klorfmorf](https://github.com/klorfmorf)** — author of [Goemon64Recomp](https://github.com/klorfmorf/Goemon64Recomp) (the recompiler host this work extends) and the [MNSG decompilation](https://github.com/klorfmorf/mnsg), whose engine reverse-engineering, splat configs, overlay structure, dual-format decompressor, and LZKN64 work were the primary reference for all GGA work here. MNSG's named symbols served as the dictionary against which GGA's equivalents were independently byte-identified; no MNSG code or data is copied into this repo.
- **[Mr-Wiseguy / N64Recomp](https://github.com/N64Recomp/N64Recomp)** and the **N64ModernRuntime** authors — the static recompilation framework and runtime that make this class of project possible.
- **[ethteck / splat](https://github.com/ethteck/splat)** — N64 ROM disassembly/splitting used to generate the GGA symbol map.
- **[Ghidra](https://ghidra-sre.org/) + [N64LoaderWV](https://github.com/zeroKilo/N64LoaderWV)** — initial static analysis of GGA's main segment.
- **The Ganbare Goemon Discord** — community support and GGA-specific context.

---

## License

Tooling and configs in this repo are released under MIT. The Goemon64Recomp host project, N64Recomp, and N64ModernRuntime are under their respective licenses. **No game assets, ROM data, or recompiled game code are included or distributed** — the recompiled C is generated locally from the user's own ROM and is gitignored.
