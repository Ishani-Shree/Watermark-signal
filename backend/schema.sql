-- Watermark schema
-- Two zones: symbol-scoped (shared, computed once) and user-scoped (per user, cheap).

CREATE TABLE symbols (
    symbol              TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    exchange            TEXT NOT NULL DEFAULT 'NSE',
    market_index_symbol TEXT NOT NULL DEFAULT '^NSEI',
    sector_index_symbol TEXT
);

CREATE TABLE snapshots (
    symbol      TEXT NOT NULL REFERENCES symbols(symbol),
    source_ts   TIMESTAMPTZ NOT NULL,
    price       NUMERIC NOT NULL,
    volume      BIGINT NOT NULL,
    prev_close  NUMERIC NOT NULL,
    source      TEXT NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    confidence  TEXT NOT NULL DEFAULT 'live', -- 'live' | 'stale' | 'replay'
    PRIMARY KEY (symbol, source_ts)
);
CREATE INDEX idx_snapshots_symbol_ts ON snapshots (symbol, source_ts DESC);

CREATE TABLE baselines (
    symbol          TEXT PRIMARY KEY REFERENCES symbols(symbol),
    ret_stddev_30d  NUMERIC,
    avg_volume_20d  NUMERIC,
    wk52_high       NUMERIC,
    wk52_low        NUMERIC,
    history_days    INTEGER NOT NULL DEFAULT 0,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE events (
    id              BIGSERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL REFERENCES symbols(symbol),
    kind            TEXT NOT NULL, -- 'vol_spike' | 'level_breach' | 'relative_move' | 'composite'
    score           NUMERIC NOT NULL,
    reason_text     TEXT NOT NULL,
    first_seen_ts   TIMESTAMPTZ NOT NULL,
    last_updated_ts TIMESTAMPTZ NOT NULL,
    cluster_key     TEXT NOT NULL,
    peak_price      NUMERIC,
    trough_price    NUMERIC,
    -- Which way the move went when the event opened. Peak and trough alone
    -- cannot tell a rise that is still running from a fall that recovered:
    -- in both cases the price sits far from one extreme and near the other.
    direction       TEXT NOT NULL DEFAULT 'up'  -- 'up' | 'down'
);
CREATE INDEX idx_events_symbol_ts ON events (symbol, first_seen_ts);
CREATE INDEX idx_events_cluster ON events (symbol, cluster_key);
-- The digest's hot path filters on last_updated_ts, not first_seen_ts: an
-- event that is still being extended must keep surfacing. Indexing the
-- column the query actually uses (Postgres ignores both while the table is
-- small, and will need this one when it is not).
CREATE INDEX idx_events_symbol_updated ON events (symbol, last_updated_ts);

CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- How much the digest is allowed to hold back. An attention filter the
    -- user cannot tune eventually becomes noise, and the failure people
    -- actually fear is the false negative -- so the filtering has a dial and
    -- an escape hatch, not just an on/off mute.
    sensitivity   TEXT NOT NULL DEFAULT 'balanced'  -- 'quiet'|'balanced'|'everything'
);

CREATE TABLE watchlist_items (
    user_id      BIGINT NOT NULL REFERENCES users(id),
    symbol       TEXT NOT NULL REFERENCES symbols(symbol),
    note         TEXT,
    target_price NUMERIC,
    muted_kinds  TEXT[] NOT NULL DEFAULT '{}',
    added_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, symbol)
);

CREATE TABLE read_state (
    user_id        BIGINT PRIMARY KEY REFERENCES users(id),
    last_viewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
