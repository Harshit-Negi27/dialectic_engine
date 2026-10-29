from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from tavily import TavilyClient
from openai import AsyncOpenAI
import os
import json
import asyncio
from datetime import date
from dotenv import load_dotenv
from pathlib import Path

from backend.database import init_db, get_cached_analysis, save_analysis_to_cache
from backend.youtube_utils import get_transcript_text

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)
init_db()

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

# "v1" = original prompts, "v2" = improved prompts, "v3" = strict claims, "v4" = scope check, "v5" = evidence & verifier, "v6" = perspective & contested/factual pipeline (default)
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v6")

EXCLUDE_DOMAINS = [
    "linkedin.com",
    "facebook.com",
    "x.com",
    "twitter.com",
    "instagram.com",
    "pinterest.com",
]

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ──────────────────────────────────────────────────────────────

async def _safe_llm_json(messages: list, temperature: float = 0.2, retries: int = 1) -> dict | None:
    """Call GPT-4o-mini with json_object format. On malformed JSON, retry once.
    Returns the parsed dict, or None if all attempts fail."""
    for attempt in range(1 + retries):
        try:
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=temperature,
                response_format={"type": "json_object"},
                messages=messages,
            )
            return json.loads(resp.choices[0].message.content)
        except (json.JSONDecodeError, KeyError, IndexError):
            if attempt < retries:
                continue  # retry once
            return None
        except Exception:
            return None


# ── Summarizer (unchanged across versions) ───────────────────────────────

async def agent_summarize(text: str) -> str:
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.1,
            messages=[
                {"role": "system", "content": "You are a neutral summarization assistant."},
                {"role": "user", "content": f"Provide a concise, 2-3 sentence summary:\n\n{text[:15000]}"}
            ]
        )
        return resp.choices[0].message.content
    except: return "Summary unavailable."


# ── Claim Extraction ─────────────────────────────────────────────────────

async def agent_extract_claims_v1(text: str) -> list:
    """Original v1 prompt. Returns a list of plain claim strings."""
    prompt = f"""
    Extract up to 5 distinct, argumentative claims. 
    Ignore definitions, rules, or facts.
    Return ONLY a valid JSON list of strings.
    Text: {text[:15000]}
    """
    data = await _safe_llm_json([
        {"role": "system", "content": "You are a logic filter. Respond with JSON."},
        {"role": "user", "content": prompt}
    ])
    if data is None:
        return []
    return data.get("claims", [])


async def agent_extract_claims_v2(text: str) -> list:
    """Improved v2 prompt. Returns a list of dicts: {"claim": str, "search_query": str}."""
    system = (
        "You select claims worth challenging. Be strict: quality over quantity. "
        "Output JSON only."
    )
    user = f"""Analyze the following text and extract up to 4 claims worth challenging.

A claim qualifies ONLY if ALL of these are true:
- It asserts something about the world (a cause, prediction, ranking, recommendation, or statistic).
- It is NOT a definition, description of what the article covers, or the author's framing ("this article explains...").
- A knowledgeable person could reasonably disagree, or credible evidence could exist against it.
- It is specific enough to search for.

Skip: definitions, common knowledge, generic advice, and meta-commentary about the article itself.
If fewer than 4 qualify, return fewer. If none qualify, return an empty list.

For each claim:
1. Rewrite it as a standalone sentence (no pronouns, no "the author says").
2. Write a search_query (5-10 words) aimed at finding evidence that CHALLENGES or LIMITS the claim — not one that restates it.

Return JSON: {{"claims": [{{"claim": "...", "search_query": "..."}}]}}

Text:
{text[:15000]}"""

    data = await _safe_llm_json([
        {"role": "system", "content": system},
        {"role": "user", "content": user}
    ])
    if data is None:
        return []
    return data.get("claims", [])


async def agent_extract_claims_v3(text: str) -> list:
    """Extractor v3 prompt. Adds claim type and why_debatable fields with negative examples.
    Drops any claim whose type is definition or description, or whose why_debatable is empty."""
    system = (
        "You select claims worth challenging. Be strict: quality over quantity. "
        "Output JSON only."
    )
    user = f"""Analyze the following text and extract up to 4 claims worth challenging.

A claim qualifies ONLY if ALL of these are true:
- It asserts something about the world (a cause, prediction, ranking, recommendation, or statistic).
- It is NOT a definition, description of what something is or how it works, or the author's framing ("this article explains...").
- A knowledgeable person could reasonably disagree, or credible evidence could exist against it.
- It is specific enough to search for.

Negative examples — DO NOT extract claims like these:
- "GEO focuses on whether AI-generated answers mention your business" (definition)
- "Superintelligence refers to AI that would outperform humans" (definition)
- "MCP gives AI apps a standard way to connect to tools" (description of how it works)

Skip: definitions, descriptions of functionality, common knowledge, generic advice, and meta-commentary about the article itself.
If fewer than 4 qualify, return fewer. If none qualify, return an empty list.

For each claim:
1. Rewrite it as a standalone sentence (no pronouns, no "the author says").
2. Set "type" to exactly one of: "causal", "prediction", "evaluative", "statistical", "definition", "description".
3. Write "why_debatable": one sentence explaining who could disagree and why.
4. Write "search_query": 5-10 words aimed at finding evidence that CHALLENGES or LIMITS the claim — not one that restates it.

Return JSON:
{{"claims": [{{"claim": "...", "type": "causal|prediction|evaluative|statistical|definition|description", "why_debatable": "...", "search_query": "..."}}]}}

Text:
{text[:15000]}"""

    data = await _safe_llm_json([
        {"role": "system", "content": system},
        {"role": "user", "content": user}
    ])
    if data is None:
        return []

    raw_claims = data.get("claims", [])
    valid_claims = []
    for item in raw_claims:
        if not isinstance(item, dict):
            continue
        c_type = str(item.get("type", "")).strip().lower()
        why_deb = str(item.get("why_debatable", "")).strip()

        # In code, drop any claim whose type is definition or description, or whose why_debatable is empty
        if c_type in ("definition", "description"):
            continue
        if not why_deb:
            continue
        valid_claims.append(item)

    return valid_claims


