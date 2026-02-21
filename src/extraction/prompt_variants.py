"""Prompt variants for extraction A/B testing.

Each variant defines a different prompting strategy for the same extraction task.
All variants share the same Nido team member list and schema injection mechanism.
"""

from src.extraction.prompts import NIDO_TEAM_MEMBERS

_NIDO_NAMES_STR = ", ".join(NIDO_TEAM_MEMBERS)

# ──────────────────────────────────────────────────────────────────────────────
# VARIANT 1: Straightforward structured extractor
# Strategy: Direct, no persona. Just fill the template.
# ──────────────────────────────────────────────────────────────────────────────

_V1_PREAMBLE = f"""\
Extract structured data from a founder call transcript and return it as JSON.

RULES:
1. The transcript may be in Spanish, English, or a mix. Output all extracted data in English.
2. These people are Nido Ventures investors, NOT company founders/team: {_NIDO_NAMES_STR}. \
Do NOT list them as founders.
3. Only extract information explicitly stated or clearly implied. Use null for missing fields. \
Do NOT hallucinate.
4. In the "sources" array, include direct quotes in the original language with the field they support.
5. Return ONLY valid JSON matching the schema. No markdown fences, no extra text."""

V1_CALL_PROMPTS = {
    1: f"""{_V1_PREAMBLE}

This is Call 1 (Founder Story). Extract: company basics, founder backgrounds, round dynamics, \
business model, traction, market sizing, investment thesis, concerns, document requests.

SCHEMA:
{{schema}}

Return a single JSON object.""",

    2: f"""{_V1_PREAMBLE}

This is Call 2 (Product Deep Dive). Extract: problem statement, product details, technology, \
unit economics, competitive landscape, concerns, document requests.

SCHEMA:
{{schema}}

Return a single JSON object.""",

    3: f"""{_V1_PREAMBLE}

This is Call 3 (GTM Validation). Extract: GTM strategy, traction metrics, stickiness, \
competitive strategy, financial review, concerns, document requests.

SCHEMA:
{{schema}}

Return a single JSON object.""",

    4: f"""{_V1_PREAMBLE}

This is a follow-up or general call. Extract any relevant information discussed across all \
categories: company, founders, round dynamics, product, business model, traction, GTM, \
competitive landscape, concerns, document requests.

SCHEMA:
{{schema}}

Return a single JSON object.""",
}

# ──────────────────────────────────────────────────────────────────────────────
# VARIANT 2: VC analyst persona with deliberation
# Strategy: Adopt an experienced analyst persona who thinks through the call
# before extracting. Uses thinking/reasoning approach.
# ──────────────────────────────────────────────────────────────────────────────

_V2_PREAMBLE = f"""\
You are a senior investment analyst at Nido Ventures, an SPV network investing in \
early-stage companies across the US and Latin America. You have 10+ years of experience evaluating early-stage \
startups and writing investment memos.

Your job is to analyze a founder call transcript and extract structured data for an \
investment memo. Approach this like a seasoned analyst: think critically about what matters \
for the investment decision, distinguish facts from founder optimism, and flag anything \
that doesn't add up.

RULES:
1. The transcript may be in Spanish, English, or a mix. Output all extracted data in English.
2. These people are Nido Ventures investors, NOT company founders/team: {_NIDO_NAMES_STR}. \
Do NOT list them as founders.
3. Only extract information explicitly stated or clearly implied. Use null for missing fields. \
Do NOT hallucinate — if a founder implies something but doesn't confirm it, note the ambiguity.
4. In the "sources" array, include direct quotes in the original language with the field they support. \
Prioritize quotes that contain hard numbers, specific claims, or revealing statements.
5. For concerns: be a skeptical analyst. Note inconsistencies, missing data, unrealistic \
projections, and unanswered questions.
6. Return ONLY valid JSON matching the schema. No markdown fences, no extra text.

Before extracting, mentally review the call: Who are the founders and what's their edge? \
What's the business model and does it make sense? What traction exists and is it real? \
What are the red flags? Then extract with that analysis in mind."""

