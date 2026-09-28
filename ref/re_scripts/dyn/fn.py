import sys,re
# usage: fn.py file pattern  -> list functions containing pattern ; fn.py file -p <srcpos> -> print function
txt=open(sys.argv[1]).read().split('\n==== FUNC ')
if sys.argv[2]=='-p':
    for b in txt:
        if re.search(r'@src(%s) '%sys.argv[3],b.split('\n')[0]): print('==== FUNC '+b)
else:
    for b in txt:
        if re.search(sys.argv[2],b): print(b.split('\n')[0], len(re.findall(sys.argv[2],b)))
