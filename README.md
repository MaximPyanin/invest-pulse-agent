# ORACLE

A personal intelligence agent that scans news, tech trends, and market data
to surface **business ideas** (SaaS / AI tooling) and secondary **investment
signals**, delivered on a schedule and on demand through a Telegram bot.

Under the hood it's a multi-agent [LangGraph](https://github.com/langchain-ai/langgraph)
pipeline: parallel data collectors feed a synthesizer, which feeds an idea
generator and an investment analyzer, each refined through their own
Reflexion critique loop, validated against live web/market data, and
rendered as Telegram cards with inline feedback buttons that steer future
runs.

## What it does

- **Business ideas** (primary focus): collects signals from RSS/news,
  Hacker News, Product Hunt, Reddit, GitHub trending, YouTube, and
  user-added custom sources; clusters them into cross-signal insights;
  drafts concrete SaaS/AI business ideas; critiques and rewrites weak ones
  (Reflexion loop); validates surviving ideas against real web search
  results before delivery.
- **Investment signals** (secondary): live market data (yfinance, CoinGecko,
  FRED) across equities, crypto, commodities, and forex, turned into
  trader-brief style scenarios (`do_now` / `how_to_execute` / risk), aware
  of the user's actual portfolio holdings and P&L.
- **Telegram bot**: `/digest` for an on-demand run, `/morning` for a fast
  daily brief (urgent alerts + portfolio advice), `/portfolio` to track
  holdings, `/sources` to manage custom feeds, feedback buttons that
  auto-recalibrate the idea generator's preferences over time.
- **Real-time alerts**: a lightweight poll loop pages the user on threshold
  breaches (VIX spike, large BTC/gold/oil/SPY moves) independent of the
  scheduled digests.

## Architecture

```
src/oracle/
  config.py, state.py, graph.py, nodes.py, main.py   framework wiring —
                                                        LangGraph topology,
                                                        settings, entry point
  models.py            Pydantic schemas shared by every agent
  observability.py      LLM client factory + cost tracking (OpenAI/Azure,
                        optional Langfuse tracing)
  db/                   domain SQLite schema, migrations, connection helpers
  prompts/               every agent's LLM system-prompt text, one module
                        per agent
  agents/                the LLM-calling pipeline stages (collectors,
                        synthesizer, idea generator, critics, validator,
                        investment analyzer, deep-dive agents)
  services/              non-LLM domain logic: portfolio tracking, feedback
                        learning/calibration, price watchlist, real-time
                        alerts, job scheduling
  bot/                   Telegram layer — message templates (views.py) and
                        handlers/ (one module per command group, split by
                        domain: digest, morning, sources, portfolio,
                        feedback, settings, callbacks)
```

The pipeline itself (see `graph.py` for the exact topology): four
collectors run in parallel and fan in to the synthesizer, which feeds the
idea-generator↔critic Reflexion loop; on exit that hands off to the
investment-analyzer↔investment-critic Reflexion loop; both finish through
a shared validator and formatter.

## Stack

Python 3.13, [LangGraph](https://github.com/langchain-ai/langgraph) +
[LangGraph SQLite checkpointer](https://pypi.org/project/langgraph-checkpoint-sqlite/),
[python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot),
OpenAI / Azure OpenAI (structured outputs), APScheduler, aiosqlite,
yfinance, httpx + BeautifulSoup, Telethon (custom Telegram-channel
sources), optional [Langfuse](https://langfuse.com/) tracing. Dependency
management via [uv](https://docs.astral.sh/uv/).

## Running it

1. Install dependencies:
   ```bash
   uv sync
   ```
2. Copy `.env.example` to `.env` and fill in at least `TELEGRAM_BOT_TOKEN`
   (from [@BotFather](https://t.me/BotFather)), `TELEGRAM_CHAT_ID`, and an
   LLM key (`OPENAI_API_KEY`, or `AZURE_OPENAI_ENDPOINT` +
   `AZURE_OPENAI_API_KEY`). Every other key is optional — the app degrades
   gracefully without it (fewer sources, no LLM calls, etc).
3. Run the bot:
   ```bash
   uv run python -m oracle.bot.main
   ```
   Or render every Telegram template against fixture data without a bot
   token or network access:
   ```bash
   uv run python -m oracle.bot.main --dry-run
   ```
4. Run the full one-shot pipeline directly (collectors → ideas →
   investments → formatter), without Telegram:
   ```bash
   uv run python -m oracle.main
   ```

### Docker

```bash
cp .env.example .env   # fill in the keys above
docker compose build
docker compose up -d
docker compose logs -f oracle
```

### Deploying to Azure Container Apps

See [DEPLOYMENT.md](DEPLOYMENT.md) for a full walkthrough (infra setup,
secrets, GitHub Actions auto-deploy on push to `main`).

## Development

- `scripts/test_idea_diversity.py`, `scripts/test_morning.py` — standalone
  smoke tests (no pytest harness; run directly with `uv run python
  scripts/<name>.py`).
- `scripts/seed_portfolio_v5.py` — seeds `portfolio_holdings` with example
  data; replace the `HOLDINGS` list with your own positions before running
  it against a real deployment.
