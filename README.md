# 🧠 Dialectic Engine
> **An AI-powered "Bullshit Detector" for the modern web.**
> *Instantly extracts, verifies, and counter-argues claims from Articles and YouTube Videos.*

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.95%2B-green)](https://fastapi.tiangolo.com/)
[![OpenAI](https://img.shields.io/badge/AI-OpenAI%20GPT--4o-orange)](https://openai.com/)
[![License](https://img.shields.io/badge/License-MIT-lightgrey)](LICENSE)

## 🎥 Demo
**[Watch the 60-Second Demo Video Here]** *(Add your Loom/YouTube link here)*

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
    
    Verifier -->|Stream JSON| User