# Budget

Hard ceiling: **$30.00** API spend (separate from the Claude Pro subscription).
Update the Actual column at the end of every session that makes a paid call. Once `src/crossfoot/llm/` has the response cache, `just cost` reports spend, cache hit rate, and `cache_read_input_tokens` — paste its output here.

| Phase | Budgeted | Actual | Notes |
|---|---|---|---|
| 0–5 | $0.00 | $0.00 | No LLM calls. Ollama for plumbing. |
| 6 | $12.00 | | ~$0.50 first Haiku fast-subset run, ~$8 prompt iteration (~40 × $0.20), ~$3.50 full dev slice |
| 7 | $3.00 | | Governance tests |
| 8 | $4.00 | | Haiku test slice ~$1.50, Sonnet test slice ~$2.00, explanation judge ~$0.50 |
| Reserve | $11.00 | | Late generator bug, failed prompt direction, residual > 500, second test-slice run |
| **Total** | **$30.00** | **$0.00** | |

## Rules that keep this inside budget
1. Nothing calls Claude until Phase 6.
2. Response cache ships (Session 3.2) before the first paid call.
3. Ollama for plumbing, Claude for measurement.
4. Haiku default. Sonnet once, for the comparison run.
5. LLM sees ≤10 candidates, ≤4 steps. Changing either changes every estimate below.

## Per-run cost assumptions
~3k cached prefix + 10 candidates × ~150 tokens + ≤3 tool results ≈ 6–8k input, ~600 output per case ≈ $0.01 on Haiku. Fast subset (50–60 cases) ≈ $0.20 cold, less warm.

Haiku 4.5 minimum cacheable prefix is **4096 tokens**. If `cache_read_input_tokens` is 0, the prefix is too short — pad or document.

## Spend log
| Date | Session | Run | Cost | Hit rate | cache_read_input_tokens |
|---|---|---|---|---|---|
| | | | | | |