async def agent_extract_claims_v4(text: str) -> list:
    """Extractor v4 prompt. Adds hyperbole and attribution claim types with negative examples.
    Drops claims whose type is definition, description, hyperbole, or attribution, or whose why_debatable is empty."""
    system = (
        "You select claims worth challenging. Be strict: quality over quantity. "
        "Output JSON only."
    )
    user = f"""Analyze the following text and extract up to 4 claims worth challenging.

A claim qualifies ONLY if ALL of these are true:
- It asserts something about the world (a cause, prediction, ranking, recommendation, or statistic).
- It is NOT a definition, description of what something is or how it works, or the author's framing ("this article explains...").
- It is NOT hyperbole (rhetorical exaggeration or motivational statements) or attribution ("X said/believes Y").
- A knowledgeable person could reasonably disagree, or credible evidence could exist against it.
- It is specific enough to search for.

Negative examples — DO NOT extract claims like these:
- "GEO focuses on whether AI-generated answers mention your business" (definition)
- "Superintelligence refers to AI that would outperform humans" (definition)
- "MCP gives AI apps a standard way to connect to tools" (description of how it works)
- "Understanding key terms puts you ahead of 90% of people" (hyperbole)
- "The intelligence explosion could be much closer than expected according to Geoffrey Hinton" (attribution)

Skip: definitions, descriptions of functionality, hyperbole, attributions, common knowledge, generic advice, and meta-commentary about the article itself.
If fewer than 4 qualify, return fewer. If none qualify, return an empty list.

For each claim:
1. Rewrite it as a standalone sentence (no pronouns, no "the author says").
2. Set "type" to exactly one of: "causal", "prediction", "evaluative", "statistical", "definition", "description", "hyperbole", "attribution".
3. Write "why_debatable": one sentence explaining who could disagree and why.
4. Write "search_query": 5-10 words aimed at finding evidence that CHALLENGES or LIMITS the claim — not one that restates it.

Return JSON:
{{"claims": [{{"claim": "...", "type": "causal|prediction|evaluative|statistical|definition|description|hyperbole|attribution", "why_debatable": "...", "search_query": "..."}}]}}

Text:
{text[:15000]}"""

    data = await _safe_llm_json([
        {"role": "system", "content": system},
        {"role": "user", "content": user}
    ])
    if data is None:
        return []

    raw_claims = data.get("claims", [])
    valid_claims = []
    for item in raw_claims:
        if not isinstance(item, dict):
            continue
        c_type = str(item.get("type", "")).strip().lower()
        why_deb = str(item.get("why_debatable", "")).strip()

        # In code, drop any claim whose type is definition, description, hyperbole, or attribution, or whose why_debatable is empty
        if c_type in ("definition", "description", "hyperbole", "attribution"):
            continue
        if not why_deb:
            continue
        valid_claims.append(item)

    return valid_claims


async def agent_extract_claims_v5(text: str, published_date: str = None, today_date: str = None) -> list:
    """Extractor v5 prompt. Uses publication date and today's date to resolve relative time words.
    Outputs settling_evidence, neutral search_query, time_bound, and deadline.
    Drops definitions, descriptions, hyperbole, attributions, ambiguous terms, or empty why_debatable."""
    pub_info = f"Article publication date: {published_date}" if published_date else "Article publication date: Unknown"
    today_info = f"Today's date: {today_date}" if today_date else f"Today's date: {date.today().isoformat()}"

    system = (
        "You select claims worth challenging. Be strict: quality over quantity. "
        "Output JSON only."
    )
    user = f"""Analyze the following text and extract up to 4 claims worth challenging.
{pub_info}
{today_info}

A claim qualifies ONLY if ALL of these are true:
- It asserts something about the world (a cause, prediction, ranking, recommendation, or statistic).
- It is NOT a definition, description of what something is or how it works, or the author's framing ("this article explains...").
- It is NOT hyperbole (rhetorical exaggeration or motivational statements) or attribution ("X said/believes Y").
- Its key terms are NOT ambiguous or used in an idiosyncratic/special sense.
- A knowledgeable person could reasonably disagree, or credible evidence could exist against it.
- It is specific enough to search for.

Negative examples — DO NOT extract claims like these:
- "GEO focuses on whether AI-generated answers mention your business" (definition)
- "Superintelligence refers to AI that would outperform humans" (definition)
- "MCP gives AI apps a standard way to connect to tools" (description of how it works)
- "Understanding key terms puts you ahead of 90% of people" (hyperbole)
- "The intelligence explosion could be much closer than expected according to Geoffrey Hinton" (attribution)

Skip: definitions, descriptions of functionality, hyperbole, attributions, ambiguous/special terms, common knowledge, generic advice, and meta-commentary about the article itself.
If fewer than 4 qualify, return fewer. If none qualify, return an empty list.

For each claim:
1. Rewrite it as a standalone sentence (no pronouns, no "the author says"). Resolve relative time words ("today", "within six months", "recently") into absolute dates using the publication date, keeping any baseline the author gave.
2. Set "type" to exactly one of: "causal", "prediction", "evaluative", "statistical", "definition", "description", "hyperbole", "attribution".
3. Write "why_debatable": one sentence explaining who could disagree and why.
4. Write "settling_evidence": one sentence stating what kind of data or source would settle this (e.g. "headcount of employed software engineers, not job postings").
5. Write "search_query": 5-10 words, NEUTRAL: aimed at finding the settling evidence, NOT at supporting or attacking the claim.
6. Set "time_bound": true if the claim asserts a deadline, timeframe, or prediction, else false.
7. Set "deadline": the year (e.g. "2025" or null) if time_bound, else null.

Return JSON:
{{"claims": [{{"claim": "...", "type": "causal|prediction|evaluative|statistical|definition|description|hyperbole|attribution", "why_debatable": "...", "settling_evidence": "...", "search_query": "...", "time_bound": true|false, "deadline": "YYYY"|null}}]}}

Text:
{text[:15000]}"""

    data = await _safe_llm_json([
        {"role": "system", "content": system},
        {"role": "user", "content": user}
    ])
    if data is None:
        return []

    raw_claims = data.get("claims", [])
    valid_claims = []
    for item in raw_claims:
        if not isinstance(item, dict):
            continue
        c_type = str(item.get("type", "")).strip().lower()
        why_deb = str(item.get("why_debatable", "")).strip()

        # In code, drop any claim whose type is definition, description, hyperbole, or attribution, or whose why_debatable is empty
        if c_type in ("definition", "description", "hyperbole", "attribution"):
            continue
        if not why_deb:
            continue
        valid_claims.append(item)

    return valid_claims


