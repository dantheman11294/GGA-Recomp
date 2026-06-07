#!/usr/bin/env python3
# READ-ONLY diagnostic. Does NOT modify gga.us.toml or gga.syms.toml.
import os, re, sys, struct, subprocess
from collections import Counter
ROOT=os.path.expanduser("~/recomp")
ROM=os.path.join(ROOT,"gga.us.z64"); TOML=os.path.join(ROOT,"gga.us.toml")
SYMS=os.path.join(ROOT,"gga.syms.toml"); TOOL=os.path.join(ROOT,"N64Recomp_tool")
LOG=os.path.join(ROOT,"recomp_run.log"); RF=os.path.join(ROOT,"Goemon64Recomp","RecompiledFuncs")
SECTION_ROM,SECTION_VRAM=0x1050,0x80000450
def die(m): print("FATAL:",m); sys.exit(1)
for p,l in [(ROM,"ROM"),(TOML,"toml"),(SYMS,"syms"),(TOOL,"recompiler")]:
    if not os.path.exists(p): die(f"{l} not found at {p}")
rom=open(ROM,"rb").read(); magic=rom[:4]
order={b"\x80\x37\x12\x40":"z64",b"\x37\x80\x40\x12":"v64",b"\x40\x12\x37\x80":"n64"}.get(magic,"z64")
print(f"ROM size={len(rom):#x}  byteorder={order} (magic {magic.hex()})")
def word_at(o):
    b=rom[o:o+4]
    if len(b)<4: return None
    if order=="n64": return struct.unpack("<I",b)[0]
    if order=="v64": return struct.unpack(">I",bytes([b[1],b[0],b[3],b[2]]))[0]
    return struct.unpack(">I",b)[0]
def off_for(v): return SECTION_ROM+(v-SECTION_VRAM)
st=open(SYMS).read(); name_to={}
for m in re.finditer(r'name\s*=\s*"([^"]+)"\s*,\s*vram\s*=\s*(0x[0-9A-Fa-f]+)\s*,\s*size\s*=\s*(0x[0-9A-Fa-f]+)',st):
    name_to[m.group(1)]=(int(m.group(2),16),int(m.group(3),16))
print(f"syms: {len(name_to)} functions parsed")
print("Running recompiler ...")
r=subprocess.run([TOOL,TOML],capture_output=True,text=True,cwd=ROOT)
out=(r.stdout or "")+"\n"+(r.stderr or ""); open(LOG,"w").write(out)
print(f"exit={r.returncode}  full log -> {LOG}")
if os.path.isdir(RF):
    cs=[f for f in os.listdir(RF) if f.endswith(".c")]
    fh=os.path.join(RF,"funcs.h"); fsz=os.path.getsize(fh) if os.path.exists(fh) else None
    print(f"RecompiledFuncs: {len(cs)} .c files; funcs.h size={fsz}")
static_vrams={int(v,16) for v in re.findall(r'static_\d+_([0-9A-Fa-f]{8})',out)}
err_names=set(re.findall(r'Error (?:in )?recompiling (\S+)',out))
unh=re.findall(r'Unhandled instruction:\s*(\S+)',out)
print(f"\nstatic_N_ targets={len(static_vrams)}  named_errors={len(err_names)}  unhandled_lines={len(unh)}")
if unh: print("  unhandled types:",dict(Counter(unh)))
cand=set(static_vrams)
for n in err_names:
    mm=re.match(r'static_\d+_([0-9A-Fa-f]{8})',n)
    if mm: cand.add(int(mm.group(1),16))
    elif n in name_to: cand.add(name_to[n][0])
def classify(w):
    op=(w>>26)&0x3F
    if op==0x2F: return ("cache",f"cacheop=0x{(w>>16)&0x1F:02X}")
    if w==0x0000000F: return ("sync","")
    if op==0x10:
        rs=(w>>21)&0x1F
        if rs==0x00: return ("mfc0","")
        if rs==0x04: return ("mtc0","")
        if (w>>25)&1:
            f=w&0x3F
            return ({0x01:"tlbr",0x02:"tlbwi",0x06:"tlbwr",0x08:"tlbp",0x18:"eret"}.get(f,f"cop0_0x{f:02X}"),"")
    return (None,None)
def analyze(vram,maxw=0x200):
    o=off_for(vram); bad=[]; size=None
    for i in range(maxw):
        w=word_at(o+i*4)
        if w is None: break
        c,info=classify(w)
        if c: bad.append((vram+i*4,w,c,info))
        if w==0x03E00008: size=(i+2)*4; break
    return size,bad
CACHE={0xAC:["osInvalDCache"],0x74:["osWritebackDCache","osInvalICache"],0x28:["osWritebackDCacheAll"]}
TLB={0xB8:["__osProbeTLB"],0xB4:["osMapTLB"],0x58:["osMapTLBRdb"],0x44:["osUnmapTLBAll"]}
COP0={0x20:["__osDisableInt"],0x1C:["__osRestoreInt"],0x10:["__osSetSR","__osSetFpcCsr"],0xC:["__osGetSR","__osSetCompare","osGetCount"],0xA0:["osSetIntMask"]}
SYNC={0xE0:["osPiRawStartDma"],0xAC:["__osSiRawStartDma"],0x8C:["__osSpRawStartDma"]}
def guess(cats,size):
    if "cache" in cats: tbl=CACHE
    elif any(c.startswith("tlb") for c in cats): tbl=TLB
    elif "sync" in cats: tbl=SYNC
    elif cats&{"mfc0","mtc0"}: tbl=COP0
    else: return []
    return tbl.get(size,[])
print("\n================= FAILING FUNCTIONS (paste this block back) =================")
rows=[]
for v in sorted(cand):
    size,bad=analyze(v); cats={b[2] for b in bad}; g=guess(cats,size) if size else []
    rows.append(v)
    print(f"vram=0x{v:08X} size={('0x%X'%size) if size else '?'} cats={sorted(cats)} guess={g or '-'}")
    for (bv,bw,bc,bi) in bad[:6]:
        print(f"        0x{bv:08X}: word=0x{bw:08X}  {bc} {bi}")
if not rows:
    print("No failing functions parsed. Tail of log:")
    print(out[-1200:])
print("=============================================================================")
print(f"({len(rows)} functions need naming. Sizes read straight from your GGA ROM.)")
