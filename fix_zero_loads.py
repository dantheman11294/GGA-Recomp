#!/usr/bin/env python3
"""Post-process N64Recomp output: 'lw $zero' etc. generate `0 = MEM_x(...)`,
an assignment to the literal 0 (illegal C). Writing $zero is a no-op on MIPS,
but the load may be a deliberate bus probe, so preserve the access via (void).
Rewrites `<indent>0 = <LOADEXPR>;` for any indentation/nesting."""
import re, sys, glob, os

# standalone literal-zero LHS (not r20, not 0x..); any load-ish RHS up to ';'
LOAD = re.compile(r'^(\s*)0 = ((?:MEM_[BHWD]U?|LD|SD|do_lw[lr]|do_sw[lr])\([^;]*\));')

def fix(path):
    out, n = [], 0
    for line in open(path):
        m = LOAD.match(line)
        if m:
            out.append(f"{m.group(1)}(void)({m.group(2)});\n")
            n += 1
        else:
            out.append(line)
    if n:
        open(path, 'w').writelines(out)
    return n

total = 0
for p in glob.glob(os.path.join(sys.argv[1], '*.c')):
    total += fix(p)
print(f"fix_zero_loads: rewrote {total} discarded-load(s)")

# GUARD: fail loudly if any illegal '0 = <load>' remain (matches standalone 0 only)
leftover = []
pat = re.compile(r'(^|[^0-9A-Za-z_])0 = (?:MEM_|LD|SD|do_[ls]w)')
for p in glob.glob(os.path.join(sys.argv[1], '*.c')):
    for i, line in enumerate(open(p), 1):
        if pat.search(line):
            leftover.append(f"{p}:{i}: {line.strip()}")
if leftover:
    print(f"ERROR: {len(leftover)} illegal '0 = <load>' lines REMAIN after fixup:", file=sys.stderr)
    for l in leftover[:10]:
        print("  "+l, file=sys.stderr)
    sys.exit(1)
print("fix_zero_loads: verified 0 illegal discarded-loads remain")
