// content.js
function createSidebar() {
  let existing = document.getElementById("dialectic-sidebar");
  if (existing) existing.remove();

  const sidebar = document.createElement("div");
  sidebar.id = "dialectic-sidebar";
  sidebar.innerHTML = `
    <div id="dialectic-header">Dialectic Engine ⚖️</div>
    <div id="dialectic-body">
      <p>🧠 Analyzing claims... please wait</p>
    </div>
  `;
  document.body.appendChild(sidebar);
}

function updateSidebar(contentHTML) {
  const body = document.getElementById("dialectic-body");
  if (body) body.innerHTML = contentHTML;
}

async function fetchAnalysis(articleText) {
  updateSidebar("<p>⏳ Analyzing article...</p>");
  try {
    const response = await fetch("http://127.0.0.1:8000/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: articleText })
    });
    const data = await response.json();

    if (data.summary) {
      let html = `<div class="section"><h3>📝 Summary</h3><p>${data.summary}</p></div>`;
      html += `<div class="section"><h3>🎯 Key Claims & Counterpoints</h3>`;
      data.claims.forEach((item, i) => {
        html += `
          <div class="claim-card">
            <strong>Claim ${i + 1}:</strong> ${item.claim}<br>
            <em>Counterpoint:</em> ${item.counterpoint}
          </div>`;
      });
      html += `</div>`;
      updateSidebar(html);
    } else {
      updateSidebar("<p>⚠️ Analysis failed — please try again.</p>");
    }
  } catch (err) {
    updateSidebar(`<p>❌ Error: ${err.message}</p>`);
  }
}

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "ANALYZE_PAGE") {
    createSidebar();
    const articleText = document.body.innerText.slice(0, 7000); // limit size
    fetchAnalysis(articleText);
  }
});
