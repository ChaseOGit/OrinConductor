#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/orin-switch2/orin-native"

ACTION="${1:-play}"
GAME_ID="${2:-0400000000010000}"
RAW_DIR="$HOME/orin-switch2/raw_backups"
BIN_PATH="dm/host/build/orin_host_${GAME_ID}"
PACK_DIR="packs/${GAME_ID}"
SHADER_DIR="packs/${GAME_ID}-shaders"
LOG_FILE="play_${GAME_ID}.log"

if [ "$ACTION" = "stop" ]; then
  echo "[launcher] Gracefully stopping running game..."
  pkill -TERM -f "dm/host/build/orin_host" 2>/dev/null || echo "No game running."
  exit 0
fi

if [ "$ACTION" = "shot" ]; then
  if [ -e /tmp/dm_ctl.txt ]; then
    echo "shot $PWD/boot_screen.png" >> /tmp/dm_ctl.txt
    sleep 0.4
    echo "[launcher] Requested live screenshot -> boot_screen.png"
  else
    echo "[launcher] No active DM_VK_CTL control file."
  fi
  exit 0
fi

# Resolve raw source (.nup, merged folder, base+update auto-merge, standalone update, or flat romfs)
resolve_romfs_and_src() {
  if [ -f "$RAW_DIR/${GAME_ID}.nup" ]; then
    SRC_PATH="$RAW_DIR/${GAME_ID}.nup"
    ROMFS_PATH="$SRC_PATH"
    return 0
  fi
  if [ -d "$RAW_DIR/${GAME_ID}_merged/romfs" ]; then
    SRC_PATH="$RAW_DIR/${GAME_ID}_merged"
    ROMFS_PATH="$SRC_PATH/romfs"
    return 0
  fi
  if [ -d "$RAW_DIR/${GAME_ID}/updates" ] && [ -d "$RAW_DIR/${GAME_ID}/base/romfs" ]; then
    LATEST_UPD=$(ls -1 "$RAW_DIR/${GAME_ID}/updates" | sort -n | tail -n 1)
    MERGED="$RAW_DIR/${GAME_ID}_merged"
    echo "[launcher] Auto-merging base + update $LATEST_UPD (hardlinks, 0 extra disk space)..."
    mkdir -p "$MERGED/exefs" "$MERGED/romfs"
    cp -al "$RAW_DIR/${GAME_ID}/base/exefs/"* "$MERGED/exefs/" 2>/dev/null || true
    cp -alf "$RAW_DIR/${GAME_ID}/updates/$LATEST_UPD/exefs/"* "$MERGED/exefs/" 2>/dev/null || true
    cp -al "$RAW_DIR/${GAME_ID}/base/romfs/"* "$MERGED/romfs/" 2>/dev/null || true
    cp -alf "$RAW_DIR/${GAME_ID}/updates/$LATEST_UPD/romfs/"* "$MERGED/romfs/" 2>/dev/null || true
    SRC_PATH="$MERGED"
    ROMFS_PATH="$MERGED/romfs"
    return 0
  fi
  if [ -d "$RAW_DIR/${GAME_ID}/updates" ]; then
    LATEST_UPD=$(ls -1 "$RAW_DIR/${GAME_ID}/updates" | sort -n | tail -n 1)
    if [ -n "$LATEST_UPD" ] && [ -d "$RAW_DIR/${GAME_ID}/updates/$LATEST_UPD/romfs" ]; then
      echo "[launcher] Using standalone update dump $LATEST_UPD for $GAME_ID..."
      SRC_PATH="$RAW_DIR/${GAME_ID}/updates/$LATEST_UPD"
      ROMFS_PATH="$SRC_PATH/romfs"
      return 0
    fi
  fi
  if [ -d "$RAW_DIR/${GAME_ID}/romfs" ]; then
    SRC_PATH="$RAW_DIR/${GAME_ID}"
    ROMFS_PATH="$SRC_PATH/romfs"
    return 0
  fi
  echo "[error] No backup found in $RAW_DIR for $GAME_ID"
  exit 1
}

compile_host_bin() {
  echo "[launcher] Compiling host binary for $GAME_ID -> $BIN_PATH..."
  GAME_ID="$GAME_ID" ./build_safe.sh GAME="$GAME_ID"
  cp -f dm/host/build/orin_host "$BIN_PATH"
}

