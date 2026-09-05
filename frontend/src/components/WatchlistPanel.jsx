import { useEffect, useState } from "react";

const MUTABLE_KINDS = [
  { key: "z_move", label: "volatility" },
  { key: "vol_spike", label: "volume" },
  { key: "relative_move", label: "vs index" },
  { key: "level_breach", label: "52-week" },
  { key: "target_hit", label: "target" },
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

/* Staleness is about whether WE are still polling (fetched_at), not about
   how long ago the price last moved (source_ts). A stock that has not
   traded for an hour is not stale data — it is an unchanged price, freshly
   confirmed. Conflating the two would cry wolf on every quiet stock. */
function freshness(item) {
  if (!item.fetched_at) return "none";
  if (ageMinutes(item.fetched_at) > STALE_AFTER_MINUTES) return "stale";
  return item.confidence || "none";
}

function Holding({ item, onUpdate, onRemove, onToggleMute }) {
  /* Held locally, not derived from props on every render. PATCH replaces the
     whole muted_kinds array, so two quick toggles both computed from the
     same stale prop would make the second silently undo the first. */
  const [muted, setMuted] = useState(() => new Set(item.muted_kinds || []));
  useEffect(() => {
    setMuted(new Set(item.muted_kinds || []));
  }, [item.muted_kinds]);

  const state = freshness(item);
  const [target, setTarget] = useState(
    item.target_price != null ? String(item.target_price) : ""
  );
  const [note, setNote] = useState(item.note || "");

  function toggleMute(kind) {
    const willMute = !muted.has(kind);
    const next = new Set(muted);
    if (willMute) next.add(kind);
    else next.delete(kind);
    setMuted(next); // optimistic, so a fast second click builds on the first
    // Sent as a delta: the server applies it to whatever is actually stored,
    // so a concurrent toggle from another device cannot be overwritten.
    onToggleMute(item.symbol, kind, willMute);
  }

  function saveTarget(e) {
    e.preventDefault();
    const parsed = parseFloat(target);
    if (!Number.isFinite(parsed) || parsed <= 0) return;
    onUpdate(item.symbol, { target_price: parsed });
  }

  function saveNote() {
    if (note === (item.note || "")) return; // nothing changed; skip the write
    onUpdate(item.symbol, { note });
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
            {item.change_pct != null && (
              <span
                className={`holding__change ${
                  item.change_pct >= 0 ? "holding__change--up" : "holding__change--down"
                }`}
              >
                {item.change_pct >= 0 ? "+" : ""}
                {item.change_pct.toFixed(2)}%
              </span>
            )}
          </div>
          <div className="holding__asof">
            <span className={`badge badge--${state}`}>{state}</span>
            <span title="when this price is from · when we last checked">
              quoted {timeAgo(item.source_ts)} · checked {timeAgo(item.fetched_at)}
            </span>
          </div>
        </div>
      </div>

      {/* Market context. A price alone is not market information -- these
          three say whether today's move is unusual for THIS stock. */}
      <div className="market-row">
        {item.volume_ratio != null && (
          <span className="market-stat" title="today's volume vs its own 20-day average">
            <span className="market-stat__label">Vol</span>
            {item.volume_ratio.toFixed(1)}× avg
          </span>
        )}
        {item.range_position != null && (
          <span className="market-stat" title="position within the 52-week range">
            <span className="market-stat__label">52w</span>
            <span className="range-bar" aria-hidden="true">
              <span
                className="range-bar__dot"
                style={{ left: `${Math.min(100, Math.max(0, item.range_position * 100))}%` }}
              />
            </span>
            {Math.round(item.range_position * 100)}%
          </span>
        )}
        {item.wk52_low != null && item.wk52_high != null && (
          <span className="market-stat market-stat--muted">
            {Number(item.wk52_low).toFixed(0)} – {Number(item.wk52_high).toFixed(0)}
          </span>
        )}
      </div>

      <input
        className="note-input"
        type="text"
        maxLength={120}
        placeholder="Why are you watching this?"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        onBlur={saveNote}
        onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
      />

      <div className="holding__controls">
        <form className="target-form" onSubmit={saveTarget}>
          <label className="mute-row__label" htmlFor={`target-${item.symbol}`}>
            Target
          </label>
          <input
            id={`target-${item.symbol}`}
            className="target-input"
            type="number"
            step="0.01"
            min="0"
            inputMode="decimal"
            placeholder="—"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            onBlur={saveTarget}
          />
        </form>

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

export default function WatchlistPanel({
  items,
  symbols,
  onAdd,
  onUpdate,
  onRemove,
  onToggleMute,
}) {
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
            <Holding
              key={item.symbol}
              item={item}
              onUpdate={onUpdate}
              onRemove={onRemove}
              onToggleMute={onToggleMute}
            />
          ))}
        </div>
      )}
    </section>
  );
}
