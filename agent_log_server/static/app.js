/* UI with WebSocket for real-time updates */
const whoEl = document.getElementById("who");
const msgEl = document.getElementById("msg");
const chatEl = document.getElementById("chat");
const statusEl = document.getElementById("status");
const sendBtn = document.getElementById("send");
const refreshBtn = document.getElementById("refresh");
const quitBtn = document.getElementById("quit");

const STORAGE_KEY = "agent_log_who";
let socket = null;

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.style.color = isError ? "red" : "";
}

function loadWho() {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved) whoEl.value = saved;
  if (!whoEl.value) whoEl.value = "agent";
}

function saveWho() {
  localStorage.setItem(STORAGE_KEY, whoEl.value.trim());
}

function escapeHtml(s) {
  if (!s) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[c]));
}

function addMessage(m, append = true) {
  const div = document.createElement("div");
  div.className = "msg";
  const ts = escapeHtml(m.ts || "");
  const who = escapeHtml(m.who || "");
  const msg = escapeHtml(m.message || "");
  div.innerHTML = `<div class="meta"><span class="ts">${ts}</span> <span class="who">${who}</span></div><div class="body">${msg}</div>`;
  
  if (append) {
    chatEl.appendChild(div);
    chatEl.scrollTop = chatEl.scrollHeight;
  } else {
    chatEl.prepend(div);
  }
}

async function fetchHistory() {
  try {
    setStatus("loading history…");
    const r = await fetch("/api/messages?limit=100", { cache: "no-store" });
    if (!r.ok) throw new Error(`Fetch history failed: ${r.status}`);
    const data = await r.json();
    chatEl.innerHTML = "";
    data.forEach(m => addMessage(m));
    setStatus(`connected`);
  } catch (e) {
    setStatus(String(e), true);
  }
}

function connectWS() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws`;
  
  socket = new WebSocket(wsUrl);

  socket.onopen = () => {
    console.log("WS connected");
    setStatus("connected");
  };

  socket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      addMessage(data);
    } catch (e) {
      console.error("WS message error:", e);
    }
  };

  socket.onclose = () => {
    console.log("WS closed, retrying...");
    setStatus("disconnected (retrying...)", true);
    setTimeout(connectWS, 2000);
  };

  socket.onerror = (err) => {
    console.error("WS error:", err);
    socket.close();
  };
}

async function postMessage() {
  const who = whoEl.value.trim();
  const message = msgEl.value.trim();
  if (!who || !message) return;
  saveWho();

  try {
    const r = await fetch("/api/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ who, message }),
    });
    if (!r.ok) {
      const t = await r.text();
      throw new Error(`Post failed: ${r.status} ${t}`);
    }
    msgEl.value = "";
  } catch (e) {
    setStatus(String(e), true);
  }
}

async function quitServer() {
  if (!confirm("Shutdown server?")) return;
  try {
    setStatus("shutting down…");
    await fetch("/api/shutdown", { method: "POST" });
  } catch (e) {
    setStatus("shutdown requested");
  }
}

sendBtn.addEventListener("click", postMessage);
refreshBtn.addEventListener("click", fetchHistory);
quitBtn.addEventListener("click", quitServer);

msgEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    postMessage();
  }
});

whoEl.addEventListener("change", saveWho);

loadWho();
fetchHistory();
connectWS();