pack_game_assets() {
  resolve_romfs_and_src
  echo "[launcher] Packing NSOs & Vulkan shaders for $GAME_ID from $SRC_PATH..."
  mkdir -p "$PACK_DIR" "$SHADER_DIR"
  PATH="/usr/local/bin:$PATH" python3 scripts/pack_android.py "$SRC_PATH" \
    --out "$PWD/$PACK_DIR" \
    --shader-cache "$PWD/$SHADER_DIR/lib" \
    --shader-pkg "$PWD/$SHADER_DIR/pkg" \
    -j 4 -J 6
}

build_and_pack() {
  pkill -x llama-server 2>/dev/null || true
  if [ "${FORCE_BUILD:-0}" = "1" ] || [ ! -f "$BIN_PATH" ]; then
    compile_host_bin
  else
    echo "[launcher] Binary $BIN_PATH already exists (use 'build' or 'prep' to recompile)."
  fi

  resolve_romfs_and_src
  if [ "${FORCE_PACK:-0}" = "1" ] || [ ! -f "$PACK_DIR/main.patched.bin" ] || [ ! -d "$SHADER_DIR/lib" ] || [ -z "$(ls -A "$SHADER_DIR/lib" 2>/dev/null)" ]; then
    pack_game_assets
  else
    echo "[launcher] Pack & shader library already ready in $PACK_DIR."
  fi
}

if [ "$ACTION" = "build" ]; then
  compile_host_bin
  echo "✅ Compiled $BIN_PATH!"
  exit 0
fi

if [ "$ACTION" = "repack" ]; then
  pack_game_assets
  echo "✅ Repacked $PACK_DIR & $SHADER_DIR!"
  exit 0
fi

if [ "$ACTION" = "prep" ]; then
  FORCE_BUILD=1 build_and_pack
  echo "✅ $GAME_ID is built and packed, ready to launch!"
  exit 0
fi

# ACTION == play
pkill -TERM -f "dm/host/build/orin_host" 2>/dev/null || true
pkill -x llama-server 2>/dev/null || true
sleep 1

build_and_pack
sudo -n /usr/local/bin/orin-power-mode super >/dev/null 2>&1 || true

export DISPLAY=:0
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
if [ -f "/run/user/$(id -u)/gdm/Xauthority" ] && pgrep -x gnome-shell >/dev/null 2>&1; then
  export XAUTHORITY="/run/user/$(id -u)/gdm/Xauthority"
elif ls /run/user/$(id -u)/.mutter-Xwaylandauth.* >/dev/null 2>&1 && pgrep -x Xwayland >/dev/null 2>&1; then
  export XAUTHORITY=$(ls /run/user/$(id -u)/.mutter-Xwaylandauth.* | head -n 1)
else
  unset XAUTHORITY
  if ! DISPLAY=:0 xset q >/dev/null 2>&1; then
    sudo -n rm -f /tmp/.X11-unix/X0 /tmp/.X0-lock 2>/dev/null || true
    sudo -n bash -c 'nohup /usr/lib/xorg/Xorg :0 -ac -noreset -nolisten tcp +extension GLX +extension RANDR +extension RENDER >/tmp/xorg_play.log 2>&1 &'
    sleep 2
  fi
fi

DISPLAY=:0 xset s off -dpms s noblank 2>/dev/null || true
DISPLAY=:0 xrandr --auto 2>/dev/null || true
NATIVE_RES=$(DISPLAY=:0 xrandr 2>/dev/null | awk '/\*/ {print $1; exit}')
export DM_WINDOW_SIZE="${NATIVE_RES:-1280x720}"

unset DM_KEYS
mkdir -p "$HOME/orin-switch2/saves/${GAME_ID}"
export DM_SAVE="$HOME/orin-switch2/saves/${GAME_ID}"
export DM_SAVE_DIR="$DM_SAVE"
export DM_ROMFS="$ROMFS_PATH"
export DM_VK_POOL_CACHED=1
export DM_VK_POOL_READBACK_RANGES=1
export DM_VK_PLACE_PART_MB=0
export DM_GUEST_FILE=1
export DM_VK_CTL=/tmp/dm_ctl.txt
rm -f /tmp/dm_ctl.txt && touch /tmp/dm_ctl.txt

nohup "./$BIN_PATH" "$PACK_DIR" > "$LOG_FILE" 2>&1 &
GPID=$!
sleep 3

if kill -0 "$GPID" 2>/dev/null; then
  echo "✅ Launched $GAME_ID (PID $GPID, Res: $DM_WINDOW_SIZE) -> logging to $LOG_FILE"
else
  echo "❌ $GAME_ID exited early! Tail of $LOG_FILE:"
  tail -n 25 "$LOG_FILE"
  exit 1
fi
