#!/usr/bin/env python3
"""Very small Ignition -> pseudo-JS folder (linear, goto-based). Good enough to read logic.
usage: decomp.py main.annotated.txt.pkl <srcpos>[,<srcpos>...] | -n <name-regex>"""
import pickle, re, sys

P = pickle.load(open(sys.argv[1], 'rb')); INS = P['ins']; F = P['F']

BIN = {'Add': '+', 'Sub': '-', 'Mul': '*', 'Div': '/', 'Mod': '%', 'Exp': '**', 'BitwiseOr': '|', 'BitwiseXor': '^',
       'BitwiseAnd': '&', 'ShiftLeft': '<<', 'ShiftRight': '>>', 'ShiftRightLogical': '>>>',
       'TestEqual': '==', 'TestEqualStrict': '===', 'TestLessThan': '<', 'TestGreaterThan': '>',
       'TestLessThanOrEqual': '<=', 'TestGreaterThanOrEqual': '>=', 'TestInstanceOf': 'instanceof', 'TestIn': 'in',
       'TestReferenceEqual': '==='}
READS_ACC = re.compile(r'^(Star|Sta|Set|Define|Test|JumpIf|Return|Throw|ReThrow|Add|Sub|Mul|Div|Mod|Exp|Bitwise|Shift|Inc|Dec|Negate|LogicalNot|ToBoolean|ToBooleanLogicalNot|TypeOf|ToNumber|ToNumeric|ToString|ToObject|ToName|GetKeyedProperty|SwitchOn|StaInArrayLiteral|ThrowReferenceErrorIfHole|ThrowIfNotSuperConstructor|ThrowSuperNotCalled|ThrowSuperAlreadyCalledIfNotHole|SetPendingMessage|PushContext|ForInPrepare|GetIterator|CreateArrayFromIterable|CloneObject|Jump$)')

def rng(a):  # "r3-r5" -> [r3,r4,r5]
    m = re.match(r'([ra])(\d+)-[ra](\d+)', a)
    if not m: return [a]
    return [f'{m.group(1)}{i}' for i in range(int(m.group(2)), int(m.group(3)) + 1)]

