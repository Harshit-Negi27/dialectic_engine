# 📋 Architecture & Implementation Decisions Log

This document records architectural choices, workflow decisions, and optimizations made to the **Dialectic Engine** project, along with the rationale behind each change.

---

## Decision 1: Concurrent Agent Execution & Per-Claim Streaming
* **Date:** 2026-10-20
* **Status:** Implemented

### Context & Problem
Originally, the backend pipeline executed sequentially:
1. `agent_summarize` ran and blocked for ~2 seconds.
2. `agent_extract_claims` ran only after summarization completed, blocking for another ~2 seconds.
3. Although RAG tasks ran concurrently with `asyncio.gather`, the backend waited for **all** claims to finish before yielding anything to the user.
4. Total latency was high, and users stared at an "Analyzing..." spinner for 8–15 seconds before seeing any claim or counterpoint.
5. In addition, if a cache hit occurred, the summary was omitted in the UI due to a discrepancy between `partial_summary` and `complete` message handling.

### Changes Made
1. **Parallelized Pipeline Initialization (Backend):**
   - Launched `agent_summarize(text)` and `agent_extract_claims(text)` simultaneously using `asyncio.create_task`.
   - Both tasks process the same text and do not depend on each other.
   - Await `summary_task` to yield `partial_summary` as soon as it's available.
   - Then immediately await `claims_task` (which was already executing in the background).

2. **Per-Claim Streaming via `asyncio.as_completed` (Backend):**
   - Wrapped each claim's RAG and counterpoint generation into individual async tasks.
   - Used `asyncio.as_completed` to yield each claim (`status: "claim_ready"`) the instant Tavily search + OpenAI analysis finish for that claim.
   - Maintained full analysis collection for 7-day SQLite caching (`cache.db`).

3. **Incremental UI Rendering (Frontend `popup.js`):**
   - Added support for `status: "claim_ready"`. As each claim card arrives, it immediately renders to the DOM with dynamic progress text (e.g., "Verified 1/4 claims...").
   - Added robust cache hit handling so returning users get both the summary and all verified claims immediately without duplication.

### Impact
* Reduced initial time-to-first-claim from ~8–12s down to ~2–4s.
* Zero perceived lag: user can read the first claim & counter-argument while the remaining claims are still being processed.

---

## Decision 2: Anti-Buffering Headers & Visual Latency Telemetry
* **Date:** 2026-10-23
* **Status:** Implemented

### Context & Problem
During testing, responses on reverse proxies (such as Render or Nginx) were being buffered, flushing all output chunks in a single batch after 20 seconds. In addition, developers had no visual indicator showing whether the extension was communicating with the local development server or the cloud fallback, nor when each event fired.

### Changes Made
1. **Streaming HTTP Headers (`backend/main.py`):**
   - Added `X-Accel-Buffering: no` and `Cache-Control: no-cache` to FastAPI's `StreamingResponse`.
   - Instructs intermediate reverse proxies to flush NDJSON chunks immediately without holding them in 4KB/8KB buffers.
2. **Real-time Telemetry & DevTools Logging (`popup.js`):**
   - Display active backend source badge (`[Local: 127.0.0.1:8000]` vs `[Cloud: Render]`).
   - Added stopwatch badges on cards (`⏱️ 1.8s`, `⏱️ 3.4s`) and timestamped `console.log` logs for each received chunk.

---

## Decision 3: Dedicated Health Check Endpoints for Keep-Alive Pings
* **Date:** 2026-10-23
* **Status:** Implemented

### Context & Problem
Render's free tier spins down web services after 15 minutes of inactivity, causing ~50-second cold starts. Pinging `/` previously returned `404 Not Found`.

### Changes Made
- Added `@app.get("/health")` and `@app.get("/")` returning `{"status": "ok"}` in `backend/main.py`.
- Enables external cron services (e.g., cron-job.org) to ping the server every 10–14 minutes with negligible CPU and zero token consumption.

---

## Decision 4: Prompt Quality Rewrite with A/B Testing (v1 vs v2)
* **Date:** 2026-10-24
* **Status:** Implemented

### Context & Problem
The original (v1) prompts had several quality issues:
1. **Claim extraction was unfocused:** The prompt said "extract argumentative claims" but gave no criteria for what makes a claim debatable. The model often returned definitions, author framing ("this article explains..."), or common knowledge as "claims."
2. **Tavily searched for confirmation, not counter-evidence:** The raw claim text was used as the Tavily search query, which naturally returned results that *supported* the claim rather than challenging it.
3. **The model hallucinated URLs:** v1 told the model to "list 1-2 source URLs from the text" — but LLMs routinely mangle, truncate, or invent URLs. There was no validation.
4. **Counterpoints were generic:** The system prompt was just `"JSON only."` with no grounding instructions. The model defaulted to vague hedging ("a balanced approach is needed", "it depends on context") instead of citing specific findings.
5. **No error recovery:** If the model returned malformed JSON (which happens ~2-5% of the time with complex prompts), the entire agent silently returned an empty result.

