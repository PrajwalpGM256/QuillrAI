# llm-guardrail

An OpenAI-compatible streaming proxy that redacts PII from an SSE completion stream in real time, chunk-invariant by construction.

---

## What it does

- Forwards `POST /v1/chat/completions` to an upstream LLM provider and relays the response, streamed or not, with PII redacted inline.
- Detects email addresses, US SSNs, and credit card numbers (regex plus Luhn; no ML detector).
- Replaces each match with `[REDACTED]`.

The upstream here is `mock_provider/`, an adversarial mock that splits PII at chosen offsets. No API keys are needed.

---

## Quickstart

```sh
uv sync                                  # Python 3.13, pinned in uv.lock
uv run python scripts/run_stack.py       # mock provider on :9100, guardrail on :8100
```

From Git Bash (PowerShell 5.1 strips the inner quotes from the JSON):

```sh
# one character per event; [REDACTED] arrives inline as each value completes
curl -sN localhost:8100/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"mock-1","stream":true,"messages":[],"mock":{"chunk_size":1,"delay_ms":20}}'

# an SSN split across two events
curl -sN localhost:8100/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"mock-1","stream":true,"messages":[],"mock":{"text":"ssn 123-45-6789 ok","splits":[9]}}'
```

The `mock` object accepts `text`, `splits` (character offsets), `chunk_size`, `delay_ms`, `status`, `break_after`, `n`, and `no_finish`.

---

## The invariant

For any input and any way of splitting it into chunks, the concatenated streamed output is byte-identical to redacting the whole input at once. Streaming and one-shot redaction are the same function (`redact(b) = feed(b) + finish()`), so this is the only correctness bar, and it is proved with Hypothesis rather than examples.

---

## Settlement point

From the `llm_guardrail/redactor.py` docstring:

```
The settlement point S is the first position, scanning from the cursor, where decide_at returns
OPEN: the furthest point before which no currently-open match can still grow. Text before S is
emitted immediately; text from S on is held. MATCH emits [REDACTED] and jumps past the match; NONE
emits one character.
```

A position is OPEN when, for some pattern, the text from that position to the end of the buffer is a prefix of a possible match (`regex` `fullmatch(..., partial=True)`).

---

## Patterns

| Type | Regex summary (`d` = `[0-9]`) | Max match | Window L | Notes |
|---|---|---|---|---|
| SSN | `ddd-dd-dddd`, not adjacent to digits | 11 | 12 | Dashes only. SSA exclusions: area 000, 666, 900-999; group 00; serial 0000 |
| Card | `d(?:[ -]?d){12,18}`, not adjacent to digits | 37 | 38 | 13 to 19 digits with optional single separators. Longest Luhn-valid end wins |
| Email | `local{1,64}@(label.){1,4}tld{2,63}`, word-bounded | 384 | 385 | RFC 5321/1035 limits: 64 + 1 + 4 x (63 + 1) + 63. Keeps the window finite |

The window is the longest match plus one lookahead character. Both are derived from the regex source with the stdlib parser, and an unbounded pattern is refused at import.

**Why Luhn.** `[0-9]{13,19}` also matches order numbers, timestamps, phone numbers and account IDs. Every issued card number passes the Luhn mod-10 check and Luhn rejects about 9 in 10 random digit strings, so the regex finds candidates and Luhn decides.

---

## Design decisions

| Decision | Choice | Why |
|---|---|---|
| One code path | Streaming, non-streaming, and `redact()` all run the same `StreamRedactor` and `decide_at()` | Two implementations could diverge; one cannot |
| Buffer bound | Held text is at most `L - 1` = 384 bytes. OPEN requires the held text to be a prefix of a possible match, so it is no longer than the longest match. An incomplete UTF-8 character (at most 3 bytes) waits in the decoder and is bounded separately | Derived from the patterns, not a tuned constant. Matches are ASCII, so held characters equal held bytes |
| OPEN test | `fullmatch(partial=True)`, not `match(partial=True)` | `match` returns `a@b.com` inside `a@b.com.x` while `a@b.com.xy` could still arrive, which breaks invariance |
| Fail closed upstream | Non-200: same status, body replaced by a gateway error. Broken, malformed, or truncated stream: held text dropped, one `stream_failed` error event, no `[DONE]` | Upstream error bodies can echo the prompt, and a held tail may be the start of a PII value |
| Tools | Requests with `tools` or `functions` get 400 and are never forwarded; a `tool_calls` delta fails the stream | Tool-call arguments would be an unredacted channel |
| Client credential | Never forwarded. `UPSTREAM_API_KEY` is sent if set | The client's token is not issued for the upstream (same rule as task-2) |
| Wire format | OpenAI deltas re-emitted 1:1, one redactor per choice index | Real OpenAI clients work unchanged |

