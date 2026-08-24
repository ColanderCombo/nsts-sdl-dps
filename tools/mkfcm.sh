#!/bin/zsh
set -e
SDL=${0:A:h}/..
cd $SDL
PY=build/venv/bin/python
ROOT=code/OI340700
OUT=build/OI340700

cmake -S . -B build
cmake --build build -j $(sysctl -n hw.ncpu)

$PY -m con80.con80build --build-all --root $ROOT --out $OUT

$PY -m tools.mmu2fcm --mmu $OUT --config IPL --phases 10 \
    --con80 $ROOT/CON80 --out $OUT/IPL \
    --stamp-checksums

for c in SSW G16 G2 G3 S2 S4 P9 G8 G9; do
  $PY -m tools.mmu2fcm --mmu $OUT --config $c \
      --con80 $ROOT/CON80 --out $OUT/$c \
      --stamp-phase-tables --stamp-checksums \
      --system-id 29.01A.01B --iload-id 29.1.0.00.0B
done
