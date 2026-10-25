from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from tavily import TavilyClient
from openai import AsyncOpenAI
import os
import json
import asyncio
from dotenv import load_dotenv
from pathlib import Path

from backend.database import init_db, get_cached_analysis, save_analysis_to_cache
from backend.youtube_utils import get_transcript_text

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)
init_db()

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

# "v1" = original prompts, "v2" = improved prompts, "v3" = strict claim selection & direct challenge (default)
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v3")

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


async def agent_extract_claims(text: str) -> list:
    """Dispatcher: routes to v1, v2, or v3 based on PROMPT_VERSION."""
    if PROMPT_VERSION == "v1":
        return await agent_extract_claims_v1(text)
    if PROMPT_VERSION == "v2":
        return await agent_extract_claims_v2(text)
    return await agent_extract_claims_v3(text)


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


async def agent_rag_and_counterpoint(claim: str, search_query: str = "") -> dict:
    """Dispatcher: routes to v1, v2, or v3 based on PROMPT_VERSION."""
    if PROMPT_VERSION == "v1":
        return await agent_rag_and_counterpoint_v1(claim)
    if PROMPT_VERSION == "v2":
        return await agent_rag_and_counterpoint_v2(claim, search_query or claim)
    return await agent_rag_and_counterpoint_v3(claim, search_query or claim)


# ── The Generator (Streaming Logic) ──────────────────────────────────────

async def analysis_generator(text: str):
    """Yields chunks of data as they become available."""
    
    # Check cache
    cached = await asyncio.to_thread(get_cached_analysis, text)
    if cached:
        yield json.dumps({"status": "complete", "data": cached}) + "\n"
        return

    # Launch Summary and Claim Extraction concurrently
    yield json.dumps({"status": "progress", "message": " Analyzing content & extracting claims..."}) + "\n"
    summary_task = asyncio.create_task(agent_summarize(text))
    claims_task = asyncio.create_task(agent_extract_claims(text))

    # Await and stream summary as soon as it's ready
    summary = await summary_task
    yield json.dumps({"status": "partial_summary", "summary": summary}) + "\n"

    # Await claims (already running in parallel)
    claims_raw = await claims_task
    
    if not claims_raw:
        msg = "No strongly debatable claims found." if PROMPT_VERSION in ("v2", "v3") else "No claims found."
        yield json.dumps({"status": "no_claims", "message": msg}) + "\n"
        return

    total_claims = len(claims_raw)
    yield json.dumps({"status": "progress", "message": f" Verifying {total_claims} claims in parallel..."}) + "\n"

    # Process each claim: research facts with Tavily and generate counterpoint with OpenAI
    async def process_single_claim(claim_item):
        # v2/v3 return dicts with {"claim": str, "search_query": str, ...}; v1 returns plain strings
        if isinstance(claim_item, dict):
            claim_text = claim_item.get("claim", "")
            search_query = claim_item.get("search_query", claim_text)
        else:
            claim_text = str(claim_item)
            search_query = claim_text

        res = await agent_rag_and_counterpoint(claim_text, search_query)
        stance = res.get("stance", "contested" if PROMPT_VERSION == "v1" else "insufficient")
        raw_output = res.get("raw_agent_output", res)
        tavily_count = res.get("tavily_count", 0)

        # Logging to server console:
        print(f"\n[Claim Processed]: {claim_text}", flush=True)
        print(f"  • Tavily results returned: {tavily_count}", flush=True)
        print(f"  • Raw counterpoint output: {json.dumps(raw_output) if isinstance(raw_output, dict) else raw_output}", flush=True)
        print(f"  • Parsed stance: {stance}\n", flush=True)

        return {
            "claim": claim_text,
            "counterpoint": res.get("counterpoint", "Analysis failed."),
            "sources": res.get("sources", []),
            "stance": stance,
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
        analysis_generator(raw_text),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )