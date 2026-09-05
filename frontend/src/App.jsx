import { useEffect, useState } from "react";
import "./App.css";
import { api, clearToken, hasToken } from "./api";
import AuthScreen from "./components/AuthScreen";
import DemoControls from "./components/DemoControls";
import DigestPanel from "./components/DigestPanel";
import WatchlistPanel from "./components/WatchlistPanel";

function App() {
  const [authed, setAuthed] = useState(hasToken());
  const [digest, setDigest] = useState(null);
  const [digestError, setDigestError] = useState(null);
  const [digestLoading, setDigestLoading] = useState(true);
  const [watchlist, setWatchlist] = useState([]);
  const [symbols, setSymbols] = useState([]);
  const [health, setHealth] = useState(null);

  async function refreshAll() {
    setDigestLoading(true);
    setDigestError(null);
    try {
      const [digestResult, watchlistResult, symbolsResult, healthResult] =
        await Promise.all([
          api.getDigest(),
          api.getWatchlist(),
          api.getSymbols(),
          api.getHealth(),
        ]);
      setDigest(digestResult);
      setWatchlist(watchlistResult.items);
      setSymbols(symbolsResult.symbols);
      setHealth(healthResult);
    } catch (err) {
      setDigestError(err.message);
    } finally {
      setDigestLoading(false);
    }
  }

  useEffect(() => {
    if (authed) refreshAll();
  }, [authed]);

  async function refreshWatchlistOnly() {
    const result = await api.getWatchlist();
    setWatchlist(result.items);
  }

  async function handleAdd(symbol) {
    await api.addToWatchlist(symbol);
    await refreshWatchlistOnly();
  }

  async function handleUpdate(symbol, patch) {
    await api.updateWatchlistItem(symbol, patch);
    await refreshWatchlistOnly();
  }

  async function handleRemove(symbol) {
    await api.removeFromWatchlist(symbol);
    await refreshWatchlistOnly();
  }

  function logout() {
    clearToken();
    setAuthed(false);
    setDigest(null);
    setWatchlist([]);
    setHealth(null);
  }

  if (!authed) {
    return <AuthScreen onAuthed={() => setAuthed(true)} />;
  }

  const sourceMode = health?.provider === "yfinance" ? "live" : "replay";
  const degraded = !!health?.degraded;

  return (
    <div className="app">
      <header className="app-bar">
        <div className="brandmark">
          <span className="brandmark__glyph" aria-hidden="true" />
          <span className="brandmark__word">Watermark</span>
        </div>

        <div className="app-bar__right">
          {health && (
            <span
              className={`source-pill ${degraded ? "source-pill--degraded" : ""}`}
              title={
                degraded
                  ? `Provider unavailable: ${health.provider_health?.last_error || "unknown"}`
                  : `Data provider: ${health.provider}`
              }
            >
              <span
                className={`source-pill__dot source-pill__dot--${degraded ? "stale" : sourceMode}`}
              />
              {degraded ? "Feed down" : sourceMode === "live" ? "Live feed" : "Replay feed"}
            </span>
          )}
          <button className="link-button" onClick={logout}>
            Log out
          </button>
        </div>
      </header>

      {sourceMode === "replay" && (
        <DemoControls onComplete={refreshAll} degraded={degraded} />
      )}

      {degraded && (
        <div className="degraded-banner">
          <strong>Price feed unavailable.</strong> Showing the last prices we actually
          received, marked with their age. Nothing below is estimated or filled in —
          we would rather show you stale data you can see is stale than a number
          nobody reported.
        </div>
      )}

      <DigestPanel digest={digest} loading={digestLoading} error={digestError} />

      <WatchlistPanel
        items={watchlist}
        symbols={symbols}
        onAdd={handleAdd}
        onUpdate={handleUpdate}
        onRemove={handleRemove}
      />
    </div>
  );
}

export default App;
