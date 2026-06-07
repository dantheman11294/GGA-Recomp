#!/usr/bin/env python3
"""
recomp_orchestrator.py  --  deterministic build driver for the GGA N64Recomp port.

PHILOSOPHY
  The recompiler and C compiler are the ground-truth verifier. This loop applies
  ONLY known-good, rule-based fixes, then keeps a change exclusively when the
  objective error set strictly shrinks; otherwise it git-reverts the change.
  An LLM is consulted ONLY for failure patterns no rule matches, and its proposed
  action is validated by the same toolchain + rollback gate before it can persist.

  It edits gga.us.toml / gga.syms.toml with tomlkit (style-preserving) -- never
  regex/sed -- to avoid the file-corruption class of bug.

USAGE
  python3 recomp_orchestrator.py            # run the loop
  python3 recomp_orchestrator.py --dry-run  # parse + plan, apply nothing
  python3 recomp_orchestrator.py --max 100  # iteration cap (default 60)

It never auto-pip-installs and never deletes source files. On a stall or an
unknown failure with no LLM backend configured, it writes escalation.json and stops.
"""
import os, re, sys, json, struct, shutil, subprocess, argparse, datetime
try:
    import tomlkit
except ImportError:
    sys.exit("Missing dependency. Run:  pip install tomlkit --break-system-packages")

ROOT  = os.path.expanduser("~/recomp")
ROM   = os.path.join(ROOT, "gga.us.z64")
TOML  = os.path.join(ROOT, "gga.us.toml")
SYMS  = os.path.join(ROOT, "gga.syms.toml")
TOOL  = os.path.join(ROOT, "N64Recomp_tool")
REPO  = os.path.join(ROOT, "Goemon64Recomp")
RF    = os.path.join(REPO, "RecompiledFuncs")
BUILD = os.path.join(REPO, "build")
BIN   = os.path.join(BUILD, "Goemon64Recompiled")
RUNLOG= os.path.join(ROOT, "orchestrator_runs.jsonl")
SECTION_ROM, SECTION_VRAM, MAIN_SECTION = 0x1050, 0x80000450, ".main"

# ----------------------------------------------------------------------------- ROM
def load_rom():
    rom = open(ROM, "rb").read()
    magic = rom[:4]
    order = {b"\x80\x37\x12\x40":"z64", b"\x37\x80\x40\x12":"v64",
             b"\x40\x12\x37\x80":"n64"}.get(magic, "z64")
    return rom, order

def word_reader(rom, order):
    def w(off):
        b = rom[off:off+4]
        if len(b) < 4: return None
        if order == "n64": return struct.unpack("<I", b)[0]
        if order == "v64": return struct.unpack(">I", bytes([b[1],b[0],b[3],b[2]]))[0]
        return struct.unpack(">I", b)[0]
    return w

def off_for(vram): return SECTION_ROM + (vram - SECTION_VRAM)

COP0_REG = {9:"Count", 11:"Compare", 12:"Status", 13:"Cause", 14:"EPC", 15:"PRId", 16:"Config"}
def classify(w):
    op = (w >> 26) & 0x3F
    if op == 0x2F: return ("cache", f"cacheop=0x{(w>>16)&0x1F:02X}")
    if w == 0x0000000F: return ("sync", "")
    if op == 0x10:
        rs = (w >> 21) & 0x1F
        rd = (w >> 11) & 0x1F
        if rs == 0x00: return ("mfc0", f"${rd}={COP0_REG.get(rd,'?')}")
        if rs == 0x04: return ("mtc0", f"${rd}={COP0_REG.get(rd,'?')}")
        if (w >> 25) & 1:
            f = w & 0x3F
            return ({1:"tlbr",2:"tlbwi",6:"tlbwr",8:"tlbp",0x18:"eret"}.get(f, f"cop0_0x{f:02X}"), "")
    return (None, None)

def analyze(vram, w_at, maxw=0x200):
    """Return (size, [(vram,word,cat,info)...]) by disassembling to first `jr ra`."""
    o = off_for(vram); bad = []; size = None
    for i in range(maxw):
        word = w_at(o + i*4)
        if word is None: break
        c, info = classify(word)
        if c: bad.append((vram + i*4, word, c, info))
        if word == 0x03E00008: size = (i + 2) * 4; break
    return size, bad

def recover_func_end(start, target, w_at, bound=None, maxw=0x800):
    """For an orphaned branch label: the enclosing function's true end is the first
    `jr ra` (+delay slot) at/after the target. analyze()'s first-jr-ra stops too early
    on multi-return functions, so this scans from the TARGET. Returns end addr, or None
    if it would cross `bound` (the next mapped function) -- meaning the target is a
    separate missing function, not an under-sizing, so we escalate instead of guessing."""
    v = target
    for _ in range(maxw):
        if w_at(off_for(v)) == 0x03E00008:
            end = v + 8
            return None if (bound is not None and end > bound) else end
        v += 4
    return None

# size+register-aware canonical naming (accurate where unambiguous; else None)
def canonical_name(cats, size, bad):
    if "cache" in cats:
        return {0xAC:"osInvalDCache", 0x28:"osWritebackDCacheAll"}.get(size)  # 0x74 ambiguous -> None
    if cats & {"tlbr","tlbwi","tlbwr","tlbp"}:
        return {0xB8:"__osProbeTLB", 0xB4:"osMapTLB", 0x58:"osMapTLBRdb", 0x44:"osUnmapTLBAll"}.get(size)
    if "sync" in cats:
        return {0xE0:"osPiRawStartDma", 0xAC:"__osSiRawStartDma", 0x8C:"__osSpRawStartDma"}.get(size)
    if cats & {"mfc0","mtc0"}:
        regs = {b[3] for b in bad if b[2] in ("mfc0","mtc0")}
        if size == 0xC and any("Count"   in r for r in regs): return "osGetCount"
        if size == 0xC and any("Compare" in r for r in regs): return "__osSetCompare"
        if size == 0xC and any("Status"  in r for r in regs): return "__osGetSR"
        if size == 0x10 and any("Status" in r for r in regs): return "__osSetSR"
        if size == 0x20: return "__osDisableInt"
        if size == 0x1C: return "__osRestoreInt"
        if size == 0xA0: return "osSetIntMask"
    return None

