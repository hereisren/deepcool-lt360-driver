const fs=require('fs');
function calib(){ return ["hello_calib", 1.5, 123456789012, {a:1}, [0xAA,0x2E,7]]; }
calib();
const log=fs.readFileSync('v8b.log','utf8');
const m=log.match(/code-creation,JS,\d+,\d+,(0x[0-9a-f]+),\d+,calib [^\n]*/);
console.log(m[0]);
const fd=fs.openSync('/proc/self/mem','r');
const addr=Number(BigInt(m[1]));
const base=Math.floor(addr/4)*4-0x40;
const b=Buffer.alloc(0x60); fs.readSync(fd,b,0,b.length,base);
for(let i=0;i<b.length;i+=4) console.log((base+i).toString(16), b.readUInt32LE(i).toString(16).padStart(8,'0'), b.slice(i,i+4).toString('hex'));
