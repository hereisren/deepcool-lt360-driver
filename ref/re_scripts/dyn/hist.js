const fs=require('fs'),vm=require('vm'),v8=require('v8');const H=require('./heap.js');
v8.setFlagsFromString('--no-lazy');v8.setFlagsFromString('--no-flush-bytecode');
const f=process.argv[2]; const b=fs.readFileSync(f);
const d=new vm.Script('',{produceCachedData:true}).createCachedData(); d.slice(12,16).copy(b,12);
const len=b.readUInt32LE(8);
globalThis.__keep=new vm.Script(len>1?'"'+'​'.repeat(len-2)+'"':'',{filename:f,cachedData:b});
setTimeout(()=>{
const log=fs.readFileSync(process.argv[3],'utf8');
const re=/code-creation,JS,\d+,\d+,(0x[0-9a-f]+),\d+,([^,]*) evalmachine/g; let m, n=0; const h={};
while((m=re.exec(log))){ const bc=Number(BigInt(m[1])); H.setCage(bc); const ba=bc-34; if(H.itype(ba)!==0xbf) {h.notba=(h.notba||0)+1;continue;} n++;
 const cp=H.u32(ba+8); if(H.isSmi(cp)) continue; const c=H.ptr(cp); const t0=H.itype(c); if(t0!==0xaf){h['cp'+t0.toString(16)]=(h['cp'+t0.toString(16)]||0)+1;continue;}
 const L=H.smi(H.u32(c+4)); for(let i=0;i<L;i++){const v=H.u32(c+8+4*i); const k=H.isSmi(v)?'smi':H.itype(H.ptr(v)).toString(16); h[k]=(h[k]||0)+1;}}
console.log(n,h);},500);
