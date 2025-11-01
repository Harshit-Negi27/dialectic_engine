// Listens for when the user clicks the extension's toolbar icon
chrome.action.onClicked.addListener((tab) => {
  // Sends a message to the content script in the active tab
  chrome.tabs.sendMessage(tab.id, { type: "ANALYZE_PAGE" });
});


// Listens for messages from other parts of the extension (like content.js)
chrome.runtime.onMessage.addListener(async (message, sender, sendResponse) => {
  // Check if the message is the page text from the content script
  if (message.type === "PAGE_TEXT") {
    console.log("📄 Received text from content script:", message.data.slice(0, 200));

    try {
      // Send the text to your backend
      const response = await fetch("http://127.0.0.1:8000/analyze", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ text: message.data }),
      });

      if (!response.ok) {
        // Handle bad responses
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const result = await response.json();
      console.log("✅ Backend response:", result);

      // Send the analysis result back to the sender (popup.js or content.js)
      sendResponse({ success: true, data: result });

    } catch (error) {
      console.error("❌ Error contacting backend:", error);
      sendResponse({ success: false, error: error.message });
    }
  }

  // Let Chrome know we'll respond asynchronously (required for async sendResponse)
  return true;
});