async def agent_extract_claims_v6(text: str) -> list:
    """Extractor v6 prompt (based on v3 code path).
    Per claim returns 'claim', 'claim_kind', 'why_debatable', 'search_query'.
    claim_kind is 'contested' or 'factual'.
    Drops definitions, descriptions, hyperbole, attribution claims, and truisms."""
    system = (
        "You select claims worth challenging. Be strict: quality over quantity. "
        "Output JSON only."
    )
    user = f"""Analyze the following text and extract up to 4 claims worth challenging.

A claim qualifies ONLY if ALL of these are true:
- It asserts something about the world (a cause, prediction, ranking, recommendation, or statistic).
- It is NOT a definition, description of what something is or how it works, or the author's framing ("this article explains...").
- It is NOT hyperbole (rhetorical exaggeration or motivational statements) or attribution ("X said/believes Y").
- It is NOT a truism or obvious common knowledge ("good health habits lead to longer lives").
- A knowledgeable person or credible evidence could reasonably disagree or provide a strong alternative perspective.
- It is specific enough to search for.

Negative examples — DO NOT extract claims like these:
- "GEO focuses on whether AI-generated answers mention your business" (definition)
- "Superintelligence refers to AI that would outperform humans" (definition)
- "MCP gives AI apps a standard way to connect to tools" (description of how it works)
- "Understanding key terms puts you ahead of 90% of people" (hyperbole)
- "The intelligence explosion could be much closer than expected according to Geoffrey Hinton" (attribution)
- "Eating nutritious food improves physical health" (truism)

Skip: definitions, descriptions of functionality, hyperbole, attributions, truisms, generic advice, and meta-commentary about the article itself.
If fewer than 4 qualify, return fewer. If none qualify, return an empty list.

For each claim:
1. Rewrite it as a standalone sentence (no pronouns, no "the author says").
2. Set "claim_kind" to:
   - "contested": reasonable experts disagree on an empirical or evaluative question; forecasts count.
   - "factual": a checkable number or event.
   (If it is a definition, description, hyperbole, attribution, or truism, classify it as such so it can be discarded).
3. Write "why_debatable": one sentence explaining who could disagree and why.
4. Write "search_query" (5-10 words):
   - For "contested" claims: search_query MUST target credible criticism or the opposing side (e.g. "arguments against X", "critics of X", "debate over X").
   - For "factual" claims: use a neutral query aimed at checking or verifying the claim directly (as in v3).

Return JSON:
{{"claims": [{{"claim": "...", "claim_kind": "contested|factual", "why_debatable": "...", "search_query": "..."}}]}}

Text:
{text[:15000]}"""

    data = await _safe_llm_json([
        {"role": "system", "content": system},
        {"role": "user", "content": user}
    ])
    if data is None:
        return []

    raw_claims = data.get("claims", [])
    valid_claims = []
    for item in raw_claims:
        if not isinstance(item, dict):
            continue
        c_kind = str(item.get("claim_kind", "")).strip().lower()
        why_deb = str(item.get("why_debatable", "")).strip()

        # In code: keep only "contested" or "factual", drop definitions, descriptions, hyperbole, attributions, truisms, or empty why_debatable
        if c_kind not in ("contested", "factual"):
            continue
        if not why_deb:
            continue
        valid_claims.append(item)

    return valid_claims


async def agent_extract_claims(text: str, published_date: str = None, today_date: str = None) -> list:
    """Dispatcher: routes to v1, v2, v3, v4, v5, or v6 based on PROMPT_VERSION."""
    if PROMPT_VERSION == "v1":
        return await agent_extract_claims_v1(text)
    if PROMPT_VERSION == "v2":
        return await agent_extract_claims_v2(text)
    if PROMPT_VERSION == "v3":
        return await agent_extract_claims_v3(text)
    if PROMPT_VERSION == "v4":
        return await agent_extract_claims_v4(text)
    if PROMPT_VERSION == "v5":
        return await agent_extract_claims_v5(text, published_date=published_date, today_date=today_date)
    return await agent_extract_claims_v6(text)


