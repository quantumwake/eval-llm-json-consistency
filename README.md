# Long-Running JSON Output Consistency Harness

Tests whether Anthropic models produce long, structurally-correct JSON
consistently across models, shot modes (zero/one/two-shot system prompts),
sampling configs, and hundreds of iterations. Design details in [SPEC.md](SPEC.md).

## Setup

```bash
cp .env.example .env      # then paste your API key into .env
make install
```

## Run

```bash
make dry-run              # see the matrix and request count (no API calls)
make smoke                # cheap live run: 2 iterations per cell
make run                  # full run (config default: 100 iterations per cell)

# Options on any target:
make run MODEL=claude-sonnet-4-6 ITERATIONS=300
make analyze              # open the results notebook
```

(`runner.py` can also be called directly; `make` targets are thin wrappers
around it; the key can come from `.env` via make or a plain exported
`ANTHROPIC_API_KEY`.)

Each run writes a self-contained directory:

```
results/<UTC-timestamp>/
  config.json     # verbatim copy of the config that produced this run
  meta.json       # start time, iteration/concurrency settings
  results.jsonl   # one row per API call: all metrics (appended live)
  outputs/        # raw text of every response, incl. partials from stalls/errors
```

## Analyze

```bash
jupyter lab analyze.ipynb   # loads the latest run automatically, re-run top to bottom
```

Plots: schema-valid rate by model x shot mode, content determinism by sampling
config, extraction-mode breakdown (raw vs fenced vs embedded JSON), latency and
output-token distributions, plus a failure drill-down.

## Configuring

Everything lives in `config.json`:

- **models**: each entry has `supports_sampling`. Temperature/top_p/top_k were
  removed on Opus 5 / Sonnet 5 / Opus 4.7+ / Fable 5 (the API rejects them with
  a 400); they still work on Opus 4.6 / Sonnet 4.6 / Haiku 4.5. Models with
  `supports_sampling: false` run once per cell with API defaults.
- **sampling**: list of knob sets, e.g. `{"label": "greedy", "temperature": 0.0}`.
  Add `top_p` / `top_k` the same way. Applied only to models that support them.
- **shot_modes**: how many of each task's `examples` get embedded in the system
  prompt (0, 1, 2).
- **tasks**: prompt + examples + a JSON Schema. `flat_inventory` is one level
  deep (300 items, for length pressure); `nested_org` is four levels deep. Add your
  own by following the same shape.
- **iterations / concurrency / max_tokens**: run-level knobs; iterations and
  concurrency can be overridden on the CLI.

## Cost awareness

Total requests = models x tasks x shot modes x effective sampling configs x
iterations. Outputs are deliberately long (~10K tokens for the 300-item flat
task, ~5K for the nested one), so cost adds up fast: the default config at 25
iterations is 1,050 requests (~$120 across all three models); a single-model run
(`MODEL=claude-sonnet-4-6`) is 450 requests (~$50). Check `--dry-run` before a
full run, and trim models/samplings or lower iterations if needed.
