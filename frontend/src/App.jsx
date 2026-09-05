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

  const [actionError, setActionError] = useState(null);
  // A one-off "show me everything", separate from the saved preference --
  // the user should be able to peek without changing a setting back.
  const [showEverything, setShowEverything] = useState(false);

  async function toggleShowEverything() {
    const next = !showEverything;
    setShowEverything(next);
    // Never ack the escape-hatch view: peeking at what was held back must
    // not mark unseen signals as read.
    await refreshAll({ showAll: next });
  }

  async function changeSensitivity(level) {
    setActionError(null);
    try {
      await api.setSensitivity(level);
      setShowEverything(false);
      await refreshAll({ showAll: false });
    } catch (err) {
      setActionError(err.message);
    }
  }

  // A 401 anywhere clears the token; drop straight back to the login form
  // rather than stranding the user on an error screen.
  useEffect(() => {
    const onUnauthorized = () => {
      setAuthed(false);
      setDigest(null);
      setWatchlist([]);
    };
    window.addEventListener("watermark:unauthorized", onUnauthorized);
    return () => window.removeEventListener("watermark:unauthorized", onUnauthorized);
  }, []);

  async function refreshAll({ ack = false, showAll = showEverything } = {}) {
    setDigestLoading(true);
    setDigestError(null);
    // Settled, not all: a failing /symbols call must not discard a perfectly
    // good digest and show "couldn't load your digest".
    const [digestResult, watchlistResult, symbolsResult, healthResult] =
      await Promise.allSettled([
        api.getDigest(showAll),
        api.getWatchlist(),
        api.getSymbols(),
        api.getHealth(),
      ]);

    if (digestResult.status === "fulfilled") {
      setDigest(digestResult.value);
      // Reading is side-effect free, so mark it read explicitly -- and only
      // on a genuine user-facing refresh, never on a background one.
      if (ack && digestResult.value.cursor) {
        api.ackDigest(digestResult.value.cursor).catch(() => {});
      }
    } else {
      setDigestError(digestResult.reason.message);
    }
    if (watchlistResult.status === "fulfilled") setWatchlist(watchlistResult.value.items);
    if (symbolsResult.status === "fulfilled") setSymbols(symbolsResult.value.symbols);
    if (healthResult.status === "fulfilled") setHealth(healthResult.value);

    setDigestLoading(false);
  }

  useEffect(() => {
    if (authed) refreshAll({ ack: true });
  }, [authed]);

  /* Mutations change the digest too -- watched_count, the muted set and
     target-derived events all live there -- so refresh both. Without ack:
     changing a mute should not mark unseen signals as read. */
  async function mutate(action) {
    setActionError(null);
    try {
      await action();
      await refreshAll();
    } catch (err) {
      setActionError(err.message);
    }
  }

  const handleAdd = (symbol) => mutate(() => api.addToWatchlist(symbol));
  const handleUpdate = (symbol, patch) =>
    mutate(() => api.updateWatchlistItem(symbol, patch));
  const handleRemove = (symbol) => mutate(() => api.removeFromWatchlist(symbol));
  const handleToggleMute = (symbol, kind, muted) =>
    mutate(() => api.toggleMute(symbol, kind, muted));

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

      {/* Keyed on the demo switch, NOT on the provider. Gating these on
          `provider === replay` hid them exactly when the deployment ran on
          live data -- the case where being able to replay a scripted day on
          demand matters most. */}
      {health?.demo_controls && (
        <DemoControls
          onComplete={() => refreshAll()}
          degraded={degraded}
          live={sourceMode === "live"}
        />
      )}

      {actionError && <div className="degraded-banner">{actionError}</div>}

      {degraded && (
        <div className="degraded-banner">
          <strong>Price feed unavailable.</strong> Showing the last prices we actually
          received, marked with their age. Nothing below is estimated or filled in —
          we would rather show you stale data you can see is stale than a number
          nobody reported.
        </div>
      )}

      <DigestPanel
        digest={digest}
        loading={digestLoading}
        error={digestError}
        showEverything={showEverything}
        onToggleShowEverything={toggleShowEverything}
        onChangeSensitivity={changeSensitivity}
      />

      <WatchlistPanel
        items={watchlist}
        symbols={symbols}
        onAdd={handleAdd}
        onUpdate={handleUpdate}
        onRemove={handleRemove}
        onToggleMute={handleToggleMute}
      />
    </div>
  );
}

export default App;