# ── Counterpoint / RAG ───────────────────────────────────────────────────

async def agent_rag_and_counterpoint_v1(claim: str) -> dict:
    """Original v1 prompt. Searches Tavily with the raw claim text."""
    search_context = "No results found."
    tavily_count = 0
    try:
        response = await asyncio.to_thread(
            tavily_client.search, query=claim, search_depth="advanced", max_results=2, exclude_domains=EXCLUDE_DOMAINS
        )
        results = response.get('results', [])
        tavily_count = len(results)
        if results:
            context_list = [f"[Source: {r['url']}]\nContent: {r['content']}" for r in results]
            search_context = "\n\n".join(context_list)
    except: pass

    prompt = f"""
    Claim: "{claim}"
    Facts: {search_context}
    1. Write a balanced counterpoint.
    2. List 1-2 source URLs from the text.
    Return JSON: {{"counterpoint": "...", "sources": ["..."]}}
    """
    data = await _safe_llm_json(
        [{"role": "system", "content": "JSON only."}, {"role": "user", "content": prompt}],
        temperature=0.4
    )
    if data is None:
        return {"counterpoint": "Analysis failed.", "sources": [], "stance": "contested", "raw_agent_output": None, "tavily_count": tavily_count}
    return {
        "counterpoint": data.get("counterpoint", "Analysis failed."),
        "sources": data.get("sources", []),
        "stance": "contested",
        "raw_agent_output": data,
        "tavily_count": tavily_count,
    }


async def agent_rag_and_counterpoint_v2(claim: str, search_query: str) -> dict:
    """Improved v2 prompt. Uses the dedicated search_query for Tavily,
    grounds citations with numbered indices, and adds a stance field."""
    tavily_results = []
    try:
        response = await asyncio.to_thread(
            tavily_client.search, query=search_query, search_depth="advanced", max_results=2, exclude_domains=EXCLUDE_DOMAINS
        )
        tavily_results = response.get('results', [])
    except:
        pass

    if tavily_results:
        source_blocks = []
        for i, r in enumerate(tavily_results, start=1):
            source_blocks.append(f"[{i}] URL: {r['url']}\nContent: {r['content']}")
        sources_text = "\n\n".join(source_blocks)
    else:
        sources_text = "No sources found."

    system = (
        "You are a rigorous research assistant. Use ONLY the sources provided. "
        "Never invent facts, numbers, or URLs. Output JSON only."
    )
    user = f"""Claim: "{claim}"

Sources:
{sources_text}

Instructions:
1. State the strongest evidence-based challenge to the claim using ONLY these sources.
   Be specific: name the finding, number, example, or condition from a source that
   complicates the claim. Avoid generic reasoning ("it depends", "a balanced approach
   is needed") unless a source states a concrete finding behind it.
   Cite every statement as [1], [2], etc.
   2-3 sentences, max 70 words.
2. If the sources contain no real evidence against the claim, say so honestly.
3. Determine a stance:
   - "contested" — sources contain concrete evidence against or limiting the claim
   - "supported" — sources mostly back the claim
   - "insufficient" — sources are irrelevant or too thin to judge

Return JSON: {{"stance": "contested|supported|insufficient", "counterpoint": "... [1] ...", "cited": [1, 2]}}"""

    data = await _safe_llm_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2
    )

    if data is None:
        return {"stance": "insufficient", "counterpoint": "Analysis failed.", "sources": [], "raw_agent_output": None, "tavily_count": len(tavily_results)}

    cited_indices = data.get("cited", [])
    real_sources = []
    for idx in cited_indices:
        if isinstance(idx, int) and 1 <= idx <= len(tavily_results):
            url = tavily_results[idx - 1].get("url", "")
            if url and url not in real_sources:
                real_sources.append(url)

    return {
        "stance": data.get("stance", "insufficient"),
        "counterpoint": data.get("counterpoint", ""),
        "sources": real_sources,
        "raw_agent_output": data,
        "tavily_count": len(tavily_results),
    }


async def agent_rag_and_counterpoint_v3(claim: str, search_query: str) -> dict:
    """Counterpoint prompt v3. Adds the strict direct challenge rule and excludes domains."""
    tavily_results = []
    try:
        response = await asyncio.to_thread(
            tavily_client.search, query=search_query, search_depth="advanced", max_results=2, exclude_domains=EXCLUDE_DOMAINS
        )
        tavily_results = response.get('results', [])
    except:
        pass

    if tavily_results:
        source_blocks = []
        for i, r in enumerate(tavily_results, start=1):
            source_blocks.append(f"[{i}] URL: {r['url']}\nContent: {r['content']}")
        sources_text = "\n\n".join(source_blocks)
    else:
        sources_text = "No sources found."

    system = (
        "You are a rigorous research assistant. Use ONLY the sources provided. "
        "Never invent facts, numbers, or URLs. Output JSON only."
    )
    user = f"""Claim: "{claim}"

Sources:
{sources_text}

Instructions:
1. State the strongest evidence-based challenge to the claim using ONLY these sources.
   The challenge must directly address what the claim asserts. Do not argue against a different or nearby point. If you cannot, set stance to insufficient.
   Be specific: name the finding, number, example, or condition from a source that
   complicates the claim. Avoid generic reasoning ("it depends", "a balanced approach
   is needed") unless a source states a concrete finding behind it.
   Cite every statement as [1], [2], etc.
   2-3 sentences, max 70 words.
2. If the sources contain no real evidence against the claim, say so honestly.
3. Determine a stance:
   - "contested" — sources contain concrete evidence against or limiting the claim
   - "supported" — sources mostly back the claim
   - "insufficient" — sources are irrelevant or too thin to judge

Return JSON: {{"stance": "contested|supported|insufficient", "counterpoint": "... [1] ...", "cited": [1, 2]}}"""

    data = await _safe_llm_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2
    )

    if data is None:
        return {"stance": "insufficient", "counterpoint": "Analysis failed.", "sources": [], "raw_agent_output": None, "tavily_count": len(tavily_results)}

    cited_indices = data.get("cited", [])
    real_sources = []
    for idx in cited_indices:
        if isinstance(idx, int) and 1 <= idx <= len(tavily_results):
            url = tavily_results[idx - 1].get("url", "")
            if url and url not in real_sources:
                real_sources.append(url)

    return {
        "stance": data.get("stance", "insufficient"),
        "counterpoint": data.get("counterpoint", ""),
        "sources": real_sources,
        "raw_agent_output": data,
        "tavily_count": len(tavily_results),
    }


