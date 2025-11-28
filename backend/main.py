from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from tavily import TavilyClient
from openai import AsyncOpenAI
import os
import json
import asyncio
from dotenv import load_dotenv
from pathlib import Path
from backend.youtube_utils import get_transcript_text

# Import the Database functions
from backend.database import init_db, get_cached_analysis, save_analysis_to_cache

# Setup & configuration-
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

# Initialize the Database
init_db()

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

if not client.api_key:
    print("ERROR: OPENAI_API_KEY not found.")
    exit(1)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data Models
class Claim(BaseModel):
    claim: str
    counterpoint: str
    sources: list[str]

class AnalysisResponse(BaseModel):
    summary: str
    claims: list[Claim]

async def agent_summarize(text: str, client) -> str:
    print("Running Summarizer Agent")
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            temperature=0.1,
            messages=[
                {"role": "system", "content": "You are a neutral, academic summarization assistant."},
                {"role": "user", "content": f"Provide a concise, 2-3 sentence neutral summary of this article:\n\n{text}"}
            ]
        )
        return resp.choices[0].message.content
    except Exception as e:
        print(f"Summarizer Error: {e}")
        return "Summary could not be generated."

async def agent_extract_claims(text: str, client) -> list[str]:
    print(" Running Claim Extractor Agent ")
    prompt = f"""
    Extract the top 3-5 distinct, factual, or argumentative claims from the article.
    Return ONLY a valid JSON list of strings.
    Example: ["The sky is blue.", "Water boils at 100°C."]
    Article: {text}
    """
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a claim extraction robot. You only respond with a JSON list of strings."},
                {"role": "user", "content": prompt}
            ]
        )
        claims_list = json.loads(resp.choices[0].message.content)
        if isinstance(claims_list, dict):
            return claims_list.get("claims", []) 
        return claims_list
    except Exception as e:
        print(f"Claim Extractor Error: {e}")
        return []

async def agent_rag_and_counterpoint(claim: str, client) -> Claim:
    print(f" RAG AGENT: Processing '{claim[:30]}...' ")
    search_context = "No relevant search results found."
    
    try:
        response = await asyncio.to_thread(
            tavily_client.search,
            query=claim,
            search_depth="advanced", 
            max_results=3,
        )
        results = response.get('results', [])
        if results:
            context_list = [f"[Source: {r['url']}]\nContent: {r['content']}" for r in results]
            search_context = "\n\n".join(context_list)
    except Exception as e:
        print(f"    -> Tavily Search Error: {e}")

    prompt = f"""
    You are a generic critical analysis assistant.
    Claim: "{claim}"
    Verified Facts & Context:
    {search_context}
    Tasks:
    1. **Counterpoint**: Write a balanced, one-paragraph counterpoint based on Verified Facts.
    2. **Sources**: List 1-2 source URLs *only from the text provided above*.
    Return ONLY valid JSON:
    {{"counterpoint": "...", "sources": ["https://..."]}}
    """
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            temperature=0.4,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a critical thinker. You only respond with JSON."},
                {"role": "user", "content": prompt}
            ]
        )
        data = json.loads(resp.choices[0].message.content)
        return Claim(
            claim=claim,
            counterpoint=data.get("counterpoint", "No counterpoint generated."),
            sources=data.get("sources", [])
        )
    except Exception as e:
        print(f"RAG Agent Error: {e}")
        return Claim(claim=claim, counterpoint="Analysis failed.", sources=[])


# Coordinator Endpoint
@app.post("/analyze", response_model=AnalysisResponse)
async def analyze(req: Request):
    data = await req.json()
    text = data.get("text", "")

    # If the text looks like a YouTube URL, it will fetch the transcript
    if "youtube.com" in text or "youtu.be" in text:
        print(f" YouTube URL detected: {text}")
        transcript = await asyncio.to_thread(get_transcript_text, text)
        if transcript:
            print("Transcript fetched successfully!")
            text = transcript # Swap the URL for the full video text
        else:
            print("Could not fetch transcript. Analyzing raw URL (might fail).")


    if len(text) > 20000: text = text[:20000]

    # Check the cache first, in a thread so it doesn't block the async loop
    cached_result = await asyncio.to_thread(get_cached_analysis, text)
    if cached_result:
        return AnalysisResponse(**cached_result)

    # 2. If not in the cache, run agents 
    print("Summary & Claims")
    summary, claims_list = await asyncio.gather(
        agent_summarize(text, client),
        agent_extract_claims(text, client)
    )

    if not claims_list:
        return AnalysisResponse(summary=summary, claims=[])

    print(f"--- Phase 2: {len(claims_list)} RAG Agents ---")
    tasks = [agent_rag_and_counterpoint(c, client) for c in claims_list if c]
    results = await asyncio.gather(*tasks)
    processed_claims = [res for res in results if res]

    # Create the final object
    final_response = AnalysisResponse(summary=summary, claims=processed_claims)

    # Save to cache & coonvert pydantic model to dict for saving
    await asyncio.to_thread(save_analysis_to_cache, text, final_response.model_dump())

    print(" Analysis Complete & Saved to Cache")
    return final_response