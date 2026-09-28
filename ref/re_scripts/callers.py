import pickle, sys
f = pickle.load(open(sys.argv[1]+'.idx','rb'))
starts = sorted(f)
def callers(t): return [b for b,(s,c,n) in f.items() if t in c]
def show(t, depth, ind=0, seen=set()):
    for c in sorted(callers(t)):
        s = f[c][0]
        print('  '*ind + f'{c:#x} (n={f[c][2]}) strs={s[:3]}')
        if depth>1 and c not in seen:
            seen.add(c); show(c, depth-1, ind+1, seen)
for a in sys.argv[2:]:
    t=int(a,16); print('== callers of', hex(t)); show(t, 4)
