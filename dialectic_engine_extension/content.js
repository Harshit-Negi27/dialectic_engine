async function findCaptionUrlInSource() {
  try {
    const response = await fetch(window.location.href);
    const html = await response.text();
    const regex = /"baseUrl":"(https:\/\/www\.youtube\.com\/api\/timedtext[^"]+)"/g;
    
    let match;
    const urls = [];

    while ((match = regex.exec(html)) !== null) {
      let url = match[1].replace(/\\u0026/g, "&").replace(/\\/g, "");
      urls.push(url);
    }

    if (urls.length === 0) return null;

    const englishUrl = urls.find(u => u.includes("lang=en"));
    return englishUrl || urls[0];

  } catch (e) {
    return null;
  }
}

async function fetchTranscript(url) {
  try {
    const response = await fetch(url);
    const xmlText = await response.text();
    const parser = new DOMParser();
    const xmlDoc = parser.parseFromString(xmlText, "text/xml");
    const texts = xmlDoc.getElementsByTagName("text");
    
    let fullText = "";
    for (let i = 0; i < texts.length; i++) {
      fullText += texts[i].textContent.replace(/&#39;/g, "'").replace(/&quot;/g, '"') + " ";
    }
    return fullText;
  } catch (e) {
    return null;
  }
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "GET_YOUTUBE_DATA") {
    (async () => {
      const captionUrl = await findCaptionUrlInSource();
      
      if (captionUrl) {
        const transcript = await fetchTranscript(captionUrl);
        sendResponse({ transcript: transcript });
      } else {
        const pageText = document.title + " " + (document.querySelector('#description')?.innerText || "");
        sendResponse({ transcript: null, pageText: pageText.slice(0, 5000) });
      }
    })();
    return true; 
  }
});