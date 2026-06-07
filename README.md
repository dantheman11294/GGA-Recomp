# GGA-Recomp

Work-in-progress static recompilation of **Goemon's Great Adventure** (Nintendo 64, US) using [N64Recomp](https://github.com/N64Recomp/N64Recomp).

This repo contains the GGA-specific tooling, configuration, and reverse-engineering work developed to extend the existing [Goemon64Recomp](https://github.com/klorfmorf/Goemon64Recomp) project (which currently targets Mystical Ninja Starring Goemon) to support GGA.

This project is a means to an end. My main goal is to try and automate the process of an n64 recomp project as much as possible from ROM to playable EXE. I am looking into building an agentic workflow that could potentially accomplish this - but as I am new to N64 modding in general, it's a far more practical first step to get this one game working and go from there.

---

## Current Status

- ✅ Nisitenma-Ichigo file table fully reversed (dual zlib + LZKN64 compression)
- ✅ Decompressed ROM produced (`gga.us.decompressed.z64`)
- ✅ 621-function symbol map generated via splat + `splat_to_syms.py`
- ✅ GGA `GameEntry` patched into the recompiler (ROM hash, internal name, game ID)
- ✅ Binary builds and boots into recompiled game code (Option A / raw ROM)
- ✅ Overlay VRAM map resolved from `D_80026AF8` window descriptor table:
  - `file 5` → `0x800C7B10` resident shared library (owns `0x800D…` exports)
  - `file 7/8/9` → `0x801738A0` mutually-exclusive stage overlays
- 🔄 Full overlay splat config in progress
- 🔄 Runtime dual-format decompressor (zlib branch via miniz) in progress

---

## Repo Contents

| File/Dir | Description |
|---|---|
| `decompress_gga.py` | Dual-format Nisitenma-Ichigo decompressor (zlib + LZKN64); produces `gga.us.decompressed.z64` |
| `splat_to_syms.py` | Converts splat `.s` disassembly → N64Recomp `.syms.toml` symbol map |
| `overlay_scope.py` | Overlay window analysis tooling |
| `gga.splat.toml` | N64Recomp recompiler config (active, Option A raw ROM) |
| `gga.splat.syms.toml` | Generated symbol map (621 functions) |
| `splat_gga/goemonsgreatadv.yaml` | splat disassembly config for GGA |
| `splat_gga/symbol_addrs.txt` | Named symbols for splat |
| `splat_gga/undefined_funcs_auto.txt` | Splat undefined function list |
| `GGA_RECOMP_STATUS.md` | Detailed session notes and reverse-engineering findings |
| `ghidra_scripts/` | Ghidra helper scripts used during analysis |

The Mystical Ninja Starring Goemon/GGA host project lives at [Goemon64Recomp](https://github.com/klorfmorf/Goemon64Recomp) and is not duplicated here.

---

## How It Works

GGA uses Konami's **Nisitenma-Ichigo** file system (same as MNSG). Files are compressed with either zlib (`0x78 0xDA` magic) or LZKN64 (bit 31 of the table entry set). The loader (`func_80003FF0` in main) walks a group descriptor table (`D_80026AF8`) to assign each file to a fixed VRAM window at runtime.

I used Claude to help me here.

Key VRAM windows discovered:

| Window | Range | Role |
|---|---|---|
| `0x800C7310` | `..0x800C7B10` | Small resident region |
| `0x800C7B10` | `..0x801738A0` | **Shared library** (file 5; exports `0x800D…` functions) |
| `0x801738A0` | `..varies` | Stage overlays (files 7/8/9, mutually exclusive) |
| `0x08000000` | `..varies` | TLB-mapped overlays |
| `0x8036A000`+ | heap/staging | DMA staging arena |

---

## Prerequisites

- [N64Recomp](https://github.com/N64Recomp/N64Recomp) — static recompiler tool
- [splat](https://github.com/ethteck/splat) — N64 ROM disassembler/splitter
- [Goemon64Recomp](https://github.com/klorfmorf/Goemon64Recomp) — recompiler host project (clone separately)
- Python 3.10+, `lzkn64` pip package
- A US copy of Goemon's Great Adventure (`gga.us.z64`, SHA not distributed here)

---

## Credits

This project stands entirely on the shoulders of:

- **[klorfmorf](https://github.com/klorfmorf)** — author of [Goemon64Recomp](https://github.com/klorfmorf/Goemon64Recomp) (the recompiler host this work extends) and the [MNSG decompilation](https://github.com/klorfmorf/mnsg), whose engine reverse-engineering, splat configs, overlay window structure, `tools/rommy.py` dual-format decompressor, and LZKN64 library were the primary reference for all GGA work here. The Goemon64Recomp project is by klorfmorf and the MNSG decomp team.

- **[Mr-Wiseguy / N64Recomp](https://github.com/N64Recomp/N64Recomp)** — the static recompilation framework that makes this entire class of project possible.

- **[ethteck / splat](https://github.com/ethteck/splat)** — N64 ROM disassembly and splitting tool used to generate the GGA symbol map.

- **[The MNSG decompilation team](https://github.com/klorfmorf/mnsg)** — whose named symbols, overlay structure, and Nisitenma-Ichigo format documentation were essential references throughout.

- **[Ghidra](https://ghidra-sre.org/) + [N64LoaderWV](https://github.com/zeroKilo/N64LoaderWV)** — used for initial static analysis of GGA's main code segment.

- **The Ganbare Goemon Discord** — community support and context for GGA-specific reverse-engineering questions.

---

## License

Tooling and configs in this repo are released under MIT. The Goemon64Recomp host project and N64Recomp are under their respective licenses. No game assets or ROM data are included or distributed.
