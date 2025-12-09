# 🧠 Dialectic Engine
> **An AI-powered "Bullshit Detector" for the modern web.**
> *Instantly extracts, verifies, and counter-argues claims from Articles and YouTube Videos.*

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.95%2B-green)](https://fastapi.tiangolo.com/)
[![OpenAI](https://img.shields.io/badge/AI-OpenAI%20GPT--4o-orange)](https://openai.com/)
[![License](https://img.shields.io/badge/License-MIT-lightgrey)](LICENSE)

## 🎥 Demo
<video src="https://github.com/user-attachments/assets/6deac345-ce4d-4ae3-a54b-8863cce51f46" controls="controls" style="max-width: 100%;">
</video>

![Dialectic Engine Screenshot](https://via.placeholder.com/800x400?text=App+Screenshot+Placeholder)

---

## 🚀 Overview
Dialectic Engine is a Chrome Extension + FastAPI backend designed to combat echo chambers. Unlike standard summarizers, it specifically hunts for **argumentative claims** and uses autonomous AI agents to find **contradictory evidence** from trusted sources in real-time.

It features a **multimodal pipeline** capable of processing both text articles and YouTube transcripts (including Shorts), even when captions are auto-generated or in foreign languages.

## ✨ Key Features
* **🕷️ Multimodal Analysis:** Seamlessly handles News Articles, Blog Posts, YouTube Videos, and YouTube Shorts.
* **⚡ Real-Time Streaming:** Uses Server-Sent Events (SSE) to stream insights instantly (0.5s time-to-first-byte) instead of waiting for full generation.
* **🛡️ Robust Anti-Blocking:** Implements a multi-layered fallback strategy (Local DOM -> Network Regex -> Piped API) to extract YouTube transcripts without triggering IP bans.
* **🧠 Intelligent Filtering:** Custom prompt engineering filters out definitions and facts, focusing only on *debatable* opinions and predictions.
* **💸 Cost Efficient:** Built-in SQLite caching layer with 7-day TTL (Time-To-Live) prevents redundant API calls for viral content.

## 🏗️ Architecture
The system follows a **parallelized agentic workflow** to minimize latency:

```mermaid
graph TD
    User[Chrome Extension] -->|URL/Text| Router{Content Type?}
    
    Router -->|Article| Scraper[Local DOM Scraper]
    Router -->|YouTube| Extractor[Transcript Pipeline]
    
    Extractor -->|Strategy A| Local[Local Regex]
    Extractor -->|Strategy B| Piped[Piped API Bypass]
    
    Scraper & Piped -->|Raw Text| Backend[FastAPI Server]
    
    Backend -->|Check| Cache[(SQLite DB)]
    
    Cache -->|Miss| Agents[Async Agents]
    
    subgraph Parallel Execution
        Agents --> Summary[Summarizer Agent]
        Agents --> Claims[Claim Extraction Agent]
    end
    
    Claims -->|For Each Claim| Verifier[Tavily Search Agent]
    
    Verifier -->|Stream JSON| User```

# 🛠️ Tech Stack

**Frontend:** Chrome Extension (Manifest V3), Vanilla JS, Server-Sent Events (SSE)  
**Backend:** Python, FastAPI, Uvicorn  
**AI & Search:** OpenAI (GPT-4o-mini), Tavily Search API  
**Data:** SQLite (Local Caching)  
**Deployment:** Render (Dockerized)

---

# ⚡ Installation (Local Dev)

## 1. Clone the Repository

```bash
git clone https://github.com/yourusername/dialectic-engine.git
cd dialectic-engine
```

## 2. Backend Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate      # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r backend/requirements.txt

# Set up environment variables
# Create a .env file in /backend with:
# OPENAI_API_KEY=sk-...
# TAVILY_API_KEY=tvly-...
```

## 3. Run the Server

```bash
uvicorn backend.main:app --reload
```

Server will start at:  
👉 http://127.0.0.1:8000

## 4. Load the Extension

1. Open Chrome and go to: `chrome://extensions`
2. Enable **Developer Mode** (top right)
3. Click **Load unpacked**
4. Select the `extension/` folder

---

# 🧩 Engineering Challenges Solved

## 1. The "YouTube Block"

YouTube aggressively blocks datacenter IPs (Render/AWS).  
Solved by implementing a **client-side extraction fallback** using the user's browser + Piped API, bypassing IP blocks entirely.

## 2. Concurrency

Reduced latency from **20s → ~10s** by using:

- `asyncio.gather`
- Streaming responses  
- Parallel summarization + claim extraction

## 3. State Management

YouTube often removes `ytInitialPlayerResponse` from the DOM.  
Fixed using a **“Spy Script” DOM injection technique** to capture ephemeral variables before they disappear.

---

# 🔮 Future Roadmap

- [ ] **Chat Mode:** Add follow-up chat interface using OpenAI Agents SDK  
- [ ] **Audio Analysis:** Support for Instagram Reels/TikTok (Whisper)  
- [ ] **User Accounts:** Sync history across devices  

---

# 🤝 Contributing

Open to contributions!  
Feel free to submit a Pull Request.

Built by **Harshit Negi**.
