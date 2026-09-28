const fs=require('fs'),vm=require('vm'),v8=require('v8');
v8.setFlagsFromString('--no-lazy');v8.setFlagsFromString('--no-flush-bytecode');
const f=process.argv[2]; const b=fs.readFileSync(f);
const d=new vm.Script('',{produceCachedData:true}).createCachedData();
console.log('file hdr',b.slice(0,16).toString('hex'),'dummy hdr',d.slice(0,16).toString('hex'));
d.slice(12,16).copy(b,12);
const len=b.readUInt32LE(8);
const s=new vm.Script(len>1?'"'+'​'.repeat(len-2)+'"':'',{filename:f,cachedData:b});
console.log('rejected:',s.cachedDataRejected);
