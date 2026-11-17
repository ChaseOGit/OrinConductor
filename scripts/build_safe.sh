#!/usr/bin/env bash
set -euo pipefail
export CCACHE_DIR="${CCACHE_DIR:-$HOME/orin-switch2/../ccache}"
export DM_TARGET=android
export CC="ccache clang -D_GNU_SOURCE"
export CXX="ccache clang++ -D_GNU_SOURCE"
export CFLAGS="-O3 -mcpu=native -fuse-ld=mold -fPIC -D_GNU_SOURCE"
export CXXFLAGS="-O3 -mcpu=native -fuse-ld=mold -fPIC -D_GNU_SOURCE"
GAME_TARGET="${GAME_ID:-0400000000010000}"

make -C dm/host -j2 GAME="$GAME_TARGET" "$@" 2>&1 | tee build.log
if [ -f "dm/host/dmhost" ]; then
  mkdir -p dm/host/build
  cp -f dm/host/dmhost dm/host/build/orin_host
fi
