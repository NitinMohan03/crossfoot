# Crossfoot
## A multi-source financial reconciliation agent
### Research, Problem Framing, and Technology Selection

*Crossfooting is the accounting practice of verifying that totals agree when summed across more than one dimension. It is what this system does: check that four independent records of the same money agree, and surface the ones that don't.*

**Repo name:** `crossfoot`
**One-line description:** Reconciles transactions across fragmented financial systems using deterministic rules first and an LLM only for the residual, with evals proving it works.

**Status:** Planning complete, critiqued, revised (see §4.14). Ready to build.
**Last updated:** 14 September 2026

---

# PART 1 — Building Efficiently with Claude

Everything in this section comes from Anthropic's official Claude Code guidance and current cost-optimization practice. The single organizing principle: **Claude's context window fills fast, and performance degrades as it fills.** Almost every practice below is downstream of that one constraint.

## 1.1 The core failure modes and their fixes

There are five documented failure patterns. Recognizing them early is worth more than any prompting trick.

| Failure | What it looks like | Fix |
|---|---|---|
| **Kitchen sink session** | You start on the matching engine, ask something about Docker, go back to matching. Context is now full of irrelevant material. | `/clear` between unrelated tasks. |
| **Correcting in circles** | Claude gets it wrong, you correct, still wrong, you correct again. Each failed attempt stays in context. | After two failed corrections, `/clear` and rewrite the initial prompt incorporating what you learned. A clean session with a better prompt beats a long session with accumulated corrections. |
| **Bloated CLAUDE.md** | Real rules buried under speculative ones. The model weighs them equally and follows none. | Ruthlessly prune. For each line ask: "would removing this cause a mistake?" If not, cut it. |
| **Trust without verification** | Plausible-looking code that breaks on edge cases. A silent wrong answer costs far more than a loud crash. | Never accept work without a check Claude can run. |
| **Infinite exploration** | "Investigate how X works" with no scope. Claude reads 200 files. | Scope narrowly, or delegate to a subagent so exploration doesn't consume main context. |

## 1.2 Verification is the anti-hallucination mechanism

This is the highest-leverage practice for this project specifically.

Claude stops when work *looks* done. Without a check it can run, "looks done" is the only available signal, and you become the verification loop. Give Claude something that produces pass/fail and the loop closes itself: Claude does the work, runs the check, reads the result, iterates.

**For this project, verification is unusually easy and you should exploit that.** Reconciliation has deterministic ground truth. You know which transactions should match. That means:

- Every matching-logic change can be verified against a labeled fixture set
- The eval suite doubles as Claude's verification loop
- You can gate on numbers, not vibes

Escalating levels of enforcement:

1. **In-prompt**: "implement X, run `pytest tests/test_matching.py`, iterate until it passes"
2. **Session-level**: set the check as a `/goal` condition — a separate evaluator re-checks after every turn
3. **Deterministic gate**: a Stop hook runs the check as a script and blocks the turn from ending until it passes
4. **Second opinion**: a verification subagent with fresh context tries to refute the result, so the agent doing the work isn't the one grading it

Always ask Claude to **show evidence** rather than assert success — the test output, the command run, what it returned. Reviewing evidence is faster than re-running verification yourself.

## 1.3 Explore → Plan → Implement → Commit

Letting Claude jump straight to code produces code that solves the wrong problem.

- **Explore**: plan mode (`Shift+Tab` until `⏸ plan mode on`). Claude reads and answers without changing anything.
- **Plan**: ask for a detailed implementation plan. `Ctrl+G` opens it in your editor for direct editing.
- **Implement**: approve the plan, let Claude code against it.
- **Commit**: descriptive message, PR.

Plan mode has overhead. Skip it when you could describe the diff in one sentence. Use it when the change spans multiple files, when you're uncertain about approach, or when you're unfamiliar with the code being modified. For this project, that means: plan mode for the matching engine and the agent graph, skip it for adding a field to a Pydantic model.

## 1.4 Let Claude interview you before large features

For anything substantial, this prompt shape produces far better specs than writing them yourself:

```
I want to build [brief description]. Interview me in detail using the AskUserQuestion tool.
Ask about technical implementation, edge cases, concerns, and tradeoffs. Don't ask obvious
questions, dig into the hard parts I might not have considered.
Keep interviewing until we've covered everything, then write a complete spec to SPEC.md.
```

Then **start a fresh session to execute it**. The new session has clean context focused entirely on implementation, and you have a written spec to reference.

The most useful specs name the files and interfaces involved, state what is out of scope, and end with an end-to-end verification step. Time spent making the spec precise pays off more than time spent watching the implementation.

## 1.5 Context management tactics

- `/clear` frequently between tasks — the single most useful habit
- `/compact <instructions>` for controlled compaction, e.g. `/compact focus on the matching logic`
- `Esc` to interrupt mid-action, context preserved
- `Esc Esc` or `/rewind` to restore previous conversation and code state
- `/btw` for side questions whose answers never enter conversation history
- `/context` to confirm what's loaded
- **Subagents for investigation**: "use subagents to investigate X" — they explore in separate context windows and report back summaries, keeping main context clean

Customize compaction in CLAUDE.md, e.g. *"when compacting, always preserve the full list of modified files and the eval command"*.

## 1.6 CLAUDE.md design for this project

Keep it short. Bloated files cause Claude to ignore your actual instructions. Include only what Claude cannot infer from the code.

**Include:**
- Commands Claude can't guess (`just eval`, `docker compose up -d --wait`, seed scripts)
- The rule that no matching-logic change ships without running the eval suite
- Architectural decisions specific to this project (why deterministic-first, why LLM is escalation-only)
- Environment quirks (required env vars, which services must be running)
- Non-obvious gotchas discovered during the build

**Exclude:**
- Anything derivable from reading the code
- Standard Python conventions
- File-by-file descriptions
- Long explanations

Use `/init` to generate a starter, then prune. Run `/doctor` on a checked-in CLAUDE.md and Claude proposes cuts for content it can derive from the codebase.

Push domain knowledge that's only *sometimes* relevant into **skills** (`.claude/skills/<name>/SKILL.md`) rather than CLAUDE.md — skills load on demand without bloating every conversation. Good candidates here: EDI/remittance format notes, the eval metric definitions, the fixture-generation conventions.

## 1.7 Hooks for things that must happen every time

CLAUDE.md instructions are advisory. Hooks are deterministic.

Worth setting up for this project:
- Run `ruff` + `mypy` after every file edit
- Block writes to `tests/fixtures/golden/` (protects your ground truth from being "helpfully" edited to make tests pass — this is a real risk and the most important hook here)
- Run the fast eval subset on any change under `src/matching/`

Claude can write these: *"write a hook that blocks writes to tests/fixtures/golden/"*.

## 1.8 Adversarial review

Before treating work as done, have a subagent review the diff in fresh context. It sees only the diff and your criteria, not the reasoning that produced the change.

```
Use a subagent to review the matching engine diff against SPEC.md. Check that every
requirement is implemented, the listed edge cases have tests, and nothing outside
the task's scope changed. Report gaps, not style preferences.
```

**Caution:** a reviewer prompted to find gaps will usually report some even when the work is sound. Tell it to flag only gaps affecting correctness or stated requirements, and treat the rest as optional. Chasing every finding leads to over-engineering.

## 1.9 Token economics

Three levers, and they stack.

**Prompt caching** — cached input tokens cost roughly 10% of the standard rate, with a one-time write premium. Default TTL is 5 minutes. Structure prompts **static content first, dynamic content last** so the stable prefix is cacheable. For this project the cacheable prefix is: system prompt, tool schemas, the reconciliation rules, few-shot examples. The dynamic tail is the specific transaction batch.

**Batch API** — flat 50% discount on both input and output, asynchronous, typically completed within hours and guaranteed within 24. Perfect fit for single-shot bulk passes. Poor fit for anything interactive or multi-step — which is why this project does not use it (§3.4, §3.8): the adjudication agent calls tools. **Cache and batch discounts stack** where batch applies.

**Model routing** — route by task complexity. In this project: Haiku for classification and simple extraction, Sonnet for reconciliation reasoning, Opus only for genuinely ambiguous escalations. Most calls don't need the most expensive model.

**Fourth lever specific to this architecture:** the cheapest LLM call is the one you don't make. Deterministic matching should handle the large majority of transaction pairs. The LLM only sees what rules couldn't resolve. This is both an architecture decision and a cost decision, and it's the honest way to build this system anyway.

## 1.10 Preventing hallucination in the built system

Distinct from preventing it while building. Four mechanisms:

1. **Structured outputs with constrained decoding.** Guarantees schema conformance. Critical caveat: **structured outputs guarantee format, not accuracy.** You can get perfectly formatted wrong answers. Always validate semantic correctness separately.
2. **Ground every claim in retrieved evidence.** The agent must cite the source record for every match decision. No citation, no match.
3. **Confidence thresholds with escalation.** Below threshold, escalate to human rather than guess. Reconciliation has a natural "I don't know" state — use it.
4. **Deterministic verification of arithmetic.** Never let the LLM do the math. It proposes matches; Python verifies the amounts balance.

## 1.11 Session hygiene for a multi-week build

