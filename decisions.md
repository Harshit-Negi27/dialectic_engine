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