# ----------------------------------------------------------------------------- git
def git(*args, check=True):
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True, check=check)

def ensure_git():
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        git("init", "-q"); git("config", "user.email", "orchestrator@local")
        git("config", "user.name", "recomp-orchestrator")
    # track only the two files we mutate
    git("add", "gga.us.toml", "gga.syms.toml", check=False)
    git("commit", "-q", "-m", "orchestrator: baseline", "--allow-empty", check=False)

def snapshot(msg):
    git("add", "gga.us.toml", "gga.syms.toml", check=False)
    git("commit", "-q", "-m", msg, "--allow-empty", check=False)
    return git("rev-parse", "HEAD").stdout.strip()

def rollback_to(rev):
    git("checkout", rev, "--", "gga.us.toml", "gga.syms.toml", check=False)

# ----------------------------------------------------------------------------- toml edits
def load_doc(path): return tomlkit.parse(open(path).read())
def save_doc(path, doc): open(path, "w").write(tomlkit.dumps(doc))

def syms_func_names(doc):
    names = {}
    for sec in doc.get("section", []):
        for fn in sec.get("functions", []):
            names[str(fn["name"])] = (sec, fn)
    return names

def main_section(doc):
    for sec in doc.get("section", []):
        if str(sec.get("name")) == MAIN_SECTION:
            return sec
    return None

def add_func_to_syms(doc, name, vram, size):
    sec = main_section(doc)
    if sec is None: raise RuntimeError(".main section not found in syms")
    if name in syms_func_names(doc): return            # idempotent: never add a duplicate
    # parse a snippet so vram/size keep hex rendering and match the file's existing style
    fn = tomlkit.parse(f'x = {{ name = "{name}", vram = {hex(vram)}, size = {hex(size)} }}')["x"]
    sec["functions"].append(fn)

def set_func_size(doc, name, size):
    for sec in doc.get("section", []):
        for fn in sec.get("functions", []):
            if str(fn["name"]) == name:
                fn["size"] = tomlkit.parse(f"x = {hex(size)}")["x"]
                return True
    return False

def dedupe_syms():
    """Heal duplicate-named functions (a bad run could have appended dupes). Keeps first."""
    doc = load_doc(SYMS); seen = set(); removed = []
    for sec in doc.get("section", []):
        fns = sec.get("functions")
        if fns is None: continue
        keep = []
        for fn in list(fns):
            nm = str(fn["name"])
            if nm in seen: removed.append(nm); continue
            seen.add(nm); keep.append(fn)
        if len(keep) != len(fns):
            del fns[:]
            for fn in keep: fns.append(fn)
    if removed:
        save_doc(SYMS, doc)
        print(f"dedupe_syms: removed {len(removed)} duplicate function entr(ies): {sorted(set(removed))}")
    return removed

def patch_list(doc, key):
    patches = doc.setdefault("patches", tomlkit.table())
    if key not in patches: patches[key] = tomlkit.array()
    return patches[key]

def add_to(doc, key, name):
    arr = patch_list(doc, key)
    if name not in [str(x) for x in arr]: arr.append(name)

def remove_from(doc, key, name):
    if "patches" in doc and key in doc["patches"]:
        arr = doc["patches"][key]
        for i, x in enumerate(list(arr)):
            if str(x) == name: del arr[i]; return True
    return False

# ----------------------------------------------------------------------------- invariants
PROTECTED = {"recomp_entrypoint"}
def check_invariants(toml_doc, syms_doc):
    errs = []
    inp = toml_doc.get("input", {})
    for k in ("rom_file_path", "symbols_file_path"):
        v = str(inp.get(k, ""))
        if v and not v.startswith("/"): errs.append(f"{k} is not absolute: {v}")
    for key in ("stubs", "ignored"):
        for n in [str(x) for x in toml_doc.get("patches", {}).get(key, [])]:
            if n in PROTECTED: errs.append(f"protected symbol '{n}' must not be in {key}")
    for sec in syms_doc.get("section", []):
        rom = int(str(sec.get("rom", "0")), 0)
        if rom == 0 and str(sec.get("name")) != ".entry":
            errs.append(f"section '{sec.get('name')}' has rom=0 (only .entry may)")
    # instruction patches must target a known function and a vram inside it
    fns = syms_func_names(syms_doc)
    for blk in toml_doc.get("patches", {}).get("instruction", []):
        fn = str(blk.get("func", ""))
        v  = int(str(blk.get("vram", "0")), 0)
        if fn in PROTECTED:
            errs.append(f"instruction patch targets protected '{fn}'")
        elif fn not in fns:
            errs.append(f"instruction patch func '{fn}' not in syms")
        else:
            fv, fs = int(str(fns[fn][1]["vram"]), 0), int(str(fns[fn][1]["size"]), 0)
            if not (fv <= v < fv + fs):
                errs.append(f"instruction patch vram {hex(v)} outside {fn} [{hex(fv)},{hex(fv+fs)})")
    return errs

# ----------------------------------------------------------------------------- run + parse
def run_recompiler():
    r = subprocess.run([TOOL, TOML], capture_output=True, text=True, cwd=ROOT)
    return r.returncode, (r.stdout or "") + "\n" + (r.stderr or "")

def parse_recomp(out):
    static = {int(v,16) for v in re.findall(r'static_\d+_([0-9A-Fa-f]{8})', out)}
    errs   = set(re.findall(r'Error (?:in )?recompiling (\S+)', out))
    unh    = re.findall(r'Unhandled instruction:\s*(\S+)', out)
    return {"static": static, "err_names": errs, "unhandled": unh}

FATAL_MARKERS = ("segmentation fault", "assertion", "terminate called", "failed to open",
                 "could not open", "cannot open", "no such file", "panicked", "core dumped")
def recomp_fatal(out):
    o = out.lower()
    return any(m in o for m in FATAL_MARKERS)

def artifact_ok():
    fh = os.path.join(RF, "funcs.h")
    if os.path.exists(fh) and os.path.getsize(fh) > 0: return True
    return os.path.isdir(RF) and any(f.endswith(".c") for f in os.listdir(RF))

def recompiler_clean(parsed, out):
    # N64Recomp's exit code is unreliable: a benign ghost-stub warning returns nonzero.
    # Cleanliness = no real errors parsed, an artifact was produced, and no fatal output.
    return (not parsed["unhandled"] and not parsed["err_names"] and not parsed["static"]
            and not recomp_fatal(out) and artifact_ok())

PROTO_BEGIN = "// >>> orchestrator forward declarations (auto-generated)"
PROTO_END   = "// <<< orchestrator forward declarations"
def generate_prototypes():
    """The recompiler emits an empty funcs.h, so functions are called before declaration
    (implicit-declaration / 'conflicting types' errors). Harvest every emitted definition
    from the funcs_*.c files and write matching forward declarations into funcs.h, which
    every funcs_*.c already includes after recomp.h. Idempotent; runs before each build."""
    if not os.path.isdir(RF): return 0
    names = []
    for fn in sorted(os.listdir(RF)):
        if fn.startswith("funcs") and fn.endswith(".c"):
            txt = open(os.path.join(RF, fn), errors="replace").read()
            names += re.findall(r'RECOMP_FUNC\s+void\s+(\w+)\s*\(', txt)
    names = sorted(set(names))
    if not names: return 0
    block = (PROTO_BEGIN + "\n"
             + "\n".join(f"RECOMP_FUNC void {n}(uint8_t* rdram, recomp_context* ctx);" for n in names)
             + "\n" + PROTO_END + "\n")
    fh = os.path.join(RF, "funcs.h")
    cur = open(fh, errors="replace").read() if os.path.exists(fh) else ""
    if PROTO_BEGIN in cur:                                   # replace stale block (idempotent)
        cur = re.sub(re.escape(PROTO_BEGIN) + r".*?" + re.escape(PROTO_END) + r"\n?", "", cur, flags=re.S)
    open(fh, "w").write(cur.rstrip() + "\n\n" + block)
    return len(names)

def find_binary():
    """The built executable may not be named exactly BIN. Find any ELF executable in
    the build tree, preferring one whose name looks like the game, else the largest."""
    cands = []
    if os.path.exists(BIN) and os.path.getsize(BIN) > 0: cands.append(BIN)
    if os.path.isdir(BUILD):
        for dp, _, fns in os.walk(BUILD):
            for fn in fns:
                if fn.endswith((".so", ".o", ".a", ".cmake", ".txt", ".py", ".sh", ".json", ".ninja", ".inl")):
                    continue
                p = os.path.join(dp, fn)
                if not (os.path.isfile(p) and os.access(p, os.X_OK)): continue
                try:
                    with open(p, "rb") as f:
                        if f.read(4) != b"\x7fELF": continue
                except OSError:
                    continue
                cands.append(p)
    if not cands: return None
    pref = [c for c in cands if re.search(r"goemon|recomp", os.path.basename(c), re.I)]
    return max(pref or cands, key=lambda c: os.path.getsize(c))

def run_build(force_reconfigure):
    if force_reconfigure and os.path.isdir(BUILD): shutil.rmtree(BUILD)
    cfg = subprocess.run(["cmake","-S",REPO,"-B",BUILD,"-DCMAKE_BUILD_TYPE=Debug",
                          "-DCMAKE_C_COMPILER=clang","-DCMAKE_CXX_COMPILER=clang++"],
                         capture_output=True, text=True)
    out = "=== CONFIGURE ===\n" + cfg.stdout + cfg.stderr + "\n=== BUILD ===\n"
    if cfg.returncode != 0:                      # configure failed -> don't bother building
        return cfg.returncode, out + "(configure failed; build skipped)"
    bld = subprocess.run(["cmake","--build",BUILD], capture_output=True, text=True)
    return bld.returncode, out + bld.stdout + bld.stderr

def parse_link(out):
    """Build-error parser: symbol-link errors plus compile/cmake/missing-header errors,
    so a failing build is measurable and actionable (not just symbol-level)."""
    multi = set(re.findall(r"multiple definition of [`']([A-Za-z_]\w*)", out))
    undef = set(re.findall(r"undefined reference to [`']([A-Za-z_]\w*)", out))
    compile_e = re.findall(r'^\s*([^\s:]+\.(?:c|cc|cpp|cxx|h|hpp|inl)):(\d+):(?:\d+:)?\s*error:\s*(.+)$',
                           out, re.MULTILINE)
    missing_h = (re.findall(r"fatal error:\s*'([^']+)'\s*file not found", out)            # clang
                 + re.findall(r"fatal error:\s*([^\s:]+):\s*No such file", out))            # gcc
    cmake_e   = re.findall(r'CMake Error[^\n]*', out)
    labels    = {int(h, 16) for h in re.findall(r"use of undeclared label 'L_([0-9A-Fa-f]{8})'", out)}
    # undeclared-label errors are tracked separately; don't also count them as generic compiles
    compile_e = [c for c in compile_e if "undeclared label" not in c[2]]
    return {"multiple": multi, "undefined": undef, "compile": compile_e,
            "missing_header": missing_h, "cmake": cmake_e, "undeclared_labels": labels}

