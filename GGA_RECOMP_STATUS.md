# Goemon's Great Adventure (GGA) Recompilation — Status & Findings

ROM: gga.us.z64 — 16MB (0x1000000), SHA1 9f9e860816f9e2a68bb5f4f56086e0c7450c64d7,
XXH3_64 0xA62841FD58D33B4E, internal name "GOEMONS GREAT ADV", game code NGME (@0x3B).
Base project: klorfmorf's MNSG recomp (Goemon64Recomp runtime + N64ModernRuntime).

=== DONE THIS SESSION (durable, validated) ===

1. SYMBOL MAP REBUILT VIA SPLAT
- Old hand map covered ~10% (111 funcs) — root cause of all earlier artifacts.
- splat (splat_gga/) produced 621 correctly-bounded functions.
- splat_to_syms.py converts splat .s -> N64Recomp syms TOML.
- Active configs: gga.splat.toml + gga.splat.syms.toml.

2. CLEAN RECOMPILE + LAUNCHING BINARY
- N64Recomp_tool gga.splat.toml -> exit 0 (RECOMPILE CLEAN).
- Build -> Goemon64Recomp/build/Goemon64Recompiled (112MB) links and runs.
- GGA identity patched in src/main/main.cpp (~L355):
    .rom_hash = 0xA62841FD58D33B4EULL
    .internal_name = "GOEMONS GREAT ADV"
    .game_id = u8"gga.us", .mod_game_id = "gga"
    .decompression_routine = nullptr, .has_compressed_code = false  (Option A raw-ROM)

3. OPTION A VALIDATED: boots into recompiled game code
- ROM gate PASSES. Menu works, Select ROM -> Start Game.
- RT64 inits (RX 7900 XTX), recomp heap inits.
- Executes recomp_entrypoint -> func_80000DE0 (game main) -> func_800121D0.
- Crash: SIGSEGV to NULL at funcs_0.c:1876 in main, because STUBBED boot/init code
  never populated a function pointer. Expected; confirms end-to-end execution. Fix = Option B.

4. BREAKTHROUGH: cracked compression, produced decompressed ROM
- Konami Nisitenma-Ichigo file table. Header "Nisitenma-Ichigo" @ ROM 0x2C784.
- TABLE STARTS @ 0x2C794 (after 16-char header).
- Entries: big-endian u32; (word & 0x7FFFFFFF)=offset, bit31=compressed flag.
  File N spans entry[N]..entry[N+1].
- TERMINATOR: single zero word (offset==0) at entry 2577 (ROM 0x2EFD8).
- DUAL FORMAT by byte after the 4-byte size word:
    0x78 (zlib magic 78 da) -> zlib (~1942 files)
    else -> LZKN64 (1 file, entry 8); plus ~79 raw.
- decompress_gga.py: LZKN64 mirrors runtime lzkn64_decompress, decompresses IN-PLACE into
  shared buffer (back-refs see prior files); zlib via Python zlib.
- Output gga.us.decompressed.z64: 4446 prologues total (2213 overlay region) vs ~0 compressed.
  File 8 @ decompressed ROM 0x5D6300 starts 27 bd ff e0 (real code).

=== OPEN PROBLEM (where we stopped): overlay -> VRAM mapping ===

splat needs each overlay code file as overlay:yes segment with correct VRAM base
(cf MNSG file_11..14 in Goemon64Recomp/lib/mnsg/config/usa/splat.yaml:
 type:code overlay:yes vram:0x801CB460/0x8020D2A0 exclusive_ram_id:static_overlay_N).

Four decompressed code files call into 0x800D.... (targets 0x800D05E0/3E64/3FF0/41AC):
  file 5: decompressed ROM 0x582D30..0x5CAEE0 size 0x481B0  (165 jal->0x800D)
  file 7: 0x5CAEE0..0x5D6300 size 0xB420   (8)
  file 8: 0x5D6300..0x61A700 size 0x44400  (44)
  file 9: 0x61A700..0x645630 size 0x2AF30  (35)

Heuristic VRAM-base sweep (jal target lands on 27bd prologue) only hit 4-14% -> the
"fixed base, jals point at own funcs" model is WRONG. Likely: 0x800D.... is a shared
dispatch/library region loaded separately, and/or mutually-exclusive overlays sharing a
VRAM window (MNSG pattern). Answer is in the game's LOADER (DMA code in .main), not bytes.

=== NEXT STEPS (Option B back half) ===

