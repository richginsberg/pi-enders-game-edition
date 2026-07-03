#!/usr/bin/env bash
# Mock `pi --mode json -p --no-session --model <m> <prompt>` for fanout tests.
# Emits NDJSON like the real pi JSON mode; echoes model+complexity so tests can
# assert routing metadata reached the child.
prompt="${@: -1}"
model=""
prev=""
for a in "$@"; do
  [ "$prev" = "--model" ] && model="$a"
  prev="$a"
done

# track concurrency via a lock dir counter
if [ -n "$MOCK_CONCURRENCY_DIR" ]; then
  n=$(ls "$MOCK_CONCURRENCY_DIR" | wc -l)
  touch "$MOCK_CONCURRENCY_DIR/$$"
  echo "$((n + 1))" >> "$MOCK_CONCURRENCY_DIR/../peak.log"
fi

sleep "${MOCK_SLEEP:-0.2}"

echo '{"type":"message_end","message":{"role":"user","content":"'"$prompt"'"}}'
echo '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"done:'"$prompt"'|model='"$model"'|cx='"$DNC_COMPLEXITY"'"}]}}'

[ -n "$MOCK_CONCURRENCY_DIR" ] && rm -f "$MOCK_CONCURRENCY_DIR/$$"
exit 0
