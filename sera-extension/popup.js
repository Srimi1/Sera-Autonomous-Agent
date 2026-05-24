// Sera browser extension — popup controller (MV3)
const STATUS = document.getElementById("status");
const BTN    = document.getElementById("ingest-btn");
const TOKEN  = document.getElementById("token-input");
const SERVER = document.getElementById("server-input");

// Restore saved settings
chrome.storage.local.get(["seraToken", "seraServer"], (data) => {
  if (data.seraToken)  TOKEN.value  = data.seraToken;
  if (data.seraServer) SERVER.value = data.seraServer;
});

function setStatus(msg, cls = "") {
  STATUS.textContent = msg;
  STATUS.className = cls;
}

BTN.addEventListener("click", async () => {
  const token  = TOKEN.value.trim();
  const server = SERVER.value.trim().replace(/\/$/, "");
  if (!token)  { setStatus("Paste your bearer token.", "err"); return; }
  if (!server) { setStatus("Server address is required.", "err"); return; }

  // Save settings
  chrome.storage.local.set({ seraToken: token, seraServer: server });

  BTN.disabled = true;
  setStatus("Extracting page content…");

  let tab;
  try {
    [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  } catch (e) {
    setStatus("Cannot query active tab: " + e.message, "err");
    BTN.disabled = false;
    return;
  }

  let result;
  try {
    [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => ({
        url:     location.href,
        title:   document.title,
        content: document.body?.innerText?.slice(0, 32_000) ?? "",
      }),
    });
  } catch (e) {
    setStatus("Cannot read page: " + e.message, "err");
    BTN.disabled = false;
    return;
  }

  const { url, title, content } = result.result;
  if (!content.trim()) {
    setStatus("Page has no readable text.", "err");
    BTN.disabled = false;
    return;
  }

  setStatus("Sending to Sera…");
  try {
    const resp = await fetch(`${server}/v1/ingest`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${token}`,
      },
      body: JSON.stringify({ url, title, content }),
    });
    const body = await resp.json();
    if (resp.ok && body.ok) {
      setStatus(`Ingested ✓ (chunk #${body.chunk_id})`, "ok");
    } else {
      setStatus(`Error ${resp.status}: ${body.error ?? "unknown"}`, "err");
    }
  } catch (e) {
    setStatus("Network error: " + e.message, "err");
  } finally {
    BTN.disabled = false;
  }
});
