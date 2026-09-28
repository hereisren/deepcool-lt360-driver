import pefile,struct,capstone,sys,re
p=sys.argv[1]; addr=int(sys.argv[2],16)
pe=pefile.PE(p,fast_load=True); base=pe.OPTIONAL_HEADER.ImageBase
pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXCEPTION']])
img=pe.get_memory_mapped_image()
fb=None
for e in pe.DIRECTORY_ENTRY_EXCEPTION:
    b,en=base+e.struct.BeginAddress, base+e.struct.EndAddress
    if b<=addr<en: fb=(b,en);break
print('func',[hex(x) for x in fb])
def s_at(va):
    o=va-base
    if 0<=o<len(img):
        m=re.match(rb'[\x20-\x7e]{3,}',img[o:o+80]); return m.group().decode() if m else None
md=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_64)
for i in md.disasm(img[fb[0]-base:fb[1]-base],fb[0]):
    a=''
    m=re.search(r'\[rip ([+-]) (0x[0-9a-f]+)\]',i.op_str)
    if m:
        t=i.address+i.size+int(m.group(2),16)*(1 if m.group(1)=='+' else -1); s=s_at(t); a=f' ; {t:#x} {s!r}' if s else f' ; {t:#x}'
    print(f'{i.address:x}: {i.mnemonic} {i.op_str}{a}')
