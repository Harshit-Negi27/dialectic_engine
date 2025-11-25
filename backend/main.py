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

# --- 1. Setup & Configuration ---
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

if not client.api_key:
    print("ERROR: OPENAI_API_KEY not found. Check your backend/.env file.")
    exit(1)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 2. Data Models ---
class Claim(BaseModel):
    claim: str
    counterpoint: str
    sources: list[str]

class AnalysisResponse(BaseModel):
    summary: str
    claims: list[Claim]

# --- 3. Agents ---

async def agent_summarize(text: str, client) -> str:
    print("--- Running Summarizer Agent ---")
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
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
    print("--- Running Claim Extractor Agent ---")
    prompt = f"""
    Extract the top 3-5 distinct, factual, or argumentative claims from the article.
    Return ONLY a valid JSON list of strings.
    Example: ["The sky is blue.", "Water boils at 100°C."]
    Article: {text}
    """
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
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

# --- AGENT 3 & 4: RAG & Counterpoint (Speed Optimized) ---
async def agent_rag_and_counterpoint(claim: str, client) -> Claim:
    # No more 'delay' parameter! Tavily handles concurrency well.
    print(f"--- RAG AGENT: Processing '{claim[:30]}...' ---")

    # Step 1: Search with Tavily
    search_context = "No relevant search results found."
    
    try:
        # We run Tavily in a thread to keep the event loop unblocked
        response = await asyncio.to_thread(
            tavily_client.search,
            query=claim,
            search_depth="basic",
            max_results=3,
        )
        
        # Extract the content
        results = response.get('results', [])
        if results:
            context_list = [f"[Source: {r['url']}]\nContent: {r['content']}" for r in results]
            search_context = "\n\n".join(context_list)
        
    except Exception as e:
        print(f"    -> Tavily Search Error: {e}")

    # Step 2: Generate Analysis
    prompt = f"""
    You are a generic critical analysis assistant.
    
    Claim: "{claim}"
    
    Verified Facts & Context:
    {search_context}
    
    Tasks:
    1.  **Counterpoint**: Write a balanced, one-paragraph counterpoint based on the Verified Facts.
    2.  **Sources**: List 1-2 source URLs *only from the text provided above*.
    3. Don't use while or While at the beginning of every counterpoint, keep changing the way of initiating the counterpoint so it does not look repetitive and boring.
    
    Return ONLY valid JSON:
    {{"counterpoint": "...", "sources": ["https://..."]}}
    """
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
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


# --- 4. Coordinator Endpoint (Double Parallelism) ---
@app.post("/analyze", response_model=AnalysisResponse)
async def analyze(req: Request):
    data = await req.json()
    text = data.get("text", "")

    if len(text) > 20000: 
        text = text[:20000]

    # --- SPEED UP 1: Run Summary & Claim Extraction TOGETHER ---
    print("--- Starting Phase 1: Summary & Extraction (Parallel) ---")
    summary_task = agent_summarize(text, client)
    claims_task = agent_extract_claims(text, client)
    
    # Wait for both to finish simultaneously
    summary, claims_list = await asyncio.gather(summary_task, claims_task)

    processed_claims = []
    if not claims_list:
        return AnalysisResponse(summary=summary, claims=[])

    # --- SPEED UP 2: Run All RAG Agents TOGETHER (No Delay) ---
    print(f"--- Starting Phase 2: {len(claims_list)} RAG Agents (Parallel) ---")
    tasks = []
    for claim_text in claims_list:
        if claim_text:
            # We removed the 'delay' argument
            tasks.append(agent_rag_and_counterpoint(claim_text, client))
    
    results = await asyncio.gather(*tasks)
    processed_claims = [res for res in results if res]

    print("--- Analysis Complete ---")
    return AnalysisResponse(summary=summary, claims=processed_claims)