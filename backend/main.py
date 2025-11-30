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

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

#  Agents

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

async def agent_extract_claims(text: str) -> list:
    prompt = f"""
    Extract up to 5 distinct, argumentative claims. 
    Ignore definitions, rules, or facts.
    Return ONLY a valid JSON list of strings.
    Text: {text[:15000]}
    """
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a logic filter. Respond with JSON."},
                {"role": "user", "content": prompt}
            ]
        )
        data = json.loads(resp.choices[0].message.content)
        return data.get("claims", [])
    except: return []

async def agent_rag_and_counterpoint(claim: str) -> dict:
    search_context = "No results found."
    try:
        response = await asyncio.to_thread(
            tavily_client.search, query=claim, search_depth="advanced", max_results=2
        )
        results = response.get('results', [])
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
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini", temperature=0.4, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": "JSON only."}, {"role": "user", "content": prompt}]
        )
        return json.loads(resp.choices[0].message.content)
    except: return {"counterpoint": "Analysis failed.", "sources": []}

#  The Generator (Streaming Logic) 
async def analysis_generator(text: str):
    """Yields chunks of data as they become available."""
    
    # Check cache
    cached = await asyncio.to_thread(get_cached_analysis, text)
    if cached:
        yield json.dumps({"status": "complete", "data": cached}) + "\n"
        return

    #  Summary
    yield json.dumps({"status": "progress", "message": " Generating Summary..."}) + "\n"
    summary = await agent_summarize(text)
    # Send Summary IMMEDIATELY so user can read it
    yield json.dumps({"status": "partial_summary", "summary": summary}) + "\n"

    # Claims
    yield json.dumps({"status": "progress", "message": " Extracting Claims..."}) + "\n"
    claims = await agent_extract_claims(text)
    
    if not claims:
        yield json.dumps({"status": "error", "message": "No claims found."}) + "\n"
        return

    # RAG
    yield json.dumps({"status": "progress", "message": f" Verifying {len(claims)} Claims..."}) + "\n"
    
    full_claims_data = []
    
    
    tasks = [agent_rag_and_counterpoint(c) for c in claims]
    
    
    
    rag_results = await asyncio.gather(*tasks)
    
    for i, res in enumerate(rag_results):
        claim_obj = {
            "claim": claims[i],
            "counterpoint": res.get("counterpoint"),
            "sources": res.get("sources")
        }
        full_claims_data.append(claim_obj)

    # Final payload
    final_data = {"summary": summary, "claims": full_claims_data}
    
    # Save to Ccche
    await asyncio.to_thread(save_analysis_to_cache, text, final_data)
    
    yield json.dumps({"status": "complete", "data": final_data}) + "\n"


# Endpoint
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

    return StreamingResponse(analysis_generator(raw_text), media_type="application/x-ndjson")