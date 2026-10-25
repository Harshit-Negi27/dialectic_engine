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
        func: () => document.body.innerText,
      });
      payloadText = result.slice(0, 20000);
    }

    const connectStart = Date.now();
    statusDiv.innerHTML = "Connecting to backend...";
    
    const { response, target } = await fetchBackendAnalysis(payloadText);
    const connectTime = ((Date.now() - connectStart) / 1000).toFixed(2);
    console.log(`[Dialectic] Connected to ${target} in ${connectTime}s`);

    loading.style.display = "none";

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let summaryRendered = false;
    let claimsRenderedCount = 0;
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
            statusDiv.innerHTML = `<span style="font-size: 11px; color: #666;">[${target} • ${elapsed}]</span><br>${msg.message}`;
          } else if (msg.status === "partial_summary") {
            if (!summaryRendered) {
              renderSummaryCard(msg.summary, elapsed);
              summaryRendered = true;
            }
          } else if (msg.status === "claim_ready") {
            statusDiv.innerHTML = `<span style="font-size: 11px; color: #666;">[${target} • ${elapsed}]</span><br>${msg.progress || "Verifying claims..."}`;
            renderSingleClaim(msg.claim, elapsed);
            claimsRenderedCount++;
          } else if (msg.status === "complete") {
            statusDiv.innerHTML = `<span style="font-size: 11px; color: #28a745; font-weight: bold;">[${target} • Finished in ${elapsed}]</span><br>Analysis Complete ✅`;
            // On cache hit or direct complete, render summary if missing
            if (!summaryRendered && msg.data && msg.data.summary) {
              renderSummaryCard(msg.data.summary, elapsed);
              summaryRendered = true;
            }
            // If no individual claims were streamed (e.g. cache hit or older backend), render them now
            if (claimsRenderedCount === 0 && msg.data && msg.data.claims) {
              renderClaims(msg.data.claims, elapsed);
              claimsRenderedCount = msg.data.claims.length;
            }
          } else if (msg.status === "no_claims") {
            statusDiv.innerHTML = `<span style="font-size: 11px; color: #28a745; font-weight: bold;">[${target} • Finished in ${elapsed}]</span><br>Analysis Complete ✅`;
            const output = document.getElementById("output");
            output.innerHTML += `<div class="card" style="color: #666; font-style: italic; padding: 12px; margin-top: 8px;">${msg.message}</div>`;
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

async function fetchBackendAnalysis(text) {
  // Try local backend first (if running during development), otherwise fallback to production Render
  try {
    const localRes = await fetch("http://127.0.0.1:8000/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ text }),
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
    body: JSON.stringify({ text }),
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

function renderSingleClaim(c, elapsed) {
  const output = document.getElementById("output");
  const badge = elapsed ? `<span style="float: right; font-size: 10px; color: #888; font-weight: normal;">⏱️ ${elapsed}</span>` : "";
  const claimDiv = document.createElement("div");
  claimDiv.className = "card claim";

  const stance = (c.stance || "contested").toLowerCase();

  if (stance === "supported") {
    claimDiv.innerHTML = `
      <strong>Claim:</strong>${badge} ${c.claim}
      <div style="font-size: 12px; color: #28a745; margin-top: 6px;">Sources broadly support this claim.</div>
    `;
  } else if (stance === "insufficient") {
    claimDiv.innerHTML = `
      <strong>Claim:</strong>${badge} ${c.claim}
      <div style="font-size: 12px; color: #888; font-style: italic; margin-top: 6px;">Couldn't find reliable evidence either way.</div>
    `;
  } else {
    // "contested" or unspecified (v1 fallback): show full counterpoint and sources
    let sourcesHTML = "";
    if (c.sources && c.sources.length > 0) {
      const sourceLinks = c.sources.map(s => `<a href="${s}" target="_blank" rel="noopener noreferrer">${s}</a>`).join("<br>");
      sourcesHTML = `<div class='sources'><strong>Sources:</strong><br>${sourceLinks}</div>`;
    } else {
      sourcesHTML = `<div class='sources'><strong>Sources:</strong> None found.</div>`;
    }

    claimDiv.innerHTML = `
      <strong>Claim:</strong>${badge} ${c.claim}
      <div class='counterpoint'>${c.counterpoint || "Analysis failed."}</div>
      ${sourcesHTML}
    `;
  }

  output.appendChild(claimDiv);
}

function renderClaims(claims, elapsed) {
  const output = document.getElementById("output");
  if (!claims || claims.length === 0) {
    output.innerHTML += `<div>No claims found.</div>`;
    return;
  }
  claims.forEach(c => renderSingleClaim(c, elapsed));
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