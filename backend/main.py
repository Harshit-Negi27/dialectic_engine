# backend/main.py
from duckduckgo_search import DDGS
from pydantic import BaseModel
import json
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from duckduckgo_search import DDGS  
from openai import OpenAI     
import os
import json
from dotenv import load_dotenv
from pathlib import Path
import asyncio                      



load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY")) 
if not client.api_key:
    print("ERROR: OPENAI_API_KEY not found.")
    exit(1)

app = FastAPI()

class Claim(BaseModel):
    claim: str
    counterpoint: str
    sources: list[str]

class AnalysisResponse(BaseModel):
    summary: str
    claims: list[Claim]

# AGENT 1
def agent_summarize(text: str, client) -> str:
    print("--- Running Summarizer Agent ---")
    try:
        resp = client.chat.completions.create(
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

# AGENT 2
def agent_extract_claims(text: str, client) -> list[str]:
    print("--- Running Claim Extractor Agent ---")
    prompt = f"""
    Extract the top 3-5 distinct, factual, or argumentative claims from the article.
    Return ONLY a valid JSON list of strings.
    Example: ["The sky is blue.", "Water boils at 100°C."]

    Article:
    {text}
    """
    try:
        resp = client.chat.completions.create(
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

# AGENT 3 & 4:
def agent_rag_and_counterpoint(claim: str, client) -> dict:
    print(f"--- Running RAG/Counterpoint Agent for: {claim} ---")

    # RETRIEVAL
    search_results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(claim, max_results=4):
                search_results.append(f"[Source: {r['href']}]\nSnippet: {r['body']}\n")
        search_context = "\n".join(search_results)
    except Exception as e:
        print(f"DDGS Search Error: {e}")
        search_context = "Web search failed."

    prompt = f"""
    You are a critical analysis assistant.

    Claim: "{claim}"

    Search Results:
    {search_context}

    Tasks:
    1.  **Counterpoint**: Write a balanced, one-paragraph counterpoint or add context. Base it on the search results.
    2.  **Sources**: Provide 1-2 source URLs *only from the Search Results provided*. Do NOT invent URLs.

    Return ONLY valid JSON in this format:
    {{"counterpoint": "...", "sources": ["https://..."]}}
    """
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a fact-checking assistant. You only respond with JSON."},
                {"role": "user", "content": prompt}
            ]
        )
        data = json.loads(resp.choices[0].message.content)
        return {
            "counterpoint": data.get("counterpoint", "No counterpoint found."),
            "sources": data.get("sources", [])
        }
    except Exception as e:
        print(f"RAG Agent Error: {e}")
        return {"counterpoint": "Analysis failed.", "sources": []}

# COORDINATOR
@app.post("/analyze", response_model=AnalysisResponse)
async def analyze(req: Request):
    data = await req.json()
    text = data.get("text", "")

    if len(text) > 20000: # Truncate to save tokens
        text = text[:20000]

    # Run Summarizer
    summary = agent_summarize(text, client)

    # Run Claim Extractor
    claims_list = agent_extract_claims(text, client)

    processed_claims = []
    if not claims_list:
        return AnalysisResponse(summary=summary, claims=[])

    # Run RAG/Counterpoint Agent for each claim
    for claim_text in claims_list:
        if not claim_text: continue
        rag_result = agent_rag_and_counterpoint(claim_text, client)
        processed_claims.append(
            Claim(
                claim=claim_text,
                counterpoint=rag_result["counterpoint"],
                sources=rag_result["sources"]
            )
        )

    print("--- Analysis Complete ---")
    return AnalysisResponse(summary=summary, claims=processed_claims)