def score(parsed, link=None, fatal=False):
    """Lower is better, lexicographic: (unhandled, named_errs, fatal, build_errors, static).
    build_errors counts ALL build problems so the build frontier is measurable."""
    keys = ("multiple", "undefined", "compile", "missing_header", "cmake", "undeclared_labels")
    build_n = sum(len(link[k]) for k in keys) if link is not None else 99
    return (len(parsed["unhandled"]), len(parsed["err_names"]), 1 if fatal else 0,
            build_n, len(parsed["static"]))

# ----------------------------------------------------------------------------- fixers
def action_sig(a):
    """Stable identity of an action, so the loop won't re-propose one that already failed."""
    return (a.get("op"), a.get("name"), a.get("vram"), a.get("size"),
            a.get("target_label"), a.get("target_sym"), a.get("target_vram"))

UNHANDLED_STUB = {"cache", "tlbr", "tlbwi", "tlbwr", "tlbp"}     # no-op on PC -> empty stub
UNHANDLED_IGN  = {"sync", "mfc0", "mtc0"}                         # runtime provides -> ignore

def companion_for(cats, canon):
    if cats & UNHANDLED_STUB: return "stubs"
    if cats & UNHANDLED_IGN:  return "ignored" if canon else "stubs"  # ignore needs the real name to link
    return None                                                       # clean code -> just name it

def propose_fix(parsed, w_at, syms_doc, toml_doc, tried_failed=frozenset()):
    """Return (action, files_changed) or (None, False). Names AND stubs/ignores a
    libultra function in one step, and re-finds functions it already named (which
    show up as named recompile errors, not static_N_ targets). Skips actions that
    were already tried and rejected this run."""
    existing  = syms_func_names(syms_doc)                       # name -> (sec, fn)
    vram_of   = {int(str(fn["vram"]), 0): nm for nm, (sec, fn) in existing.items()}
    stubs     = {str(x) for x in toml_doc.get("patches", {}).get("stubs", [])}
    ignored   = {str(x) for x in toml_doc.get("patches", {}).get("ignored", [])}

    # candidate vrams from BOTH static targets and named recompile errors
    cand = {}                                                   # vram -> known name or None
    for v in parsed["static"]: cand.setdefault(v, None)
    for n in parsed["err_names"]:
        m = re.match(r'static_\d+_([0-9A-Fa-f]{8})', n)
        if m: cand.setdefault(int(m.group(1), 16), None); continue
        m2 = re.match(r'func_([0-9A-Fa-f]{8})$', n)
        if m2: cand.setdefault(int(m2.group(1), 16), n); continue
        if n in existing: cand.setdefault(int(str(existing[n][1]["vram"]), 0), n)

    for v in sorted(cand):
        size, bad = analyze(v, w_at)
        cats  = {b[2] for b in bad}
        canon = canonical_name(cats, size, bad)
        name  = cand[v] or vram_of.get(v) or canon or f"func_{v:08X}"
        named = (name in existing) or (v in vram_of)
        comp  = companion_for(cats, canon)
        act = fc = None
        if comp:                                                # function carries an unhandled instr
            inlist = name in (stubs if comp == "stubs" else ignored)
            if named and inlist:  continue                      # already handled
            if named:                                           # named but not yet stubbed/ignored
                op = "add_stub" if comp == "stubs" else "add_ignore"
                act, fc = {"op": op, "name": name, "target_vram": v,
                           "why": f"{sorted(cats)} -> {comp}"}, False
            else:
                if size is None: return None, False             # can't size -> escalate
                op = "add_func_stub" if comp == "stubs" else "add_func_ignore"
                act, fc = {"op": op, "name": name, "vram": v, "size": size, "target_vram": v,
                           "why": f"name+{comp[:-1]} ({sorted(cats)})"}, True
        else:                                                   # clean code -> name it so it recompiles
            if named: continue
            if size is None: return None, False
            act, fc = {"op": "add_func", "name": name, "vram": v, "size": size, "target_vram": v,
                       "why": "name un-named target (no unhandled instrs)"}, True
        if act and action_sig(act) not in tried_failed:
            return act, fc
    return None, False

BRANCH_OPS = {0x01, 0x04, 0x05, 0x06, 0x07, 0x14, 0x15, 0x16, 0x17}  # regimm + beq/bne/blez/bgtz + *likely*
def branch_target(addr, w):
    off = w & 0xFFFF
    if off & 0x8000: off -= 0x10000
    return addr + 4 + (off << 2)

def find_branch_to(func_lo, func_hi, label, w_at):
    """Locate the branch instruction inside [func_lo,func_hi) whose target == label."""
    v = func_lo
    while v < func_hi:
        w = w_at(off_for(v))
        if w is not None and (w >> 26) in BRANCH_OPS and branch_target(v, w) == label:
            return v, w
        v += 4
    return None, None

