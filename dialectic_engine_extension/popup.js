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

    statusDiv.innerText = "Analyzing...";
    
    const response = await fetch("https://dialectic-engine.onrender.com/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ text: payloadText }),
    });

    loading.style.display = "none";

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

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
          
          if (msg.status === "progress") {
            statusDiv.innerText = msg.message;
          } else if (msg.status === "partial_summary") {
            const sumDiv = document.createElement('div');
            sumDiv.className = 'card summary';
            sumDiv.innerHTML = `<strong>Summary:</strong><br>${msg.summary}`;
            output.appendChild(sumDiv);
          } else if (msg.status === "complete") {
            statusDiv.innerText = "Analysis Complete";
            renderClaims(msg.data.claims);
          } else if (msg.status === "error") {
            statusDiv.innerHTML = `<span style="color:red">${msg.message}</span>`;
          }
        } catch (e) {
          console.error(e);
        }
      }
    }

  } catch (error) {
    loading.style.display = "none";
    statusDiv.innerHTML = `<div style="color:red;">Error: ${error.message}</div>`;
  }
});

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

function renderClaims(claims) {
  const output = document.getElementById("output");
  if (!claims || claims.length === 0) {
    output.innerHTML += `<div>No claims found.</div>`;
    return;
  }
  
  claims.forEach((c) => {
    let sourcesHTML = "";
    if (c.sources && c.sources.length > 0) {
      const sourceLinks = c.sources.map(s => `<a href="${s}" target="_blank">${s}</a>`).join('<br>');
      sourcesHTML = `<div class='sources'><strong>Sources:</strong><br>${sourceLinks}</div>`;
    } else {
      sourcesHTML = `<div class='sources'><strong>Sources:</strong> None found.</div>`;
    }
    
    output.innerHTML += `
      <div class='card claim'>
        <strong>Claim:</strong> ${c.claim}
        <div class='counterpoint'>${c.counterpoint}</div>
        ${sourcesHTML}
      </div>
    `;
  });
}