"""Domain database schema — table DDL and version migrations.

Schema for the four producer/consumer tables described in `oracle.db`
(the package's top-level docstring): signals, feedback, topic_timeline,
user_sources, learning_weights, llm_cost_log, portfolio_holdings, and
investment_watchlist.
"""

from __future__ import annotations

import logging

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA_VERSION = 6


# ============================================================================
# Schema (DDL)
# ============================================================================

SCHEMA_SQL = """
-- ----------------------------------------------------------------------------
-- schema_version — migration tracking
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- ----------------------------------------------------------------------------
-- signals — every piece of evidence collected from any source
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type     TEXT NOT NULL,
        -- 'rss','reddit','hn','producthunt','telegram','youtube',
        -- 'website','twitter_x','market','fred','crunchbase','github_trending'
    source_id       TEXT NOT NULL,
        -- e.g. 'reuters/businessNews', 'r/SaaS', '@bensbites'
    external_id     TEXT,
        -- ID at source (HN item ID, Reddit post ID, TG msg ID) — for dedup
    title           TEXT,
    content         TEXT NOT NULL,
    url             TEXT,
    published_at    TEXT NOT NULL,              -- ISO 8601 UTC
    collected_at    TEXT NOT NULL,              -- ISO 8601 UTC
    freshness_score INTEGER NOT NULL,           -- 0..100 (100=<2h, 75=<24h, 50=<72h, 0=stale)
    is_breaking     INTEGER NOT NULL DEFAULT 0, -- 1 if <2h old at collection
    topic_tags      TEXT,                       -- JSON array: ["agentic-rag","openai"]
    raw_metadata    TEXT,                       -- JSON blob: source-specific extras
    UNIQUE(source_type, source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_signals_published  ON signals(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_collected  ON signals(collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_source     ON signals(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_signals_freshness  ON signals(freshness_score DESC);
CREATE INDEX IF NOT EXISTS idx_signals_breaking   ON signals(is_breaking) WHERE is_breaking = 1;

-- ----------------------------------------------------------------------------
-- feedback — Maksim's likes/dislikes/saves on ideas and investment signals
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type     TEXT NOT NULL,                -- 'idea' or 'investment_signal'
    item_id       TEXT NOT NULL,                -- UUID of the idea/signal as shown
    digest_id     TEXT,                          -- which digest run produced it (no FK — runs may be purged)
    feedback_type TEXT NOT NULL,                -- 'like','dislike','save','deep_dive'
    reason        TEXT,
        -- only set for dislikes on ideas:
        -- 'competitors','customer','revenue','complex','market','timing'
    item_snapshot TEXT NOT NULL,                -- JSON of the full item AT TIME OF FEEDBACK
                                                 -- (so weights work even if the item evolves)
    created_at    TEXT NOT NULL                 -- ISO 8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_feedback_created    ON feedback(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_item_type  ON feedback(item_type, feedback_type);
CREATE INDEX IF NOT EXISTS idx_feedback_digest     ON feedback(digest_id);

-- ----------------------------------------------------------------------------
-- topic_timeline — temporal tracking of topics (e.g. "agentic-rag")
-- Used for lifecycle stage detection (EMERGING/GROWING/PEAK/DECLINING)
-- and the /history Telegram command.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS topic_timeline (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    topic            TEXT NOT NULL UNIQUE,        -- normalized: lowercase-hyphenated, e.g. "agentic-rag"
    display_name     TEXT NOT NULL,               -- human form: "Agentic RAG"
    first_seen_at    TEXT NOT NULL,               -- when ORACLE first noticed it
    last_seen_at     TEXT NOT NULL,               -- most recent signal mentioning it
    total_mentions   INTEGER NOT NULL DEFAULT 0,
    weekly_mentions  TEXT,                         -- JSON: {"2026-W14": 12, "2026-W15": 35}
    lifecycle_stage  TEXT NOT NULL DEFAULT 'EMERGING',
        -- 'EMERGING','GROWING','PEAK','DECLINING'
    velocity         REAL NOT NULL DEFAULT 0.0,   -- this_week_mentions / max(last_week_mentions, 1)
    notes            TEXT                          -- optional: synthesizer's running notes
);
CREATE INDEX IF NOT EXISTS idx_topic_last_seen  ON topic_timeline(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_topic_lifecycle  ON topic_timeline(lifecycle_stage);
CREATE INDEX IF NOT EXISTS idx_topic_velocity   ON topic_timeline(velocity DESC);

-- ----------------------------------------------------------------------------
-- user_sources — custom sources Maksim adds via /add_source
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type     TEXT NOT NULL,
        -- 'telegram_channel','youtube_channel','website_blog','rss_custom',
        -- 'twitter_x','reddit_custom'
    source_url      TEXT NOT NULL,                -- URL or identifier ('@bensbites', 'https://example.com/feed')
    display_name    TEXT NOT NULL,                -- human-friendly
    category        TEXT NOT NULL,                -- 'business_ideas','investments','geopolitics','ai_tech','other'
    added_at        TEXT NOT NULL,
    last_fetched_at TEXT,                          -- ISO 8601 UTC of last successful fetch
    last_seen_id    TEXT,                          -- incremental fetching cursor (TG msg ID, RSS GUID, etc)
    is_active       INTEGER NOT NULL DEFAULT 1,
    fetch_config    TEXT,                          -- JSON: source-specific config (max_items, filters, etc)
    quality_score   REAL NOT NULL DEFAULT 0.0,    -- learning system: useful signals per fetch
    UNIQUE(source_type, source_url)
);
CREATE INDEX IF NOT EXISTS idx_sources_active    ON user_sources(is_active, source_type);
CREATE INDEX IF NOT EXISTS idx_sources_category  ON user_sources(category);
CREATE INDEX IF NOT EXISTS idx_sources_quality   ON user_sources(quality_score DESC);

-- ----------------------------------------------------------------------------
-- learning_weights — calibration outputs for the personalization loop (Step 14)
-- Single row per "key". Updated by oracle.learning every N feedbacks (default 30).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS learning_weights (
    key         TEXT PRIMARY KEY,
        -- 'prompt_injection_ideas'      → text injected into idea_generator system prompt
        -- 'prompt_injection_critic'     → text injected into critic system prompt
        -- 'last_calibration_at'         → ISO timestamp of most recent calibration
        -- 'calibrations_run'            → count of calibrations ever run (number)
        -- 'feedbacks_at_last_calibration' → snapshot of total feedback count at calibration time
        -- 'weekly_summary'              → most recent weekly summary text for /stats
    value       REAL,                          -- numeric weight, NULL for text-only keys
    text_value  TEXT,                          -- text content (for prompts, summaries)
    rationale   TEXT,                          -- why this value was chosen
    updated_at  TEXT NOT NULL                  -- ISO 8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_learning_updated ON learning_weights(updated_at DESC);

-- ----------------------------------------------------------------------------
-- llm_cost_log — per-call token + cost audit trail (Step 17)
-- Written by oracle.observability.log_llm_usage after every LLM response.
-- Read by /stats and /cost bot commands. Complements Langfuse tracing with
-- local, query-able cost data that survives without an API call.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_cost_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT,                     -- LangGraph checkpoint thread_id
    agent          TEXT NOT NULL,            -- synthesizer | idea_generator | critic | ...
    model          TEXT NOT NULL,            -- gpt-4o | gpt-4o-mini | ...
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL    NOT NULL DEFAULT 0.0,
    latency_ms     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_cost_created ON llm_cost_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_cost_agent   ON llm_cost_log(agent);
CREATE INDEX IF NOT EXISTS idx_llm_cost_run     ON llm_cost_log(run_id);

-- ----------------------------------------------------------------------------
-- portfolio_holdings — Maksim's actual positions (Step 20)
-- Read by investment_analyzer to give PERSONALIZED advice based on what he
-- actually owns + live prices. P&L computed on-the-fly from market_data.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portfolio_holdings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_label     TEXT NOT NULL,                 -- must match a label in market.py
                                                    -- catalogs (e.g. 'BTC','NVDA','GOLD','EURPLN')
    asset_class     TEXT NOT NULL,                 -- 'crypto','stock','etf','commodity','forex','cash'
    quantity        REAL NOT NULL DEFAULT 0.0,     -- units owned (BTC, shares, oz, ...). 0 = unknown / USD-only
    avg_buy_price   REAL NOT NULL DEFAULT 0.0,     -- average entry in USD. 0 = unknown / USD-only
    currency        TEXT NOT NULL DEFAULT 'USD',   -- entry currency (USD/PLN/EUR/...)
    notes           TEXT,                          -- free-form ('long-term hold','swing','staking',...)
    added_at        TEXT NOT NULL,                 -- ISO 8601 UTC of first add
    updated_at      TEXT NOT NULL,                 -- ISO 8601 UTC of last edit
    -- v5 additions:
    usd_invested    REAL,                          -- $-cost-basis when quantity/price not tracked (ETFs entered by $ amount)
    isin            TEXT,                          -- ISIN identifier (ETFs, bonds, crypto-ETPs)
    price_at_add    REAL,                          -- market price snapshot at the moment of add (for P&L drift)
    UNIQUE(asset_label)                            -- one row per asset; re-add updates avg
);
CREATE INDEX IF NOT EXISTS idx_portfolio_class ON portfolio_holdings(asset_class);

-- ----------------------------------------------------------------------------
-- investment_watchlist — assets Maksim tapped "📌 Watch" on
-- Used by alerts.py to fire custom alerts when watched asset moves ±5% from
-- the price baseline captured at the moment of the tap.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS investment_watchlist (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_label     TEXT NOT NULL,                 -- e.g. 'NUCL', 'BTC'
    baseline_price  REAL NOT NULL,                 -- price when Maksim tapped Watch
    added_at        TEXT NOT NULL,                 -- ISO 8601 UTC
    source_sig_id   TEXT,                          -- digest signal id (for context)
    user_note       TEXT,                          -- optional free-text
    last_alert_at   TEXT,                          -- ISO 8601 UTC of last fired alert (dedup)
    is_active       INTEGER NOT NULL DEFAULT 1,    -- 0 if user removed it
    UNIQUE(asset_label)                            -- one active watch per asset
);
CREATE INDEX IF NOT EXISTS idx_watchlist_active ON investment_watchlist(is_active);
"""


# ============================================================================
# Migrations
# ============================================================================


async def _migrate_v4_to_v5(conn: aiosqlite.Connection) -> None:
    """Add usd_invested, isin, price_at_add columns to portfolio_holdings.

    SQLite ALTER TABLE doesn't support `IF NOT EXISTS` for ADD COLUMN, so we
    introspect via PRAGMA table_info to make this idempotent.
    """
    cursor = await conn.execute("PRAGMA table_info(portfolio_holdings)")
    rows = await cursor.fetchall()
    existing_cols = {row[1] for row in rows}  # row[1] = column name

    to_add: list[tuple[str, str]] = []
    if "usd_invested" not in existing_cols:
        to_add.append(("usd_invested", "REAL"))
    if "isin" not in existing_cols:
        to_add.append(("isin", "TEXT"))
    if "price_at_add" not in existing_cols:
        to_add.append(("price_at_add", "REAL"))

    for col_name, col_type in to_add:
        log.info("migration v5: adding column %s %s to portfolio_holdings", col_name, col_type)
        await conn.execute(f"ALTER TABLE portfolio_holdings ADD COLUMN {col_name} {col_type}")
