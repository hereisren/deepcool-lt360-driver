const fs=require('fs');const H=require('./heap.js');
function calib(){ return ["hello_calib", 1.5, 123456789012, {a:1}, [0xAA,0x2E,7], [1.25,2.5], "héllo中", `t${calib}x`, /re+/g, function inner(){}, null, true, undefined]; }
class K { #p=1; static s=2; m(){return this.#p;} }
function calib2(){ let o={x:1}; o.foo; o.bar=2; try{}catch(e){} return new K(); }
calib(); calib2();
const log=fs.readFileSync('v8c.log','utf8');
for (const nm of ['calib','calib2']){
 const m=log.match(new RegExp('code-creation,JS,\\d+,\\d+,(0x[0-9a-f]+),\\d+,'+nm+' '));
 const bc=Number(BigInt(m[1])); H.setCage(bc);
 const ba=bc-34; console.log(nm,'bytecodearray itype',H.itype(ba).toString(16));
 const cp=H.ptr(H.u32(ba+8)); console.log(' cp itype',H.itype(cp).toString(16),'len',H.smi(H.u32(cp+4)));
 const n=H.smi(H.u32(cp+4));
 for(let i=0;i<n;i++){const v=H.u32(cp+8+4*i); if(H.isSmi(v)){console.log(' ',i,'smi',H.smi(v));continue;}
   const o=H.ptr(v); console.log(' ',i,'itype',H.itype(o).toString(16),H.rd(o,32).toString('hex'));
   if(i==0&&nm=='calib'){ // boilerplate: [map, flags, elements]
     const el=H.ptr(H.u32(o+8)); const L=H.smi(H.u32(el+4)); console.log('   elements itype',H.itype(el).toString(16),'len',L);
     for(let j=0;j<L;j++){const w=H.u32(el+8+4*j); if(H.isSmi(w)){console.log('    ',j,'smi',H.smi(w));continue;} const q=H.ptr(w); console.log('    ',j,'itype',H.itype(q).toString(16),H.rd(q,24).toString('hex'));}
   }
 }
}
