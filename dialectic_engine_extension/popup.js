document.getElementById("analyze").addEventListener("click", async () => {
  const loading = document.getElementById("loading");
  const output = document.getElementById("output");
  loading.style.display = "block";
  output.innerHTML = "";

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => document.body.innerText,
    });

    const text = result.slice(0, 6000); 

    const response = await fetch("http://127.0.0.1:8000/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ text }),
    });

    const data = await response.json();
    loading.style.display = "none";

    if (data.summary) {
      output.innerHTML += `<div class='card summary'><strong>Summary:</strong><br>${data.summary}</div>`;
    }

    if (data.claims && data.claims.length > 0) {
      data.claims.forEach((c, i) => {
        
        // Create a variable to hold the sources HTML
        let sourcesHTML = "";
        if (c.sources && c.sources.length > 0) {
          // Loop over each source URL and create a link
          const sourceLinks = c.sources.map(sourceUrl => 
            `<a href="${sourceUrl}" target="_blank">${sourceUrl}</a>`
          ).join('<br>'); // Join links with a line break

          sourcesHTML = `<div class='sources'><strong>Sources:</strong><br>${sourceLinks}</div>`;
        } else {
          // Handle cases with no sources
          sourcesHTML = `<div class='sources'><strong>Sources:</strong> None found.</div>`;
        }
        

        // Add the new sourcesHTML to the output
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
