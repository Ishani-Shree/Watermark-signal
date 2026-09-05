import { useState } from "react";

const SEVERITY = [
  { min: 80, key: "critical", label: "urgent", color: "var(--critical)" },
  { min: 65, key: "serious", label: "high", color: "var(--serious)" },
  { min: 50, key: "warning", label: "moderate", color: "var(--warning)" },
  { min: -Infinity, key: "quiet", label: "low", color: "var(--text-muted)" },
];

const KIND_LABEL = {
  z_move: "Volatility",
  vol_spike: "Volume",
  relative_move: "vs Index",
  level_breach: "52-week level",
  path_summary: "While you were away",
};

function severityFor(score) {
  return SEVERITY.find((band) => score >= band.min);
}

/* The backend composes reason_text as `SYMBOL  part | part | part`.
   We author both sides of that format, so splitting it back into chips is
   safe — and it reads far better than one run-on sentence. */
function signalsFrom(reasonText, symbol) {
  if (!reasonText) return [];
  const withoutSymbol = reasonText.startsWith(symbol)
    ? reasonText.slice(symbol.length)
    : reasonText;
  return withoutSymbol
    .split("|")
    .map((part) => part.trim())
    .filter(Boolean);
}

function headline(digest) {
  if (digest.mode === "empty_watchlist") return "Nothing on your watchlist yet.";

  // The headline must agree with the body. Promising "here's what you
  // missed" above an empty digest reads as a bug even when the filtering
  // is working exactly as intended -- so an empty result gets its own line.
  if (digest.events.length === 0) {
    if (digest.gap_minutes === null) return "All quiet to start with.";
    if (digest.mode === "long_gap") return "Nothing moved while you were away.";
    return "Still quiet since you last checked.";
  }

  // The revert line is earned by an event that actually round-tripped --
  // not by the length of the gap. Saying "the price is where you left it"
  // above a stock that simply moved and stayed would be false.
  if (digest.events.some((e) => e.reverted))
    return "The price is where you left it. Here's what you missed.";

  if (digest.gap_minutes === null) return "Here's where things stand right now.";
  if (digest.mode === "long_gap") return "Here's what moved while you were away.";
  return "Since you last checked.";
}

function eyebrow(digest) {
  if (digest.mode === "empty_watchlist") return "Get started";
  if (digest.gap_minutes === null) return "First look";
  const mins = digest.gap_minutes;
  if (mins < 1) return "Just now";
  if (mins < 60) return `${Math.round(mins)} min away`;
  if (mins < 1440) return `${Math.round(mins / 60)} hr away`;
  return `${Math.round(mins / 1440)} days away`;
}

function EventCard({ event }) {
  const [open, setOpen] = useState(false);
  const severity = severityFor(event.score);
  const signals = signalsFrom(event.reason_text, event.symbol);
  const isHero = event.kind === "path_summary";

  return (
    <article className={`event ${isHero ? "event--hero" : ""}`}>
      <button
        type="button"
        className="event__button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <div className="event__top">
          <div className="event__id">
            <span className="event__symbol">{event.symbol}</span>
            <span className="event__kind">{KIND_LABEL[event.kind] || event.kind}</span>
          </div>
          <span className={`severity severity--${severity.key}`}>{severity.label}</span>
        </div>

        {signals.length > 0 && (
          <div className="signals">
            {signals.map((signal, i) => (
              <span className="signal" key={i}>
                {signal}
              </span>
            ))}
          </div>
        )}

        <div className="meter">
          <div className="meter__track">
            <div
              className="meter__fill"
              style={{
                width: `${Math.min(100, Math.max(0, event.score))}%`,
                background: severity.color,
              }}
            />
          </div>
          <span className="meter__value">{Math.round(event.score)} / 100</span>
        </div>

        <div className="event__expand">{open ? "Hide detail" : "Why this surfaced"}</div>
      </button>

      {open && (
        <div className="event__detail">
          <dl className="detail-grid">
            <div className="detail-grid__item">
              <dt>Signal type</dt>
              <dd>{KIND_LABEL[event.kind] || event.kind}</dd>
            </div>
            <div className="detail-grid__item">
              <dt>First seen</dt>
              <dd>{new Date(event.first_seen_ts).toLocaleString()}</dd>
            </div>
            <div className="detail-grid__item">
              <dt>Last active</dt>
              <dd>{new Date(event.last_updated_ts).toLocaleString()}</dd>
            </div>
            {event.peak_price != null && (
              <div className="detail-grid__item">
                <dt>Peak</dt>
                <dd>{event.peak_price.toFixed(2)}</dd>
              </div>
            )}
            {event.trough_price != null && (
              <div className="detail-grid__item">
                <dt>Trough</dt>
                <dd>{event.trough_price.toFixed(2)}</dd>
              </div>
            )}
            {event.event_count != null && (
              <div className="detail-grid__item">
                <dt>Events in window</dt>
                <dd>{event.event_count}</dd>
              </div>
            )}
            <div className="detail-grid__item">
              <dt>Composite score</dt>
              <dd>{event.score.toFixed(1)} / 100</dd>
            </div>
          </dl>
        </div>
      )}
    </article>
  );
}

export default function DigestPanel({ digest, loading, error }) {
  if (loading) {
    return (
      <section className="digest">
        <p className="digest__eyebrow">Loading</p>
        <h2 className="digest__headline">Checking what changed…</h2>
      </section>
    );
  }

  if (error) {
    return (
      <section className="digest">
        <p className="digest__eyebrow">Problem</p>
        <h2 className="digest__headline">Couldn't load your digest.</h2>
        <p className="error-text">{error}</p>
      </section>
    );
  }

  if (!digest) return null;

  const hasEvents = digest.events.length > 0;

  return (
    <section className="digest">
      <p className="digest__eyebrow">
        Your digest <span>·</span> {eyebrow(digest)}
      </p>
      <h2 className="digest__headline">{headline(digest)}</h2>

      {!hasEvents && digest.mode !== "empty_watchlist" && (
        <p className="digest__quiet">
          <strong>Nothing worth your attention.</strong>
          Everything on your watchlist moved within its normal range. Staying quiet is
          the point — you'll hear from us when something genuinely breaks pattern.
        </p>
      )}

      {digest.mode === "empty_watchlist" && (
        <p className="digest__quiet">
          <strong>Add a stock to begin.</strong>
          Once you're watching something, this space fills with what actually changed
          since your last visit — ranked, explained, and capped at what matters.
        </p>
      )}

      {hasEvents && (
        <div className="event-list">
          {digest.events.map((event) => (
            <EventCard key={`${event.symbol}-${event.last_updated_ts}`} event={event} />
          ))}
        </div>
      )}

      {digest.watched_count > 0 && (
        <p className="suppressed">
          <span className="suppressed__count">
            {digest.flagged_count} of {digest.watched_count}
          </span>
          {/* The noun agrees with watched_count ("1 of 3 watched stocks"),
              not with flagged_count. */}
          {digest.watched_count === 1 ? " watched stock broke pattern." : " watched stocks broke pattern."}
          {digest.watched_count - digest.flagged_count > 0 &&
            ` The other ${digest.watched_count - digest.flagged_count} stayed quiet — checked, scored, and not worth showing.`}
          {digest.suppressed_count > 0 &&
            ` ${digest.suppressed_count} ranked below the cut.`}
        </p>
      )}
    </section>
  );
}
