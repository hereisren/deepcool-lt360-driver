// Load index.jsc (V8 11.0.226.20 code cache) WITHOUT executing it, then walk the
// deserialized heap via /proc/self/mem to dump every function's constant pool and
// every ScopeInfo's context-local names. Addresses come from --log-code output.
// usage: electron(as node) --log-code --log-code-disassemble --logfile=L --no-logfile-per-isolate dumpcp.js <jsc> <L> <out.json>
const fs = require('fs'), vm = require('vm'), v8 = require('v8'); const H = require('./heap.js');
v8.setFlagsFromString('--no-lazy'); v8.setFlagsFromString('--no-flush-bytecode');
const [jsc, logf, outf] = process.argv.slice(2);
const b = fs.readFileSync(jsc);
const d = new vm.Script('', { produceCachedData: true }).createCachedData(); d.slice(12, 16).copy(b, 12);
const len = b.readUInt32LE(8);
globalThis.__keep = new vm.Script(len > 1 ? '"' + '​'.repeat(len - 2) + '"' : '', { filename: jsc, cachedData: b });

const T = { FA: 0xaf, NUM: 0x82, ODD: 0x83, ABP: 0x92, OBP: 0xbc, SCOPE: 0x105, SFI: 0x106, BA: 0xbf, SYM: 0x80 };
const scopes = {}; const out = { funcs: {}, scopes };

function str(o, depth = 0) {
  const t = H.itype(o);
  if (t >= 0x80 || depth > 8) return null;
  const rep = t & 7, oneByte = (t & 8) !== 0;
  if (rep === 0) { const n = H.u32(o + 8); if (n > 1e6) return null; const raw = H.rd(o + 12, oneByte ? n : 2 * n); return oneByte ? raw.toString('latin1') : raw.toString('utf16le'); }
  if (rep === 1) { const a = str(H.ptr(H.u32(o + 12)), depth + 1), c = str(H.ptr(H.u32(o + 16)), depth + 1); return a == null || c == null ? null : a + c; }
  if (rep === 5) return str(H.ptr(H.u32(o + 12)), depth + 1);
  return `<string rep ${rep}>`;
}
function fa(o) { const n = H.smi(H.u32(o + 4)); const r = []; for (let i = 0; i < n; i++) r.push(H.u32(o + 8 + 4 * i)); return r; }
function scopeInfo(o) {
  const id = o.toString(16); if (scopes[id]) return id;
  const s = scopes[id] = { flags: H.smi(H.u32(o + 4)), params: H.smi(H.u32(o + 8)), n: H.smi(H.u32(o + 12)), names: [] };
  s.type = s.flags & 0xf;
  let off = 16;
  if (s.n < 75) { for (let i = 0; i < s.n; i++) s.names.push(str(H.ptr(H.u32(o + off + 4 * i)))); off += 4 * s.n; }
  else { // NameToIndexHashTable: [map,len, nElem,nDel,cap, (key,val)*]
    const ht = H.ptr(H.u32(o + off)); off += 4; const e = fa(ht); s.names = new Array(s.n).fill(null);
    for (let i = 3; i + 1 < e.length; i += 2) { const k = e[i], v = e[i + 1]; if (!H.isSmi(k) && H.isSmi(v)) { const nm = str(H.ptr(k)); if (nm != null) s.names[H.smi(v)] = nm; } }
  }
  off += 4 * s.n; // context_local_infos
  // heuristic: outer_scope_info is the first ScopeInfo pointer in the next few trailing fields
  for (let i = 0; i < 6; i++) { const v = H.u32(o + off + 4 * i); if (!H.isSmi(v)) { const p = H.ptr(v); try { const t = H.itype(p); if (t === T.SCOPE) { s.outer = scopeInfo(p); break; } if (t < 0x80 && i < 3 && s.fname === undefined) s.fname = str(p); } catch (e) { } } }
  return id;
}
function val(v, depth = 0) {
  if (H.isSmi(v)) return { t: 'smi', v: H.smi(v) };
  const o = H.ptr(v); const t = H.itype(o);
  if (t < 0x80) return { t: 'str', v: str(o) };
  switch (t) {
    case T.NUM: return { t: 'num', v: H.rd(o + 4, 8).readDoubleLE(0) };
    case T.ODD: return { t: 'odd', v: str(H.ptr(H.u32(o + 12))) };
    case T.SYM: { const dv = H.u32(o + 12); return { t: 'sym', v: H.isSmi(dv) ? null : str(H.ptr(dv)) }; }
    case T.SCOPE: return { t: 'scope', v: scopeInfo(o) };
    case T.SFI: {
      const fd = H.ptr(H.u32(o + 4)); const r = { t: 'sfi' };
      if (H.itype(fd) === T.BA) r.bc = (fd + 34).toString(16);
      const ns = H.ptr(H.u32(o + 8)); const nt = H.itype(ns);
      if (nt === T.SCOPE) r.scope = scopeInfo(ns); else if (nt < 0x80) r.name = str(ns);
      return r;
    }
    case T.ABP: {
      const kind = H.smi(H.u32(o + 4)); const el = H.ptr(H.u32(o + 8)); const n = H.smi(H.u32(el + 4));
      if (kind === 4 || kind === 5) { const r = []; for (let i = 0; i < n; i++) r.push(H.rd(el + 8 + 8 * i, 8).readDoubleLE(0)); return { t: 'arr', kind, v: r.map(x => ({ t: 'num', v: x })) }; }
      return { t: 'arr', kind, v: depth > 6 ? '...' : fa(el).map(x => val(x, depth + 1)) };
    }
    case T.OBP: { const e = fa(o); const props = []; for (let i = 1; i + 1 < e.length; i += 2) props.push([val(e[i], depth + 1), depth > 6 ? '...' : val(e[i + 1], depth + 1)]); return { t: 'obj', flags: H.smi(e[0]), v: props }; }
    case T.FA: return { t: 'fa', v: depth > 6 ? '...' : fa(o).map(x => val(x, depth + 1)) };
    default: return { t: 'raw', itype: t.toString(16), hex: H.rd(o, 24).toString('hex') };
  }
}
setTimeout(() => {
  const log = fs.readFileSync(logf, 'utf8');
  const re = /code-creation,JS,\d+,\d+,(0x[0-9a-f]+),\d+,([^\n]*?) evalmachine\.<anonymous>:1:(\d+),(0x[0-9a-f]+)/g; let m;
  while ((m = re.exec(log))) {
    const bc = Number(BigInt(m[1])); H.setCage(bc); const ba = bc - 34;
    const f = out.funcs[bc.toString(16)] = { name: m[2], pos: +m[3], sfi: Number(BigInt(m[4])).toString(16) };
    const cpv = H.u32(ba + 8); if (H.isSmi(cpv)) { f.cp = []; continue; }
    try { f.cp = fa(H.ptr(cpv)).map(x => { try { return val(x); } catch (e) { return { t: 'err', v: String(e) }; } }); } catch (e) { f.cp = [{ t: 'err', v: String(e) }]; }
    // the function's own ScopeInfo (SFI.name_or_scope_info)
    try { const sfi = Number(BigInt(m[4])); const ns = H.ptr(H.u32(sfi + 8)); if (H.itype(ns) === T.SCOPE) f.scope = scopeInfo(ns); } catch (e) { }
  }
  fs.writeFileSync(outf, JSON.stringify(out));
  console.log('funcs', Object.keys(out.funcs).length, 'scopes', Object.keys(scopes).length);
}, 500);