async def agent_rag_and_counterpoint_v4(claim: str, search_query: str) -> dict:
    """Counterpoint prompt v4. Adds evidence_measures and same_scope fields with strict inference rules.
    If same_scope is false, forces stance to 'insufficient'."""
    tavily_results = []
    try:
        response = await asyncio.to_thread(
            tavily_client.search, query=search_query, search_depth="advanced", max_results=2, exclude_domains=EXCLUDE_DOMAINS
        )
        tavily_results = response.get('results', [])
    except:
        pass

    if tavily_results:
        source_blocks = []
        for i, r in enumerate(tavily_results, start=1):
            source_blocks.append(f"[{i}] URL: {r['url']}\nContent: {r['content']}")
        sources_text = "\n\n".join(source_blocks)
    else:
        sources_text = "No sources found."

    system = (
        "You are a rigorous research assistant. Use ONLY the sources provided. "
        "Never invent facts, numbers, or URLs. Output JSON only."
    )
    user = f"""Claim: "{claim}"

Sources:
{sources_text}

Instructions:
1. State the strongest evidence-based challenge to the claim using ONLY these sources.
   The challenge must directly address what the claim asserts. Do not argue against a different or nearby point.
   Cite every statement as [1], [2], etc.
   2-3 sentences, max 70 words.
2. In "evidence_measures", write a short phrase stating what the cited sources actually measure (population, metric, timeframe).
3. In "same_scope", return true or false: does what the sources measure match what the claim asserts in population, metric AND timeframe?
   Rules:
   - Do not infer conclusions the source does not state (for example, do not turn 'X% plan to adopt' into 'the rest will not').
   - If the sources measure something different from the claim, set same_scope to false.
4. If the sources contain no real evidence against the claim, or if same_scope is false, say so honestly.
5. Determine a stance:
   - "contested" — sources contain concrete evidence against or limiting the claim (only if same_scope is true)
   - "supported" — sources mostly back the claim
   - "insufficient" — sources are irrelevant, measure a different scope, or are too thin to judge

Return JSON:
{{"evidence_measures": "...", "same_scope": true|false, "stance": "contested|supported|insufficient", "counterpoint": "... [1] ...", "cited": [1, 2]}}"""

    data = await _safe_llm_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2
    )

    if data is None:
        return {
            "stance": "insufficient",
            "counterpoint": "Analysis failed.",
            "sources": [],
            "evidence_measures": "N/A",
            "same_scope": False,
            "raw_agent_output": None,
            "tavily_count": len(tavily_results)
        }

    same_scope = bool(data.get("same_scope", False))
    stance = data.get("stance", "insufficient")
    # In code: if same_scope is false, treat the result as stance "insufficient"
    if not same_scope:
        stance = "insufficient"

    cited_indices = data.get("cited", [])
    real_sources = []
    for idx in cited_indices:
        if isinstance(idx, int) and 1 <= idx <= len(tavily_results):
            url = tavily_results[idx - 1].get("url", "")
            if url and url not in real_sources:
                real_sources.append(url)

    return {
        "stance": stance,
        "counterpoint": data.get("counterpoint", ""),
        "sources": real_sources,
        "evidence_measures": data.get("evidence_measures", ""),
        "same_scope": same_scope,
        "raw_agent_output": data,
        "tavily_count": len(tavily_results),
    }


# ── Evidence Agent v5 & Verifier v5 ──────────────────────────────────────

