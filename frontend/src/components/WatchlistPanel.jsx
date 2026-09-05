import { useState } from "react";

const MUTABLE_KINDS = [
  { key: "z_move", label: "volatility" },
  { key: "vol_spike", label: "volume" },
  { key: "relative_move", label: "vs index" },
  { key: "level_breach", label: "52-week" },
];

const STALE_AFTER_MINUTES = 30;

function ageMinutes(iso) {
  if (!iso) return null;
  return (Date.now() - new Date(iso).getTime()) / 60000;
}

function timeAgo(iso) {
  const mins = ageMinutes(iso);
  if (mins === null) return "no data yet";
  if (mins < 1) return "moments ago";
  if (mins < 60) return `${Math.round(mins)}m ago`;
  if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
  return `${Math.round(mins / 1440)}d ago`;
}

/* Staleness is about AGE, not just what ingest recorded. A quote that was
   'live' when written is still stale if nothing has arrived since — show
   that plainly rather than smoothing it over. */
function freshness(item) {
  if (!item.source_ts) return "none";
  const mins = ageMinutes(item.source_ts);
  if (mins > STALE_AFTER_MINUTES) return "stale";
  return item.confidence || "none";
}

function Holding({ item, onUpdate, onRemove }) {
  const muted = new Set(item.muted_kinds || []);
  const state = freshness(item);

  function toggleMute(kind) {
    const next = new Set(muted);
    if (next.has(kind)) next.delete(kind);
    else next.add(kind);
    onUpdate(item.symbol, { muted_kinds: Array.from(next) });
  }

  return (
    <article className="holding">
      <div className="holding__row">
        <div className="holding__id">
          <div className="holding__symbol">{item.symbol}</div>
          <div className="holding__name">{item.name}</div>
        </div>

        <div className="holding__price">
          <div className="holding__value">
            {item.price != null ? Number(item.price).toFixed(2) : "—"}
          </div>
          <div className="holding__asof">
            <span className={`badge badge--${state}`}>{state}</span>
            {timeAgo(item.source_ts)}
          </div>
        </div>
      </div>

      <div className="holding__controls">
        <div className="mute-row">
          <span className="mute-row__label">Mute</span>
          {MUTABLE_KINDS.map((kind) => {
            const isMuted = muted.has(kind.key);
            return (
              <button
                type="button"
                key={kind.key}
                className={`mute-chip ${isMuted ? "mute-chip--muted" : ""}`}
                onClick={() => toggleMute(kind.key)}
                title={isMuted ? `Unmute ${kind.label}` : `Mute ${kind.label}`}
              >
                {kind.label}
              </button>
            );
          })}
        </div>

        <button className="remove-button" onClick={() => onRemove(item.symbol)}>
          Remove
        </button>
      </div>
    </article>
  );
}

export default function WatchlistPanel({ items, symbols, onAdd, onUpdate, onRemove }) {
  const [selected, setSelected] = useState("");

  function submitAdd(e) {
    e.preventDefault();
    if (!selected) return;
    onAdd(selected);
    setSelected("");
  }

  const watched = new Set(items.map((i) => i.symbol));
  const available = symbols.filter((s) => !watched.has(s.symbol));

  return (
    <section className="watchlist">
      <div className="watchlist__head">
        <h2 className="section-title">Watchlist</h2>
        <span className="section-count">
          {items.length} {items.length === 1 ? "position" : "positions"}
        </span>
      </div>

      <form onSubmit={submitAdd} className="add-form">
        <select value={selected} onChange={(e) => setSelected(e.target.value)}>
          <option value="">Add a stock…</option>
          {available.map((s) => (
            <option key={s.symbol} value={s.symbol}>
              {s.name} — {s.symbol}
            </option>
          ))}
        </select>
        <button type="submit" disabled={!selected}>
          Add
        </button>
      </form>

      {items.length === 0 ? (
        <p className="empty-state">
          Nothing watched yet. Add a stock above and your digest starts tracking it.
        </p>
      ) : (
        <div className="holding-list">
          {items.map((item) => (
            <Holding key={item.symbol} item={item} onUpdate={onUpdate} onRemove={onRemove} />
          ))}
        </div>
      )}
    </section>
  );
}