- Name sessions with `/rename` and treat them like branches: `matching-engine`, `eval-harness`, `mcp-servers`
- `claude --continue` / `claude --resume` to pick up a workstream
- Worktrees for genuinely parallel work
- Writer/Reviewer pattern across two sessions: one implements, a second with fresh context reviews. Fresh context improves review because Claude isn't biased toward code it just wrote.

---

# PART 2 — Problem Framing and Project Plan

## 2.1 The problem, stated plainly

A business receives money and spends money through several systems that do not agree with each other.

The bank knows a payment of $4,417.00 landed on the 14th from "ACME CORP LTD". The ERP knows invoice INV-2024-0891 was issued to Acme Corporation for $4,420.00 on the 2nd. The payment processor knows a settlement batch closed on the 15th containing 47 payments totalling $210,338.12, one of which was $4,417.00 net of a $3.00 fee. A PDF remittance advice, emailed as an attachment, says Acme is paying invoices 0891 and 0892 together, less a credit note.

These are the same commercial event. No two systems describe it identically. No shared identifier links them. Amounts differ by fees, currency conversion, and partial payments. Dates differ by settlement lag. Names differ by legal entity versus trading name versus whatever the bank's 32-character description field truncated it to.

Someone reconciles this by hand, every month, in a spreadsheet. **That is the problem.**

## 2.2 Why this problem, specifically

Three reasons, in order of importance.

**It has deterministic ground truth.** This is rare for an LLM project and it is the entire reason this is worth building. You can construct a fixture set where you know the correct answer, then produce real precision and recall numbers. Most AI portfolio projects can only produce screenshots. This one produces a confusion matrix.

**It is structurally the hardest version of every problem the target companies named.** Fragmented sources that disagree. Exact-match retrieval that dense embeddings handle badly (vendor names, invoice numbers). Structured output feeding a downstream system rather than a human reader. An agent that must know when to stop and escalate. Permission boundaries over financial data. Cost control at volume. There is no cluster in the market analysis that this problem does not touch.

**It is the domain the target companies operate in.** Ramp automates how spend flows and closes books. Intuit's stated goal is turning "AI pilot" into "AI in the general ledger." Stripe is payment infrastructure. Perpay is consumer finance. This is not an analogy to their problem — it is a scaled-down instance of it.

## 2.3 The core insight that shapes the architecture

**Reconciliation is not an LLM problem. It is a matching problem with an LLM escape hatch.**

Most transaction pairs match on deterministic rules: same amount, same date window, same reference. Those should never touch a model. They're faster, free, and auditable as plain code.

The LLM earns its place on the residual — the cases where rules are ambiguous or contradictory. That's maybe 5-15% of volume depending on data quality, and it's the part a human currently does by squinting at two screens.

This matters for three reasons:
- **Cost**: LLM calls scale with the residual, not with volume
- **Auditability**: the deterministic path is explainable by construction, which matters in finance
- **Honesty**: building it the other way round, LLM-first, would be architecturally wrong, and any interviewer who has shipped this kind of system will know it

If you take one thing from this plan into an interview, it should be this paragraph. "I used an LLM where it added value and rules where they were better" is a stronger signal than any framework name.

## 2.4 System overview

```
SOURCES (heterogeneous by design)
  Postgres — ERP invoices and credit notes
  CSV/fixed-width — legacy bank statement export
  REST API — payment processor settlements (mock FastAPI: cursor pagination, 429s, bearer auth)
  PDF — emailed remittance advices (3–4 layout templates, one held out)
  + FX rate table, holiday calendar (generated alongside)
        │
        ▼  each exposed as an MCP server (stdio, read-only tools)
NORMALIZATION LAYER                                    ◄──────────────┐
  canonical transaction schema · entity resolution · currency         │
        │                                                             │
        ▼                                                             │
DETERMINISTIC MATCHER  ──── high-confidence matches ────┐             │
  exact · amount+date window · reference match          │             │
        │ residual                                       │             │
        ▼                                                │             │
CANDIDATE BLOCKING (baseline candidate generator)        │             │
  SQL amount±tol · business-day window · pg_trgm name    │             │
  → RapidFuzz rank → top-10 to the LLM                   │             │
        │                                                │             │
        │   ┌─ RETRIEVAL EXPERIMENT (remittance text only)│             │
        │   │  BM25 + dense → RRF → rerank(≤20)           │             │
        │   │  measured against blocking, either direction│             │
        │   └─────────────────────────────────────────────│             │
        ▼                                                │             │
LLM ADJUDICATION (LangGraph, agentic-lite)               │   tool calls│
  ≤3 MCP tool calls · ≤4 steps · then forced decision ───┼─────────────┘
  structured output · evidence citation                  │
  per-case token budget · step cap · repeat-call detect  │
        │                                                │
        ├── match (score computed in Python) ────────────┤
        ├── flag discrepancy ────────────────────────────┤
        └── escalate to human (only write path) ─────────┤
                                                         ▼
                                              MatchDecision OUTPUT
                                              + AUDIT TRAIL
                                                         │
                                                         ▼
                                              EVAL HARNESS
                                              auto-match precision · coverage ·
                                              escalation rate · recall@P≥0.99,
                                              per type · regression gate in CI
```

## 2.5 Data design

Ground truth is the foundation. Get this wrong and nothing downstream means anything.

**Generate synthetically with known labels.** Real financial data is not public for obvious reasons, and the standard practice in the research literature is synthetic generation calibrated to realistic patterns. Generating your own is better than downloading one anyway, because you control which discrepancy types appear and at what rates.

Build a generator that emits paired records across all four sources, with a manifest recording the true match set.

**Vocabulary, because the numbers depend on it.** An **event** is one commercial fact (Acme pays invoice 0891). A **record** is one row in one source. A clean event produces one record per source. A partial payment is one event, one invoice record, N bank records. All rates below are **per event**. All cost estimates are per residual event reaching the LLM.

**Discrepancy types to inject, with target rates:**

| Type | Rate | Why it's hard |
|---|---|---|
| Clean match | ~57% | Baseline — should be caught by rules alone |
| Fee deduction | ~10% | Amount differs by a small variable amount |
| Timing lag | ~8% | Settlement date vs transaction date, 1-5 days |
| Name variant | ~7% | Legal name vs trading name vs truncated bank description |
| Partial payment | ~5% | One invoice, multiple payments |
| Consolidated payment | ~4% | One payment, multiple invoices |
| FX rounding | ~3% | Cross-currency, rate applied at different times |
| **Reasoning-required** | ~3% | DBA / subsidiary names ("Alphabet" pays a "Google" invoice), remittance free-text ("short-paid, damaged goods, see CN-12 next month"), consolidated payment with rounding. Not solvable by fuzzy matching or arithmetic alone. **This class exists so the LLM has something only it can do.** |
| Duplicate | ~2% | Same transaction ingested twice |
| Orphan | ~1% | Present in one source, genuinely absent from others |

Rates are a starting point. The important property is that each type is **labeled**, so you can report accuracy per type rather than one aggregate number. Per-type breakdown is what makes the results credible — but only if each type has enough cases (see eval slices below).

**Scale target: 10,000 events.** The deterministic phases cost nothing regardless of size, and only the residual reaching the LLM costs money — so size is bounded by the eval slice, not the dataset. 10k keeps rare types above statistical noise and stays well inside pgvector's comfortable range.

**Eval slices.** Two slices, generated from different seeds:
- **Dev slice (seed A)** — used for all prompt iteration and attribution work.
- **Test slice (seed B)** — run **once**, at the end, for the headline numbers. Never used during iteration.

Each slice is **stratified** so every discrepancy type has ≥100 events, regardless of its natural rate. The natural rates are for realism in the full dataset; the stratified slices are for statistics. A per-type precision on n=20 has a ±20pp confidence interval and is not a result.

**Manifest scoring unit: the match group.** A group is `{source record ids} → {target record ids}`. A predicted group is a true positive only if it is identical to a ground-truth group; partial overlap counts as one false positive and one false negative. Orphans carry an explicit `no_match` label and duplicates carry `duplicate_of`, so the confusion matrix has a no-match row and correct non-matches are scored. Define this in the Phase 1 spec — the manifest format and the eval harness both depend on it.

**Output schema, defined here so it is directly comparable to the manifest:**

```
MatchDecision
  source_ids: list[str]
  target_ids: list[str]
  match_type: clean | fee | timing | name | partial | consolidated | fx | reasoning | duplicate | no_match
  action: match | flag | escalate
  evidence: list[{record_id, field, value}]     # cited, never free text
  score: Decimal                                 # computed in Python from evidence features, not by the model
```

**Extra generated inputs the sources depend on:** a daily FX rate table for three currencies; a holiday calendar for one jurisdiction (pick UK or US, state it in the README); 3–4 remittance PDF layout templates, **one of which is held out from parser development** so PDF ingestion is tested on a layout it has not seen.

**Circularity disclosure.** The generator and the matcher share an author. The matcher will tend to encode the generator's rules, so deterministic coverage on synthetic data is an **upper bound** on real-world coverage. Say this in the README. The reasoning-required class and the held-out PDF template are the two places where the data is deliberately harder than the rules know about.

## 2.6 Build phases

### Phase 0 — Foundation
Repo, Docker Compose, Postgres with pgvector, CLAUDE.md, pre-commit hooks, CI skeleton.

**Done when:** `docker compose up` gives a working environment and CI runs on push.

### Phase 1 — Data generation and ground truth
Synthetic generator, all four source formats, discrepancy injection, ground-truth manifest.

