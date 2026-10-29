document.getElementById("analyze").addEventListener("click", async () => {
  const loading = document.getElementById("loading");
  const output = document.getElementById("output");
  let statusDiv = document.getElementById("status");

  if (!statusDiv) {
    statusDiv = document.createElement("div");
    statusDiv.id = "status";
    statusDiv.style.color = "#666";
    statusDiv.style.marginBottom = "10px";
    loading.parentNode.insertBefore(statusDiv, loading.nextSibling);
  }

  loading.style.display = "block";
  statusDiv.innerText = "Connecting...";
  output.innerHTML = "";

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    let payloadText = "";

    if (tab.url.includes("youtube.com") || tab.url.includes("youtu.be")) {
      try {
        const response = await chrome.tabs.sendMessage(tab.id, { action: "GET_YOUTUBE_DATA" });

        if (response && response.transcript) {
          payloadText = response.transcript;
        } else {
          throw new Error("Local extraction failed");
        }
      } catch (e) {
        try {
          const videoId = extractVideoId(tab.url);
          const pipedTranscript = await fetchTranscriptFromPiped(videoId);

          if (pipedTranscript) {
            payloadText = pipedTranscript;
          } else {
            throw new Error("API failed");
          }
        } catch (apiError) {
          payloadText = `[TRANSCRIPT MISSING] Title: ${tab.title}`;
        }
      }
    } else {
      const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          let pubDate = null;
          // 1. meta article:published_time
          const metaSelectors = [
            'meta[property="article:published_time"]',
            'meta[name="article:published_time"]',
            'meta[name="publication_date"]',
            'meta[name="pubdate"]',
            'meta[name="date"]',
            'meta[property="og:published_time"]'
          ];
          for (const sel of metaSelectors) {
            const el = document.querySelector(sel);
            if (el && el.content) { pubDate = el.content; break; }
          }
          // 2. JSON-LD datePublished
          if (!pubDate) {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            for (const s of scripts) {
              try {
                const data = JSON.parse(s.innerText);
                const findDate = (obj) => {
                  if (!obj || typeof obj !== 'object') return null;
                  if (obj.datePublished) return obj.datePublished;
                  if (Array.isArray(obj)) {
                    for (const item of obj) {
                      const d = findDate(item);
                      if (d) return d;
                    }
                  }
                  if (obj['@graph'] && Array.isArray(obj['@graph'])) {
                    for (const item of obj['@graph']) {
                      const d = findDate(item);
                      if (d) return d;
                    }
                  }
                  return null;
                };
                const d = findDate(data);
                if (d) { pubDate = d; break; }
              } catch (e) {}
            }
          }
          // 3. <time> tag
          if (!pubDate) {
            const timeTag = document.querySelector('time[datetime]') || document.querySelector('time');
            if (timeTag) {
              pubDate = timeTag.getAttribute('datetime') || timeTag.innerText.trim();
            }
          }
          return {
            text: document.body.innerText,
            published_date: pubDate || null
          };
        },
      });
      payloadText = result.text.slice(0, 20000);
      publishedDate = result.published_date;
    }

    const connectStart = Date.now();
    statusDiv.innerHTML = "Connecting to backend...";
    
    const pageUrl = tab.url || "";
    const { response, target } = await fetchBackendAnalysis(payloadText, publishedDate, pageUrl);
    const connectTime = ((Date.now() - connectStart) / 1000).toFixed(2);
    console.log(`[Dialectic] Connected to ${target} in ${connectTime}s (Published date: ${publishedDate || "Unknown"}, Page: ${pageUrl})`);

    loading.style.display = "none";

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let summaryRendered = false;
    let counterpointsCount = 0;
    let totalClaims = 0;
    const streamStart = Date.now();
    const getElapsed = () => ((Date.now() - streamStart) / 1000).toFixed(2) + "s";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const msg = JSON.parse(line);
          const elapsed = getElapsed();
          console.log(`[Dialectic @ ${elapsed}] Chunk received:`, msg);
          
          if (msg.status === "progress") {
            if (msg.total_claims) {
              totalClaims = msg.total_claims;
            }
            statusDiv.innerHTML = `<span style="font-size: 11px; color: #666;">[${target} • ${elapsed}]</span><br>${msg.message}`;
          } else if (msg.status === "partial_summary") {
            if (!summaryRendered) {
              renderSummaryCard(msg.summary, elapsed);
              summaryRendered = true;
            }
          } else if (msg.status === "claim_ready") {
            const c = msg.claim;
            const isDisplayable = c.show_card ?? (c.stance === "contested" && c.sources && c.sources.length > 0);
            if (isDisplayable && counterpointsCount < 5) {
              renderCounterpointCard(c);
              counterpointsCount++;
            }
            statusDiv.innerHTML = `<span style="font-size: 11px; color: #666;">[${target} • ${elapsed}]</span><br>Checking ${totalClaims || 'claims'}...`;
          } else if (msg.status === "complete") {
            // On cache hit or direct complete, render summary if missing
            if (!summaryRendered && msg.data && msg.data.summary) {
              renderSummaryCard(msg.data.summary, elapsed);
              summaryRendered = true;
            }
            // If no individual claims were streamed (e.g. cache hit), render displayable claims now
            if (counterpointsCount === 0 && msg.data && msg.data.claims) {
              for (const c of msg.data.claims) {
                const isDisplayable = c.show_card ?? (c.stance === "contested" && c.sources && c.sources.length > 0);
                if (isDisplayable && counterpointsCount < 5) {
                  renderCounterpointCard(c);
                  counterpointsCount++;
                }
              }
            }
            statusDiv.innerHTML = `<span style="font-size: 11px; color: #28a745; font-weight: bold;">[${target} • Finished in ${elapsed}]</span><br>Done: ${counterpointsCount} counterpoints found`;
            if (counterpointsCount === 0) {
              const output = document.getElementById("output");
              output.innerHTML += `<div class="card" style="color: #666; font-style: italic; padding: 12px; margin-top: 8px;">No strongly challengeable claims found in this article.</div>`;
            }
          } else if (msg.status === "no_claims") {
            statusDiv.innerHTML = `<span style="font-size: 11px; color: #28a745; font-weight: bold;">[${target} • Finished in ${elapsed}]</span><br>Done: 0 counterpoints found`;
            const output = document.getElementById("output");
            output.innerHTML += `<div class="card" style="color: #666; font-style: italic; padding: 12px; margin-top: 8px;">No strongly challengeable claims found in this article.</div>`;
          } else if (msg.status === "error") {
            statusDiv.innerHTML = `<span style="color:red">[${target}] Error: ${msg.message}</span>`;
          }
        } catch (e) {
          console.error("Failed to parse stream chunk:", e);
        }
      }
    }

  } catch (error) {
    loading.style.display = "none";
    statusDiv.innerHTML = `<div style="color:red;">Error: ${error.message}</div>`;
  }
});

