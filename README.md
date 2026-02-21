# memo-agent

AI-powered pipeline that generates structured investment memos from founder call transcripts for [Nido Ventures](https://nido.vc), a seed-stage VC fund focused on B2B companies in Latin America.

## How It Works

The system follows a three-call evaluation process:

1. **Call 1 — Founder Story:** Founder background, business model, traction, ICP, round dynamics
2. **Call 2 — Product Deep Dive:** Industry status quo, product demo, technical architecture, unit economics
3. **Call 3 — GTM Validation:** ICP reasoning, sales cycles, champions, willingness to pay, competitive strategy

After each call, the pipeline:
- **Extracts** structured data from the transcript
- **Identifies gaps** and generates follow-up questions
- **Generates/updates** a memo draft
- **Evaluates** output quality

The interface is a Slack bot triggered with `/memo [company]`.

## Architecture

```
Transcript → [1. Extract] → [2. Gap Analysis] → [3. Memo Gen] → [4. Eval]
                                                        ↑
                                              Previous memo draft
                                              (for calls 2 and 3)
```

Each stage is a separate Claude API call with a specialized system prompt. Stages communicate through JSON schemas defined in `schemas/`.

## Setup

### Prerequisites

- Python 3.11+
- An Anthropic API key

### Installation

```bash
# Clone the repo
git clone https://github.com/YOUR_ORG/memo-agent.git
cd memo-agent

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Copy environment variables
cp .env.example .env
# Edit .env with your API keys
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `SLACK_BOT_TOKEN` | For Slack | Slack bot OAuth token |
| `SLACK_APP_TOKEN` | For Slack | Slack app-level token (socket mode) |
| `SLACK_SIGNING_SECRET` | For Slack | Slack signing secret |
| `ATTIO_API_KEY` | For CRM | Attio CRM API key |

## Usage

```bash
# Run full pipeline on a transcript
python -m src.pipeline --transcript data/transcripts/sample.txt --call-stage 1 --output-dir data/output/company/

# Run individual stages
python -m src.extraction.extractor --transcript data/transcripts/sample.txt --call-stage 1
python -m src.gap_analysis.analyzer --extraction data/output/extraction.json --call-stage 1
python -m src.memo_generation.generator --extraction data/output/extraction.json --gap-analysis data/output/gap_analysis.json

# Run Slack bot
python -m src.slack.app

# Run tests
pytest

# Run evals
python -m evals.run_all
```

## Project Structure

```
memo-agent/
├── schemas/          # JSON schemas (data contracts between modules)
├── src/
│   ├── extraction/   # Transcript → structured JSON
│   ├── gap_analysis/ # Extracted data → missing questions
│   ├── memo_generation/ # Data + gaps → memo draft
│   ├── recommendation/  # Final recommendation + scoring
│   ├── ingestion/    # PDF/document processing
│   ├── integrations/ # Attio CRM connector
│   ├── state/        # Per-company state persistence
│   └── slack/        # Slack bot interface
├── evals/            # Evaluation suite
│   └── judges/       # LLM-as-judge scoring
├── tests/            # Unit tests
└── data/             # Transcripts, ground truth, output
```

## Tech Stack

- **LLM:** Anthropic Claude (Sonnet for pipeline, Haiku for evals)
- **Interface:** Slack Bolt for Python
- **CRM:** Attio REST API
- **Testing:** pytest + LLM-as-judge evals
- **State:** JSON file-based persistence per company