**Done when:** you can regenerate the full dataset deterministically from a seed, and the manifest is machine-readable.

**This phase is the foundation of everything.** Resist rushing it.

### Phase 2 — Ingestion and normalization
Mock payment-processor REST API (FastAPI: cursor pagination, 429 rate limits, bearer auth) — it must exist before an MCP server can wrap it. FX rate and holiday tables. Four MCP servers over stdio, each exposing read-only tools. Canonical schema. Entity resolution. Currency normalization.

**Done when:** all four sources land in the canonical schema, a single query surfaces records from all of them, and each server's agent-facing tools answer correctly from a test harness.

### Phase 3 — Eval harness and response cache
The four metrics (auto-match precision, coverage, escalation rate, recall at precision ≥0.99), overall and per discrepancy type, group-level scoring per §2.5. Confusion matrix with a no-match row. CI regression gate. The disk-backed LLM response cache.

**Built before the matcher and before the LLM, deliberately.** The harness is what scores the deterministic baseline in Phase 4 — building it after would mean building it twice.

**Done when:** `just eval` scores a hand-written set of predicted groups against the manifest and produces the expected numbers, and CI fails on regression beyond a threshold.

### Phase 4 — Deterministic matcher
Rule-based matching before any LLM involvement. Exact, amount+date window, reference match. Bounded many-to-many.

**Done when:** `just eval` against the matcher produces the baseline numbers with zero LLM calls. **This number is your control.** Every subsequent claim about LLM value is measured against it. Expect precision near 1.0 — the LLM can only lower it, so the LLM's contribution is measured as recall gained at held precision.

### Phase 5 — Candidate blocking and the retrieval experiment
**Blocking baseline first**: SQL amount-within-tolerance, business-day date window, `pg_trgm` similarity on names and references, RapidFuzz ranking. This is the candidate generator that ships to Phase 6. It is record-linkage blocking, not retrieval — numeric proximity is not a text-relevance problem.

**Then the experiment**: BM25 + dense + RRF + rerank over **remittance narrative text**, the only genuine free text in the system. Measured against blocking on the remittance-bearing residual.

**Done when:** blocking recall@k per type is reported, and the hybrid-vs-blocking comparison is reported **in whichever direction it comes out**. A null result is a result. Do not pre-commit to a finding.

### Phase 6 — LLM adjudication (agentic-lite)
LangGraph agent over the top-10 blocked candidates. The agent may call ≤3 MCP tools (`get_remittance_for_payment`, `list_open_invoices`, `lookup_alias`) across ≤4 steps, then must emit a `MatchDecision`. Structured output. Evidence citation. Score computed in Python from evidence features. Escalation path.

**Done when:** on the dev slice, recall at auto-match precision ≥0.99 exceeds the Phase 4 baseline, with coverage and escalation rate reported per type and the delta attributable to specific discrepancy types.

### Phase 7 — Governance
Per-case token budget with hard halt. Step cap. Repeated-identical-tool-call detection. Tool failure classification: retriable vs terminal. Full replayable traces. Audit log. These are real controls over a real loop, not ornaments — the agent can call tools, so it can loop.

**Done when:** you can replay any match decision and show every input that produced it, and a deliberately induced runaway (a tool stubbed to always return "need more information") is caught and halted.

### Phase 8 — Interface and packaging
Minimal review UI for escalated items. README with architecture diagram. Results write-up.

**Done when:** someone else can clone, run, and understand it without you present.

## 2.7 Problems you will actually hit

These are the ones worth pre-committing a position on.

**Entity resolution is harder than the LLM part.** "ACME CORP LTD" / "Acme Corporation" / "ACME CORP LT" (truncated) / "Acme Corp (UK)". Fuzzy string matching gets you most of the way. The remainder needs either an entity registry or LLM judgment. *Position: build a registry that learns aliases **only from human-confirmed escalations**, and is frozen during eval runs. If it learned from the manifest that would be leakage; if it learned from LLM decisions, errors would compound and results would depend on processing order.*

**LLM self-reported confidence is uncalibrated.** A model asked for a number between 0 and 1 will give you one; it will not be a probability. Thresholding on it and tuning the threshold on a small dev set is noise. *Position: the model emits a decision and cited evidence. The score is computed in Python from evidence features — amount delta, name similarity, date delta, whether a reference was cited, group size. This also keeps arithmetic out of the model, which is already a rule.*

**Candidate generation over structured records is blocking, not retrieval.** Amount, date, and reference are the discriminating fields, and none of them are text-relevance problems. A cross-encoder trained on passage relevance will score "4,417.00 vs 4,420.00" arbitrarily. *Position: SQL + trigram + RapidFuzz blocking is the candidate generator. Hybrid retrieval is an experiment on the one field that is actually free text — remittance narrative — and its result is reported either way.*

**Many-to-many matching explodes combinatorially.** One-to-one is easy. One payment against three invoices, or two payments against one invoice split across periods, is a subset-sum problem. *Position: bound it. Cap candidate group size at 4 and escalate beyond that. Document the bound as a deliberate tradeoff — bounded scope with honest escalation is better engineering than an unbounded search that occasionally hangs.*

**Precision and recall trade against each other, and the right point is not 50/50.** In reconciliation a false match is much worse than a missed match — a false match silently corrupts the ledger, a missed match lands in a review queue. *Position: tune for precision, report both, and say explicitly in the README that you optimized for precision because of the asymmetric cost. This is exactly the kind of reasoning Ramp's JD is screening for.*

**LLM-as-judge has documented biases.** Position bias, verbosity bias, self-preference. It cannot be the only evaluator. *Position: use deterministic ground truth as primary. LLM-as-judge only for explanation quality, where there is no ground truth, and note its limitations in the write-up.*

**Cost can run away during development.** Every eval run over the full set costs money. *Position: a stratified fast subset of 50–60 dev-slice cases for iteration — large enough that one flipped case is not a 7-point swing, small enough to run for cents. Full dev slice only when the prompt is stable. Test slice once. Response cache on disk so anything downstream of the LLM call costs zero to re-run. Track cost per eval run as a first-class metric.*

**Chunking PDFs destroys the context remittance lines need.** A line item means nothing without the header naming the payer. *Position: parse remittance PDFs structurally rather than chunking them semantically. This is a case where the naive RAG approach is simply wrong, and knowing that is worth saying out loud.*

**Timezones and settlement calendars.** Transaction date, value date, settlement date, posting date. Weekends and bank holidays. *Position: **timestamps** are normalized to UTC at ingest with the original kept alongside. **Calendar dates** (value date, posting date) stay calendar dates — they have no timezone, and converting one to UTC can shift it by a day. Business-day windows use one jurisdiction's holiday calendar, generated in Phase 1 and named in the README.*

**Floating point on money.** Never floats. `Decimal` or integer minor units, everywhere, no exceptions. *Position: enforce with a hook or a lint rule so it cannot silently regress.*

**Scope creep.** The tempting additions — a fifth source, real-time streaming, a fine-tuned matcher — will each cost a week and add little to the story. *Position: four sources, batch processing, no fine-tuning. Write the exclusions into the README as explicit non-goals.*

## 2.8 What "done" means

The project is finished when you can state, with evidence, **on the held-out test slice**:

- The four metrics — auto-match precision, coverage (automation rate), escalation rate, recall at precision ≥0.99 — overall and per discrepancy type. Plain precision/recall hides escalation: an agent that escalates everything scores precision 1.0, recall 0, and is useless.
- Per-discrepancy-type breakdown showing where it succeeds and fails, each type with ≥100 cases
- The result of the retrieval experiment (blocking vs blocking+hybrid on remittance text), in whichever direction it came out
- The lift from LLM adjudication over the deterministic baseline, expressed as recall gained at held precision
- Cost per thousand events reconciled, and what fraction of that is the residual
- A regression your eval suite caught that manual testing would have missed
- A failure mode you discovered mid-build and engineered against

That last one matters more than the rest. The F1 project is credible because the consecutive-lap suppression gate exists as a response to a real discovered problem. Replicating that pattern here is the goal.

---

# PART 3 — Technology Selection

Selection criteria: currently used in production by companies in the target set, named explicitly in the JDs, or the documented industry default. Nothing chosen for novelty.

## 3.1 Language and core

| Choice | Rationale |
|---|---|
| **Python 3.12+** | Named in every relevant JD. Non-negotiable. |
| **uv** | Current standard for Python dependency management. Fast, lockfile-based. |
| **Pydantic v2** | Schema definition, validation, and the bridge to structured outputs. Ubiquitous in production AI code. |
| **Ruff + mypy** | Lint and type checking. Fast enough to run in a hook after every edit. |
| **pytest** | Test runner. The eval harness plugs into it. |

## 3.2 Agent orchestration

**LangGraph.**

Named explicitly in the Intuit JD alongside LangChain, MCP servers, and multi-agent. It leads production adoption among agent frameworks, with the largest verified enterprise deployment list, and it is described as the default for stateful production workflows in regulated industries. It models workflows as directed graphs — nodes are steps, edges are transitions — which buys predictability, state persistence, conditional routing, and rollback.

For this project the graph shape maps naturally onto the problem: blocked candidates in → adjudicate → optionally call a tool for more evidence → adjudicate again → score → escalate-or-commit, with a hard termination condition.