async function fetchBackendAnalysis(text, publishedDate = null, pageUrl = null) {
  // Try local backend first (if running during development), otherwise fallback to production Render
  const payload = JSON.stringify({ text, published_date: publishedDate, page_url: pageUrl });
  try {
    const localRes = await fetch("http://127.0.0.1:8000/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: payload,
    });
    if (localRes.ok) {
      return { response: localRes, target: "Local (127.0.0.1:8000)" };
    }
  } catch (e) {
    console.warn("[Dialectic] Local backend not reachable on :8000, falling back to Render...");
  }

  const renderRes = await fetch("https://dialectic-engine-pqdo.onrender.com/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: payload,
  });
  return { response: renderRes, target: "Cloud (Render)" };
}

function renderSummaryCard(summaryText, elapsed) {
  const output = document.getElementById("output");
  const sumDiv = document.createElement("div");
  sumDiv.className = "card summary";
  const badge = elapsed ? `<span style="float: right; font-size: 10px; color: #888; font-weight: normal;">⏱️ ${elapsed}</span>` : "";
  sumDiv.innerHTML = `<strong>Summary:</strong>${badge}<br>${summaryText}`;
  output.appendChild(sumDiv);
}

function formatSources(sources) {
  if (sources && sources.length > 0) {
    const sourceLinks = sources.map(s => `<a href="${s}" target="_blank" rel="noopener noreferrer">${s}</a>`).join("<br>");
    return `<div class='sources'><strong>Sources:</strong><br>${sourceLinks}</div>`;
  }
  return `<div class='sources'><strong>Sources:</strong> None found.</div>`;
}

function renderCounterpointCard(c) {
  const output = document.getElementById("output");
  const card = document.createElement("div");
  card.className = "card claim";
  const sourcesHTML = formatSources(c.sources);
  const counterpointText = c.argument || c.counterpoint || "Analysis failed.";

  card.innerHTML = `
    <strong>Claim:</strong> ${c.claim}
    <div class="counterpoint">${counterpointText}</div>
    ${sourcesHTML}
  `;
  output.appendChild(card);
}

function extractVideoId(url) {
  const urlObj = new URL(url);
  if (urlObj.hostname === 'youtu.be') return urlObj.pathname.slice(1);
  if (urlObj.pathname.includes('/shorts/')) return urlObj.pathname.split('/')[2];
  return urlObj.searchParams.get("v");
}

async function fetchTranscriptFromPiped(videoId) {
  try {
    const res = await fetch(`https://pipedapi.kavin.rocks/streams/${videoId}`);
    const data = await res.json();
    
    const subtitles = data.subtitles;
    if (!subtitles || subtitles.length === 0) return null;
    
    const enSub = subtitles.find(s => s.code.startsWith('en')) || subtitles[0];
    const textRes = await fetch(enSub.url);
    const rawText = await textRes.text();
    
    const lines = rawText.split('\n');
    const cleanLines = lines.filter(line => {
      return !line.includes('-->') && line.trim() !== '' && !line.match(/^\d+$/);
    });
    
    return [...new Set(cleanLines)].join(' ');
  } catch (e) {
    return null;
  }
}