def propose_link_fix(link, toml_doc, syms_doc, w_at, tried_failed=frozenset()):
    def ok(a):                                          # first candidate not already tried+failed
        return a if action_sig(a) not in tried_failed else None
    stubs   = {str(x) for x in toml_doc.get("patches", {}).get("stubs", [])}
    ignored = {str(x) for x in toml_doc.get("patches", {}).get("ignored", [])}
    for sym in sorted(link["multiple"]):
        if sym in stubs:
            a = ok({"op": "move_stub_to_ignore", "name": sym, "target_sym": sym,
                    "why": "multiple definition -> let runtime define it"})
            if a: return a, False
    for sym in sorted(link["undefined"]):
        if sym in ignored:
            a = ok({"op": "move_ignore_to_stub", "name": sym, "target_sym": sym,
                    "why": "undefined -> empty stub so it links"})
            if a: return a, False
    # clang "use of undeclared label 'L_xxxxxxxx'": try (1) extend the under-sized enclosing
    # function, then (2) if that was already tried+failed, nop the offending likely-branch.
    fns = sorted((int(str(fn["vram"]), 0), int(str(fn["size"]), 0), nm)
                 for nm, (_, fn) in syms_func_names(syms_doc).items())
    for tgt in sorted(link.get("undeclared_labels", set())):
        prev = [f for f in fns if f[0] <= tgt]
        nxt  = [f for f in fns if f[0] > tgt]
        if not prev: continue
        fv, fs, fname = prev[-1]
        end = recover_func_end(fv, tgt, w_at, bound=(nxt[0][0] if nxt else None))
        if end is not None and (end - fv) > fs:
            a = ok({"op": "extend_func", "name": fname, "size": end - fv, "target_label": tgt,
                    "why": f"extend {fname} 0x{fs:X}->0x{end-fv:X} to contain L_{tgt:08X}"})
            if a: return a, True
        bv, bw = find_branch_to(fv, tgt + 4, tgt, w_at)
        if bv is not None and fv <= bv < fv + fs:
            a = ok({"op": "patch_instruction", "name": fname, "vram": bv, "value": 0,
                    "target_label": tgt,
                    "why": f"nop unresolved likely-branch @{hex(bv)} -> L_{tgt:08X} in {fname}"})
            if a: return a, True
    return None, False

def add_instruction_patch(doc, func, vram, value=0):
    patches = doc.setdefault("patches", tomlkit.table())
    if "instruction" not in patches: patches["instruction"] = tomlkit.aot()
    blk = tomlkit.parse(f'[[x]]\nfunc = "{func}"\nvram = {hex(vram)}\nvalue = {hex(value)}\n')["x"][0]
    patches["instruction"].append(blk)

def apply_action(a, toml_doc, syms_doc):
    op = a["op"]
    if op == "add_func":   add_func_to_syms(syms_doc, a["name"], a["vram"], a["size"])
    elif op == "add_stub": add_to(toml_doc, "stubs", a["name"])
    elif op == "add_ignore": add_to(toml_doc, "ignored", a["name"])
    elif op == "move_stub_to_ignore":
        remove_from(toml_doc, "stubs", a["name"]); add_to(toml_doc, "ignored", a["name"])
    elif op == "move_ignore_to_stub":
        remove_from(toml_doc, "ignored", a["name"]); add_to(toml_doc, "stubs", a["name"])
    elif op == "patch_instruction":
        add_instruction_patch(toml_doc, a["name"], a["vram"], a.get("value", 0))
    elif op == "add_func_stub":
        add_func_to_syms(syms_doc, a["name"], a["vram"], a["size"]); add_to(toml_doc, "stubs", a["name"])
    elif op == "add_func_ignore":
        add_func_to_syms(syms_doc, a["name"], a["vram"], a["size"]); add_to(toml_doc, "ignored", a["name"])
    elif op == "extend_func":
        if not set_func_size(syms_doc, a["name"], a["size"]):
            raise ValueError(f"extend_func: {a['name']} not in syms")
    else: raise ValueError(f"unknown op {op}")

# ----------------------------------------------------------------------------- main loop
def log_iter(rec):
    with open(RUNLOG, "a") as f: f.write(json.dumps(rec, default=str) + "\n")

def failing_candidates(parsed, w_at, limit=12):
    cand = set(parsed["static"])
    for n in parsed["err_names"]:
        m = re.match(r'static_\d+_([0-9A-Fa-f]{8})', n)
        if m: cand.add(int(m.group(1), 16))
    rows = []
    for v in sorted(cand)[:limit]:
        size, bad = analyze(v, w_at)
        cats = sorted({b[2] for b in bad})
        rows.append({"vram": hex(v), "size": hex(size) if size else None, "cats": cats,
                     "suggested_name": canonical_name(set(cats), size, bad) or f"func_{v:08X}",
                     "disasm": [(bv, bw, bc, bi) for (bv, bw, bc, bi) in bad[:8]]})
    return rows

def build_payload(out, bout, parsed, link, w_at):
    p = load_doc(TOML).get("patches", {})
    # in the build phase the build output is the signal; lead with it so the model
    # doesn't chase a stale recompile warning.
    tail = (("=== BUILD OUTPUT ===\n" + bout[-2800:] + "\n=== recompile tail ===\n" + out[-700:])
            if bout else out[-1500:])
    return {"tail": tail,
            "unhandled": parsed["unhandled"], "static": [hex(x) for x in sorted(parsed["static"])],
            "err_names": sorted(parsed["err_names"]),
            "link": {k: sorted(v) for k, v in (link or {}).items()},
            "stubs": [str(x) for x in p.get("stubs", [])],
            "ignored": [str(x) for x in p.get("ignored", [])],
            "candidates": failing_candidates(parsed, w_at)}

def fill_candidate(cand, parsed, w_at):
    """Fill vram/size the model omitted, from ROM + parsed failure set."""
    if cand["op"] == "add_func" and not ("vram" in cand and "size" in cand):
        v = cand.get("vram")
        if v is None:
            m = re.match(r'func_([0-9A-Fa-f]{8})$', cand["name"])
            if m: v = int(m.group(1), 16)
            elif len(parsed["static"]) == 1: v = next(iter(parsed["static"]))
        if v is not None:
            size, _ = analyze(v, w_at)
            cand["vram"] = v
            if size and "size" not in cand: cand["size"] = size
    if cand["op"] == "patch_instruction" and "value" not in cand: cand["value"] = 0
    return cand

