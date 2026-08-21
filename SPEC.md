# Spec: Long-Running JSON Output Consistency Harness

## Purpose

Measure how reliably Anthropic models produce **long, structurally-correct JSON**
when asked via plain prompting (no constrained decoding), across:

- **Models** (e.g. Haiku 4.5, Sonnet 4.6, Opus 5)
- **Shot modes**: zero-shot, one-shot, two-shot examples embedded in the system prompt
- **Sampling configs**: temperature / top_p / top_k where the model supports them
- **Repetition**: N iterations per combination (default 100, configurable)

The output is a flat JSONL results file that a Jupyter notebook plots.

## Deliberate design choices

1. **No structured outputs / constrained decoding.** The API's `output_config.format`
   would guarantee valid JSON and defeat the purpose. We test the model's *natural*
   JSON discipline under prompting alone, and validate client-side with JSON Schema.

2. **Sampling params are model-gated.** `temperature` / `top_p` / `top_k` were
   **removed** on the newest models (Opus 5, Sonnet 5, Opus 4.7/4.8, Fable 5; the
   API returns 400 if sent). They still work on Opus 4.6, Sonnet 4.6, Haiku 4.5,
   and older. Each model entry in `config.json` carries `supports_sampling`; for
   models where it is `false`, the runner collapses all sampling configs into a
   single `api-default` run (no knobs sent) instead of erroring.

3. **Thinking is never sent.** On 4.6-family and Haiku models, omitting `thinking`
   disables it (required anyway to use temperature). On Opus 5 it defaults to
   adaptive; we leave the default alone.

4. **Streaming.** Long outputs can exceed HTTP timeouts on non-streaming requests,
   so every call uses `client.messages.stream(...)` and collects the final message.

5. **Examples live in the system prompt** (per requirement: "zero/one/two-shot
   example *system prompts*"), not as message-history turns.

## Tasks

A task = user prompt + system prompt fragment + up to 2 examples + a JSON Schema.
Two built-in tasks cover the depth requirement:

| Task | Shape | Depth | Length driver |
|---|---|---|---|
| `flat_inventory` | array of 300 product objects | 1 level | exactly 300 items (~10K output tokens) |
| `nested_org` | company → departments → teams → members | 4 levels | 8 depts × ≥3 teams × ≥5 members (≥120 leaf members) |

Schemas include cheap sanity constraints ("the data makes sense"): positive prices,
plausible founding years, bounded experience, `additionalProperties: false`,
exact/minimum item counts.

## Metrics recorded per iteration

| Field | Meaning |
|---|---|
| `parse_ok` | output parsed as JSON at all |
| `schema_ok` | parsed JSON validates against the task schema |
| `extraction` | `raw` (pure JSON), `fenced` (```json fences), `embedded` (JSON inside prose), `none` |
| `canonical_hash` | sha256 of sorted-key compact JSON; measures **semantic determinism** |
| `raw_hash` | sha256 of raw text; measures **formatting determinism** |
| `depth` | measured nesting depth of the parsed output |
| `latency_s`, `input_tokens`, `output_tokens`, `stop_reason` | run health |
| `schema_error` | first validation error, if any |
| `ttft_s` | time to first stream event (request accepted and generating) |
| `max_gap_s` | longest silence between stream events; mid-flight health |
| `stalled` | no event for `stall_timeout` s (config, default 90); call aborted |
| `error` / `error_phase` | any failure, tagged `pre-stream` (never started) or `mid-stream` (died while generating) |

Stall detection: the stream is consumed event-by-event with a per-event timeout
(`asyncio.wait_for`), so a connection that goes silent mid-generation is caught
after `stall_timeout` seconds rather than hanging until the SDK's 10-minute
request timeout. Partial text from stalled/errored calls is saved to `outputs/`.

Determinism is analyzed post-hoc: for a (model, task, shots, sampling) cell, the
share of iterations matching the modal `canonical_hash` = semantic determinism;
same with `raw_hash` = byte-level determinism.

## Repeatability

- Everything is driven by `config.json`; no hidden state.
- Each run writes to `results/<UTC-timestamp>/` containing:
  - `config.json` (verbatim copy)
  - `meta.json` (start time, iteration/concurrency/model settings, planned requests)
  - `results.jsonl` (one row per iteration, **appended live**; an interrupted
    run keeps every completed row and the notebook works on partial results)
  - `outputs/` (raw text of **every** response: valid, invalid, and partial
    from stalled/errored calls; each results row's `output_file` points to its
    file, so the full corpus is available for post-hoc analysis)
- Combination order is deterministic; iterations are independent API calls.

## Matrix size / cost control

Planned requests = Σ over models of (tasks × shots × effective-samplings × iterations).
The runner prints the plan and total request count before starting; `--dry-run`
prints it and exits. Use `--iterations` to override the config for cheap smoke runs.

## Out of scope (kept simple on purpose)

- No retry logic beyond the SDK's built-in (2 retries on 429/5xx).
- No prompt caching, no batching, no database; flat files only.
- No semantic "does the data make sense" LLM-judge; schema sanity bounds only.
