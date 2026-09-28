#!/usr/bin/env python3
"""Merge V8 --log-code-disassemble output with the constant pools / ScopeInfos dumped by
dumpcp.js into a readable, annotated Ignition listing.
usage: annotate.py main.log main.cp.json out.txt"""
import json, re, sys
from collections import deque

logf, cpf, outf = sys.argv[1:4]
D = json.load(open(cpf)); F = D['funcs']; S = D['scopes']

dis = {}
for line in open(logf, encoding='utf-8', errors='replace'):
    if line.startswith('code-disassemble,') and ',INTERPRETED_FUNCTION,' in line:
        _, addr, _, body = line.rstrip('\n').split(',', 3)
        dis[format(int(addr, 16), 'x')] = body.replace('\\x2C', ',').split('\\n')

# which operand (0-based) of each bytecode is a constant-pool index
CPOP = {'LdaConstant': 0, 'LdaGlobal': 0, 'LdaGlobalInsideTypeof': 0, 'StaGlobal': 0,
        'LdaLookupSlot': 0, 'LdaLookupContextSlot': 0, 'LdaLookupGlobalSlot': 0, 'StaLookupSlot': 0,
        'LdaLookupSlotInsideTypeof': 0, 'LdaLookupContextSlotInsideTypeof': 0, 'LdaLookupGlobalSlotInsideTypeof': 0,
        'GetNamedProperty': 1, 'GetNamedPropertyFromSuper': 1, 'SetNamedProperty': 1, 'DefineNamedOwnProperty': 1,
        'CreateClosure': 0, 'CreateBlockContext': 0, 'CreateCatchContext': 1, 'CreateFunctionContext': 0,
        'CreateEvalContext': 0, 'CreateWithContext': 1, 'CreateRegExpLiteral': 0, 'CreateArrayLiteral': 0,
        'CreateObjectLiteral': 0, 'GetTemplateObject': 0, 'ThrowReferenceErrorIfHole': 0,
        'SwitchOnSmiNoFeedback': 0, 'SwitchOnGeneratorState': 1}
CTXOP = {'CreateFunctionContext', 'CreateBlockContext', 'CreateCatchContext', 'CreateEvalContext', 'CreateWithContext'}

def fname(bc):
    f = F.get(bc); return (f['name'] or '<anon>') if f else '?'

def show(v, depth=0):
    t = v['t']
    if t == 'str': return json.dumps(v['v'], ensure_ascii=False)
    if t in ('smi', 'num'): return repr(v['v'])
    if t == 'odd': return v['v']
    if t == 'sym': return f"Symbol({v['v']})"
    if t == 'sfi': return f"<fn {fname(v.get('bc'))} @{F.get(v.get('bc'), {}).get('pos', '?')}>"
    if t == 'scope':
        s = S[v['v']]; return f"<scope t{s['type']} {s['names'][:6]}{'...' if s['n'] > 6 else ''}>"
    if t == 'arr': return '[' + ', '.join(show(x, depth + 1) for x in v['v']) + ']' if isinstance(v['v'], list) else '[...]'
    if t == 'obj': return '{' + ', '.join(f"{show(k)}: {show(x, depth + 1) if isinstance(x, dict) else x}" for k, x in v['v']) + '}'
    if t == 'fa': return '#(' + ', '.join(show(x, depth + 1) for x in v['v']) + ')' if isinstance(v['v'], list) else '#(...)'
    return f"<{t} {v.get('itype', '')}>"

INS = re.compile(r'^\s*(\d+ [SE]> )?\s*0x[0-9a-f]+ @\s+(\d+) : ((?:[0-9a-f]{2} )+)\s*(\S+)\s*(.*)$')

def local(scope, slot):
    s = S.get(scope)
    if not s: return None
    i = slot - 2
    return s['names'][i] if 0 <= i < len(s['names']) else None

entry_ctx = {}
STRUCT = {}
children = {}
minpos = min(f['pos'] for f in F.values())
top = [bc for bc, f in F.items() if f['pos'] == minpos]
out = []

def process(bc, ctx):
    lines = dis.get(bc, []); f = F[bc]; cp = f.get('cp', [])
    cur = list(ctx); acc = None; regs = {}
    res = []
    for ln in lines:
        m = INS.match(ln)
        if not m:
            res.append('    ' + ln); continue
        off, op, ops = m.group(2), m.group(4), m.group(5)
        base = op.split('.')[0]
        args = [a.strip() for a in ops.split(',')] if ops else []
        note = []
        if base in CPOP:
            k = CPOP[base]
            if k < len(args) and args[k].startswith('['):
                i = int(args[k][1:-1])
                if i < len(cp):
                    v = cp[i]; note.append(show(v))
                    if base == 'CreateClosure' and v['t'] == 'sfi' and v.get('bc') in F:
                        entry_ctx.setdefault(v['bc'], list(cur)); children.setdefault(bc, []).append(v['bc'])
                    if base in CTXOP and v['t'] == 'scope': acc = cur + [v['v']]
        if base == 'PushContext':
            regs[args[0]] = list(cur); cur = acc if acc is not None else cur + ['?']
        elif base == 'PopContext':
            cur = regs.get(args[0], cur[:-1])
        elif base == 'Mov' and args and args[0] == '<context>':
            regs[args[1]] = list(cur)
        elif base in ('LdaCurrentContextSlot', 'LdaImmutableCurrentContextSlot', 'StaCurrentContextSlot'):
            if cur: n = local(cur[-1], int(args[0][1:-1])); note.append(f'ctx:{n}')
        elif base in ('LdaContextSlot', 'LdaImmutableContextSlot', 'StaContextSlot'):
            b = cur if args[0] == '<context>' else regs.get(args[0], cur)
            slot, depth = int(args[1][1:-1]), int(args[2][1:-1])
            if len(b) > depth: n = local(b[-1 - depth], slot); note.append(f'ctx^{depth}:{n}')
        STRUCT.setdefault(bc, []).append((int(off), op, args, note))
        res.append(f"{off:>6}: {op} {ops}" + (f"    ; {' | '.join(str(x) for x in note)}" if note else ''))
    return res

order = deque((t, []) for t in top); seen = set(); body = {}
while order:
    bc, ctx = order.popleft()
    if bc in seen or bc not in F: continue
    seen.add(bc); body[bc] = process(bc, ctx)
    for c in children.get(bc, []): order.append((c, entry_ctx[c]))
for bc in F:
    if bc not in seen: body[bc] = process(bc, [])

parent = {c: p for p, cs in children.items() for c in cs}
with open(outf, 'w') as o:
    for bc in sorted(F, key=lambda b: F[b]['pos']):
        f = F[bc]; chain = []; p = parent.get(bc)
        while p: chain.append(fname(p)); p = parent.get(p)
        o.write(f"\n==== FUNC {f['name'] or '<anon>'} @src{f['pos']} bc=0x{bc}  parents: {' < '.join(chain[:4])}\n")
        o.write('\n'.join(body.get(bc, [])) + '\n')
import pickle
pickle.dump({'ins': STRUCT, 'F': F, 'parent': parent}, open(outf + '.pkl', 'wb'))
print('written', len(body))
