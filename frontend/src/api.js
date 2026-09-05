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
    throw new Error(body.detail || `Request failed: ${res.status}`);
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
  updateWatchlistItem: (symbol, patch) =>
    request(`/watchlist/${encodeURIComponent(symbol)}`, { method: "PATCH", body: JSON.stringify(patch) }),
  removeFromWatchlist: (symbol) =>
    request(`/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" }),
  getDigest: () => request("/digest"),
  runScenario: () => request("/demo/run-scenario", { method: "POST" }),
  resetDemo: () => request("/demo/reset", { method: "POST" }),
  setChaos: (enabled) => request(`/demo/chaos?enabled=${enabled}`, { method: "POST" }),
  runIngest: () => request("/ingest"),
};