---

## Residual risks

- Prompt-side PII is not redacted; only responses are.
- `tool_calls` are rejected, not redacted.
- A UTF-16 surrogate pair split across two JSON deltas decodes to U+FFFD.
- Emails outside the RFC limits (for example a local part over 64 characters) are not detected.

---

## Verification

```sh
uv run ruff check . && uv run pytest -q                    # single gate: lint + 142 tests
HYPOTHESIS_PROFILE=deep uv run pytest -q tests/test_invariance.py tests/test_buffer_bound.py tests/test_reference_oracle.py
```

| Test file | Proves |
|---|---|
| `test_engine_contract.py` | The `regex` partial-match semantics the OPEN test depends on, pinned against the installed version, including why `match(partial)` would be wrong |
| `test_patterns.py` | Widths derived from the RFC and ISO limits, max-length witnesses built from those limits, SSA exclusions, longest-Luhn card selection, Luhn on published numbers and on every single-digit change |
| `test_reference_oracle.py` | `redact()` equals an independent brute-force stdlib-`re` implementation on arbitrary text and bytes, and does not depend on pattern order. This catches a decision-procedure bug that the invariance test, which compares the redactor with itself, cannot see |
| `test_invariance.py` | Hypothesis: any chunking of PII-rich text or arbitrary bytes (including invalid UTF-8) equals the one-shot result; lookbehind context survives a chunk boundary |
| `test_buffer_bound.py` | Hypothesis: `held_bytes <= 384` and `decoder_pending_bytes <= 3` after every feed, held text starts at an OPEN position, and 384 is reached exactly |
| `test_adversarial_splits.py` | Every two-chunk byte split of an SSN, a card, and an email next to multi-byte characters, plus every three-chunk split |
| `test_utf8.py` | Chunks split inside 2-, 3- and 4-byte characters are deferred, emitted bytes are always valid UTF-8, no U+FFFD is introduced |
| `test_fragments.py` | Under one-character chunking, no emitted chunk contains any fragment of a PII value |
| `test_sse.py` | The SSE parser yields identical events under any byte chunking, including CR, CRLF, and splits inside characters |
| `test_proxy.py` | Stream and non-stream paths, every two-chunk split through the mock, transport splits inside characters, separate choices, no credential forwarding, and every fail-closed path |
| `test_ttft.py` | Over real sockets: first redacted content arrives in under 0.3 s and before a third of a 1 s stream; a real dropped connection fails closed. In-process transports buffer whole responses, so this cannot run in-process |
| `test_config.py` | Invalid configuration exits 2 before binding a port |

---

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `UPSTREAM_URL` | required | Upstream chat completions endpoint (http/https) |
| `UPSTREAM_API_KEY` | unset | Sent upstream as `Authorization: Bearer` |
| `UPSTREAM_TIMEOUT` | `60` | Seconds |
| `MAX_REQUEST_BYTES` | `1048576` | Request body limit (413 above it) |
| `HOST` / `PORT` | `127.0.0.1` / `8100` | Bind address |
| `LOG_LEVEL` | `INFO` | stderr log level |

`scripts/run_stack.py` also reads `GUARDRAIL_PORT` and `PROVIDER_PORT` (defaults `8100` and `9100`).

---

## Project layout

```
guardrail.py             entry point: load config (exit 2 if invalid), serve on 127.0.0.1
llm_guardrail/
  patterns.py            pattern definitions, derived windows, luhn()
  redactor.py            decide_at(), StreamRedactor (settlement point), redact()
  sse.py                 incremental SSE parser and serializer
  app.py                 /v1/chat/completions: forward, per-choice redaction, fail closed
  config.py              environment config, validated at startup
  logging_setup.py       single stderr log handler
mock_provider/server.py  adversarial OpenAI-compatible mock (splits, delays, errors, drops)
scripts/run_stack.py     run mock provider and guardrail locally
tests/                   pytest suite; reference.py is the independent oracle
```
