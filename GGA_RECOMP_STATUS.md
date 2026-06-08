# Goemon's Great Adventure (GGA) Recompilation — Status & Findings

**ROM:** `gga.us.z64` — 16 MB (0x1000000), SHA1 `9f9e860816f9e2a68bb5f4f56086e0c7450c64d7`,
internal name "GOEMONS GREAT ADV", game code NGME.
Decompressed target: `gga.us.decompressed.z64`, SHA1 `19ab1cd5f9e8df7d954ee8099907afcc40adf493`.
Resident VRAM->ROM mapping: `rom = vram - 0x7FFFF400` (same constant as MNSG; verified by byte-matching).
Base project: klorfmorf's MNSG recomp (Goemon64Recomp runtime + N64ModernRuntime).

---

## CURRENT STATE: boots through the OS layer into overlay loading

The recompiled binary builds, links, and **boots into live recompiled game code**. Execution
proceeds: recomp entrypoint -> CPU/FPU init -> OS/libultra layer (runtime-provided) -> thread
creation -> VI init -> start of the engine's asset/overlay loading. It currently stops at the
runtime's `osEPiRawStartDma` guard, reached via the game's overlay loader (see "Current frontier").

### The recompile->boot chain (done, verified)
1. **Correct ROM.** The recompiler config targets `gga.us.decompressed.z64`. (An earlier
   wrong-ROM config — pointing at the *compressed* ROM — was the root cause of a whole class
   of phantom "overlay won't recompile / interior rodata" problems. Fixed.)
2. **Symbol map.** splat -> `splat_to_syms.py` -> `gga.splat.syms.toml`: 2633 functions across
   6 sections (.entry, .main [619], file_5 [851], file_7 [33], file_8 [935], file_9 [194]).
3. **`main` rename.** `0x80000DE0` was labeled `main`, which C reserves; `splat_to_syms.py`
   renames it to `func_80000DE0`.
4. **`0 = MEM_x()` fixup.** `lw $zero` lowers to an illegal assignment-to-0; `fix_zero_loads.py`
   rewrites these to `(void)(...)` and asserts none remain after the pass (guard added after
   a missed-pattern build break).
5. **Size override.** `func_8000FD50` size corrected to merge a mis-split tail.
6. **One label-bug stub.** `func_8019ABE4` (a stage-overlay function) hits an N64Recomp
   label-emission bug (drops in-range branch targets); stubbed as debt pending a tool update.
7. **Links** as `Goemon64Recompiled` (RelWithDebInfo; Debug suppressed weak symbol emission).

### Runtime boot fixes (done, verified)
8. **CU1 Status tolerance.** The runtime's `cop0_status_write` asserted on any unmodeled Status
   bit; it now tolerates CU1 (0x20000000, FPU-enable), a no-op in static recomp.
   Kept as `runtime_patches/cu1_status_tolerate.patch` (not committed into the upstream tree).
9. **libultra interception (the structural unlock).** GGA's splat symbols are anonymous
   `func_<addr>`; N64Recomp's `reimplemented_funcs` interception is **name-based**. So GGA's
   libultra functions were identified by byte-matching against MNSG's named, same-engine,
   same-SDK libultra (libultra is byte-identical across the two games):
   - **Method:** reloc-masked comparison (mask j/jal targets and lui/addiu/load/store
     immediates) for functions with game-specific addresses; exact comparison to disambiguate
     register-distinguished primitives (e.g. SI vs SP vs DP device-busy). Verified against
     GGA's *own* ROM bytes — never assumed from MNSG by role.
   - **Applied:** 38 functions renamed in `gga.reimpl_names.txt` — but **only** those whose
     `<name>_recomp` the runtime actually implements (reimplemented_funcs membership != runtime
     implementation exists; naming an unimplemented one causes an undefined-reference link error).
     Includes `osInitialize` (which collapses the entire SI-init crash chain), `osCreateThread`/
     `osStartThread`, `osSendMesg`/`osRecvMesg`, `osSpTask*`, `osVi*`, `osContStartQuery`,
     `osSetTimer`, cache/TLB ops, the `__ull/__ll` math helpers, etc.
   - **Held:** ~110 further byte-matched library names in `gga.libultra_names.txt` (cosmetic
     or not-yet-needed) for a later batch.

### Boot progression observed (each fix advanced the frontier)
- Initial: SIGSEGV in `__osSiDeviceBusy` (SI register poll `0xA4800018`), reached via
  `osInitialize -> __osSiRawReadIo`. NOTE: the prior handoff's "funcs_0.c:1876 NULL crash"
  was a misdiagnosis — that is a boot *frame* three levels up, not the fault.
  -> Fixed by naming `osInitialize` (runtime provides it; the whole SI chain becomes runtime code).
- Then: game threads run (`[Game] IDLE`, `[Game] 5`, `[Game] PIMGR`). Crashes walked forward
  through game-thread code into hardware-touch functions.
