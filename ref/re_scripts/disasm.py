# Annotated full disassembly of a PE .node using .pdata function bounds.
import pefile, capstone, sys, struct, re
path, out = sys.argv[1], sys.argv[2]
pe = pefile.PE(path)
base = pe.OPTIONAL_HEADER.ImageBase
img = pe.get_memory_mapped_image()
def rd(va, n): return img[va-base: va-base+n]
secs = {s.Name.rstrip(b'\0').decode(): (base+s.VirtualAddress, base+s.VirtualAddress+s.Misc_VirtualSize) for s in pe.sections}
# imports
imps = {}
for d in pe.DIRECTORY_ENTRY_IMPORT:
    for i in d.imports: imps[i.address] = (i.name or b'#%d'%i.ordinal).decode()
funcs = []
pd = pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]
for off in range(0, pd.Size, 12):
    b,e,u = struct.unpack_from('<III', img, pd.VirtualAddress+off)
    if b: funcs.append((base+b, base+e))
funcs.sort()
def strat(va):
    lo, hi = secs['.rdata']
    if not (lo <= va < hi) and not (secs['.data'][0] <= va < secs['.data'][1]): return None
    s = rd(va, 80)
    m = re.match(rb'[\x20-\x7e]{4,}', s)
    return m.group().decode() if m else None
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64); md.detail = False
with open(out, 'w') as f:
    for b, e in funcs:
        f.write(f'\n;==== FUNC {b:#x} .. {e:#x}\n')
        for ins in md.disasm(rd(b, e-b), b):
            ann = ''
            m = re.search(r'\[rip ([+-]) (0x[0-9a-f]+)\]', ins.op_str)
            if m:
                tgt = ins.address + ins.size + (int(m.group(2),16) * (1 if m.group(1)=='+' else -1))
                ann = f' ; ->{tgt:#x}'
                if tgt in imps: ann += f' IMP:{imps[tgt]}'
                s = strat(tgt)
                if s: ann += f' STR:{s[:60]!r}'
            f.write(f'{ins.address:x}: {ins.mnemonic} {ins.op_str}{ann}\n')
print(len(funcs), 'functions')
