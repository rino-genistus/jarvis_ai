# Latency

Apple M4, 24 GB · qwen2.5:7b on Ollama · measured 2026-09-29 to 2026-10-01

## Per turn: you stop talking → transcript

| Stage | Before | Now |
|---|---|---|
| End of speech detected | 0.8–1 s | 0.53 s |
| Transcription | 0.37 s (Whisper small) | 0.15 s (Parakeet) |
| Intent check | 0.3–7 s (qwen) | 0.01–0.1 s (embeddings) |

## Transcript → first word

| Turn | Before | Now |
|---|---|---|
| First turn of a conversation | 4.1 s | 0.8 s |
| Chat after chat | 1.4–1.7 s | 0.5–0.6 s |
| Chat after a tool turn | 5.4 s | 2.5 s |
| Tool turn (acknowledgement) | 0.3–7 s | 0.00 s |

## Tool turns

| Metric | Before | Now |
|---|---|---|
| Tool selection | 6–8 s | 1.8–3.8 s |
| Simple lookup, full answer | 8.5–12 s | 5–9 s |
| Find-then-change (agent loop) | — | 9–25 s |
| Gmail unread | 2.8 s + ~6 s reading ~1,200 tokens | same |
| Reminders | 4–5 s | same |

## Startup

| | Time |
|---|---|
| Engine ready, models warm | 7.7 s |

## Kokoro variants

| Variant | First sentence | Long sentence |
|---|---|---|
| PyTorch (current) | 0.31 s | 0.58 s |
| ONNX fp32 | 0.71 s | 1.19 s |
| ONNX int8 | 1.27 s | 3.10 s |