- Stubbed (justified, vestigial-under-runtime): `func_800189FC` (PI domain-config read, runtime
  ignores), `func_80012460` (DOM2/64DD absent-device probe; verified read address unmapped and
  the game's own skip-path pre-zeroes the output, so returning 0 matches), `func_8001C8C0`
  (`__osViInit`; the runtime owns VI through `osViSwapBuffer`/`osViSetMode` + RT64, so VI
  hardware init is vestigial).
- **Current frontier:** the engine's overlay loader calls into the PI-manager DMA path
  (`__osDevMgrMain` = `func_80018B40`), which reaches the runtime's `osEPiRawStartDma` abort-stub.

---

## CURRENT FRONTIER: the overlay / asset loader (next phase)

**What the runtime expects.** The runtime reimplements the *high-level* DMA path:
`osPiStartDma_recomp` / `osEPiStartDma_recomp` perform DMA synchronously via `recomp::do_rom_read`
(reading the user's ROM), and `osCreatePiManager_recomp` is a deliberate no-op (no PI-manager
thread). For overlays specifically, the game's loader must be patched to call the runtime's
`recomp_load_overlays(rom_addr, ram, size)` and `overlay_apply_relocations(file_id, load_addr)`
(see MNSG's `patches/required.c`, which patches its overlay loader `func_80001C00_2800`).

**Why this is a distinct phase.** MNSG solved overlays using its **decompilation** — named loader,
named file tables (`D_800573D8`, `D_80054ACC`), named decompressor — and hand-written
`RECOMP_PATCH`es. GGA's overlay loader does **not** byte-match MNSG's (best masked match ~ random),
so GGA's overlay system genuinely differs, and GGA has **no decomp**. Replicating the approach
requires reverse-engineering GGA's overlay loader, file tables, and compression, then writing a
game-side patch against anonymous `func_<addr>` symbols.

**Scope.** Resident funcs: 1474; overlay-region funcs: 1165 (~44% of code). Overlay loading gates
execution of nearly half the game.

### Next steps
1. Identify GGA's overlay-loader function (the engine routine that reads the file table and
   DMAs files to their VRAM windows), its file tables, and its decompressor — by tracing from
   the DMA call site and byte-matching *game* functions where they match MNSG.
2. Write a `RECOMP_PATCH` for it that calls `recomp_load_overlays` + `overlay_apply_relocations`,
   mirroring MNSG's `func_80001C00_2800` patch but for GGA's structures.
3. Stand up a GGA patch build (the existing `patches.toml` pipeline is wired for MNSG's decomp;
   GGA needs its own patch config against GGA syms).
4. Consider consulting the recomp dev community / N64ModernRuntime (the `osEPiRawStartDma` guard
   message explicitly invites an issue) on wiring an overlay loader without a decomp.

### Hardware-access functions to file as GitHub issues (worklist)
From a scan of all functions touching MMIO (`lui 0xA4xx`), the **game-code** candidates needing
proper handling (vs. dead libultra leaves) are: `func_80010850`, `func_80010DD0`, `func_80012C30`,
`func_800189FC` (stubbed), `func_80012460` (stubbed), `func_8001CA00`, `func_8001E6C0`,
`func_8001EFF0`, `func_80020910`. Reachability is confirmed only as boot reaches each; file
issues as they are confirmed live, each documenting address, register block, behavior, and fix.

---

## KEY FILES (in ~/recomp unless noted)
- `gga.us.z64` — raw ROM (gitignored)
- `gga.us.decompressed.z64` — decompressed target ROM (gitignored)
- `decompress_gga.py` — dual-format (zlib + LZKN64) decompressor
- `splat_to_syms.py` — splat -> N64Recomp syms; applies size overrides + `main` rename + libultra renames
- `fix_zero_loads.py` — `0 = MEM_x()` -> `(void)(...)`, with zero-remaining guard
- `find_bad_labels.py` — scans generated C for unemitted goto labels
- `gga.reimpl_names.txt` — 38 applied libultra renames (runtime-provided only)
- `gga.libultra_names.txt` — ~110 further byte-matched names, held for later
- `gga.size_overrides.txt` — function-size corrections
- `gga.splat.toml` / `gga.splat.syms.toml` — active recompiler config + generated syms
- `runtime_patches/cu1_status_tolerate.patch` — tracked vendored-runtime change
- `splat_gga/` — splat project (goemonsgreatadv.yaml, asm/ [gitignored], symbol_addrs.txt)
- `Goemon64Recomp/` — recompiler host (separate clone, gitignored; not ours to commit to)
- MNSG reference (in the host tree): `lib/mnsg/config/japan_0/symbol_addrs/symbol_addrs.txt`
  (naming dictionary), `patches/required.c` (overlay-loader patch template)

## WORKING METHOD / RECURRING LESSONS
- One change at a time, attributable. Verify the artifact actually changed (mtime, grep); don't
  trust that a command ran.
- Read bytes before deciding. Verify against GGA; never assume MNSG transfers 1:1 — byte-match.
- Clean `RecompiledFuncs/*.c`, `funcs.h`, `recomp_overlays.inl` before every regen (stale files
  get globbed — cost several wasted build rounds); re-run `cmake -B build` when the file count
  changes; confirm `recomp_overlays.inl` was written before building.
- For silent SIGSEGV, use gdb for the backtrace (stdout is block-buffered and lost on crash).
- Prefer real fixes (rename -> runtime interception; correct patch) over stubs; stub only with
  evidence it's vestigial, and track each stub as debt.
- Never commit ROMs or generated game code (RecompiledFuncs/funcs_*.c, recomp_overlays.inl) — the
  "recut" pitfall. Addresses/offsets/symbol maps are facts and are fine; copied game code/data is not.
