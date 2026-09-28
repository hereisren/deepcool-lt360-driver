#!/bin/sh
# print function body for address $1 from $2 (default L136.asm), dropping pure jmp/nop noise
awk -v a="$1" '$0 ~ "^;==== FUNC "a" " {p=1; print; next} /^;==== FUNC/ {p=0} p' ${2:-L136.asm} | grep -vE ': nop|: int3'