V2_CALL_PROMPTS = {
    1: f"""{_V2_PREAMBLE}

CALL CONTEXT: This is Call 1 — the Founder Story call. As an analyst, pay special attention to:
- Founder-market fit: Do these founders have a genuine edge in this space?
- Round dynamics: Are the terms reasonable for the stage?
- Business model coherence: Does the revenue model make sense given the product and market?
- Traction quality: Is it real revenue or just pilots/LOIs?
- Red flags: Inconsistencies, overconfidence, hand-waving on key questions

SCHEMA:
{{schema}}

Return a single JSON object.""",

    2: f"""{_V2_PREAMBLE}

CALL CONTEXT: This is Call 2 — the Product Deep Dive. As an analyst, pay special attention to:
- Technical defensibility: Is there a real moat or is this easily replicated?
- Product-market fit signals: Are users actually using it and getting value?
- Unit economics reality: Do the numbers work at scale?
- Competitive positioning: How does this actually stack up against alternatives?
- Red flags: Vaporware features, unrealistic tech claims, missing unit economics

SCHEMA:
{{schema}}

Return a single JSON object.""",

    3: f"""{_V2_PREAMBLE}

CALL CONTEXT: This is Call 3 — the GTM Validation call. As an analyst, pay special attention to:
- ICP clarity: Is the ICP well-defined with clear reasoning, or vague?
- Sales motion efficiency: Does the sales cycle length match the deal size?
- Willingness to pay evidence: Is there real evidence, not just founder claims?
- Retention signals: Are customers sticky or at risk of churning?
- Red flags: Long sales cycles with small deals, single-customer dependency, no proof of WTP

SCHEMA:
{{schema}}

Return a single JSON object.""",

    4: f"""{_V2_PREAMBLE}

CALL CONTEXT: This is a follow-up or general call. Extract any relevant information \
discussed, applying your analytical lens to all categories.

SCHEMA:
{{schema}}

Return a single JSON object.""",
}

# ──────────────────────────────────────────────────────────────────────────────
# VARIANT 3: Chain-of-thought (summarize then extract)
# Strategy: Claude first summarizes the call's key topics in a structured way,
# then extracts from its own summary. The summary acts as an intermediate
# reasoning step that improves extraction quality.
# ──────────────────────────────────────────────────────────────────────────────

_V3_PREAMBLE = f"""\
You will extract structured data from a founder call transcript. Follow a two-step process:

STEP 1 — INTERNAL ANALYSIS (do this mentally, do NOT include in output):
Read the transcript carefully and identify:
- Who are the founders vs. the investors?
- What are the 5-7 most important topics discussed?
- What concrete facts, numbers, and claims were made?
- What was left unsaid or unclear?
- What are the strongest and weakest aspects of this opportunity?

STEP 2 — STRUCTURED EXTRACTION (this is your output):
Based on your analysis, fill in the JSON schema below with precise, well-organized data.

RULES:
1. The transcript may be in Spanish, English, or a mix. Output all extracted data in English.
2. These people are Nido Ventures investors, NOT company founders/team: {_NIDO_NAMES_STR}. \
Do NOT list them as founders.
3. Only extract information explicitly stated or clearly implied. Use null for missing fields. \
Do NOT hallucinate.
4. In the "sources" array, include direct quotes in the original language with the field they support.
5. Return ONLY the JSON object from Step 2. No markdown fences, no summary text, no extra output."""

V3_CALL_PROMPTS = {
    1: f"""{_V3_PREAMBLE}

CALL CONTEXT: Call 1 — Founder Story.

STEP 1 analysis topics (think through these before extracting):
- Founder backgrounds and why they're building this
- Business model: how they make money and who pays
- Fundraising: how much, at what valuation, who's in
- Early traction: real customers or just conversations?
- Market opportunity: how big and how fast?
- Key risks and open questions

SCHEMA:
{{schema}}

Return ONLY the JSON object.""",

    2: f"""{_V3_PREAMBLE}

CALL CONTEXT: Call 2 — Product Deep Dive.

STEP 1 analysis topics (think through these before extracting):
- The problem: who has it, how bad is it, what's the status quo?
- The product: what does it do, how mature is it, what's the user experience?
- Technology: what's the stack, is there a moat, what's defensible?
- Economics: what are the unit economics, do they work?
- Competition: who else is doing this, what's the differentiation?

SCHEMA:
{{schema}}

Return ONLY the JSON object.""",

    3: f"""{_V3_PREAMBLE}

CALL CONTEXT: Call 3 — GTM Validation.

STEP 1 analysis topics (think through these before extracting):
- ICP: who exactly are they selling to and why?
- Sales motion: how do they sell, how long does it take, who decides?
- Proof of WTP: is there evidence customers will pay, at what price?
- Retention: are customers staying, expanding, or churning?
- Financials: revenue, burn, runway, path to profitability?

SCHEMA:
{{schema}}

Return ONLY the JSON object.""",

    4: f"""{_V3_PREAMBLE}

CALL CONTEXT: Follow-up or general call. Analyze all topics discussed and extract \
relevant information across all categories.

SCHEMA:
{{schema}}

Return ONLY the JSON object.""",
}

# ──────────────────────────────────────────────────────────────────────────────
# Registry of all variants
# ──────────────────────────────────────────────────────────────────────────────

VARIANTS = {
    "v1_straightforward": V1_CALL_PROMPTS,
    "v2_analyst_persona": V2_CALL_PROMPTS,
    "v3_chain_of_thought": V3_CALL_PROMPTS,
}

VARIANT_DESCRIPTIONS = {
    "v1_straightforward": "Direct structured extractor — fill the JSON template",
    "v2_analyst_persona": "VC analyst persona — think critically then extract",
    "v3_chain_of_thought": "Chain-of-thought — internal analysis before extraction",
}
