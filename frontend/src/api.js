const API_BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000";

function authHeaders() {
  const token = localStorage.getItem("watermark_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(options.headers || {}),
    },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    // An expired or invalid token must clear itself, or the app parks on an
    // error screen with no way back to the login form.
    if (res.status === 401) {
      clearToken();
      window.dispatchEvent(new Event("watermark:unauthorized"));
    }
    const detail =
      typeof body.detail === "string"
        ? body.detail
        : Array.isArray(body.detail)
          ? body.detail.map((d) => d.msg).join("; ") // FastAPI validation errors
          : `Request failed: ${res.status}`;
    throw new Error(detail);
  }
  return body;
}

export function saveToken(token) {
  localStorage.setItem("watermark_token", token);
}

export function clearToken() {
  localStorage.removeItem("watermark_token");
}

export function hasToken() {
  return !!localStorage.getItem("watermark_token");
}

export const api = {
  getHealth: () => request("/health"),
  signup: (email, password) =>
    request("/auth/signup", { method: "POST", body: JSON.stringify({ email, password }) }),
  login: (email, password) =>
    request("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  getSymbols: () => request("/symbols"),
  getWatchlist: () => request("/watchlist"),
  addToWatchlist: (symbol, note, target_price) =>
    request("/watchlist", { method: "POST", body: JSON.stringify({ symbol, note, target_price }) }),
  // A delta, not a whole-array replace -- two devices toggling different
  // signals at once compose instead of clobbering each other.
  toggleMute: (symbol, kind, muted) =>
    request(`/watchlist/${encodeURIComponent(symbol)}/mute`, {
      method: "POST",
      body: JSON.stringify({ kind, muted }),
    }),
  updateWatchlistItem: (symbol, patch) =>
    request(`/watchlist/${encodeURIComponent(symbol)}`, { method: "PATCH", body: JSON.stringify(patch) }),
  removeFromWatchlist: (symbol) =>
    request(`/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" }),
  getDigest: (showAll = false) => request(`/digest${showAll ? "?show_all=true" : ""}`),
  setSensitivity: (sensitivity) =>
    request("/settings/sensitivity", { method: "PUT", body: JSON.stringify({ sensitivity }) }),
  ackDigest: (cursor) =>
    request("/digest/ack", { method: "POST", body: JSON.stringify({ cursor }) }),
  runScenario: () => request("/demo/run-scenario", { method: "POST" }),
  resetDemo: () => request("/demo/reset", { method: "POST" }),
  setChaos: (enabled) => request(`/demo/chaos?enabled=${enabled}`, { method: "POST" }),
  runIngest: () => request("/ingest"),
};