def decomp(bc):
    ins = INS.get(bc, []); out = []
    regs = {'<this>': 'this', '<closure>': 'closure', '<context>': 'context'}
    acc = None; pend = None; awaited = None; skip_until_rethrow = False; resume_reg = None
    targets = set()
    for off, op, args, note in ins:
        m = re.search(r'@ (\d+)\)', ' '.join(args))
        if op.startswith('Jump') and m: targets.add(int(m.group(1)))
    def R(a):
        if a.startswith('a') and a[1:].isdigit(): return 'arg' + a[1:]
        return regs.get(a, a)
    def emit(s): out.append('    ' + s)
    tmp = [0]; inflow = {}; dead = False
    for off, op, args, note in ins:
        base = op.split('.')[0]
        if off in targets:
            if pend: emit(pend); pend = None
            out.append(f'  L{off}:')
            vals = inflow.get(off, []) + ([] if dead else [acc])
            uniq = list(dict.fromkeys(v for v in vals))
            acc = uniq[0] if len(uniq) == 1 else '(' + ' ‖ '.join(str(v) for v in uniq) + ')'
        dead = base in ('Jump', 'JumpConstant', 'Return', 'Throw', 'ReThrow', 'JumpLoop')
        if base.startswith('Jump'):
            mm = re.search(r'@ (\d+)\)', ' '.join(args))
            if mm: inflow.setdefault(int(mm.group(1)), []).append(acc)
        if skip_until_rethrow:
            if base == 'Star' and resume_reg is None: resume_reg = args[0]; regs[args[0]] = acc
            elif re.match(r'Star\d', base) and resume_reg is None: resume_reg = 'r' + base[4:]; regs[resume_reg] = acc
            if base == 'ReThrow': skip_until_rethrow = False; resume_reg = None
            continue
        n0 = note[0] if note else None
        if pend and not READS_ACC.match(base):
            emit(pend); pend = None
        pend = None if READS_ACC.match(base) else pend
        star = re.match(r'Star(\d+)$', base)
        if star or base == 'Star':
            r = 'r' + star.group(1) if star else args[0]
            if acc is not None and (('(' in acc and not acc.startswith('function') and not acc.startswith('(await')) or acc[:1] in '[{' or acc.startswith('new ')):
                tmp[0] += 1; v = f'{r}_{tmp[0]}'; emit(f'{v} = {acc}'); regs[r] = v
            else: regs[r] = acc
            continue
        if base == 'Mov': regs[args[1]] = R(args[0]); continue
        if base == 'Ldar': acc = R(args[0]); continue
        if base == 'LdaZero': acc = '0'; continue
        if base == 'LdaSmi': acc = args[0][1:-1]; continue
        if base in ('LdaUndefined', 'LdaNull', 'LdaTrue', 'LdaFalse', 'LdaTheHole'):
            acc = {'LdaUndefined': 'undefined', 'LdaNull': 'null', 'LdaTrue': 'true', 'LdaFalse': 'false', 'LdaTheHole': '<hole>'}[base]; continue
        if base in ('LdaConstant', 'LdaGlobal', 'LdaGlobalInsideTypeof', 'CreateArrayLiteral', 'CreateObjectLiteral', 'CreateRegExpLiteral', 'GetTemplateObject'):
            acc = n0 if base not in ('LdaGlobal', 'LdaGlobalInsideTypeof') else (n0 or '').strip('"'); continue
        if base == 'CreateClosure': acc = 'function' + (n0 or ''); continue
        if base == 'CreateEmptyArrayLiteral': acc = '[]'; continue
        if base == 'CreateEmptyObjectLiteral': acc = '{}'; continue
        if 'ContextSlot' in base and base.startswith('Lda'):
            acc = (n0 or f'ctx{args}').split(':', 1)[-1]; continue
        if 'ContextSlot' in base and base.startswith('Sta'):
            emit(f"{(n0 or f'ctx{args}').split(':', 1)[-1]} = {acc}"); continue
        if base == 'StaGlobal': emit(f"{(n0 or '').strip(chr(34))} = {acc}"); continue
        if base in ('GetNamedProperty', 'GetNamedPropertyFromSuper'):
            acc = f"{R(args[0])}.{(n0 or '?').strip(chr(34))}"; continue
        if base == 'GetKeyedProperty': acc = f'{R(args[0])}[{acc}]'; continue
        if base == 'LdaKeyedProperty': acc = f'{R(args[0])}[{acc}]'; continue
        if base in ('SetNamedProperty', 'DefineNamedOwnProperty'):
            emit(f"{R(args[0])}.{(n0 or '?').strip(chr(34))} = {acc}"); continue
        if base in ('SetKeyedProperty', 'DefineKeyedOwnProperty', 'StaInArrayLiteral', 'DefineKeyedOwnPropertyInLiteral'):
            emit(f'{R(args[0])}[{R(args[1])}] = {acc}'); continue
        if base.startswith('CallProperty') or base.startswith('CallUndefinedReceiver') or base in ('CallAnyReceiver', 'CallWithSpread'):
            regsl = [x for a in args[:-1] for x in rng(a)]
            f = R(regsl[0])
            rest = regsl[1:]
            if base.startswith('CallProperty') or base in ('CallAnyReceiver', 'CallWithSpread'): rest = rest[1:]
            acc = f"{f}({', '.join(R(x) for x in rest)})"; pend = acc; continue
        if base in ('Construct', 'ConstructWithSpread'):
            regsl = [x for a in args[1:-1] for x in rng(a)]
            acc = f"new {R(args[0])}({', '.join(R(x) for x in regsl)})"; pend = acc; continue
        if base in ('CallRuntime', 'InvokeIntrinsic', 'CallJSRuntime'):
            name = args[0].strip('[]'); regsl = [x for a in args[1:] for x in rng(a)]
            if name in ('_AsyncFunctionAwaitUncaught', '_AsyncFunctionAwaitCaught', '_AsyncGeneratorAwaitUncaught', '_AsyncGeneratorAwaitCaught'):
                awaited = R(regsl[1]); continue
            if name == '_AsyncFunctionEnter': continue
            if name in ('_AsyncFunctionResolve',): emit(f'return {R(regsl[1])}'); continue
            if name in ('_AsyncFunctionReject',): continue
            acc = f"%{name}({', '.join(R(x) for x in regsl)})"; pend = acc; continue
        if base == 'SuspendGenerator': continue
        if base == 'ResumeGenerator': acc = f'(await {awaited})'; skip_until_rethrow = True; continue
        if base == 'SwitchOnGeneratorState': continue
        if base in BIN:
            acc = f'({R(args[0])} {BIN[base]} {acc})'; continue
        m = re.match(r'(\w+?)Smi$', base)
        if m and m.group(1) in BIN: acc = f'({acc} {BIN[m.group(1)]} {args[0][1:-1]})'; continue
        if base in ('TestUndetectable',): acc = f'({acc} == null)'; continue
        if base == 'TestNull': acc = f'({acc} === null)'; continue
        if base == 'TestUndefined': acc = f'({acc} === undefined)'; continue
        if base == 'TestTypeOf': acc = f'(typeof {acc} === {args[0]})'; continue
        if base in ('Inc', 'Dec'): acc = f"({acc} {'+' if base == 'Inc' else '-'} 1)"; continue
        if base == 'Negate': acc = f'(-{acc})'; continue
        if base == 'BitwiseNot': acc = f'(~{acc})'; continue
        if base in ('LogicalNot', 'ToBooleanLogicalNot'): acc = f'(!{acc})'; continue
        if base == 'TypeOf': acc = f'(typeof {acc})'; continue
        if base in ('ToNumber', 'ToNumeric', 'ToString', 'ToName', 'ToObject', 'ToBoolean'): continue
        if base == 'ThrowReferenceErrorIfHole': continue
        if base == 'Return': emit(f'return {acc}'); continue
        if base in ('Throw', 'ReThrow'): emit(f'throw {acc}'); continue
        if base.startswith('Jump'):
            m = re.search(r'@ (\d+)\)', ' '.join(args)); t = m.group(1) if m else '?'
            cond = {'JumpIfTrue': '{a}', 'JumpIfToBooleanTrue': '{a}', 'JumpIfFalse': '!{a}', 'JumpIfToBooleanFalse': '!{a}',
                    'JumpIfNull': '{a} === null', 'JumpIfNotNull': '{a} !== null', 'JumpIfUndefined': '{a} === undefined',
                    'JumpIfNotUndefined': '{a} !== undefined', 'JumpIfUndefinedOrNull': '{a} == null', 'JumpIfJSReceiver': 'isObject({a})'}
            b2 = base.replace('Constant', '')
            if b2 in ('Jump', 'JumpLoop'): emit(f'goto L{t}')
            elif b2 in cond: emit(f"if ({cond[b2].format(a=acc)}) goto L{t}")
            else: emit(f'/* {op} {args} */')
            continue
        if base == 'CreateCatchContext': acc = '<catch>'; emit('} catch (e) {'); continue
        if base in ('PushContext', 'PopContext', 'CreateFunctionContext', 'CreateBlockContext', 'SetPendingMessage'): continue
        emit(f"/* {op} {', '.join(args)} */ " + (f"; {note}" if note else ''))
        acc = f'<{op}>'
    if pend: emit(pend)
    return out

sel = sys.argv[2:]
if sel[0] == '-n':
    bcs = [b for b, f in F.items() if re.search(sel[1], f['name'] or '')]
else:
    ps = {int(x) for x in sel[0].split(',')}; bcs = [b for b, f in F.items() if f['pos'] in ps]
for b in sorted(bcs, key=lambda b: F[b]['pos']):
    f = F[b]; chain = []; p = P['parent'].get(b)
    while p: chain.append(F[p]['name'] or '<anon>'); p = P['parent'].get(p)
    print(f"\n// ===== {f['name'] or '<anon>'} @src{f['pos']}  (in {' < '.join(chain[:3])})")
    print('\n'.join(decomp(b)))
