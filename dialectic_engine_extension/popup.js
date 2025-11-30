document.getElementById("analyze").addEventListener("click", async () => {
  const loading = document.getElementById("loading");
  const output = document.getElementById("output");
  const statusDiv = document.getElementById("status") || document.createElement("div"); // Ensure status div exists
  
  // Setup UI
  if (!document.getElementById("status")) {
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
    
    //  Detect payload 
    let payloadText = "";
    if (tab.url.includes("youtube.com/watch") || tab.url.includes("youtu.be") || tab.url.includes("youtube.com/shorts")) {
      payloadText = tab.url;
    } else {
      const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => document.body.innerText,
      });
      payloadText = result.slice(0, 20000); 
    }

    //  Start the stream
    const response = await fetch("http://127.0.0.1:8000/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ text: payloadText }),
    });

    loading.style.display = "none"; 

    // Read the stream reader
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      
      // Process all complete lines
      buffer = lines.pop(); 

      for (const line of lines) {
        if (!line.trim()) continue;
        
        try {
          const msg = JSON.parse(line);
          
          
          if (msg.status === "progress") {
            statusDiv.innerText = msg.message;
          } 
          else if (msg.status === "partial_summary") {
            // Render Summary immediately
            const sumDiv = document.createElement('div');
            sumDiv.className = 'card summary';
            sumDiv.innerHTML = `<strong>Summary:</strong><br>${msg.summary}`;
            output.appendChild(sumDiv);
          }
          else if (msg.status === "complete") {
            statusDiv.innerText = "✅ Analysis Complete";
            // Render claims 
            renderClaims(msg.data.claims);
          }
          else if (msg.status === "error") {
            statusDiv.innerHTML = `<span style="color:red">${msg.message}</span>`;
          }

        } catch (e) {
          console.error("Stream parse error:", e);
        }
      }
    }

  } catch (error) {
    loading.style.display = "none";
    statusDiv.innerHTML = `<div style="color:red;">Error: ${error.message}</div>`;
  }
});

function renderClaims(claims) {
    const output = document.getElementById("output");
    if (!claims || claims.length === 0) {
        output.innerHTML += `<div>No claims found.</div>`;
        return;
    }
    
    claims.forEach((c, i) => {
        let sourcesHTML = "";
        if (c.sources && c.sources.length > 0) {
            const sourceLinks = c.sources.map(s => `<a href="${s}" target="_blank">${s}</a>`).join('<br>');
            sourcesHTML = `<div class='sources'><strong>Sources:</strong><br>${sourceLinks}</div>`;
        } else {
            sourcesHTML = `<div class='sources'><strong>Sources:</strong> None found.</div>`;
        }

        output.innerHTML += `
          <div class='card claim'>
            <strong>Claim ${i + 1}:</strong> ${c.claim}
            <div class='counterpoint'>⚖️ ${c.counterpoint}</div>
            ${sourcesHTML} 
          </div>
        `;
    });
}