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
    trough_price    NUMERIC
);
CREATE INDEX idx_events_symbol_ts ON events (symbol, first_seen_ts);
CREATE INDEX idx_events_cluster ON events (symbol, cluster_key);

CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
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
