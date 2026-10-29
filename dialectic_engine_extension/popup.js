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
    
    const { response, target } = await fetchBackendAnalysis(payloadText, publishedDate);
    const connectTime = ((Date.now() - connectStart) / 1000).toFixed(2);
    console.log(`[Dialectic] Connected to ${target} in ${connectTime}s (Published date: ${publishedDate || "Unknown"})`);

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

async function fetchBackendAnalysis(text, publishedDate = null) {
  // Try local backend first (if running during development), otherwise fallback to production Render
  const payload = JSON.stringify({ text, published_date: publishedDate });
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

function getConsensusNote(consensus) {
  const c = String(consensus || "").trim().toLowerCase();
  if (c === "broad_agreement") {
    return "Consensus: Broad expert agreement with claim (opposing argument is a minority view).";
  }
  if (c === "minority_dissent") {
    return "Consensus: Claim is a minority view (critics hold the dominant expert position).";
  }
  if (c === "split") {
    return "Consensus: Legitimate debate with divided expert opinions.";
  }
  return `Consensus: ${consensus || "Split debate"}`;
}

function getOrCreateV6Section(id, titleHtml) {
  const output = document.getElementById("output");
  let sec = document.getElementById(id);
  if (!sec) {
    sec = document.createElement("div");
    sec.id = id;
    sec.style.marginTop = "12px";
    sec.innerHTML = titleHtml;
    const alsoChecked = document.getElementById("section-also-checked");
    if (alsoChecked) {
      output.insertBefore(sec, alsoChecked);
    } else {
      output.appendChild(sec);
    }
  }
  return sec;
}

function getOrCreateAlsoCheckedSection() {
  const output = document.getElementById("output");
  let sec = document.getElementById("section-also-checked");
  if (!sec) {
    sec = document.createElement("details");
    sec.id = "section-also-checked";
    sec.style.marginTop = "12px";
    sec.style.border = "1px solid #e1e4e8";
    sec.style.borderRadius = "6px";
    sec.style.padding = "8px 10px";
    sec.style.background = "#fafbfc";
    sec.innerHTML = `
      <summary id="summary-also-checked" style="cursor: pointer; font-weight: bold; font-size: 12px; color: #586069;">
        Also checked: 0 claims hold up or lack a strong source
      </summary>
      <div id="content-also-checked" style="margin-top: 8px;"></div>
    `;
    output.appendChild(sec);
  }
  return sec;
}

function addToAlsoChecked(c, reason) {
  getOrCreateAlsoCheckedSection();
  const content = document.getElementById("content-also-checked");
  const summaryEl = document.getElementById("summary-also-checked");

  const item = document.createElement("div");
  item.style.padding = "6px 0";
  item.style.borderBottom = "1px solid #eaecef";
  item.style.fontSize = "12px";
  item.innerHTML = `
    <strong>Claim:</strong> ${c.claim}
    <div style="color: #6a737d; font-style: italic; margin-top: 2px;">${reason}</div>
  `;
  content.appendChild(item);

  const count = content.children.length;
  summaryEl.innerText = `Also checked: ${count} claim${count === 1 ? '' : 's'} hold up or lack a strong source`;
}

function renderV6Claim(c, elapsed, badge, deadlineBadge) {
  const kind = String(c.claim_kind || "").toLowerCase();
  const sourcesHTML = formatSources(c.sources);

  if (kind === "contested") {
    if (c.dropped || !c.who_argues) {
      addToAlsoChecked(c, "No credible opposing argument found in sources.");
      return;
    }
    const section = getOrCreateV6Section("section-other-side", '<h3 style="font-size: 14px; margin: 6px 0; color: #0969da;">The other side</h3>');
    const card = document.createElement("div");
    card.className = "card claim";
    card.innerHTML = `
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
        <span style="font-size: 10px; background: #e8f4fd; color: #0969da; border: 1px solid #c8e1fb; border-radius: 3px; padding: 2px 6px; font-weight: 600;">Perspective, not a verdict</span>
        ${badge}
      </div>
      <div style="font-size: 11px; color: #57606a; margin-bottom: 6px; font-style: italic;">${getConsensusNote(c.consensus)}</div>
      <strong>Claim:</strong> ${c.claim}${deadlineBadge}
      <div style="font-size: 12px; font-weight: 600; color: #333; margin-top: 6px;">Argued by: ${c.who_argues}</div>
      <div class="counterpoint" style="margin-top: 4px;">${c.argument || c.counterpoint || ""}</div>
      ${sourcesHTML}
    `;
    section.appendChild(card);
    return;
  }

  // Factual claim
  const stance = String(c.stance || "").toLowerCase();
  if (stance === "contested") {
    const section = getOrCreateV6Section("section-challenged", '<h3 style="font-size: 14px; margin: 6px 0; color: #cf222e;">Challenged</h3>');
    const card = document.createElement("div");
    card.className = "card claim";
    card.innerHTML = `
      <strong>Claim:</strong>${badge} ${c.claim}${deadlineBadge}
      <div class="counterpoint">${c.counterpoint || "Evidence conflicts with claim."}</div>
      ${sourcesHTML}
    `;
    section.appendChild(card);
    return;
  }

  // Factual claim that holds up or lacks strong source
  const reason = stance === "supported" ? "Sources back this claim." : "Holds up or lack of strong opposing source.";
  addToAlsoChecked(c, reason);
}

function renderSingleClaim(c, elapsed) {
  const output = document.getElementById("output");
  const badge = elapsed ? `<span style="float: right; font-size: 10px; color: #888; font-weight: normal;">⏱️ ${elapsed}</span>` : "";
  const deadlineBadge = (c.time_bound && c.deadline_passed) 
    ? `<span style="font-size: 10px; color: #d9534f; background: #fff1f0; border: 1px solid #ffa39e; border-radius: 3px; padding: 1px 5px; margin-left: 6px; font-weight: 500;">Prediction - deadline passed</span>` 
    : "";

  if (c.claim_kind) {
    renderV6Claim(c, elapsed, badge, deadlineBadge);
    return;
  }

  // Fallback for v1-v5 claims without claim_kind
  const claimDiv = document.createElement("div");
  claimDiv.className = "card claim";

  const stance = (c.stance || "contested").toLowerCase();

  if (stance === "insufficient") {
    claimDiv.innerHTML = `
      <strong>Claim:</strong>${badge} ${c.claim}${deadlineBadge}
      <div style="font-size: 12px; color: #888; font-style: italic; margin-top: 6px;">Couldn't find reliable evidence either way.</div>
    `;
  } else {
    // contested, supported, mixed (and v1 fallback): show summary/counterpoint + sources
    const sourcesHTML = formatSources(c.sources);

    claimDiv.innerHTML = `
      <strong>Claim:</strong>${badge} ${c.claim}${deadlineBadge}
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