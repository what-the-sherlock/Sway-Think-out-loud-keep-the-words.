# Sway — Documentation

Local voice dictation with LLM cleanup. Hold a hotkey, speak, release — cleaned-up
text appears at your cursor in whatever application has focus.

**All documents verified against `1e85b9f`, 2026-08-26.** 1391 tests passing.

---

## Start here

| If you want to… | Read |
|---|---|
| Understand what Sway is and why it exists | [Product Brief](product-brief.md) |
| Know exactly what it must do, and what is still open | [PRD](prd.md) |
| Understand how it is built | [Technical Specification](technical-spec.md) |
| Know what happens to your data | [Data & Privacy](data-and-privacy.md) |
| Understand why a decision was made the way it was | [Decision Log](decision-log.md) |
| Know what is tested and what is not | [Test Plan](test-plan.md) |
| Know what ships next | [Roadmap](roadmap.md) |
| Call the local HTTP API | [API Reference](api-reference.md) |
| Design or change any screen, component, or overlay state | [Design System & UX Guide](design.md) |

## Authority order

When two documents disagree, the higher one wins:

1. [`CLAUDE.md`](../CLAUDE.md) — hard constraints and architecture rules
2. [`PRODUCTION_BRIEF.md`](../PRODUCTION_BRIEF.md) — the D0–D8 productisation plan
3. Everything in this folder

## The full set

### Product

- **[product-brief.md](product-brief.md)** — the problem, the users, positioning
  against Wispr Flow and OS dictation, product principles, measured success metrics,
  non-goals, and risks.
- **[prd.md](prd.md)** — personas, nine core user journeys, ~90 numbered functional
  requirements each marked Shipped / Partial / Open, the settings inventory,
  non-functional targets, and the open questions.
- **[roadmap.md](roadmap.md)** — phase-by-phase status for both numbering schemes,
  and what to do next in evidence-ranked order.

### Engineering

- **[technical-spec.md](technical-spec.md)** — system overview, the threading model
  thread by thread, module map, the pipeline stage by stage, local resolution, the
  one-call rule, guardrails, SQLite schemas, the two configuration layers, the
  dashboard server, error handling, the measured latency budget, packaging, platform
  isolation, and every dependency's justification.
- **[api-reference.md](api-reference.md)** — the complete loopback HTTP contract:
  transport, token and origin auth, status codes, every GET and POST route.
- **[data-and-privacy.md](data-and-privacy.md)** — what leaves the machine, what is
  stored where and for how long, the API key path, the threat model (including what
  is *not* defended against), how to delete everything, and how to verify the claims
  yourself.
- **[decision-log.md](decision-log.md)** — 15 ADRs, each with the measurement that
  settled it where one exists.
- **[test-plan.md](test-plan.md)** — the testing philosophy, automated coverage by
  layer, ten invariants that must never regress, a 37-row manual verification matrix,
  the diagnostic scripts, and the known gaps.

### Reference material (pre-existing)

- **[d0-audit.md](d0-audit.md)** — the D0 architecture map, the measured latency
  baseline every performance claim in these documents cites, and the source-vs-exe
  divergence list.
- **[ui-reference-analysis.md](ui-reference-analysis.md)** — analysis of the
  reference interface the dashboard's structure derives from.
- **[ui-derived-screens.md](ui-derived-screens.md)** — the screen-by-screen
  specification derived from it.
- **[ui-gap-analysis.md](ui-gap-analysis.md)** — what the implementation does and does
  not yet match.
- **[architecture-diagram-prompts.md](architecture-diagram-prompts.md)** — prompts for
  generating the architecture diagrams.

---

## Known documentation debt

Stated here rather than left to be discovered:

- **`README.md` in the repository root is stale.** It still calls the package `flow`
  (it is `sway`), describes the status as Phase 5, and names `llama-3.1-8b-instant` as
  the cleanup model — which Groq has retired. The current model is
  `openai/gpt-oss-20b`.
- **`PRODUCTION_BRIEF.md` predates the rename** and refers to `flow.*` modules and
  `Flow.exe` throughout. The architecture it describes is accurate; only the names
  have moved.
- Several docstrings in `src/sway/` still say `flow.` in cross-references for the same
  reason.
