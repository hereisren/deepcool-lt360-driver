import re, sys, collections
asm = open(sys.argv[1]).read().split('\n;==== FUNC ')
funcs = {}
for blk in asm[1:]:
    hdr, *lines = blk.split('\n')
    b = int(hdr.split()[0],16)
    strs = []; calls = []
    for l in lines:
        m = re.search(r"STR:'([^']*)'", l)
        if m: strs.append(m.group(1)[:50])
        m = re.match(r'\S+: call (0x[0-9a-f]+)', l)
        if m: calls.append(int(m.group(1),16))
        m = re.search(r'IMP:(\S+)', l)
        if m and ' call ' in l: calls.append(m.group(1))
    funcs[b] = (strs, calls, len(lines))
import pickle; pickle.dump(funcs, open(sys.argv[1]+'.idx','wb'))
