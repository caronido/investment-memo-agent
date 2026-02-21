# CLAUDE.md

> This file is read by Claude Code at the start of every session. Keep it updated as the project evolves.

## Project Overview

**memo-agent** is an AI-powered pipeline that generates structured investment memos from founder call transcripts for Nido Ventures, a seed-stage VC fund focused on B2B companies in Latin America. The system follows a three-call evaluation process (Founder Story → Product Deep Dive → GTM Validation), extracting structured data from each call, identifying gaps, generating/updating a memo draft, and evaluating output quality.

The interface is a Slack bot triggered on-demand with `/memo [company]`.

## Tech Stack

- Python 3.11+
- Anthropic Claude API (claude-sonnet-4-20250514 for pipeline, claude-haiku-4-5-20251001 for evals)
- Slack Bolt for Python (slash commands, Block Kit formatting)
- Attio REST API (CRM data, transcripts, decks)
- pytest for unit tests
- JSON file-based state management (per company)

## Repo Structure

```
memo-agent/
├── CLAUDE.md                 # This file (project context for Claude Code)
├── README.md                 # Project docs, architecture, setup, design decisions
├── pyproject.toml            # Dependencies and project config
├── .env.example              # Environment variable template
├── .gitignore
│
├── schemas/                  # JSON schemas defining data contracts between modules
│   ├── extraction_call1.json # Extraction schema for Call 1 (founder/business focus)
│   ├── extraction_call2.json # Extraction schema for Call 2 (product/tech focus)
│   ├── extraction_call3.json # Extraction schema for Call 3 (GTM/commercial focus)
│   ├── gap_analysis.json     # Gap analysis output schema
│   └── memo_template.json    # Full memo template with sections, descriptions, call-stage mapping
│
├── src/
│   ├── __init__.py
│   ├── pipeline.py           # Orchestrates the full flow: extract → gaps → memo → eval
│   │
│   ├── extraction/           # Session 1-3: Transcript → structured JSON
│   │   ├── __init__.py
│   │   ├── extractor.py      # Main extraction function
│   │   ├── prompts.py        # System prompts per call stage
│   │   └── prompt_variants.py # A/B test variants (Session 3)
│   │
│   ├── gap_analysis/         # Session 4-5: Extracted data → missing questions + doc requests
│   │   ├── __init__.py
│   │   ├── analyzer.py       # Main gap analysis function
│   │   └── prompts.py        # System prompts
│   │
│   ├── memo_generation/      # Session 6-7: Extracted data + gaps → memo draft
│   │   ├── __init__.py
│   │   ├── generator.py      # Main memo generation function
│   │   ├── prompts.py        # System prompts + Nido template voice
│   │   └── prompt_variants.py # A/B test variants (Session 7)
│   │
│   ├── recommendation/       # Session 13: Final recommendation after Call 3
│   │   ├── __init__.py
│   │   └── engine.py         # Recommendation + scoring rubric
│   │
│   ├── ingestion/            # Session 10: PDF decks, data room docs
│   │   ├── __init__.py
│   │   ├── document_processor.py  # PDF extraction via Claude vision
│   │   └── merger.py         # Merge transcript + document data with source attribution
│   │
│   ├── integrations/         # Session 12: External service connectors
│   │   ├── __init__.py
│   │   └── attio.py          # Attio CRM API client
│   │
│   ├── state/                # Session 9: Per-company state persistence
│   │   ├── __init__.py
│   │   └── manager.py        # Read/write state.json per company
│   │
│   └── slack/                # Session 11-12: Slack bot interface
│       ├── __init__.py
│       ├── app.py            # Bolt app, slash command handlers
│       ├── parser.py         # Command parsing + fuzzy company matching
│       └── formatters.py     # Block Kit message formatting
│
├── evals/                    # Evaluation suite (built alongside each module)
│   ├── __init__.py
│   ├── run_all.py            # Master eval runner (Session 14)
│   ├── baselines.json        # Best scores per eval for regression detection
│   │
│   ├── eval_extraction.py    # Session 2: Extraction evals
│   ├── eval_gap_analysis.py  # Session 5: Gap analysis evals
│   ├── eval_memo.py          # Session 7: Memo generation evals
│   ├── eval_multicall.py     # Session 9: Multi-call progression evals
│   ├── eval_pipeline.py      # Session 8: End-to-end pipeline evals
│   │
│   ├── ab_test_extraction.py # Session 3: Extraction prompt comparison
│   ├── ab_test_memo.py       # Session 7: Memo prompt comparison
│   │
│   └── judges/               # LLM-as-judge prompts and scoring logic
│       ├── __init__.py
│       ├── extraction_judge.py
│       ├── gap_judge.py
│       └── memo_judge.py
│
├── data/                     # Test data (not committed to git except samples)
│   ├── transcripts/          # Raw Grain transcripts (.txt)
│   │   └── sample_lazo_call1.txt
│   ├── ground_truth/         # Human-annotated correct extractions
│   │   └── sample_lazo_call1_gt.json
│   ├── documents/            # PDF decks, financial models for testing
│   └── output/               # Pipeline output, organized per company
│       └── lazo/
│           ├── extraction_call1.json
│           ├── gap_analysis_call1.json
│           ├── memo_v1.md
│           └── state.json
│
└── tests/                    # Unit tests (pytest)
    ├── test_extraction.py
    ├── test_gap_analysis.py
    ├── test_memo_generation.py
    └── test_pipeline.py
```