def write_escalation(it, cur, out, bout, parsed, link, reason, candidates=None):
    esc = {"iteration": it, "reason": reason, "score": list(cur),
           "recomp_tail": out[-1500:], "build_tail": (bout[-4000:] if bout else ""),
           "parsed": {"unhandled": parsed["unhandled"],
                      "static": [hex(x) for x in sorted(parsed["static"])],
                      "err_names": sorted(parsed["err_names"])},
           "build_errors": {k: (sorted(v) if isinstance(v, set) else v)
                            for k, v in (link or {}).items()}}
    if candidates is not None: esc["llm_candidates_tried"] = candidates
    json.dump(esc, open(os.path.join(ROOT, "escalation.json"), "w"), indent=2, default=str)
    log_iter({"it": it, "event": "escalate", "reason": reason, "score": list(cur)})

def accept_change(cur, new, resolved):
    """Keep a change if it strictly improves, OR if it resolved its specific target
    without regressing any primary metric (unhandled, named-errs, link). Lets the
    libultra cascade proceed (static may bump) while blocking real regressions."""
    no_regression = (new[0] <= cur[0] and new[1] <= cur[1] and new[3] <= cur[3])
    return (new < cur) or (resolved and no_regression)

# ----------------------------------------------------------------------------- census
LIKELY_OPS = {0x14, 0x15, 0x16, 0x17}
def mnemonic(op):
    return {0x01:"regimm",0x04:"beq",0x05:"bne",0x06:"blez",0x07:"bgtz",
            0x14:"beql",0x15:"bnel",0x16:"blezl",0x17:"bgtzl"}.get(op, f"op0x{op:02x}")

def scan_orphaned_labels(w_at, syms_doc):
    """Compiler-independent: per C function, find `goto L_X;` with no `L_X:` in the same
    function. Exact, complete count of the boundary/label problem across all emitted C."""
    import glob
    funcs = {nm: (int(str(fn["vram"]), 0), int(str(fn["size"]), 0))
             for nm, (_, fn) in syms_func_names(syms_doc).items()}
    starts = sorted(v for v, _ in funcs.values())
    def next_start(v):
        return next((s for s in starts if s > v), None)
    results = []
    for cf in sorted(glob.glob(os.path.join(RF, "funcs*.c"))):
        cur = None; per = {}
        for ln in open(cf, errors="replace"):
            m = re.search(r'RECOMP_FUNC\s+void\s+(\w+)\s*\(', ln)
            if m: cur = m.group(1); per.setdefault(cur, {"g": set(), "l": set()}); continue
            if cur is None: continue
            for g in re.findall(r'goto\s+L_([0-9A-Fa-f]{8})\s*;', ln): per[cur]["g"].add(int(g, 16))
            for l in re.findall(r'^\s*L_([0-9A-Fa-f]{8})\s*:', ln):     per[cur]["l"].add(int(l, 16))
        for fname, d in per.items():
            for tgt in sorted(d["g"] - d["l"]):
                fv, fs = funcs.get(fname, (None, None))
                rec = {"file": os.path.basename(cf), "func": fname, "label": f"L_{tgt:08X}",
                       "label_addr": tgt, "func_vram": hex(fv) if fv is not None else None,
                       "func_size": hex(fs) if fs is not None else None}
                if fv is not None:
                    ns = next_start(fv)
                    end = recover_func_end(fv, tgt, w_at, bound=ns)
                    bv, bw = find_branch_to(fv, max(tgt + 4, fv + fs + 0x80), tgt, w_at)
                    if bv is not None:
                        rec["branch_at"] = hex(bv); rec["branch_kind"] = mnemonic(bw >> 26)
                        rec["branch_is_likely"] = (bw >> 26) in LIKELY_OPS
                    rec["next_func"] = hex(ns) if ns else None
                    rec["bytes_past_funcend"] = tgt - (fv + fs)
                    rec["subclass"] = ("missing_function_region" if (ns is not None and tgt >= ns)
                                       else "undersized_boundary" if end is not None
                                       else "unresolved")
                results.append(rec)
    return results

def neutralize_orphaned_gotos(labels):
    """For the relink probe only: blank the dangling gotos in the GENERATED C (regenerated
    on the next recompile, so syms stays the source of truth) to let objects compile."""
    by_file = {}
    for r in labels: by_file.setdefault(r["file"], set()).add(r["label"])
    for fn, lbls in by_file.items():
        p = os.path.join(RF, fn); txt = open(p, errors="replace").read()
        for lbl in lbls:
            txt = re.sub(rf'goto\s+{lbl}\s*;', '/*census-neutralized*/;', txt)
        open(p, "w").write(txt)

def run_build_keepgoing():
    if os.path.isdir(BUILD): shutil.rmtree(BUILD)
    cfg = subprocess.run(["cmake","-S",REPO,"-B",BUILD,"-DCMAKE_BUILD_TYPE=Debug",
                          "-DCMAKE_C_COMPILER=clang","-DCMAKE_CXX_COMPILER=clang++"],
                         capture_output=True, text=True)
    out = "=== CONFIGURE ===\n" + cfg.stdout + cfg.stderr + "\n=== BUILD(-k) ===\n"
    if cfg.returncode != 0: return cfg.returncode, out + "(configure failed)"
    bld = subprocess.run(["cmake","--build",BUILD,"--","-k"], capture_output=True, text=True)
    return bld.returncode, out + bld.stdout + bld.stderr

