---
name: eval-metrics
description: How crossfoot's four eval metrics (auto-match precision, coverage, escalation rate, recall at precision ≥0.99) and per-type breakdown are defined and computed at the match-group level. Load when touching src/crossfoot/eval/, writing eval tests, interpreting `just eval` output, or reporting results.
---

# Eval metrics

## Scoring unit: the match group
A group is `{source record ids} → {target record ids}`. Scoring is **group-level**, not pairwise:
- Predicted group identical to a ground-truth group → **TP**.
- Partial overlap → **one FP and one FN** (not partial credit).
- Orphans carry explicit `no_match` in the manifest; duplicates carry `duplicate_of`. Correct non-matches are scored — the confusion matrix has a **no-match row**.

## The four metrics
Plain precision/recall hides escalation: escalate-everything scores P=1.0, R=0, and is useless. Report all four, always together.

| Metric | Definition |
|---|---|
| **Auto-match precision** | Of groups the system committed as `match` (not flagged, not escalated), fraction identical to a ground-truth group. |
| **Coverage** (automation rate) | Fraction of ground-truth groups resolved automatically (`match` or `no_match`) without escalation. |
| **Escalation rate** | Fraction of cases sent to human review. |
| **Recall at precision ≥0.99** | Sweep the Python-computed score threshold; report recall at the highest threshold where auto-match precision ≥0.99. This is the headline number. |

LLM lift = recall gained at held precision over the Phase 4 deterministic baseline. Baseline precision ≈1.0, so the LLM can only lower it — lift is measured as recall, per type.

## Per-type breakdown
Every metric reported overall **and per discrepancy type** (`clean fee timing name partial consolidated fx reasoning duplicate no_match`). A per-type number needs ≥100 cases; n=20 has a ±20pp interval and is not a result. Eval slices are stratified to guarantee this.

## Retrieval recall@k (Phase 5)
Separate metric for candidate generation: is every record of the true group inside the top-k candidate pool? Computed directly from the manifest — free, exact, no LLM judge. Blocking recall bounds LLM recall: if blocking recall on partials is 0.6, no prompt exceeds 0.6.

## Slices and targets
- `just eval-fast` — 50–60 stratified dev cases. Iteration only. One flipped case ≈ 2 points, acceptable.
- `just eval` — full dev slice (seed A, ~1,000 events, ≥100 per type).
- `just eval-test` — test slice (seed B). Refuses without `CROSSFOOT_ALLOW_TEST=1`. **Run once, at the end.** Never during iteration.

Stop iterating when two consecutive prompt versions move nothing. Log each prompt version with its four scores.

## Regression gate
CI compares against a committed baseline JSON and fails on regression beyond a threshold. Output: JSON + human-readable report.

## Not used
- Ragas — NL-QA-shaped, LLM-judged, costs money. Ground truth is deterministic here.
- LLM-as-judge — explanation quality only, 50-case sample, ~$0.50. Never for match correctness (position/verbosity/self-preference biases).
- Model self-reported confidence — uncalibrated. Score is computed in Python from evidence features (amount delta, name similarity, date delta, reference cited, group size).
