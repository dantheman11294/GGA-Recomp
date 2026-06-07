#!/usr/bin/env python3
"""Post-process N64Recomp output: 'lw $zero' etc. generate `0 = MEM_x(...)`,
an assignment to the literal 0 (illegal C). Writing $zero is a no-op on MIPS,
but the load may be a deliberate bus probe, so preserve the access via (void).
Rewrites lines of the form `<indent>0 = <LOADEXPR>;` only."""
import re, sys, glob, os

# only these RHS forms are loads-to-zero we rewrite; anything else is left alone
LOAD = re.compile(r'^(\s*)0 = ((?:MEM_[BHW]U?|LD|do_lw[lr])\(.*\));\s*$')

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
