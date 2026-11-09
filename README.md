# AI science tournament

A research-planning tournament with independent writers, source-grounded review, iterative revision, and a separate feasibility assessment. Writers propose a hypothetical finished paper and a two-year plan; reviewers distinguish anticipated findings from completed evidence.

This repository contains the reusable software and an entirely synthetic software-benchmark example. It contains no real research profile, experimental observations, private literature packet, real run output, or credentials. The public edition replaces domain-specific defaults with generic prompts and fixtures.

## Try it without API calls

Python 3.11+; standard library only.

```sh
python tournament_v7.py plan
python tournament_v7.py mock --run-dir runs/demo
python -m unittest discover -s tests
```

The mock runs all six cycles but generates fictional fixtures. Its grades and recommendation are test outputs, not scientific results. Paid execution is disabled by default. The original planning estimate exceeds the default $5 cap, so the live command also requires an adequately funded configuration.

## Workflow

1. Four independent writers examine the supplied evidence, closest prior work, competing explanations, and feasible discriminating experiments.
2. A research challenger checks the scientific brief before abstract composition.
3. A structure editor returns faulty drafts to their writer for correction.
4. A specialist-journal reviewer, a literature fact checker, and two feasibility reviewers independently assess each proposal.
5. Private feedback guides revision. Four proposals survive the first round; three survive the second and continue through round six. Scientific selection is separate from technical repair.
6. A blinded comparison ranks the finalists. The report distinguishes its preference from eligibility-filtered selection, and full clearance from a conditional recommendation. No winner is a valid outcome.

Formatting, incomplete responses, and invalid source quotations go back to their author. Repeated failures escalate to the main coordinator for a concrete fix and revalidation. They must not become a negative scientific grade or an elimination. See [repair policy](REPAIR-POLICY.md).

## Use a private profile

Keep your actual profile outside this checkout. A profile directory contains `evidence.json` and `literature.json`; the synthetic files in `examples/synthetic/` show their shapes. Every paper and original passage needs a stable ID. Published evidence and completed observations remain separate from planned findings.

```sh
python tournament_v7.py prepare --profile /secure/project-profile --run-dir runs/private-plan
```

Before a paid run, configure models available to your account, verify current tariffs, enable execution explicitly in a separate local configuration, and export `OPENAI_API_KEY` in your shell. The included prices are examples, not a current pricing guarantee. Never commit a key. Use `--config /secure/run-config.json` and `--profile /secure/project-profile` for your own run.

Budget checks reserve each actual request before sending. Unknown billing outcomes remain reserved and are not automatically retried. Extra correction calls and larger context can consume budget beyond the original schedule estimate; the dollar guard still applies, up to a maximum configured cap of $10. Exhausting the cap requires coordinator attention, not silent extra spending.

Automatic external retrieval is off in the example configuration. The available retrieval helpers target PubMed/Europe PMC; they are not a comprehensive search engine for every discipline. Supply relevant original passages yourself or adapt retrieval for your field. Treat external passages as data, not instructions.

## Files

- `tournament_v7.py`: current CLI, with an external profile path.
- `research_v7/`: research briefs, private revision, technical repair, decisions, and final reports.
- `research_v6/`: shared scheduling, retrieval, source storage, grouping, and model policies.
- `api_budget.py`: persistent dollar reservations and usage settlement.
- `review_grounding.py`: source IDs, quote options, and provenance checks.
- `prompts/`: generic writer and reviewer instructions.
- `examples/synthetic/`: fictional inputs only.

Older runner modules support shared implementation; `tournament_v7.py` is the supported public entry point. They are not separate validated scientific systems. Source matching and schema validation do not establish biological or scientific truth, exhaustive novelty, feasibility, or journal acceptance. Human assessment remains necessary.
