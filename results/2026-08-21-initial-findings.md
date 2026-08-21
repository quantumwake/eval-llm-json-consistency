# Memo: Initial Findings, Long-Output JSON Consistency

**Date:** 2026-08-21
**Runs:** `20260821T110511Z` (Sonnet 4.6, 300-item flat / 8-dept nested) and
`20260821T111050Z` (Haiku 4.5, 600-item flat / 8-dept nested)
**Scale:** 36 requests each (2 iterations x 18 cells: 2 tasks x 3 shot modes x 3 sampling configs)
**Caveat:** N=2 per cell. Directional, not statistical. The failure signatures are
specific enough to be worth acting on anyway.

## Headline

Both models degrade on long JSON outputs, but in completely different ways, and
neither failure mode would appear at small output sizes. Zero-shot prompting with
temperature 0 was flawless on both models; every failure involved either
temperature 1, few-shot examples, or both.

## Findings

**1. Sonnet 4.6 emits rare single-token corruption deep in long outputs (6/36 calls).**
Every failure was the identical glitch: `"price">` instead of `"price":`, appearing
8K to 28K characters into otherwise perfect output. All 300 items were always
present. Occurred only at temperature 1 with 1- or 2-shot prompts; greedy sampling
went 12/12 and zero-shot went 6/6. One wrong character kills the entire parse.

**2. Haiku 4.5 silently drops required fields under length pressure.**
In 600-item arrays, items deep in the output (#203, #239, #292, #355) lost
`category` or `price`, and one item gained a spurious field. The item count stayed
correct; the per-item structure decayed. This is content drift, not token noise.

**3. Few-shot examples actively hurt Haiku on nested structure (0/12 vs 6/6 zero-shot).**
With any example present, every single nested response produced exactly 7
departments instead of the required 8 (even at temperature 0) and wrapped the JSON
in markdown fences despite explicit instructions. The tiny 1-department example
anchored both the count and the formatting. Zero-shot was perfect.

**4. Transport was never the problem.**
72/72 calls streamed cleanly: no errors, no stalls, worst inter-event gap 3.9s
against a 90s stall threshold, all `end_turn`. Latency: ~175s per 300-item Sonnet
call (~15.5K tokens), ~90-128s per 600-item Haiku call (~20-28K tokens).

**5. Temperature 0 is not reproducibility.**
Greedy sampling produced different content across identical calls in most cells
(the API has no seed). It buys structural reliability, not byte-level determinism.

## Implications (tentative)

- For long JSON generation, prefer zero-shot + temperature 0; if examples are
  needed, match their scale to the requested output (tiny examples anchor badly).
- Client-side schema validation is non-negotiable: every failure here returned
  HTTP 200 with `end_turn` and looked healthy at the transport level.

## Next

25 iterations per cell on the interesting cells to put real rates on findings 1-3;
test whether a scale-matched example removes the anchoring effect.