async def agent_evidence_v5(claim: str, settling_evidence: str, today_date: str, search_query: str) -> dict:
    """Evidence agent v5. Finds neutral sources, extracts verbatim key_quote, measures, and summary."""
    tavily_results = []
    try:
        response = await asyncio.to_thread(
            tavily_client.search, query=search_query, search_depth="advanced", max_results=2, exclude_domains=EXCLUDE_DOMAINS
        )
        tavily_results = response.get('results', [])
    except:
        pass

    if tavily_results:
        source_blocks = []
        for i, r in enumerate(tavily_results, start=1):
            source_blocks.append(f"[{i}] URL: {r['url']}\nContent: {r['content']}")
        sources_text = "\n\n".join(source_blocks)
    else:
        sources_text = "No sources found."

    system = (
        "You are a rigorous evidence extraction assistant. Use ONLY the sources provided. "
        "Never invent facts, numbers, or URLs. Output JSON only."
    )
    user = f"""Claim: "{claim}"
Settling Evidence Description: "{settling_evidence}"
Today's Date: {today_date}

Sources:
{sources_text}

Instructions:
1. Judge the claim as of today's date ({today_date}). If the claim makes a time-bound prediction whose deadline has passed, state whether it came true.
2. Select a "key_quote": must be copied VERBATIM from one source, maximum 25 words. Do not alter a single word.
3. State "source_index": the integer (1 or 2) of the source that contains key_quote.
4. In "measures": state what the quote actually measures (population, metric, timeframe).
   Rule: Never infer what a source does not state (for example, do not turn 'X% plan to adopt' into 'the rest will not').
   Rule: If a figure is self-reported by a company, explicitly note that it is self-reported.
   Rule: If sources measure something different from the claim or are too thin, set stance to "insufficient".
5. In "summary": state the core evidence in 2-3 sentences, maximum 70 words. Cite the source as [1] or [2].
6. Determine stance: "contested" | "supported" | "mixed" | "insufficient".

Return JSON:
{{"stance": "contested|supported|mixed|insufficient", "key_quote": "...", "source_index": 1, "measures": "...", "summary": "..."}}"""

    data = await _safe_llm_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2
    )

    if data is None:
        return {
            "stance": "insufficient",
            "key_quote": "",
            "source_index": 1,
            "measures": "N/A",
            "summary": "Analysis failed.",
            "tavily_results": tavily_results,
            "quote_valid": False,
            "raw_data": None
        }

    key_quote = str(data.get("key_quote", "")).strip()
    source_index = data.get("source_index", 1)

    # In code: check that key_quote appears in that source's Tavily content (normalize whitespace and case)
    quote_valid = False
    if isinstance(source_index, int) and 1 <= source_index <= len(tavily_results):
        source_content = tavily_results[source_index - 1].get("content", "")
        norm_quote = " ".join(key_quote.lower().split())
        norm_content = " ".join(source_content.lower().split())
        if norm_quote and norm_quote in norm_content:
            quote_valid = True

    stance = data.get("stance", "insufficient")
    if not quote_valid and stance != "insufficient":
        print(f"  ⚠️ Verbatim quote check failed! key_quote '{key_quote[:40]}...' not found in source [{source_index}]. Setting stance to insufficient.", flush=True)
        stance = "insufficient"

    return {
        "stance": stance,
        "key_quote": key_quote,
        "source_index": source_index,
        "measures": data.get("measures", ""),
        "summary": data.get("summary", ""),
        "tavily_results": tavily_results,
        "quote_valid": quote_valid,
        "raw_data": data
    }


async def agent_verify_v5(claim: str, settling_evidence: str, key_quote: str, measures: str) -> dict:
    """Verifier v5. Separate parallel call at temperature 0. Judges measure match and contradiction/support."""
    system = "You are a strict, objective verification judge. Output JSON only."
    prompt = f"""Claim: "{claim}"
Settling Evidence Description: "{settling_evidence}"
Key Quote from Source: "{key_quote}"
What the Quote Measures: "{measures}"

Questions:
1. Does the quote measure what the settling evidence/claim requires in population, metric, and timeframe?
   Answer: "yes" | "partly" | "no"
2. How does the quote relate to what the claim asserts?
   - "contradicts" (directly contradicts or disproves the claim)
   - "supports" (directly backs or proves the claim)
   - "unrelated" (measures something different, inconclusive, or does not directly bear on the claim)

Return JSON:
{{"same_measure": "yes|partly|no", "relation": "contradicts|supports|unrelated"}}"""

    data = await _safe_llm_json(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        temperature=0.0
    )
    if data is None:
        return {"same_measure": "no", "relation": "unrelated"}
    return data


async def agent_evidence_and_verify_v5(claim: str, settling_evidence: str, today_date: str, search_query: str) -> dict:
    """Orchestrates evidence agent v5 and verifier agent v5."""
    ev_data = await agent_evidence_v5(claim, settling_evidence, today_date, search_query)
    initial_stance = ev_data.get("stance", "insufficient")
    key_quote = ev_data.get("key_quote", "")
    measures = ev_data.get("measures", "")
    tavily_results = ev_data.get("tavily_results", [])
    quote_valid = ev_data.get("quote_valid", False)

    verifier_data = {"same_measure": "no", "relation": "unrelated"}
    if initial_stance != "insufficient" and key_quote and quote_valid:
        verifier_data = await agent_verify_v5(claim, settling_evidence, key_quote, measures)

    same_measure = str(verifier_data.get("same_measure", "no")).strip().lower()
    relation = str(verifier_data.get("relation", "unrelated")).strip().lower()

    # Code enforcement:
    # show "contested" only if relation is contradicts and same_measure is not no;
    # show "supported" only if relation is supports and same_measure is not no;
    # otherwise treat as insufficient.
    if relation == "contradicts" and same_measure != "no":
        final_stance = "contested"
    elif relation == "supports" and same_measure != "no":
        final_stance = "supported"
    elif initial_stance == "mixed" and same_measure != "no":
        final_stance = "mixed"
    else:
        final_stance = "insufficient"

    real_sources = [r.get("url", "") for r in tavily_results if r.get("url")]

    return {
        "stance": final_stance,
        "summary": ev_data.get("summary", ""),
        "counterpoint": ev_data.get("summary", ""),
        "sources": real_sources,
        "key_quote": key_quote,
        "measures": measures,
        "quote_valid": quote_valid,
        "verifier_output": verifier_data,
        "raw_agent_output": ev_data.get("raw_data"),
        "tavily_count": len(tavily_results),
    }


