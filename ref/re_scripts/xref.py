import pefile, re, sys, capstone
path=sys.argv[1]; pats=[p.encode() for p in sys.argv[2:]]
pe=pefile.PE(path, fast_load=True); base=pe.OPTIONAL_HEADER.ImageBase
img=open(path,'rb').read()
def off2va(o):
    for s in pe.sections:
        if s.PointerToRawData<=o<s.PointerToRawData+s.SizeOfRawData: return base+s.VirtualAddress+o-s.PointerToRawData
text=[s for s in pe.sections if s.Name.startswith(b'.text')][0]
tb=text.get_data(); tva=base+text.VirtualAddress
import numpy as np
arr=np.frombuffer(tb,dtype=np.uint8)
cand=np.where(((arr[:-7]==0x48)|(arr[:-7]==0x4c))&(arr[1:-6]==0x8d)&((arr[2:-5]&0xC7)==0x05))[0]
disp=np.frombuffer(tb[:len(tb)-(len(tb)%1)],dtype=np.uint8)
d32=(arr[cand+3].astype(np.int64)|(arr[cand+4].astype(np.int64)<<8)|(arr[cand+5].astype(np.int64)<<16)|(arr[cand+6].astype(np.int64)<<24))
d32=np.where(d32>=2**31,d32-2**32,d32)
tgt=tva+cand+7+d32
for p in pats:
    for m in re.finditer(re.escape(p)+b'\0', img):
        if m.start()>0 and img[m.start()-1]!=0: continue
        va=off2va(m.start()); hits=cand[tgt==va]
        print(p, hex(va), [hex(tva+h) for h in hits])
