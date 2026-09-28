// Minimal V8 11.0 (pointer-compressed) heap reader over /proc/self/mem
const fs=require('fs');
const fd=fs.openSync('/proc/self/mem','r');
let CAGE=0;
function setCage(a){CAGE=Math.floor(a/2**32)*2**32;}
function rd(a,n){const b=Buffer.alloc(n);fs.readSync(fd,b,0,n,a);return b;}
function u32(a){return rd(a,4).readUInt32LE(0);}
function u16(a){return rd(a,2).readUInt16LE(0);}
function isSmi(v){return (v&1)===0;}
function smi(v){return (v|0)>>1;}
function ptr(v){return CAGE+(v-1);}   // untagged address
function itype(obj){const map=ptr(u32(obj));return u16(map+8);}
module.exports={fd,setCage,rd,u32,u16,isSmi,smi,ptr,itype,get CAGE(){return CAGE}};