async def agent_perspective_v6(claim: str, search_query: str) -> dict:
    """Perspective agent v6 (for contested claims).
    Searches Tavily with search_query (targeting credible criticism/opposing side).
    Output JSON: consensus, who_argues, argument, cited.
    Temperature: 0.2.
    Maps cited to real URLs and drops the card if who_argues is empty."""
    tavily_results = []
    try:
        response = await asyncio.to_thread(
            tavily_client.search, query=search_query, search_depth="advanced", max_results=2, exclude_domains=EXCLUDE_DOMAINS
        )
        tavily_results = response.get('results', [])
    except:
        pass

    if tavily_results:
        source_blocks = []
        for i, r in enumerate(tavily_results, start=1):
            source_blocks.append(f"[{i}] URL: {r['url']}\nContent: {r['content']}")
        sources_text = "\n\n".join(source_blocks)
    else:
        sources_text = "No sources found."

    system = (
        "You are an objective research assistant presenting alternative perspectives. "
        "Use ONLY the sources provided. Never invent facts, numbers, or URLs. Output JSON only."
    )
    user = f"""Contested Claim: "{claim}"

Sources:
{sources_text}

Instructions:
1. Provide the strongest argument credible critics make against or limiting this claim using ONLY the provided sources.
2. In "who_argues": name the specific organization, author, or study that makes the argument. If the sources contain no relevant credible opposing argument, leave who_argues empty ("").
3. In "argument": summarize their counter-perspective. Include one concrete element (study, number, or case). Do NOT claim it proves the claim wrong. If the claim reflects broad expert consensus, say so and present the dissent as a minority view. Cite sources as [1] or [2]. Maximum 70 words.
4. In "consensus": state the broader state of expert opinion on this question:
   - "broad_agreement" — broad expert consensus agrees with the claim; the opposing view is minority dissent
   - "split" — legitimate experts or evidence are divided on the issue
   - "minority_dissent" — the claim itself represents a minority view; critics hold the dominant position
5. In "cited": list the integer numbers of the sources cited (e.g. [1]).

Return JSON:
{{"consensus": "broad_agreement|split|minority_dissent", "who_argues": "named organization, author or study", "argument": "...", "cited": [1]}}"""

    data = await _safe_llm_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2
    )

    if data is None:
        return {
            "claim_kind": "contested",
            "consensus": "split",
            "who_argues": "",
            "argument": "Analysis failed.",
            "counterpoint": "Analysis failed.",
            "sources": [],
            "dropped": True,
            "raw_agent_output": None,
            "tavily_count": len(tavily_results),
        }

    who_argues = str(data.get("who_argues", "")).strip()
    consensus = str(data.get("consensus", "split")).strip().lower()
    argument = str(data.get("argument", "")).strip()
    cited_indices = data.get("cited", [])

    real_sources = []
    for idx in cited_indices:
        if isinstance(idx, int) and 1 <= idx <= len(tavily_results):
            url = tavily_results[idx - 1].get("url", "")
            if url and url not in real_sources:
                real_sources.append(url)

    # In code: drop the card if who_argues is empty
    dropped = not bool(who_argues)

    return {
        "claim_kind": "contested",
        "consensus": consensus,
        "who_argues": who_argues,
        "argument": argument,
        "counterpoint": argument,
        "sources": real_sources,
        "dropped": dropped,
        "raw_agent_output": data,
        "tavily_count": len(tavily_results),
    }


async def agent_rag_and_counterpoint(claim: str, search_query: str = "", settling_evidence: str = "", today_date: str = "", claim_kind: str = "factual") -> dict:
    """Dispatcher: routes to v1, v2, v3, v4, v5, or v6 based on PROMPT_VERSION."""
    if PROMPT_VERSION == "v1":
        return await agent_rag_and_counterpoint_v1(claim)
    if PROMPT_VERSION == "v2":
        return await agent_rag_and_counterpoint_v2(claim, search_query or claim)
    if PROMPT_VERSION == "v3":
        return await agent_rag_and_counterpoint_v3(claim, search_query or claim)
    if PROMPT_VERSION == "v4":
        return await agent_rag_and_counterpoint_v4(claim, search_query or claim)
    if PROMPT_VERSION == "v5":
        return await agent_evidence_and_verify_v5(claim, settling_evidence or "", today_date or date.today().isoformat(), search_query or claim)
    # v6
    if claim_kind == "contested":
        return await agent_perspective_v6(claim, search_query or claim)
    return await agent_rag_and_counterpoint_v3(claim, search_query or claim)


# ── The Generator (Streaming Logic) ──────────────────────────────────────

