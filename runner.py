#!/usr/bin/env python3
"""Consistency harness for long-running JSON outputs from the Anthropic API.

See SPEC.md for design, README.md for usage.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
from jsonschema import Draft202012Validator

FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)


# ---------------------------------------------------------------------------
# Config and plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Combo:
    """One cell of the test matrix; runs `iterations` times."""

    model: str
    supports_sampling: bool
    task_name: str
    shots: int
    sampling: dict[str, Any] = field(hash=False)

    @property
    def sampling_label(self) -> str:
        return self.sampling.get("label", "api-default")


def load_config(path: Path) -> dict:
    with path.open() as fh:
        return json.load(fh)


def effective_samplings(model: dict, samplings: list[dict]) -> list[dict]:
    """Models without sampling support run a single knob-free config."""
    if model.get("supports_sampling", False):
        return samplings
    return [{"label": "api-default"}]


def build_plan(cfg: dict) -> list[Combo]:
    plan = []
    for model in cfg["models"]:
        for task in cfg["tasks"]:
            for shots in cfg["shot_modes"]:
                for sampling in effective_samplings(model, cfg["sampling"]):
                    plan.append(Combo(
                        model=model["id"],
                        supports_sampling=model.get("supports_sampling", False),
                        task_name=task["name"],
                        shots=shots,
                        sampling=sampling,
                    ))
    return plan


# ---------------------------------------------------------------------------
# Prompt and request construction
# ---------------------------------------------------------------------------

def build_system_prompt(cfg: dict, task: dict, shots: int) -> str:
    parts = [cfg["base_system"], task["system"]]
    for example in task["examples"][:shots]:
        parts.append(
            f"Example request:\n{example['user']}\n"
            f"Example response:\n{example['assistant']}"
        )
    return "\n\n".join(parts)


def sampling_params(combo: Combo) -> dict:
    """The API knobs for this combo (empty for models without sampling)."""
    return {k: v for k, v in combo.sampling.items() if k != "label"}


def build_request(cfg: dict, task: dict, combo: Combo) -> dict:
    request = {
        "model": combo.model,
        "max_tokens": cfg.get("max_tokens", 16000),
        "system": build_system_prompt(cfg, task, combo.shots),
        "messages": [{"role": "user", "content": task["user_prompt"]}],
    }
    # SDK >= 1.0 dropped temperature/top_p/top_k from its typed signatures
    # (removed on the newest models); extra_body injects them into the raw
    # request for the older models that still accept them.
    knobs = sampling_params(combo)
    if knobs:
        request["extra_body"] = knobs
    return request


# ---------------------------------------------------------------------------
# Output analysis
# ---------------------------------------------------------------------------

def extract_json(text: str) -> tuple[str, Any]:
    """Return (extraction_mode, parsed) — mode is raw|fenced|embedded|none."""
    stripped = text.strip()
    try:
        return "raw", json.loads(stripped)
    except json.JSONDecodeError:
        pass

    fence = FENCE_RE.match(stripped)
    if fence:
        try:
            return "fenced", json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass

    embedded = _slice_embedded_json(stripped)
    if embedded is not None:
        try:
            return "embedded", json.loads(embedded)
        except json.JSONDecodeError:
            pass

    return "none", None


def _slice_embedded_json(text: str) -> str | None:
    """Slice from the first opening bracket to the last closing one."""
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    ends = [i for i in (text.rfind("}"), text.rfind("]")) if i != -1]
    if not starts or not ends:
        return None
    return text[min(starts):max(ends) + 1]


def json_depth(obj: Any) -> int:
    if isinstance(obj, dict):
        return 1 + max((json_depth(v) for v in obj.values()), default=0)
    if isinstance(obj, list):
        return 1 + max((json_depth(v) for v in obj), default=0)
    return 0


def first_schema_error(validator: Draft202012Validator, parsed: Any) -> str | None:
    error = next(validator.iter_errors(parsed), None)
    if error is None:
        return None
    path = "/".join(str(p) for p in error.absolute_path) or "<root>"
    return f"{path}: {error.message[:200]}"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def analyze_output(text: str, validator: Draft202012Validator) -> dict:
    extraction, parsed = extract_json(text)
    parse_ok = parsed is not None
    schema_error = first_schema_error(validator, parsed) if parse_ok else None
    return {
        "extraction": extraction,
        "parse_ok": parse_ok,
        "schema_ok": parse_ok and schema_error is None,
        "schema_error": schema_error,
        "depth": json_depth(parsed) if parse_ok else None,
        "canonical_hash": (
            sha256(json.dumps(parsed, sort_keys=True, separators=(",", ":")))
            if parse_ok else None
        ),
        "raw_hash": sha256(text),
        "raw_len": len(text),
    }


# ---------------------------------------------------------------------------
# API call — streamed, with mid-flight health tracking
# ---------------------------------------------------------------------------

@dataclass
class StreamStats:
    """Per-call stream health: time to first event, worst inter-event gap."""

    started: float
    first_event_s: float | None = None
    max_gap_s: float = 0.0
    text: str = ""
    _last: float | None = None

    def observe(self, now: float, delta_text: str) -> None:
        if self.first_event_s is None:
            self.first_event_s = now - self.started
        else:
            self.max_gap_s = max(self.max_gap_s, now - self._last)
        self._last = now
        self.text += delta_text

    @property
    def phase(self) -> str:
        return "mid-stream" if self.first_event_s is not None else "pre-stream"


def _delta_text(event: Any) -> str:
    if event.type == "content_block_delta" and event.delta.type == "text_delta":
        return event.delta.text
    return ""


async def _consume_stream(stream: Any, stats: StreamStats, stall_timeout: float) -> None:
    """Drain stream events; raise asyncio.TimeoutError if events stop arriving."""
    events = stream.__aiter__()
    while True:
        try:
            event = await asyncio.wait_for(events.__anext__(), timeout=stall_timeout)
        except StopAsyncIteration:
            return
        stats.observe(time.monotonic(), _delta_text(event))


async def call_model(client: anthropic.AsyncAnthropic, request: dict,
                     stall_timeout: float) -> dict:
    """Run one streamed request. Never raises — errors land in the result dict."""
    stats = StreamStats(started=time.monotonic())
    result: dict[str, Any] = {"error": None, "error_phase": None, "stalled": False}
    try:
        async with client.messages.stream(**request) as stream:
            await _consume_stream(stream, stats, stall_timeout)
            message = await stream.get_final_message()
        result.update(
            stop_reason=message.stop_reason,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )
    except asyncio.TimeoutError:
        result.update(error=f"stall: no stream event for {stall_timeout}s",
                      error_phase=stats.phase, stalled=True)
    except Exception as exc:  # noqa: BLE001 — record, don't crash the run
        result.update(error=f"{type(exc).__name__}: {exc}", error_phase=stats.phase)
    result.update(
        text=stats.text,  # partial text survives stalls and mid-stream errors
        latency_s=round(time.monotonic() - stats.started, 2),
        ttft_s=round(stats.first_event_s, 2) if stats.first_event_s else None,
        max_gap_s=round(stats.max_gap_s, 2),
    )
    return result


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------

class Runner:
    def __init__(self, cfg: dict, run_dir: Path, iterations: int, concurrency: int):
        self.cfg = cfg
        self.run_dir = run_dir
        self.iterations = iterations
        self.stall_timeout = cfg.get("stall_timeout", 90)
        self.client = anthropic.AsyncAnthropic()
        self.semaphore = asyncio.Semaphore(concurrency)
        self.tasks_by_name = {t["name"]: t for t in cfg["tasks"]}
        self.validators = {
            t["name"]: Draft202012Validator(t["schema"]) for t in cfg["tasks"]
        }
        self.completed = 0
        self.total = 0

    async def run(self, plan: list[Combo]) -> None:
        self.total = len(plan) * self.iterations
        jobs = [
            self.run_one(combo, iteration)
            for combo in plan
            for iteration in range(self.iterations)
        ]
        (self.run_dir / "outputs").mkdir(exist_ok=True)
        results_path = self.run_dir / "results.jsonl"
        with results_path.open("w") as fh:
            self.results_fh = fh
            await asyncio.gather(*jobs)
        print(f"Results written to {results_path}")

    async def run_one(self, combo: Combo, iteration: int) -> None:
        task = self.tasks_by_name[combo.task_name]
        request = build_request(self.cfg, task, combo)
        row = self.base_row(combo, iteration)
        async with self.semaphore:
            result = await call_model(self.client, request, self.stall_timeout)
        row.update(self.evaluate(combo, iteration, result))
        self.write_row(row)
        self.report_progress()

    def evaluate(self, combo: Combo, iteration: int, result: dict) -> dict:
        """Save the raw output (partial included), then analyze if the call succeeded."""
        text = result.pop("text")
        result["output_file"] = self.save_output(combo, iteration, text)
        if result["error"] is not None:
            return result
        metrics = analyze_output(text, self.validators[combo.task_name])
        return {**result, **metrics}

    def save_output(self, combo: Combo, iteration: int, text: str) -> str | None:
        if not text:
            return None
        name = (f"{combo.model}_{combo.task_name}_s{combo.shots}"
                f"_{combo.sampling_label}_i{iteration}.txt")
        (self.run_dir / "outputs" / name).write_text(text)
        return f"outputs/{name}"

    def write_row(self, row: dict) -> None:
        """Append immediately so an interrupted run keeps its completed rows."""
        self.results_fh.write(json.dumps(row) + "\n")
        self.results_fh.flush()

    def base_row(self, combo: Combo, iteration: int) -> dict:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "model": combo.model,
            "task": combo.task_name,
            "shots": combo.shots,
            "sampling": combo.sampling_label,
            **sampling_params(combo),
            "iteration": iteration,
        }

    def report_progress(self) -> None:
        self.completed += 1
        if self.completed % 25 == 0 or self.completed == self.total:
            print(f"  {self.completed}/{self.total} requests done")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def print_plan(plan: list[Combo], iterations: int) -> None:
    print(f"Test matrix: {len(plan)} combinations x {iterations} iterations "
          f"= {len(plan) * iterations} API requests")
    for combo in plan:
        print(f"  {combo.model} | {combo.task_name} | {combo.shots}-shot "
              f"| {combo.sampling_label}")


def prepare_run_dir(cfg: dict, config_path: Path, args: argparse.Namespace,
                    planned_requests: int) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(cfg.get("output_dir", "results")) / stamp
    run_dir.mkdir(parents=True)
    (run_dir / "config.json").write_text(config_path.read_text())
    meta = {"started": stamp, "iterations": args.iterations,
            "concurrency": args.concurrency, "model_filter": args.model,
            "stall_timeout": cfg.get("stall_timeout", 90),
            "planned_requests": planned_requests, "config": str(config_path)}
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return run_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--iterations", type=int, default=None,
                        help="override config iterations (use small values for smoke runs)")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="override config concurrency")
    parser.add_argument("--model", default=None,
                        help="run only this model id (must be listed in the config)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and exit without calling the API")
    return parser.parse_args()


def filter_models(cfg: dict, model_id: str | None) -> None:
    """Restrict the config to one model id, keeping its sampling capability flag."""
    if model_id is None:
        return
    matches = [m for m in cfg["models"] if m["id"] == model_id]
    if not matches:
        known = ", ".join(m["id"] for m in cfg["models"])
        raise SystemExit(f"Unknown model '{model_id}' — config defines: {known}")
    cfg["models"] = matches


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    filter_models(cfg, args.model)
    args.iterations = args.iterations or cfg.get("iterations", 100)
    args.concurrency = args.concurrency or cfg.get("concurrency", 4)

    plan = build_plan(cfg)
    print_plan(plan, args.iterations)
    if args.dry_run:
        return

    run_dir = prepare_run_dir(cfg, args.config, args, len(plan) * args.iterations)
    print(f"Run directory: {run_dir}")
    runner = Runner(cfg, run_dir, args.iterations, args.concurrency)
    try:
        asyncio.run(runner.run(plan))
    except KeyboardInterrupt:
        print(f"\nInterrupted — completed rows kept in {run_dir}/results.jsonl")


if __name__ == "__main__":
    main()
