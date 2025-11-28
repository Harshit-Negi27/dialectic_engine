document.getElementById("analyze").addEventListener("click", async () => {
  const loading = document.getElementById("loading");
  const output = document.getElementById("output");
  loading.style.display = "block";
  output.innerHTML = "";

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    
    let payloadText = "";
    
    // --- UPDATED LOGIC: Check for Standard Video AND Shorts ---
    if (tab.url.includes("youtube.com/watch") || 
        tab.url.includes("youtu.be") || 
        tab.url.includes("youtube.com/shorts")) {
      
      console.log("🎥 YouTube Video/Short Detected. Sending URL...");
      // For YouTube, we send the URL itself so the backend can fetch the transcript
      payloadText = tab.url;
      
    } else {
      // For Articles, we scrape the visible text
      console.log("📄 Article Detected. Scraping text...");
      const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => document.body.innerText,
      });
      // Limit text size to avoid huge payloads
      payloadText = result.slice(0, 20000); 
    }

    // Call the Backend
    const response = await fetch("http://127.0.0.1:8000/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ text: payloadText }),
    });

    const data = await response.json();
    loading.style.display = "none";

    // --- RENDER RESULTS ---
    
    // 1. Summary
    if (data.summary) {
      output.innerHTML += `<div class='card summary'><strong>Summary:</strong><br>${data.summary}</div>`;
    }

    // 2. Claims & Counterpoints
    if (data.claims && data.claims.length > 0) {
      data.claims.forEach((c, i) => {
        
        // Build Sources HTML
        let sourcesHTML = "";
        if (c.sources && c.sources.length > 0) {
          const sourceLinks = c.sources.map(sourceUrl => 
            `<a href="${sourceUrl}" target="_blank">${sourceUrl}</a>`
          ).join('<br>'); 
          sourcesHTML = `<div class='sources'><strong>Sources:</strong><br>${sourceLinks}</div>`;
        } else {
          sourcesHTML = `<div class='sources'><strong>Sources:</strong> None found.</div>`;
        }
        
        // Add Card to UI
        output.innerHTML += `
          <div class='card claim'>
            <strong>Claim ${i + 1}:</strong> ${c.claim}
            <div class='counterpoint'>⚖️ ${c.counterpoint}</div>
            ${sourcesHTML} 
          </div>
        `;
      });
    } else {
      output.innerHTML += `<div>No claims found.</div>`;
    }
  } catch (error) {
    loading.style.display = "none";
    output.innerHTML = `<div style="color:red;">Error: ${error.message}</div>`;
    console.error("Error:", error);
  }
});