### Changes Made

#### 1. `PROMPT_VERSION` env var for A/B testing
- Added `PROMPT_VERSION` (reads from `.env`, defaults to `"v2"`).
- All agent functions route through dispatcher functions: `agent_extract_claims()` → calls `_v1()` or `_v2()`, same for `agent_rag_and_counterpoint()`.
- This lets us run the exact same article through both prompt versions and compare output quality side by side.

#### 2. `agent_extract_claims_v2` — Stricter claim filtering + search queries
- **System prompt:** `"You select claims worth challenging. Be strict: quality over quantity. Output JSON only."`
- **User prompt changes:**
  - Explicit 4-part qualification criteria (asserts something about the world, not a definition, someone could disagree, specific enough to search).
  - Explicit skip list (definitions, common knowledge, generic advice, meta-commentary).
  - Each claim is rewritten as a standalone sentence (no pronouns).
  - Each claim gets a `search_query` (5-10 words) designed to find evidence *against* the claim.
- **Output format changed:** From `{"claims": ["string"]}` to `{"claims": [{"claim": "...", "search_query": "..."}]}`.
- **Why this matters:** The `search_query` is the single biggest quality lever. Instead of Tavily searching "AI agents can perform complex tasks" (which returns articles *about* AI agents), it now searches something like "limitations of AI agents vs traditional models" — which returns articles that actually challenge the claim.

#### 3. `agent_rag_and_counterpoint_v2` — Grounded citations + stance detection
- **Tavily search now uses `search_query`** instead of the raw claim text.
- **Numbered source format:** Tavily results are formatted as `[1] URL: ... Content: ...` and `[2] URL: ...` so the model can cite by index.
- **System prompt:** `"You are a rigorous research assistant. Use ONLY the sources provided. Never invent facts, numbers, or URLs. Output JSON only."`
- **User prompt changes:**
  - Explicit instruction to name findings, numbers, examples, or conditions from the sources.
  - Explicit ban on generic reasoning unless backed by a source.
  - Citation format: `[1]`, `[2]` inline.
  - Word limit: 2-3 sentences, max 70 words.
  - Stance field: `"contested"` / `"supported"` / `"insufficient"`.
- **Citation index → URL mapping in code:** The model returns `"cited": [1, 2]`. Our code maps those indices back to real Tavily URLs, validates index ranges, and deduplicates. The model never produces a URL itself.
- **Why this matters:** Eliminates URL hallucination entirely. The model can only reference sources we actually retrieved.

#### 4. `_safe_llm_json` — Centralized error handling with retry
- All LLM calls that expect JSON now go through `_safe_llm_json()`.
- On `json.JSONDecodeError`, `KeyError`, or `IndexError`: retries once automatically.
- On second failure or any other exception: returns `None`, and the caller provides a graceful fallback.

#### 5. `analysis_generator` updated for v2 claim format
- `process_single_claim` now handles both formats: v1 plain strings and v2 `{"claim": ..., "search_query": ...}` dicts.
- The `stance` field is included in the streamed `claim_ready` JSON when present (v2), but omitted for v1 — so the Chrome extension doesn't break regardless of version.
- Empty claims message is version-aware: v2 says "No strongly debatable claims found" (more honest), v1 says "No claims found."

### Files & Functions Changed
| File | Functions Changed |
|------|------------------|
| `backend/main.py` | Added: `_safe_llm_json`, `agent_extract_claims_v1`, `agent_extract_claims_v2`, `agent_rag_and_counterpoint_v1`, `agent_rag_and_counterpoint_v2`. Modified: `agent_extract_claims` (now dispatcher), `agent_rag_and_counterpoint` (now dispatcher), `process_single_claim`, `analysis_generator`, `health_check`. |
| `backend/.env.example` | Added `PROMPT_VERSION=v2` |

### How to A/B Test
```bash
# Run v2 (new prompts, default):
PROMPT_VERSION=v2 uvicorn backend.main:app --reload

# Run v1 (old prompts):
PROMPT_VERSION=v1 uvicorn backend.main:app --reload

# Check which version is active:
curl http://127.0.0.1:8000/health
# → {"status": "ok", "prompt_version": "v2"}
```
