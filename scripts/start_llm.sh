#!/usr/bin/env bash
set -euo pipefail
MODEL_PATH=$(ls "$HOME/models/"*Qwen3.6-35B-A3B*Q4_K_XL*.gguf | head -n 1)
SLOT_DIR="$HOME/orin-switch2/kv_slots"

if ! pgrep -x "llama-server" >/dev/null 2>&1; then
  nohup /usr/local/bin/llama-server \
    -m "$MODEL_PATH" \
    -ngl 99 -fa on \
    -ctk q8_0 -ctv q8_0 \
    -c 32768 --parallel 1 \
    --spec-type draft-mtp --spec-draft-n-max 2 \
    --slot-save-path "$SLOT_DIR" \
    --alias "Qwen3.6-35B-A3B" \
    --port 8080 > "$HOME/orin-switch2/llama_server.log" 2>&1 &
fi

# Always wait until model weights finish loading into CUDA memory (/health == ok)
for i in {1..120}; do
  if curl -s http://localhost:8080/health | grep -q "ok"; then
    exit 0
  fi
  sleep 0.5
done
exit 1