1. FIND THE LOADER in splat_gga/asm/1060.s: the fn that reads the table (refs addr of
   table @ vram for ROM 0x2C794) and DMAs files to 0x800D.... dests. It states ROM->VRAM
   mapping explicitly. MNSG names DMA fns (osPiStartDma=0x800407D0).
   Or ask recomp Discord (discord.gg/H2RAnQ4Vec): "GGA NGME, Nisitenma-Ichigo table @0x2C794,
   code files idx 5/7/8/9 calling 0x800D.... — what VRAM do they load to?"
2. NEW splat config targeting gga.us.decompressed.z64 (copy goemonsgreatadv.yaml, baserom ->
   decompressed, add overlay:yes segments per file with discovered vram + exclusive_ram_id).
3. RE-SPLAT -> 0x800D.... funcs become real. Regenerate syms via splat_to_syms.py
   (extend for multiple/overlay sections).
4. RECOMPILE — most boot stubs vanish (targets now exist), fixing funcs_0.c:1876 NULL crash.
5. RUNTIME: add decompress_gga mirroring decompress_mnsg in src/game/rom_decompression.cpp,
   BUT runtime lzkn64_decompress_rom only does LZKN64 — ADD A ZLIB BRANCH (miniz available
   via lib/N64ModernRuntime/thirdparty/miniz). Params: NGME @0x3B, FILE_TABLE_OFFSET 0x2C794,
   single-zero terminator, bit31=zlib-vs-lzkn64. Compute GGA decompressed CRC, set it.
   Then .has_compressed_code=true, .decompression_routine=goemon64::decompress_gga.

=== KEY FILES (in ~/recomp unless noted) ===
gga.us.z64                       raw ROM
gga.us.decompressed.z64          decompressed ROM (this session's output)
decompress_gga.py                dual-format (zlib+LZKN64) decompressor
splat_to_syms.py                 splat .s -> N64Recomp syms
overlay_scope.py                 overlay/code-region helper (heuristic; superseded for VRAM)
recomp_orchestrator.py           automation harness
gga.splat.toml / .syms.toml      active recompiler config (raw-ROM, Option A)
splat_gga/                       splat project (goemonsgreatadv.yaml, asm/, undefined_funcs_auto.txt)
Goemon64Recomp/build/Goemon64Recompiled   112MB launching binary (Option A)
Goemon64Recomp/src/main/main.cpp ~L355     GGA GameEntry
Goemon64Recomp/src/game/rom_decompression.cpp   MNSG decompressor (template)
Goemon64Recomp/lib/mnsg/config/usa/splat.yaml   MNSG overlay template (file_11..14)

=== RECURRING LESSON ===
Byte-layout/root-cause guessing repeatedly failed; reading actual source (runtime
decompressor, splat output, MNSG config) or using authoritative tools succeeded.
For overlay VRAM mapping: read the loader / ask community, don't sweep bytes.

=== SESSION UPDATE: overlay recompilation ===
SOLVED this session:
- Overlay VRAM map via D_80026AF8 window table:
    file_5 @ 0x800C7B10 (resident shared library; owns 0x800D.. exports)
    file_7/8/9 @ 0x801738A0 (mutually-exclusive stage overlays, exclusive_ram_id)
- splat retargeted to gga.us.decompressed.z64 (was reading compressed ROM -> garbage)
- entrypoint declared as recomp_entrypoint in .entry section (splat_to_syms.py)
- relocatable_sections registered via gga.overlays.txt (MNSG schema)
- Trailing rodata boundaries fixed for all 4 overlays (splat YAML [code][rodata] split):
    file_5 rodata @ 0x800FA6A4 | file_7 @ 0x80175C68
    file_8 @ 0x801A6DA0       | file_9 @ 0x8019AD58 (corrected from scanner)
  -> function count 2752 -> 2634, eliminated data-as-code error class
- Tools added: autofix_sizes.py (resize/stub loop w/ guards), scan_overlay_rodata.py

OPEN (next session): overlay function bodies don't all recompile.
- Recurring errors: "Unhandled branch", "Invalid alignment on sw", bogus jal targets,
  ALL pointing to splat needing INTERIOR rodata/boundary analysis per overlay
  (small data tables at function starts / between functions, e.g. file_7 has data
  at +0x10 from its start: 0x801738B0 reads as branch to 0x80C23890).
- file_5 currently fully stubbed (850 funcs) as extern library to isolate file_7/8/9.
- recomp_overlays.inl still empty -> need ONE clean run to validate registration.
- FIX DIRECTION: enable splat find_file_boundaries + per-overlay rodata segmentation
  (mirror how MNSG decomp tunes overlay rodata), re-split, re-gen syms, recompile.
