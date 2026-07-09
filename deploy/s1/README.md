# S1 (Step-3.7-Flash) — llama.cpp on the 4×3090 rig

## Fixed chat template (tool-calling)
`stepfun-tools.jinja` is the model's embedded chat template with the two minja-incompatible
filters removed (`tool_call.arguments | fromjson` and `arguments | items`) — these break
llama.cpp's tool-call grammar generation under `--jinja`, failing every agent turn with
"Unable to generate parser for this template". The rewrite iterates argument keys directly
and assumes the server delivers arguments as a mapping (recent llama.cpp parses OpenAI
tool_call arguments to objects), with a raw-string fallback. Same class of fix as the S3
Qwen3.6 template (`deploy/bc250/qwen36-tools.jinja`).

Upload to the rig (e.g. `~/model/stepfun-tools.jinja`) and pass `--chat-template-file`.

## Start command (with --jinja + fixed template + caps)
    llama-server \
      -m ~/model/Step-3.7-Flash-UD-IQ3_S-00001-of-00003.gguf \
      -ngl 99 -c 262144 -ctk q8_0 -ctv q8_0 \
      -tb 32 -t 56 --tensor-split 27,24,23,24 --flash-attn on \
      --jinja --chat-template-file ~/model/stepfun-tools.jinja \
      -n 8192 \
      --temp 0.7 --top_k 40 --top_p 0.9 --repeat_penalty 1.1 \
      --host 0.0.0.0 --port 5000 --alias stepfun-3.7-flash

Changes vs the original: added `--jinja` (REQUIRED for chat-template tool-calling — without
it llama.cpp uses built-in formatting and ignores the template), `--chat-template-file`
(the fix above), and `-n 8192` (bound runaway generation on a reasoning model). Everything
else kept as-is.

## Known limitation (not template-fixable)
A tool_call with EMPTY arguments (`arguments: ""`) 500s in llama.cpp's request deserializer
("Failed to parse tool call arguments as JSON: empty input") BEFORE the template runs — same
on S3. If it bites, normalize ""→"{}" upstream (Pi/LiteLLM) or patch llama.cpp.