## Architecture

The pipeline runs in four stages per call:

```
Transcript → [1. Extract] → [2. Gap Analysis] → [3. Memo Gen] → [4. Eval]
                                                        ↑
                                              Previous memo draft
                                              (for calls 2 and 3)
```

Each stage is a separate Claude API call with a specialized system prompt. Stages communicate through JSON schemas defined in `schemas/`. The pipeline orchestrator (`src/pipeline.py`) chains them together.

For multi-call flows, `src/state/manager.py` persists accumulated data per company in a `state.json` file. Each call's extraction merges with prior extractions, and the memo generator receives the existing draft for updating.

## Key Design Decisions

1. **Evals are built alongside features, not after.** Every module has a corresponding eval suite. Prompt changes are validated against the eval suite before being committed.
2. **Three eval types per module:** Programmatic (schema, type checks, known facts), LLM-as-judge (qualitative scoring on rubric), and human review (Renata validates quality).
3. **Bilingual handling:** Transcripts may be Spanish/English mixed. Extraction normalizes to English. The memo output is in English.
4. **Human-in-the-loop:** The agent drafts; the investment team reviews. The system never makes autonomous investment decisions.
5. **Source attribution:** Every extracted data point tracks its source (transcript, deck, financial model) for traceability.

## Memo Template Sections

The investment memo follows this structure. Each section maps to a primary call stage:

| Section | Primary Call | Description |
|---------|-------------|-------------|
| Executive Summary | 1 | Deal terms, structure, check size, valuation |
| Investment Thesis | 1 | Why this is a compelling opportunity |
| Team & Founders | 1 | Background, founder-market fit |
| Problem Statement | 1-2 | Industry pain point and status quo |
| Product & Technology | 2 | What they've built, tech stack, defensibility |
| Business Model | 2 | Revenue model, unit economics, pricing |
| Market Analysis | 1-2 | TAM/SAM/SOM, bottom-up sizing |
| GTM Strategy | 3 | ICP, sales cycles, champions, willingness to pay |
| Competitive Landscape | 2-3 | Competitors, differentiation, right to win |
| Traction & Metrics | 3 | Client list, KPIs (CAC, LTV), growth |
| Financial Review | 3 | Financial model assessment with call data |
| Concerns & Challenges | 1-2-3 | Risks, red flags, open questions |
| Scoring Rubric | 3 | Team, market, product, model, traction, competition scores |

## Three-Call Process

**Call 1 (Founder Story):** Founder background, business model, traction, ICP, round dynamics. Requests: cap table, financial model, market sizing, competitive landscape, incorporation docs.

**Call 2 (Product Deep Dive):** Industry status quo, product demo, technical architecture, unit economics. Requests: tech memo, product roadmap, hardware memo if applicable.

**Call 3 (GTM Validation):** ICP reasoning, sales cycles, champions, willingness to pay, competitive strategy, stickiness. Requests: client list, sales metrics (CAC, LTV).

## Current Status

**Completed sessions:** None yet (starting from Session 0)

**Current session:** 0 (Project Scaffolding)

**Next up:** Session 1 (Transcript Extraction)

> Update this section at the end of every Claude Code session.

## Commands Reference

```bash
# Install
pip install -e .

# Run extraction
python -m src.extraction.extractor --transcript data/transcripts/sample.txt --call-stage 1 --output data/output/extraction.json

# Run gap analysis
python -m src.gap_analysis.analyzer --extraction data/output/extraction.json --call-stage 1 --output data/output/gap_analysis.json

# Run memo generation
python -m src.memo_generation.generator --extraction data/output/extraction.json --gap-analysis data/output/gap_analysis.json --output data/output/memo_draft.md

# Run full pipeline
python -m src.pipeline --transcript data/transcripts/sample.txt --call-stage 1 --output-dir data/output/lazo/

# Run evals
python -m evals.eval_extraction --transcript data/transcripts/sample.txt --ground-truth data/ground_truth/sample_gt.json
python -m evals.eval_gap_analysis --extraction data/output/extraction.json --call-stage 1
python -m evals.eval_memo --memo data/output/memo_draft.md --extraction data/output/extraction.json
python -m evals.run_all

# Run Slack bot (local, socket mode)
python -m src.slack.app

# Run tests
pytest
```

## Environment Variables

```
ANTHROPIC_API_KEY=         # Claude API key
SLACK_BOT_TOKEN=           # Slack bot OAuth token (Session 11+)
SLACK_APP_TOKEN=           # Slack app-level token for socket mode (Session 11+)
SLACK_SIGNING_SECRET=      # Slack signing secret (Session 11+)
ATTIO_API_KEY=             # Attio CRM API key (Session 12+)
```

## Session Log

Track what was built, what was learned, and what to carry forward.

| Session | What Was Built | Key Learnings | Eval Scores |
|---------|---------------|---------------|-------------|
| 0 | Scaffolding | — | — |
| 1 | | | |
| 2 | | | |
| 3 | | | |
| 4 | | | |
| 5 | | | |
| 6 | | | |
| 7 | | | |
| 8 | | | |
| 9 | | | |
| 10 | | | |
| 11 | | | |
| 12 | | | |
| 13 | | | |
| 14 | | | |
