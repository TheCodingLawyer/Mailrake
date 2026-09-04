// The session token arrives in the URL we opened, and never leaves this page.
const TOKEN = new URLSearchParams(location.search).get("token") || "";

async function call(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Session-Token": TOKEN,
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

export const api = {
  session: () => call("/session"),
  senders: () => call("/senders"),
  storage: () => call("/storage"),
  history: () => call("/history"),
  scan: (body) => call("/scan", { method: "POST", body: JSON.stringify(body) }),
  unsubscribe: (body) => call("/unsubscribe", { method: "POST", body: JSON.stringify(body) }),
  trust: (body) => call("/trust", { method: "POST", body: JSON.stringify(body) }),
  block: (body) => call("/block", { method: "POST", body: JSON.stringify(body) }),
  retryFailed: (body) => call("/retry-failed", { method: "POST", body: JSON.stringify(body) }),
  cleanup: (body) => call("/cleanup", { method: "POST", body: JSON.stringify(body) }),
};

// EventSource cannot set headers, so the token rides in the query string.
// Same origin, same loopback check on the server side.
export function subscribe(onEvent) {
  const es = new EventSource(`/api/events?token=${encodeURIComponent(TOKEN)}`);
  es.onmessage = (e) => {
    try { onEvent(JSON.parse(e.data)); } catch { /* ignore keepalives */ }
  };
  return () => es.close();
}

export function formatBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

export function formatDate(iso) {
  const days = Math.floor((Date.now() - new Date(iso)) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days}d ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}