**The agentic-lite bound.** The agent may make at most 3 tool calls across at most 4 steps, then must emit a decision. This bound is what makes the framework choice honest: a single-shot structured-output call would not need a graph, and "loop detection" over a loop that cannot exist is theater. With bounded tool use the cycles, the budget node, and Phase 7's governance are all controlling something real.

**Deliberately not chosen:** CrewAI (role-based abstraction is the wrong shape for a deterministic pipeline with an LLM step, and teams commonly migrate off it to LangGraph as workflows outgrow it), raw API loops (you'd rebuild state management and lose the framework signal the JDs are screening for).

## 3.3 Integration layer

**MCP servers, one per source.**

MCP has moved decisively from experiment to infrastructure. Anthropic donated it to the Agentic AI Foundation under the Linux Foundation in December 2025, making it vendor-neutral. SDK downloads reached roughly 97 million monthly by March 2026, with the public server registry near 10,000 entries. Survey data puts around 41% of software organizations in limited or broad production with MCP servers. Intuit names it as a required skill.

The July 2026 spec revision moved the protocol to a stateless core that scales on ordinary HTTP infrastructure. Everything in this project runs locally, so all four servers use **stdio**. Streamable HTTP is the remote transport; demonstrate it on one server if there is time, not four.

**Why it matters architecturally:** standardizing tool connections on MCP means the orchestration framework becomes swappable. That is a real hedge against lock-in, and it's the reason the MCP-native versus adapter distinction is becoming the framework question that matters.

**The agent actually calls these servers.** MCP is a tool protocol for models; using it purely as an ETL adapter would invite the question "why not a plain Python module?" Each server exposes read-only tools the adjudication agent can call for more evidence:

| Server | Source | Agent-facing tools |
|---|---|---|
| `mcp-erp` | Postgres | `list_open_invoices(vendor, amount_range, date_range)`, `get_invoice(id)`, `get_credit_notes(vendor)` |
| `mcp-bank` | fixed-width/CSV | `get_bank_line(id)`, `find_bank_lines(amount_range, date_range)` |
| `mcp-processor` | mock REST (FastAPI: cursor pagination, 429 rate limits, bearer auth) | `get_settlement(id)`, `list_settlements(date_range)` |
| `mcp-remittance` | PDF | `get_remittance_for_payment(payment_ref)`, `get_remittance_lines(remittance_id)` |
| shared | alias registry | `lookup_alias(name)` |

Ingestion uses the same servers. Each has genuinely different auth, pagination, and failure characteristics. **The heterogeneity is the point** — four REST APIs would prove nothing.

**Permission boundary.** Every tool is read-only. The agent's only write path is the escalation queue. This is the "permission boundaries over financial data" that §2.2 and §3.9 claim — it is built, small, and demonstrable: a test asserts the agent cannot reach any write.

## 3.4 Model and API

**Claude, via the Claude API**, with structured outputs.

Structured outputs are generally available on the Claude API and constrain responses through grammar-constrained sampling. Two modes matter here:
- **JSON outputs** (`output_config.format`) for extraction tasks
- **Strict tool use** (`strict: true`) for guaranteed schema conformance on tool inputs in the agent loop

The documented caveat is important and should appear in your write-up: **structured outputs guarantee format compliance, not accuracy.** You can receive perfectly formatted incorrect answers. Semantic validation is a separate layer you must build.

**Routing:** Haiku 4.5 as the default for everything, Sonnet 5 for one comparison run, Opus not used. Note the two models have **different API surfaces**: Haiku 4.5 takes `thinking: {type: "enabled", budget_tokens: N}`; Sonnet 5 takes `thinking: {type: "adaptive"}` plus `output_config.effort`. The model swap is a config branch in `src/llm/`, not a string change.

**Prompt caching** on the static prefix (system prompt, tool schemas, rules, examples) with dynamic content last. Two facts that matter here:
- **Haiku 4.5's minimum cacheable prefix is 4096 tokens** (Sonnet 5: 1024). Below that, nothing caches — `cache_read_input_tokens` is 0 and there is no error. A 2,500-token system prompt on Haiku silently never caches. Log `cache_read_input_tokens` in `just cost` and assert it is non-zero, or document why not.
- At this project's volume caching saves well under a dollar. It is in the build to be demonstrated and measured for the write-up, not because the budget depends on it. Say so.

**Batch API** is not used. The adjudication agent is multi-step (it calls tools), and Batch is one request in, one response out, hours later. Batch is the right tool for bulk single-shot passes — historical classification, say — and would be the first thing to add if this were rebuilt at scale. Rejected in §3.8 with that reason.

## 3.5 Storage and retrieval

**PostgreSQL with pgvector.**

For workloads under roughly 10-20 million vectors, pgvector on existing Postgres is the current default. Documents and embeddings live in the same table, in the same transaction. SQL filtering, joins, and transactional consistency with no sync pipeline and no extra service.

At this project's scale — thousands of transactions — a dedicated vector database would be architecture theater. Postgres also gives you the transactional store you need anyway for financial records, plus `pg_trgm` for trigram similarity on names and identifiers.

**Candidate generation is blocking, not retrieval.**

The candidate generator that ships to the LLM is record-linkage blocking over structured fields:

```sql
WHERE amount BETWEEN :amt * 0.90 AND :amt * 1.02        -- fees deduct, they don't add
  AND value_date BETWEEN business_days(:date, -1) AND business_days(:date, +5)
  AND (similarity(vendor_norm, :vendor) > 0.3 OR reference_norm % :reference)
ORDER BY <RapidFuzz token_set_ratio on vendor> DESC, abs(amount - :amt)
LIMIT 10
```

Amount, date, and reference are the discriminating fields. None of them is a text-relevance problem. A cross-encoder trained on passage relevance will score "4,417.00 vs 4,420.00" arbitrarily, and a dense embedding of "ACME CORP LTD 4417.00" carries almost no signal about which of forty Acme invoices is meant. The deterministic matcher already consumed every case where an exact identifier was present, so the residual — by construction — is the population where "BM25 finds `INV-2024-0891`" cannot happen.

**The retrieval experiment: BM25 + dense → RRF → rerank, over remittance narrative text only.**

Remittance advices are the one source with real free text: "paying 0891 and 0892 less credit for damaged goods, balance next month." That is where lexical and semantic retrieval can plausibly earn a place. The experiment measures blocking alone against blocking + hybrid retrieval on the remittance-bearing residual, per type, and reports the result in whichever direction it comes out.

Implementation details that still matter:

- **Fuse on ranks, not scores.** BM25 scores are unbounded positive; cosine similarity is bounded. RRF operates on ranks and sidesteps the scale problem. Test RRF `k` at 10 and 60; the comparison is itself a reportable result.
- **`tsvector` is not BM25.** Postgres `ts_rank` is a different scoring function, and the default tokenizer mangles `INV-2024-0891`. Use `pg_trgm` for identifiers and names; use `rank-bm25` in Python if true BM25 over remittance text is wanted.
- **Rerank depth is capped at 20**, not 50–200. The "50-candidate threshold" is a passage-RAG finding about long-document corpora; here the candidate pool is already tight from blocking, and rerank wall-clock is the constraint (below).

**Reranker:** `BAAI/bge-reranker-v2-m3` is the common open-source cross-encoder in current pipelines. The build machine has an RTX 4060 (8 GB), which runs it comfortably. On CPU it is ~0.2–1 s per pair and an eval over 500 residual × 20 candidates takes minutes to hours — if the GPU is unavailable, fall back to `cross-encoder/ms-marco-MiniLM-L-6-v2`. Cohere Rerank is the hosted alternative; not used here because it costs money in a $0 phase.

## 3.6 Evaluation

**pytest for the deterministic suite + Langfuse for tracing.**

Current practice converges on **two tools**: a lightweight framework for CI gating plus a platform for tracking and review. A simple eval that runs on every PR beats a comprehensive suite that runs quarterly.

- **pytest** — your deterministic ground-truth suite. This is your primary signal, and it's the part most portfolio projects lack entirely. The four metrics (auto-match precision, coverage, escalation rate, recall at precision ≥0.99), group-level scoring, per-type confusion matrix with a no-match row. Retrieval quality is recall@k of the true group's records in the candidate pool — computed directly from the manifest, free, exact.
- **Langfuse** — open-source, self-hostable tracing. Every agent decision replayable.

**Ragas is deliberately not used.** Its metrics (faithfulness, answer relevance, context precision/recall) are shaped for natural-language question answering, and most of them are LLM-judged — which costs money during phases budgeted at $0. This project has labelled ground truth; deterministic recall@k from the manifest is strictly better than an LLM's opinion of context relevance.

**DeepEval** is a reasonable addition for agent-trajectory assertions. **Braintrust** and **LangSmith** are the hosted platform tier; LangSmith is the natural pairing if you stay deep in the LangChain ecosystem, and you already have LangSmith experience worth leveraging.

**On LLM-as-judge:** it has measurable, published biases that need designing around rather than trusting. Your deterministic ground truth is the primary signal. Reserve LLM-as-judge for explanation quality only — it has a $1 line in the ledger — and say so.

## 3.7 Supporting stack

| Component | Choice | Why |
|---|---|---|
| API layer | **FastAPI** | Already on your resume. Pydantic-native. Also serves the mock payment-processor API. |
| Task runner | **just** | Windows-native via winget; `make` is not. Every command in this doc is `just <target>`. |
| PDF extraction | **pdfplumber** | Structural table extraction, which is what remittance advices need. |
| PDF generation | **reportlab** | Multiple layout templates, one held out from parser development. |
| Fuzzy matching | **RapidFuzz** + **pg_trgm** | RapidFuzz in Python for ranking; trigram index in Postgres for blocking. |
| Money | **Decimal** or integer minor units | Never floats. Enforce it. |
| Local LLM | **Ollama** (`qwen2.5:7b`) | Free plumbing tests in Phase 6. Fits the 8 GB GPU. Cannot test `strict: true`. |
| Containers | **Docker Compose** (Docker Desktop) | Matches your F1 project. Single-command environment. |
| CI | **GitHub Actions** | Already on your resume. Eval gate lives here. |
| Review UI | **Streamlit** | Matches your F1 project. Not the point of the project — do not spend time here. |
| Observability | **Prometheus** + **Langfuse** | Prometheus already on your resume. Langfuse for LLM-specific traces. |

## 3.8 Explicitly rejected

| Rejected | Why |
|---|---|
| Pinecone / Qdrant / Weaviate | Correct at scale, unjustified here. Postgres is the transactional store already. Adding a vector service would be architecture for its own sake. |
| CrewAI | Role-based multi-agent is the wrong abstraction for a deterministic pipeline with an LLM escalation step. |
| Fine-tuning | High effort, low signal for this problem. Only one JD in the set asks for it. |
| Kafka / streaming | Reconciliation is inherently batch. Streaming here would be resume-driven development, and your F1 project already covers streaming. |
| Kubernetes | Compose is sufficient. K8s adds ops surface and proves nothing new. |
| A heavy frontend | The output is structured data and an audit trail. A polished UI would consume a week and add nothing an interviewer values. |
| Batch API | The adjudication agent is multi-step; Batch is single request/response with hours of latency. Incoherent with a tool-calling graph. The 50% discount would save ~$1–2 at this volume. Right tool for bulk single-shot passes at scale — say so in the README. |
| Ragas | Metrics shaped for NL question answering, mostly LLM-judged. This project has labelled ground truth; deterministic recall@k is better and free. |
| Streamable HTTP for all four servers | Everything runs locally. Four HTTP servers means four ports, health checks, and compose wiring for no gain. stdio; demonstrate HTTP on one if time allows. |
| "Governance off" comparison run | Budgets and step caps should not change outputs on non-runaway cases. If they do, that is a bug, not a result. |

## 3.9 Stack-to-JD mapping

| Technology | Named or implied in |
|---|---|
| LangGraph | Intuit (explicit), Cohere, Gradial |
| MCP servers | Intuit (explicit), Cohere, Gradial |
| RAG + vector stores + hybrid retrieval | Intuit (all three, explicit), Cohere, Ramp, Commure, Day One |
| Evals | Ramp (explicit deliverable), Cohere (explicit), OpenAI (eval-driven feedback), Commure |
| Structured extraction | Ramp Applied AI (explicit — they open-sourced jsonformer) |
| Python | Every JD in the set |
| PostgreSQL | Cohere (stack), Perpay (stack), Commure |
| REST + GraphQL + webhooks + SQL | Intuit (explicit), Ramp, Gradial, Perpay |
| Docker | Perpay, Sift, ByteDance |
| Prompt engineering | Intuit (explicit), Salesforce, Stripe |
| Permission boundaries (read-only MCP tools, escalation-only write path, tested) | Cohere, Ramp (security model deliverable), Commure |
| Financial operations domain | Ramp, Intuit, Stripe, Perpay — nice-to-have at three of them |

---

# PART 4 — Build Path

**Constraints this path is built around:**
- Claude Pro subscription. Claude Code quota is shared with Claude chat, metered on a rolling 5-hour window with a weekly cap on top.
- **$30 hard ceiling** on the project's own API spend (separate from the subscription).
- Multiple sessions over several weeks rather than a continuous sprint.
- Single-agent architecture as specified in Part 2. No multi-agent verifier.
- **Windows 11.** Git Bash is present (hooks work), `make` is not — use `just`. Docker Desktop must be running. GPU: RTX 4060 8 GB, enough for local embeddings, reranker, and a 7B Ollama model.

**Dataset: 10,000 events** (§2.5). LLM cost is bounded by the eval slices, not the dataset: dev slice and test slice, each stratified to ≥100 events per type, ~1,000 events each. The residual reaching the LLM is measured in Phase 4, not assumed — the discrepancy table implies ~22% (everything the rules are not expected to catch), so budget on **~500 residual per slice** and treat a lower number as good news.

---

## 4.0 The five rules that keep this inside budget

Everything below assumes these. Violate any one and the budget breaks.

**Rule 1 — Nothing calls Claude until Phase 6.** Phases 0 through 5 are data generation, ingestion, eval infrastructure, deterministic matching, and blocking. None of it needs an LLM. Roughly two thirds of the build costs nothing.

**Rule 2 — The response cache ships before the first paid call.** Hash inputs (prompt template version + rendered input + model + temperature + **the tool-call transcript so far**, since agentic runs make several calls per case), store the response on disk, skip the call on a hit. Most eval re-runs change something downstream of the LLM call — scoring logic, thresholds, output formatting — and those must cost zero. This is built in Phase 3, not retrofitted.

**Rule 3 — Free models for plumbing, Claude for measurement.** While you are asking "does the graph route correctly, does escalation fire, does the step cap halt," any model works. Use Ollama locally (there is no Anthropic free tier). Switch to Claude only when the model's specific behavior is what you are measuring. Caveat: Ollama cannot exercise `strict: true`, so the structured-output layer is only truly tested on the first Claude run — expect that run to surface schema issues.

**Rule 4 — Haiku is the default. Sonnet appears once.** Haiku 4.5 is $1/$5 per MTok against Sonnet 5 at $2/$10. Run everything on Haiku, then one final Sonnet run for the comparison. The delta between them is a reportable result. The two models take different thinking parameters (§3.4) — branch in config, do not assume a string swap.

**Rule 5 — The LLM sees at most 10 candidates, and takes at most 4 steps.** Every per-run cost estimate below assumes ~3k prefix + 10 candidates × ~150 tokens + ≤3 tool results ≈ 6–8k input tokens and ~600 output tokens per case, ≈ $0.01 on Haiku. Raise the candidate depth and the budget moves with it.

**Running spend ledger** — keep this in the repo and update it as you go:

| Phase | Budgeted | Actual |
|---|---|---|
| 0–5 | $0.00 | |
| 6 | $12.00 | |
| 7 | $3.00 | |
| 8 (final runs + explanation judge) | $4.00 | |
| Reserve | $11.00 | |
| **Total** | **$30.00** | |

---

## 4.1 Session sizing on Pro

Pro allows roughly 45 prompts per five-hour window, and **every prompt, tool call, file read, and thinking block draws from the same pool** — shared with Claude chat. Anthropic also reduced five-hour limits during weekday peak hours (5–11 AM PT) from March 2026.

Practical consequences:

- **Target 20–25 exchanges per session.** Leaves headroom for retries and keeps you inside one window.
- **Every session ends at a green check.** Never stop mid-implementation. Resuming costs you re-establishing context, which costs quota.
- **Schedule outside 5–11 AM PT** where your day allows it.
- **Name every session** with `/rename` — `phase2-mcp-bank`, `phase5-hybrid-retrieval`. Resume with `claude --resume`.
- **`/clear` between phases, always.** The kitchen-sink session is the most expensive failure pattern because you pay for irrelevant context on every subsequent turn.

**Two habits with the largest quota impact:**

*Subagents for anything exploratory.* When Claude reads fifteen files to understand your MCP server structure, that is fifteen file-reads in your main context and against your quota. A subagent does it in a separate window and reports a summary.

*Scope every investigation.* "Investigate how X works" with no boundary is the infinite-exploration failure. Name the files or delegate it.

**Session count estimate:** ~28 sessions across 9 phases (the mock REST API, FX/holiday tables, blocking baseline, and held-out test run each add one). At 2–3 sessions per week, 10–12 weeks.

---

## 4.2 Session 0 — Pre-flight

Do this before writing any project code. It is the highest-leverage hour of the build.

### 0a. Repo skeleton and CLAUDE.md

Start in plan mode. Ask for the structure first, approve, then let it scaffold.

```
Scaffold a Python project on Windows: uv for dependencies (pin Python 3.12 —
3.14 is installed system-wide and torch/sentence-transformers wheels lag),
src/ layout, pytest, ruff, mypy, a justfile as the task runner, Docker
Compose with Postgres + pgvector + pg_trgm. Don't write application logic
yet, just the skeleton and config.
```

Prerequisites installed before this session: `uv`, `just`, `ollama` (all via winget), Docker Desktop running.

Then `/init` to generate CLAUDE.md, and **immediately prune it**. Bloated CLAUDE.md files cause Claude to ignore your actual instructions, and if a rule keeps getting missed the file is probably too long and the rule is getting lost.

Target roughly 25 lines. Starting content:

```markdown
# Commands
- Setup: `docker compose up -d --wait && uv sync`
- Dev eval: `just eval` (dev slice, Haiku, cached)
- Fast eval: `just eval-fast` (50 stratified dev cases, for iteration)
- Test eval: `just eval-test` (test slice — run ONCE at the end, ask first)
- Regenerate data: `just seed SEED=42`

# Architecture
- Deterministic rules run FIRST. LLM only sees what rules can't resolve.
- Money is Decimal or integer minor units. Never float. No exceptions.
- Timestamps → UTC at ingest, original kept. Calendar dates stay dates — never convert a date to UTC.
- LLM emits decision + cited evidence. Score is computed in Python. Model never does arithmetic.

# Workflow
- IMPORTANT: any change under src/matching/ or src/blocking/ requires `just eval-fast` before done.
- Never edit tests/fixtures/golden/. It is ground truth.
- Show test output as evidence. Don't assert that something passes.

# Budget
- Default model is Haiku. Sonnet requires explicit instruction.
- Every LLM call goes through src/llm/cache.py. Never call the API directly.
- LLM sees ≤10 candidates, ≤4 steps. Changing either changes the budget — ask.

# Git
- IMPORTANT: never add Co-Authored-By, "Generated with Claude Code", or any
  attribution trailer to commit messages or PR descriptions.
```

Run `/context` to confirm it loaded. Later, run `/doctor` and Claude will propose cuts for anything derivable from the codebase.

### 0b. Hooks

Hooks are deterministic where CLAUDE.md instructions are advisory. Ask Claude to write them:

```
Write three hooks in .claude/settings.json:
1. Run `ruff check --fix` and `mypy` after every Python file edit
2. Block all writes to tests/fixtures/golden/
3. Block any write containing `float(` in src/matching/, src/blocking/, or src/models/
4. Block any write to src/ that calls anthropic directly outside src/llm/
```

**Hook 2 is the most important line in this entire build path.** Without it, a session that is struggling to make a test pass will eventually consider adjusting the fixture. That silently destroys your ground truth and every number downstream becomes meaningless. Make it impossible rather than discouraged.

### 0b-bis. Keep Claude off your commit history

This is a portfolio project. Every commit should read as yours. Three layers, because the exact trailer text and which setting governs it changes between Claude Code versions:

1. `.claude/settings.json`: `{"attribution": {"commit": "", "pr": ""}}`
2. The CLAUDE.md rule in the template above (the model can write a trailer by hand, bypassing the setting).
3. A `commit-msg` git hook as the deterministic backstop — runs under Git Bash on Windows:

```bash
#!/usr/bin/env bash
# .git/hooks/commit-msg
sed -i.bak '/^Co-Authored-By: Claude/d; /Generated with \[Claude Code\]/d; /^Claude-Session:/d' "$1"
rm -f "$1.bak"
```

**Verification:** throwaway commit, `git log -1 --format=%B`, confirm nothing but your message. Repeat around session 5 in case an update changed behavior.

### 0c. Skills

Skills load on demand rather than bloating every conversation. Create three, each a short `SKILL.md` under `.claude/skills/`:

- **`recon-domain`** — settlement lag, fee deduction patterns, remittance advice structure, partial vs consolidated payment semantics
- **`eval-metrics`** — how precision, recall, and per-type breakdown are computed in this repo
- **`fixture-conventions`** — the ground-truth manifest format and how discrepancy types are labeled

### 0d. Spend tracking

Create `BUDGET.md` with the ledger table from 4.0. Add a `just cost` target once the cache exists — it must report spend, cache hit rate, **and `cache_read_input_tokens`** so a silently-uncached Haiku prefix (§3.4) is visible the same day. Track it from the first paid call, not after you have overspent.

**Verification for Session 0:** `just`, `uv`, `ollama` on PATH; Docker Desktop running; `docker compose up -d --wait` succeeds; `uv run pytest` runs (zero tests is fine); a deliberate write to `tests/fixtures/golden/` is blocked by the hook; a throwaway commit contains no attribution trailer.

---

## 4.3 Phase 1 — Data generation and ground truth

**3 sessions. $0. The most important phase in the build.**

Everything downstream is measured against what you produce here. A subtle bug in the generator means every number you report is wrong, and you will not find out until late.

### Session 1.1 — Interview and spec

This is the highest-value use of the interview pattern in the whole project.

```
I'm building a synthetic transaction generator for a reconciliation system.
It must emit paired records across four sources (Postgres ERP, CSV bank export,
REST processor settlements, PDF remittance advices) with a ground-truth manifest
recording which records correspond to the same commercial event.

Interview me in detail using the AskUserQuestion tool. Ask about the discrepancy
types including the reasoning-required class, the group-level scoring unit for
many-to-many matches, the MatchDecision output schema, determinism and seeding,
the dev/test split by seed and stratification to >=100 per type, the FX rate and
holiday tables, multiple PDF layout templates with one held out, and edge cases
I probably haven't considered. Don't ask obvious questions, dig into the hard parts.

Keep interviewing until we've covered everything, then write the spec to
specs/01-data-generation.md.
```

Then **stop. `/clear`. Start a fresh session to implement.** The new session has clean context focused entirely on implementation, and you have a written spec to reference.

### Session 1.2 — Canonical schema and generator core

Plan mode first. Implement clean matches plus the two simplest discrepancy types only.

Verification: `just seed SEED=42` twice produces byte-identical output. Manifest round-trips through a validator. `MatchDecision` and a manifest group round-trip through the same comparison function.

### Session 1.3 — Remaining discrepancy types, the four output formats, eval slices

Per Part 2's table at 10,000 events:

| Type | Rate | Count |
|---|---|---|
| Clean match | 57% | 5,700 |
| Fee deduction | 10% | 1,000 |
| Timing lag | 8% | 800 |
| Name variant | 7% | 700 |
| Partial payment | 5% | 500 |
| Consolidated payment | 4% | 400 |
| FX rounding | 3% | 300 |
| Reasoning-required | 3% | 300 |
| Duplicate | 2% | 200 |
| Orphan | 1% | 100 |

Plus: FX rate table, holiday calendar, 3–4 remittance PDF templates (one held out), and the **eval slice builder** — `just slice SEED=A` and `just slice SEED=B` each emit ~1,000 events stratified to ≥100 per type, with the test slice's seed recorded and never used before Phase 8.

Verification: a script that reads the manifest, counts instances per type, and asserts each is within tolerance of target; the same script over each slice asserts ≥100 per type. Ask Claude to write this and show the output.

**Context note:** PDF generation is fiddly and will eat context on trial and error. Delegate it: *"use a subagent to work out how to generate realistic multi-line remittance PDFs with reportlab in three visually distinct layouts, report back the approach."*

---

## 4.4 Phase 2 — Ingestion and MCP servers

**5 sessions. $0.**

Four servers with genuinely different auth, pagination, and failure characteristics. The heterogeneity is the point — four REST APIs would prove nothing.

Transport is **stdio** for all four (§3.3). Each server exposes the read-only agent-facing tools listed in §3.3 as well as the bulk-read used by ingestion.

### Session 2.0 — Mock payment processor and reference tables

`mcp-processor` wraps a REST API that does not exist yet. Build it: a small FastAPI app with cursor pagination, bearer-token auth, and a rate limiter that returns 429 with `Retry-After`. Seed it from the Phase 1 settlement data. Also load the FX rate and holiday tables into Postgres.

Verification: paginating the full settlement set returns exactly the generated records; hammering it produces a 429 that the client backs off from.

### Session 2.1 — Canonical schema, entity resolution, first server

Build `mcp-erp` (Postgres) as the reference implementation. Establish the pattern the other three follow.

Entity resolution: RapidFuzz for the fuzzy layer, plus an **alias registry that learns only from human-confirmed escalations** and is frozen during eval runs (§2.7). Part 2 flags this as harder than the LLM work, and it is. Build the registry now rather than bolting it on.

### Sessions 2.2–2.4 — Remaining three servers

`mcp-bank` (fixed-width/CSV, encoding quirks), `mcp-processor` (client for the mock REST API — pagination, backoff on 429), `mcp-remittance` (PDF via pdfplumber, developed against the non-held-out templates only).

Reference the pattern explicitly each time: *"look at src/mcp/erp/ to understand the server pattern, then implement the bank statement server following it."* Pointing Claude at an existing pattern produces better results than describing what you want.

**On the remittance server:** parse structurally, do not chunk semantically. A line item is meaningless without the header naming the payer. Part 2 calls this out as a case where the naive RAG approach is simply wrong.

Verification per server: integration test that starts it, queries it, asserts records land in canonical schema; each agent-facing tool answers a known query correctly; a failure test — malformed input, empty response, rate limit — asserts graceful degradation rather than a crash. For `mcp-remittance`, a separate test on the held-out template reports its parse rate — that number goes in the README. Across all servers: a test that no tool can write.

---

## 4.5 Phase 3 — Eval harness and response cache

**2 sessions. $0. Built before the matcher and before the LLM, deliberately.**

If you build the matcher first you will write the scoring code twice — once to get a baseline number, once properly. If you build the LLM first you will tune it against vibes. A simple eval that ships on every PR beats a comprehensive framework that runs quarterly.

### Session 3.1 — Metrics and CI gate

The four metrics — auto-match precision, coverage, escalation rate, recall at precision ≥0.99 — overall and per discrepancy type, scored at the **group level** per §2.5. Confusion matrix with a no-match row. Retrieval recall@k (is the true group in the candidate pool) as a separate metric for Phase 5. JSON output plus a human-readable report.

Wire into CI: fail on regression beyond a threshold against a committed baseline.

Three targets:
- `just eval-fast` — 50–60 stratified dev-slice cases, for tight iteration
- `just eval` — full dev slice
- `just eval-test` — test slice; refuses to run unless `CROSSFOOT_ALLOW_TEST=1` is set, so it cannot be run by accident during iteration

Verification: a hand-written set of predicted groups with known TP/FP/FN/escalation counts produces exactly the expected numbers, including the partial-overlap and no-match cases.

### Session 3.2 — The response cache

**Budget-critical. Nothing calls an LLM until this exists.**

```
Implement src/llm/cache.py: a disk-backed cache wrapping all LLM calls.
Cache key is a hash of (prompt template version, rendered input, model,
thinking config, and the tool-call transcript so far — agentic runs make
several calls per case and each must be cached independently). On hit,
return the stored response without an API call. Add a --no-cache flag for
forced refresh, and a `just cost` target that reports cumulative spend,
cache hit rate, and cache_read_input_tokens from a local usage log.
Write tests proving a repeated identical call makes zero network requests.
```

Verification: run the same eval twice against a stub model, assert the second makes zero calls and reports a 100% hit rate.

This is not only a budget mechanism. Deterministic replay of LLM outputs is standard eval practice and belongs in the project regardless.

---

## 4.6 Phase 4 — Deterministic matcher

**2 sessions. $0. This produces your control number.**

### Session 4.1 — Rules

Exact match, amount-plus-date-window, reference match. Business-day-aware windows on calendar dates (never UTC-shifted).

Bound the many-to-many search here, deliberately. Part 2's position: cap candidate group size at 4 and escalate beyond that. Document the bound as a tradeoff in code comments, because an interviewer will ask.

### Session 4.2 — Baseline measurement

`just eval` against the matcher — the harness already exists, so this session is measurement and analysis, not code. Overall and per-type on the dev slice.

**Write this number into the README before going further.** Every subsequent claim about LLM value is measured against it. Committing it early keeps you honest about attribution later.

Expected: precision near 1.0, moderate coverage. Clean matches, fee deductions, and timing lags caught; name variants, partials, consolidations, and the reasoning-required class largely missed. Those misses are exactly what Phase 6 is for. **The residual count from this run replaces the ~500 estimate in the ledger.**

---

## 4.7 Phase 5 — Candidate blocking and the retrieval experiment

**3 sessions. $0 — embeddings and reranker run locally on the 4060.**

### Session 5.1 — Blocking baseline

The candidate generator that ships to Phase 6 (§3.5): SQL amount-within-tolerance, business-day window, `pg_trgm` on normalized vendor and reference, RapidFuzz ranking, top-10. Measure recall@10 per type on the residual — "is the true group's every record in the pool?"

This is the number the LLM is bounded by. If blocking recall on partials is 0.6, no prompt will get partial-payment recall above 0.6. Fix blocking first.

Verification: recall@10 per type reported; a test asserts blocking never returns a candidate outside the amount/date window.

### Session 5.2 — Hybrid indexes over remittance text

BM25 (`rank-bm25`) and dense (sentence-transformers, pgvector) over remittance narrative text only. RRF fusion, rerank depth ≤20 with `bge-reranker-v2-m3` on GPU (MiniLM fallback on CPU).

**Fuse on ranks, not scores.** BM25 scores are unbounded positive while cosine similarity is bounded; RRF sidesteps the scale problem.

### Session 5.3 — The retrieval experiment

Measure recall@10 on the remittance-bearing residual, per type, across: blocking alone; blocking + BM25; blocking + dense; blocking + RRF; blocking + RRF + rerank. Test RRF `k` at 10 and 60.

**Report the result in whichever direction it comes out.** If hybrid retrieval lifts recall on consolidated and reasoning-required cases, that is the story. If it adds nothing over blocking, that is also the story — "I measured it, it didn't help on structured records, here is why" is a stronger interview answer than an unmeasured claim. Do not write the finding before running the experiment.

---

## 4.8 Phase 6 — LLM adjudication

**4 sessions. ~$12 — the bulk of your spend. Discipline matters most here.**

### Session 6.1 — Graph skeleton on Ollama

LangGraph nodes and edges: adjudicate → maybe-call-tool → adjudicate → score → commit-or-escalate. The agentic-lite bound (≤3 tool calls, ≤4 steps) enforced in the graph, not the prompt. MCP tool bindings. A **provider-agnostic Pydantic validation layer** so the output contract is the same object whether Ollama or Claude produced it. Escalation path. **Point it at Ollama (`qwen2.5:7b`).** You are testing plumbing, not model behavior.

What Ollama cannot test: `strict: true` grammar-constrained tool use is Anthropic-only. Expect the first Claude run to surface schema mismatches. That is fine — it is one session, not ten.

Verification: end-to-end run completes; output validates as `MatchDecision`; escalation fires when the Python score is below threshold; the step cap halts a run where a tool is stubbed to always say "need more information"; a test asserts no tool call can reach a write.

### Session 6.2 — First Claude run, Haiku, fast subset

`just eval-fast` (50–60 stratified dev cases) with the cache live. At Rule 5's sizes, roughly **$0.20 per run**, less once the cache is warm.

Structure the prompt **static-first, dynamic-last**: system prompt, tool schemas, reconciliation rules, few-shot examples, then the candidate set. Then check `just cost`: if `cache_read_input_tokens` is zero, the prefix is under Haiku 4.5's 4096-token minimum (§3.4). Either pad it with more few-shot examples or accept it and note it — at this volume the difference is cents.

Enable **strict tool use** (`strict: true`) on the `MatchDecision` tool. Carry the caveat into your design: **structured outputs guarantee format compliance, not accuracy.** You can get perfectly formatted incorrect answers. Semantic validation — do the cited records exist, do the amounts balance — is a separate Python layer.

### Session 6.3 — Prompt iteration

Where budget disappears if you are careless. Discipline:

- Fast subset only — 50–60 cases is the smallest set where one flipped case is not a 7-point swing
- Cache on — only genuinely new prompt versions cost anything
- Batch related changes into one eval run rather than one-change-one-run
- Log each prompt version with its four-metric scores so you can revert
- **Stop when two consecutive iterations move nothing.** More iterations on a small set is overfitting, not progress.

Budget roughly 40 deliberate iterations at ~$0.20 = ~$8.

Ground every decision in retrieved evidence. The agent cites source record IDs for every match. No citation, no match. **Never let the model do arithmetic** — it proposes, Python verifies the amounts balance and computes the score.

### Session 6.4 — Full dev-slice run and attribution

`just eval` over the full dev-slice residual (~500 cases), Haiku, interactive — not batched, the agent calls tools. Roughly $3–4 cold, most of it cached from 6.3.

Produce the per-type delta against your Phase 4 baseline using the four metrics. Which discrepancy types did the LLM actually improve, and by how much recall at held precision? Expect the reasoning-required class to be where the lift concentrates — that is why it exists.

**Tune for precision.** A false match silently corrupts the ledger while a missed match lands in a review queue. This is not just your reasoning — practitioners independently converged on it, noting that false matches are more dangerous than visible unmatched items because they can hide misstatements, and that the design objective should be fewer unresolved exceptions rather than a dashboard claiming a high automation rate. Report coverage and escalation rate alongside, so a precision number cannot hide an agent that escalates everything.

---

## 4.9 Phase 7 — Governance

**2 sessions. ~$3.**

### Session 7.1 — Budgets, loops, failures

Per-case token budget with hard halt. Step cap (already in the graph from 6.1 — now with a budget node that also counts tokens). Repeated-identical-tool-call detection: same tool, same arguments, twice in one case → halt and escalate. Tool failure classification separating retriable (429, timeout) from terminal (404, schema error), with bounded retries on the former. These control a real loop — the agent calls tools, so it can loop.

Verification: induce a runaway deliberately — stub `lookup_alias` to always return "ambiguous, try again" — and assert the run halts at the step cap, logs why, and escalates. Induce a 429 storm from the mock processor and assert bounded backoff then escalation. Those tests are the evidence for the claim.

### Session 7.2 — Traces and audit

Langfuse self-hosted. Every decision replayable: inputs, retrieved candidates, prompt version, model, response, confidence, final action.

This addresses something the industry has not: **the audit trail problem is unsolved at most firms**, and SOC 2 expects privileged actions to be attributable to an accountable individual, where "the agent did it" is not acceptable.

Verification: pick any match from the eval output, replay it end to end from the trace alone.

---

## 4.10 Phase 8 — Packaging and final numbers

**3 sessions. ~$4.**

### Session 8.1 — Final measurement runs on the test slice

`just eval-test` with `CROSSFOOT_ALLOW_TEST=1`. **The test slice has never been run before this session.** Two runs, interactive, cold cache by construction (new inputs):

| Run | Purpose | Cost |
|---|---|---|
| Haiku, test-slice residual | Headline numbers | ~$1.50 |
| Sonnet, test-slice residual | Model comparison | ~$2.00 |

No "governance off" run — budgets and step caps should not change outputs on non-runaway cases, and if they do that is a bug to fix, not a result to report. No cache clearing — at temperature 0 with identical inputs the cached response *is* the fresh response. If you want a variance number, run 20 dev cases three times (~$0.30) and report the spread.

The Haiku-versus-Sonnet delta is a genuine result. *"Haiku achieves N% of Sonnet's recall at held precision, at half the cost"* is exactly the routing reasoning these companies screen for. The dev-vs-test gap is also a result: if dev numbers are much better than test, say so — it is the honest measure of how much prompt iteration overfit.

Explanation-quality LLM-as-judge over a 50-case sample: ~$0.50, the ledger's remaining line.

### Session 8.2 — Review interface

Streamlit, escalated items only. **Timebox to one session.** Not the point of the project.

### Session 8.3 — README and write-up

Structure:

1. The problem, stated as Part 2.1 states it — four systems, same $4,417
2. Architecture diagram
3. **The rules-first decision and why** — the most important paragraph. An AI reconciliation agent does not replace deterministic rules, it complements them, with rules resolving the easy cases
4. Results on the test slice: baseline → blocking recall → retrieval experiment (either direction) → LLM lift, all four metrics per type
5. The precision/coverage tradeoff and why you chose precision
6. Cost per thousand events, the model routing finding, and what the cache actually saved
7. **A regression the eval suite caught**
8. **A failure mode you discovered and engineered against**
9. Explicit non-goals: four sources, batch reconciliation only (not Batch API), no fine-tuning, bounded group size, bounded tool calls
10. **What synthetic data cannot prove**: generator and matcher share an author; deterministic coverage is an upper bound; the held-out PDF template and the reasoning-required class are the only places the data is harder than the rules know
11. Honest limitations, including the dev-vs-test gap

Items 7 and 8 matter most. Your F1 project is credible because the suppression gate exists as a response to a real discovered problem. Replicate that here.

Use an adversarial review on the README:

```
Use a subagent to review README.md as a skeptical senior engineer who has
shipped reconciliation systems. Where are the claims unsupported by numbers?
Where would you push back in an interview? Report substantive gaps only,
not style.
```

---

## 4.11 Anti-patterns specific to this build

**Correcting the matcher in circles.** After two failed corrections, `/clear` and rewrite the prompt with what you learned. A clean session with a better prompt almost always outperforms a long session with accumulated corrections.

**Letting Claude touch golden fixtures.** Hook 2 prevents it. If you skip the hook, this will happen.

**Building the LLM before the eval harness.** You will tune against vibes and have no attribution story.

**Running full evals during iteration.** 10x the cost for no additional signal. Fast subset until the prompt is stable.

**Touching the test slice before Phase 8.** Every run on it is a run you cannot un-see. The `CROSSFOOT_ALLOW_TEST` guard exists so this takes deliberate effort.

**Iterating on a 15-case set.** One flip is 7 points. You will chase noise and call it progress. 50–60 stratified, minimum.

**Forgetting the cache flag.** Add `just cost` output to your end-of-session routine. Catch overspend — or a silently uncached prefix — the same day.

**Writing the finding before the experiment.** "Dense retrieval will fail on invoice numbers" was in the first draft of this plan as an expected result. Then it turned out the residual has no invoice numbers by construction. Measure, then write.

**Chasing every review finding.** A reviewer asked to find gaps will report some even when the work is sound, and chasing all of them leads to over-engineering. Fix what affects correctness; note the rest.

**Scope creep.** A fifth source, streaming, fine-tuning, a polished UI. Each costs a week and adds nothing to the story. The non-goals list in the README is your commitment device.

---

## 4.12 Session map

| # | Phase | Session | Mode | Cost |
|---|---|---|---|---|
| 0 | Pre-flight | Skeleton, CLAUDE.md, hooks, skills | Plan | $0 |
| 1 | 1 | Data generation interview → SPEC | Interview | $0 |
| 2 | 1 | Schema, MatchDecision, generator core | Plan | $0 |
| 3 | 1 | Discrepancy types + four formats + FX/holiday tables | Direct | $0 |
| 4 | 1 | PDF templates (3–4, one held out) + eval slice builder | Direct | $0 |
| 5 | 2 | Mock processor REST API | Direct | $0 |
| 6 | 2 | Canonical schema, entity resolution, mcp-erp | Plan | $0 |
| 7 | 2 | mcp-bank | Direct | $0 |
| 8 | 2 | mcp-processor | Direct | $0 |
| 9 | 2 | mcp-remittance + held-out template test | Direct | $0 |
| 10 | 3 | Eval metrics (four metrics, group-level) + CI gate | Plan | $0 |
| 11 | 3 | **Response cache** | Direct | $0 |
| 12 | 4 | Deterministic rules | Plan | $0 |
| 13 | 4 | Baseline measurement (harness already exists) | Direct | $0 |
| 14 | 5 | Blocking baseline + recall@10 per type | Plan | $0 |
| 15 | 5 | Hybrid indexes over remittance text + rerank | Direct | $0 |
| 16 | 5 | Retrieval experiment | Direct | $0 |
| 17 | 6 | Graph skeleton on Ollama, bounded tool loop | Plan | $0 |
| 18 | 6 | First Claude run, Haiku, fast subset | Direct | ~$0.50 |
| 19 | 6 | Prompt iteration (~40 runs) | Direct | ~$8 |
| 20 | 6 | Full dev-slice run + attribution | Direct | ~$3.50 |
| 21 | 7 | Budgets, step cap, repeat-call detection, tool failures | Direct | ~$1.50 |
| 22 | 7 | Traces and audit | Direct | ~$1.50 |
| 23 | 8 | Test-slice runs, Haiku + Sonnet + explanation judge | Direct | ~$4 |
| 24 | 8 | Review UI | Direct | $0 |
| 25 | 8 | README, write-up, adversarial review | Direct | $0 |

**Total: ~$19, against a $30 ceiling.** Phase 6 = $12, Phase 7 = $3, Phase 8 = $4, matching the §4.0 ledger. The $11 reserve absorbs a generator bug found late, a prompt direction that does not work out, a residual larger than 500, or a second test-slice run if the first exposes a bug.

---

## 4.13 End-of-session checklist

Run this every time. It is the difference between a build that stays on budget and one that does not.

- [ ] All tests pass, evidence shown in the transcript
- [ ] `just eval-fast` run if anything under `src/matching/` or `src/blocking/` changed
- [ ] `just cost` checked — spend, hit rate, `cache_read_input_tokens` — and `BUDGET.md` updated
- [ ] Test slice untouched (`just cost` shows zero test-slice calls before Phase 8)
- [ ] Committed with a descriptive message, and `git log -1 --format=%B` shows no attribution trailer
- [ ] Session named via `/rename`
- [ ] Any new gotcha added to CLAUDE.md — or CLAUDE.md pruned if it has grown past ~30 lines
- [ ] Next session's objective written down in one sentence

---

## 4.14 Revisions

**2026-09-14 — pre-build critique.** Changes from the first draft, with reasons:

| Change | Reason |
|---|---|
| Adjudication is agentic-lite (≤3 MCP tool calls, ≤4 steps); Batch API dropped | Draft ran a tool-calling LangGraph loop *and* evaluated via Batch API. Batch is one request/one response; a multi-step agent cannot run through it. Without tool calls, loop detection and governance were controlling nothing. |
| MCP servers expose agent-facing read-only tools | Draft used MCP purely as ETL. The agent never called a server. MCP-as-ETL invites "why not a Python module?" |
| Candidate generation is SQL+trigram+RapidFuzz **blocking**; hybrid retrieval is an experiment over remittance text | Draft's headline retrieval result ("dense fails on `INV-2024-0891`") tested a query that never reaches retrieval — exact identifiers are consumed by rules first. Amount/date proximity is not a text-relevance problem. |
| 10k events; dev/test slices by seed, stratified ≥100 per type | Draft's 2k gave orphan n=20, duplicate n=40 — ±20pp intervals on the per-type numbers the plan calls its credibility. Draft iterated 120 times on 15 cases then tested on a set containing them. |
| Four metrics replace precision/recall; group-level scoring unit; `MatchDecision` schema; no-match class | "Accuracy exceeds baseline" was undefined. Baseline precision ≈1.0 so the LLM can only lower it. Escalation was invisible: escalate-everything scores P=1/R=0. |
| Reasoning-required discrepancy class added | Without it, RapidFuzz catches every synthetic name variant and the LLM lift is null. |
| Eval harness before matcher (Phases 3/4 swapped) | Session 3.2 "baseline measurement" was the harness, then Session 4.1 built it again. |
| Alias registry learns only from human-confirmed escalations, frozen during eval | "Learns confirmed aliases" from the manifest is leakage; from LLM decisions is compounding error. |
| Score computed in Python from evidence features | LLM self-reported confidence is uncalibrated; thresholding it on a small set is noise. |
| Dates stay dates; only timestamps go to UTC | Converting a value date to UTC can shift the day. |
| Haiku 4.5 4096-token cache minimum noted; `cache_read_input_tokens` logged | A 2–3k prefix on Haiku silently never caches. All 0.1x assumptions were void. |
| Mock processor REST API, FX/holiday tables, PDF templates added as sessions | `mcp-processor` wrapped an API that did not exist. FX normalization had no rate source. reportlab→pdfplumber on one layout proves nothing. |
| stdio transport; rerank depth ≤20; Ragas cut; governance-off run cut; ledger reconciled | Time and money spent on things that measured nothing. Draft's §4.0 ledger and §4.12 map disagreed ($22 vs $16). |
| Windows prerequisites; `just` replaces `make` | Build machine is Windows 11; `make` is not present. |