def run_census(w_at):
    import glob, time
    from collections import Counter
    print("=== CENSUS MODE (read-only on syms/toml; regenerates funcs + build/ only) ===")
    t0 = time.time(); syms = load_doc(SYMS)
    rc, out = run_recompiler(); parsed = parse_recomp(out)
    print(f"recompiler: unhandled={len(parsed['unhandled'])} static={len(parsed['static'])} "
          f"named_errs={len(parsed['err_names'])}")
    np = generate_prototypes(); print(f"forward declarations generated: {np}")
    cfiles = sorted(glob.glob(os.path.join(RF, "funcs*.c")))
    emitted = sum(len(re.findall(r'RECOMP_FUNC\s+void\s+\w+\s*\(', open(c, errors="replace").read()))
                  for c in cfiles)
    print(f"emitted functions: {emitted} across {len(cfiles)} files")

    labels = scan_orphaned_labels(w_at, syms)
    sub = Counter(r.get("subclass", "?") for r in labels)
    kinds = Counter(r.get("branch_kind", "?") for r in labels)
    affected = sorted({r["func"] for r in labels})
    print(f"orphaned labels (compiler-independent scan): {len(labels)} in {len(affected)} functions  "
          f"subclasses={dict(sub)}  branch_kinds={dict(kinds)}")

    print("build pass 1 (keep-going): cataloguing compile errors ...")
    rc1, bout1 = run_build_keepgoing(); link1 = parse_link(bout1)

    print("build pass 2: neutralizing dangling gotos, then relinking to expose runtime-symbol gaps ...")
    neutralize_orphaned_gotos(labels)
    rc2, bout2 = run_build_keepgoing(); link2 = parse_link(bout2)

    census = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "elapsed_sec": round(time.time()-t0, 1),
        "shape": {"syms_functions": len(syms_func_names(syms)), "emitted_functions": emitted,
                  "funcs_c_files": len(cfiles),
                  "stubs": len(load_doc(TOML).get("patches", {}).get("stubs", [])),
                  "ignored": len(load_doc(TOML).get("patches", {}).get("ignored", []))},
        "recompiler": {"unhandled_types": dict(Counter(parsed["unhandled"])),
                       "static_targets": len(parsed["static"]),
                       "named_errors": sorted(parsed["err_names"])},
        "orphaned_labels": {"total": len(labels), "affected_functions": len(affected),
                            "by_subclass": dict(sub), "by_branch_kind": dict(kinds),
                            "all": labels, "affected_function_list": affected},
        "build_pass1_with_gotos": {
            "rc": rc1, "compile_conflicting_types": len(re.findall(r'conflicting types', bout1)),
            "compile_undeclared_labels": len(link1["undeclared_labels"]),
            "missing_headers": link1["missing_header"], "cmake_errors": link1["cmake"][:30]},
        "build_pass2_relink_probe": {
            "rc": rc2, "undefined_reference_count": len(link2["undefined"]),
            "undefined_references": sorted(link2["undefined"]),
            "multiple_definition_count": len(link2["multiple"]),
            "multiple_definitions": sorted(link2["multiple"]),
            "remaining_compile_errors": len(link2["compile"]),
            "remaining_compile_samples": link2["compile"][:30],
            "binary_after_probe": find_binary()},
    }
    json.dump(census, open(os.path.join(ROOT, "census.json"), "w"), indent=2, default=str)

    print("\n================= CENSUS SUMMARY =================")
    print(f"shape: {census['shape']}")
    print(f"orphaned labels: {len(labels)} in {len(affected)} funcs  {dict(sub)}  kinds={dict(kinds)}")
    print(f"relink probe: undefined_refs={len(link2['undefined'])}  multiple_defs={len(link2['multiple'])}  "
          f"remaining_compile_errs={len(link2['compile'])}  binary={find_binary()}")
    verdict = ("FEW isolated label cases -> stub/patch them and move on"
               if len(labels) <= 12 else
               "PERVASIVE boundary errors -> a splat/ELF front-end is the real fix")
    print(f"VERDICT HINT: {verdict}")
    print("Full data -> census.json  (paste it back and I'll read the distribution)")
    return 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=60)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-build", action="store_true", help="stop once recompile is clean (do the recompile frontier first)")
    ap.add_argument("--llm", action="store_true", help="enable local Ollama escalation for failures no rule matches")
    ap.add_argument("--model", default="qwen3-coder:30b")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--samples", type=int, default=4, help="candidate fixes sampled per escalation (more = more accurate, more power)")
    ap.add_argument("--census", action="store_true", help="read-only: catalog every failure class across the whole build, write census.json, then exit")
    args = ap.parse_args()

    for p, l in [(ROM, "ROM"), (TOML, "toml"), (SYMS, "syms"), (TOOL, "recompiler")]:
        if not os.path.exists(p): sys.exit(f"FATAL: {l} not found at {p}")
    rom, order = load_rom(); w_at = word_reader(rom, order)
    if args.census:
        return run_census(w_at)                              # no git, no fixes, no source edits
    if not args.dry_run: ensure_git(); dedupe_syms()

    esc = None
    if args.llm:
        try: import ollama_escalator as esc
        except ImportError: sys.exit("--llm needs ollama_escalator.py beside this script")
        ok, names = esc.model_available(args.model, args.host)
        print(f"LLM escalation: '{args.model}' available={ok}; installed models={names}")
        if not ok: print("  (model/Ollama not reachable; escalations will write escalation.json instead)")

    def evaluate():
        rc, out = run_recompiler(); parsed = parse_recomp(out)
        clean = recompiler_clean(parsed, out); link = None; bout = ""
        if clean and not args.no_build:
            np = generate_prototypes()                         # fill empty funcs.h before building
            if np: print(f"      generated {np} forward declarations into funcs.h")
            rclink, bout = run_build(force_reconfigure=True)   # accuracy>speed: always clean-configure
            link = parse_link(bout)
            binp = find_binary()
            if rclink == 0 and binp:
                print(f"      built binary: {binp}")
                return parsed, link, out, bout, True
        return parsed, link, out, bout, False

    def try_action(action, cur, pre_parsed, pre_link, it, src):
        """Apply -> no-op check -> invariants -> rebuild -> acceptance gate.
        Returns (kept, new_score, built)."""
        rev = snapshot(f"it={it} [{src}] {action['op']} {action.get('name')}")
        td, sd = load_doc(TOML), load_doc(SYMS)
        before = tomlkit.dumps(load_doc(TOML)) + tomlkit.dumps(load_doc(SYMS))
        try: apply_action(action, td, sd)
        except Exception as e:
            print(f"      [{src}] apply error: {e} -> skip"); rollback_to(rev); return False, cur, False
        if tomlkit.dumps(td) + tomlkit.dumps(sd) == before:       # action changed nothing
            print(f"      [{src}] no-op (already in config) -> reject"); rollback_to(rev); return False, cur, False
        inv = check_invariants(td, sd)
        if inv:
            print(f"      [{src}] INVARIANT BLOCK -> revert: {inv}"); rollback_to(rev)
            log_iter({"it": it, "src": src, "event": "invariant_block", "action": action, "errors": inv})
            return False, cur, False
        save_doc(TOML, td); save_doc(SYMS, sd)
        p2, link2, out2, _, built2 = evaluate()
        if built2:
            log_iter({"it": it, "src": src, "event": "build_ok_after", "action": action}); return True, cur, True
        new = score(p2, link2, recomp_fatal(out2))
        # progress = a target that was ACTUALLY failing before is now resolved.
        nm, tv, ts, tl = action.get("name"), action.get("target_vram"), action.get("target_sym"), action.get("target_label")
        had = post = False
        if tl is not None:                                   # undeclared-label (compile) target
            pl = pre_link or {}
            had  = tl in pl.get("undeclared_labels", set())
            post = link2 is not None and tl not in link2.get("undeclared_labels", set())
        elif ts is not None:
            pl = pre_link or {"multiple": set(), "undefined": set()}
            had  = ts in (pl["multiple"] | pl["undefined"])
            post = link2 is not None and ts not in link2["multiple"] and ts not in link2["undefined"]
        else:
            had  = (tv in pre_parsed["static"]) or (nm in pre_parsed["err_names"]) \
                   or (tv is not None and f"func_{tv:08X}" in pre_parsed["err_names"])
            post = (tv not in p2["static"]) and (nm not in p2["err_names"]) \
                   and (tv is None or f"func_{tv:08X}" not in p2["err_names"])
        progressed = had and post
        keep = accept_change(cur, new, progressed)
        log_iter({"it": it, "src": src, "event": "applied", "action": action,
                  "before": list(cur), "after": list(new), "progressed": progressed, "kept": keep})
        if not keep:
            print(f"      [{src}] rejected {cur} -> {new} (progressed={progressed}) -> rollback")
            rollback_to(rev); return False, new, False
        tag = "improved" if new < cur else "target resolved (cascade)"
        print(f"      [{src}] kept {cur} -> {new} [{tag}]"); return True, new, False

    stall = 0; prev = None; tried_failed = set()
    for it in range(args.max):
        parsed, link, out, bout, built = evaluate()
        if built:
            print(f"[{it}] BUILD OK -> {BIN}")
            log_iter({"it": it, "event": "build_ok", "bin": BIN})
            print("Phase 1 complete. Hand off to the runtime / human-in-the-loop phase.")
            return 0
        clean = recompiler_clean(parsed, out)
        if clean and args.no_build:
            print(f"[{it}] recompile CLEAN (build skipped). Recompile frontier done."); return 0

        cur = score(parsed, link, recomp_fatal(out))
        print(f"[{it}] score={cur} unhandled={parsed['unhandled']} static={len(parsed['static'])} "
              f"errs={len(parsed['err_names'])}" + (f" link={link}" if link else ""))

        action, _ = (propose_link_fix(link, load_doc(TOML), load_doc(SYMS), w_at, tried_failed) if clean
                     else propose_fix(parsed, w_at, load_doc(SYMS), load_doc(TOML), tried_failed))

        if action is not None:                                  # deterministic fix
            print(f"      ACTION [rule] {action['op']} {action.get('name')} :: {action['why']}")
            if args.dry_run:
                log_iter({"it": it, "event": "dryrun", "action": action, "score": list(cur)})
                if prev == cur: stall += 1
                prev = cur
                if stall >= 2: print("dry-run: converged on deterministic plan."); return 0
                continue
            kept, new, done = try_action(action, cur, parsed, link, it, "rule")
            if done: print(f"[{it}] BUILD OK -> {BIN}"); return 0
            if not kept: tried_failed.add(action_sig(action))   # don't re-propose it; try the alternative
            stall = 0 if kept else stall + 1; prev = new
            if stall >= 6: write_escalation(it, new, out, bout, parsed, link, "stall"); return 2
            continue

        # no deterministic rule matched
        if not (args.llm and esc):
            write_escalation(it, cur, out, bout, parsed, link, "no_rule")
            print("No rule matched and --llm is off. Wrote escalation.json."); return 2
        if args.dry_run:
            print(f"      [llm] dry-run: would sample {args.samples} candidates from {args.model}"); return 0

        payload = build_payload(out, bout, parsed, link, w_at)
        cands = esc.propose_actions(payload, model=args.model, host=args.host, n=args.samples)
        print(f"      [llm] {len(cands)} candidate action(s); validating each against the toolchain")
        kept_any = False
        for cand in cands:
            cand = fill_candidate(dict(cand), parsed, w_at)
            print(f"      [llm] try {cand['op']} {cand.get('name')} :: {str(cand.get('why',''))[:70]}")
            kept, new, done = try_action(cand, cur, parsed, link, it, "llm")
            if done: print(f"[{it}] BUILD OK -> {BIN}"); return 0
            if kept: cur = new; kept_any = True; break
        if not kept_any:
            write_escalation(it, cur, out, bout, parsed, link, "llm_exhausted", candidates=cands)
            print("All LLM candidates failed the gate (or were no-ops). Wrote escalation.json."); return 2
        stall = 0; prev = cur

    print("Hit iteration cap."); return 1

if __name__ == "__main__":
    sys.exit(main())