async def analysis_generator(text: str, published_date: str = None, today_date: str = None):
    """Yields chunks of data as they become available."""
    if not today_date:
        today_date = date.today().isoformat()

    print(f"\n[Analysis Generator] Today: {today_date} | Published: {published_date}", flush=True)

    # Check cache
    cached = await asyncio.to_thread(get_cached_analysis, text)
    if cached:
        yield json.dumps({"status": "complete", "data": cached}) + "\n"
        return

    # Launch Summary and Claim Extraction concurrently
    yield json.dumps({"status": "progress", "message": " Analyzing content & extracting claims..."}) + "\n"
    summary_task = asyncio.create_task(agent_summarize(text))
    claims_task = asyncio.create_task(agent_extract_claims(text, published_date=published_date, today_date=today_date))

    # Await and stream summary as soon as it's ready
    summary = await summary_task
    yield json.dumps({"status": "partial_summary", "summary": summary}) + "\n"

    # Await claims (already running in parallel)
    claims_raw = await claims_task
    
    if not claims_raw:
        msg = "No strongly debatable claims found." if PROMPT_VERSION in ("v2", "v3", "v4", "v5", "v6") else "No claims found."
        yield json.dumps({"status": "no_claims", "message": msg}) + "\n"
        return

    total_claims = len(claims_raw)
    yield json.dumps({"status": "progress", "message": f" Verifying {total_claims} claims in parallel..."}) + "\n"

    # Process each claim: research facts with Tavily and verify evidence
    async def process_single_claim(claim_item):
        if isinstance(claim_item, dict):
            claim_text = claim_item.get("claim", "")
            claim_kind = claim_item.get("claim_kind", "factual")
            search_query = claim_item.get("search_query", claim_text)
            settling_evidence = claim_item.get("settling_evidence", "")
            time_bound = bool(claim_item.get("time_bound", False))
            deadline = claim_item.get("deadline", None)
        else:
            claim_text = str(claim_item)
            claim_kind = "factual"
            search_query = claim_text
            settling_evidence = ""
            time_bound = False
            deadline = None

        current_year = date.today().year
        deadline_passed = False
        if time_bound and deadline:
            try:
                deadline_year = int(str(deadline).strip()[:4])
                if deadline_year < current_year:
                    deadline_passed = True
            except:
                pass

        if PROMPT_VERSION == "v6":
            if claim_kind == "contested":
                res = await agent_perspective_v6(claim_text, search_query)
            else:
                res = await agent_rag_and_counterpoint_v3(claim_text, search_query)
                res["claim_kind"] = "factual"
        else:
            res = await agent_rag_and_counterpoint(claim_text, search_query, settling_evidence, today_date)

        stance = res.get("stance", "contested" if PROMPT_VERSION == "v1" else "insufficient")
        raw_output = res.get("raw_agent_output", res)
        tavily_count = res.get("tavily_count", 0)
        key_quote = res.get("key_quote", None)
        measures = res.get("measures", None)
        verifier_output = res.get("verifier_output", None)

        # Logging per claim:
        if PROMPT_VERSION == "v6":
            print(f"\n[Claim Processed (v6)]: {claim_text}", flush=True)
            print(f"  • Claim kind: {claim_kind}", flush=True)
            print(f"  • Search query: {search_query}", flush=True)
            print(f"  • Tavily results count: {tavily_count}", flush=True)
            raw_str = json.dumps(raw_output) if isinstance(raw_output, dict) else str(raw_output)
            print(f"  • Raw agent output: {raw_str}\n", flush=True)
        else:
            print(f"\n[Claim Processed]: {claim_text}", flush=True)
            print(f"  • Tavily results returned: {tavily_count}", flush=True)
            if key_quote is not None:
                print(f"  • Key quote: '{key_quote}' (quote_valid={res.get('quote_valid')})", flush=True)
            if measures is not None:
                print(f"  • Measures: {measures}", flush=True)
            if verifier_output is not None:
                print(f"  • Verifier Output: {json.dumps(verifier_output)}", flush=True)
            print(f"  • Parsed stance: {stance}\n", flush=True)

        return {
            "claim": claim_text,
            "claim_kind": claim_kind,
            "counterpoint": res.get("summary") or res.get("argument") or res.get("counterpoint", "Analysis failed."),
            "sources": res.get("sources", []),
            "stance": stance,
            "consensus": res.get("consensus", None),
            "who_argues": res.get("who_argues", None),
            "argument": res.get("argument", None),
            "dropped": res.get("dropped", False),
            "time_bound": time_bound,
            "deadline": deadline,
            "deadline_passed": deadline_passed,
        }

    claim_tasks = [asyncio.create_task(process_single_claim(c)) for c in claims_raw]
    
    full_claims_data = []
    completed_count = 0

    # Stream each claim to the frontend the instant it is ready!
    for coro in asyncio.as_completed(claim_tasks):
        claim_obj = await coro
        full_claims_data.append(claim_obj)
        completed_count += 1
        yield json.dumps({
            "status": "claim_ready",
            "claim": claim_obj,
            "progress": f"Verified {completed_count}/{total_claims} claims"
        }) + "\n"

    # Final payload
    final_data = {"summary": summary, "claims": full_claims_data}
    
    # Save to Cache
    await asyncio.to_thread(save_analysis_to_cache, text, final_data)
    
    yield json.dumps({"status": "complete", "data": final_data}) + "\n"



# ── Endpoints ────────────────────────────────────────────────────────────

@app.get("/health")
@app.get("/")
async def health_check():
    return {"status": "ok", "prompt_version": PROMPT_VERSION}

@app.post("/analyze")
async def analyze(req: Request):
    data = await req.json()
    raw_text = data.get("text", "")
    published_date = data.get("published_date", None)
    today_date = date.today().isoformat()
    
    # Handle YouTube
    if "youtube.com" in raw_text or "youtu.be" in raw_text:
        transcript = await asyncio.to_thread(get_transcript_text, raw_text)
        if transcript:
            raw_text = transcript
        else:
            return StreamingResponse(
                iter([json.dumps({"status": "error", "message": "No transcript found."}) + "\n"]),
                media_type="application/x-ndjson"
            )

    return StreamingResponse(
        analysis_generator(raw_text, published_date=published_date, today_date=today_date),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )