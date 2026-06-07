#!/usr/bin/env python3
"""Scan recompiled C for functions that 'goto L_X' without defining 'L_X:'.
These are N64Recomp label-emission failures (branch target not emitted).
Prints: func_name  file  missing_labels"""
import re, sys, glob, os
func_re = re.compile(r'RECOMP_FUNC\s+\w[\w\s\*]*?\s(func_[0-9A-Fa-f]+)\s*\(')
goto_re = re.compile(r'\bgoto\s+(L_[0-9A-Fa-f]+)\s*;')
def_re  = re.compile(r'^\s*(L_[0-9A-Fa-f]+):')
bad = []
for path in sorted(glob.glob(os.path.join(sys.argv[1], 'funcs_*.c'))):
    cur=None; gotos=set(); defs=set()
    def flush():
        if cur:
            missing = gotos - defs
            if missing: bad.append((cur, os.path.basename(path), sorted(missing)))
    for line in open(path):
        m=func_re.search(line)
        if m:
            flush(); cur=m.group(1); gotos=set(); defs=set(); continue
        for g in goto_re.findall(line): gotos.add(g)
        d=def_re.match(line)
        if d: defs.add(d.group(1))
    flush()
for fn,f,miss in bad:
    print(f"{fn}\t{f}\t{','.join(miss)}")
print(f"\n# {len(bad)} function(s) with missing labels", file=sys.stderr)
