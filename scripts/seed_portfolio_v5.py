"""Seed example portfolio data into `portfolio_holdings` (schema v5).

Run once after migrating to schema v5 to replace whatever is in the table
with a fresh set of $-denominated holdings. Idempotent: TRUNCATEs the table
first. The HOLDINGS list below is placeholder/demo data — replace it with
your own positions before running this against a real deployment.

Usage (locally or inside the running container):
    docker compose exec oracle-bot python scripts/seed_portfolio_v5.py

Or standalone:
    python scripts/seed_portfolio_v5.py

The script:
  1. Runs init_db() to ensure schema v5 is applied
  2. Truncates portfolio_holdings
  3. Inserts the example positions via add_usd_holding()
  4. Tries to fetch live market prices via collect_market_data() — if any of
     the new assets are found, stores their price as price_at_add (so future
     P&L drift can be computed). Failures are non-fatal: the script always
     completes, even if some prices aren't available.
  5. Prints the final portfolio summary
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

# Make sure Windows console can print Unicode (Russian / emojis) without errors
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

# Allow running as a script from project root: `python scripts/seed_portfolio_v5.py`
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from oracle.db import init_db  # noqa: E402
from oracle.portfolio import (  # noqa: E402
    add_usd_holding,
    format_portfolio_for_llm,
    get_portfolio_with_pnl,
    truncate_holdings,
)

log = logging.getLogger("seed_portfolio")


# Example seed data for demo purposes. Each position: (label, asset_class,
# usd_invested, ISIN, notes) — replace with your own holdings.
HOLDINGS: list[dict[str, Any]] = [
    {
        "asset_label": "AAPL",
        "asset_class": "stock",
        "usd_invested": 5000.0,
        "isin": None,
        "notes": "Example single-stock position",
    },
    {
        "asset_label": "VOO",
        "asset_class": "etf",
        "usd_invested": 3000.0,
        "isin": "US9229087690",
        "notes": "Example broad-market ETF position",
    },
    {
        "asset_label": "QQQ",
        "asset_class": "etf",
        "usd_invested": 1500.0,
        "isin": "US46090E1038",
        "notes": "Example sector ETF position",
    },
    {
        "asset_label": "BTC",
        "asset_class": "crypto",
        "usd_invested": 1000.0,
        "isin": None,
        "notes": "Example crypto position",
    },
    {
        "asset_label": "ETH",
        "asset_class": "crypto",
        "usd_invested": 500.0,
        "isin": None,
        "notes": "Example crypto position",
    },
    {
        "asset_label": "TLT",
        "asset_class": "bond",
        "usd_invested": 1200.0,
        "isin": "US4642874576",
        "notes": "Example bond ETF position",
    },
    {
        "asset_label": "CASH-USD",
        "asset_class": "cash",
        "usd_invested": 2000.0,
        "isin": None,
        "notes": "Example cash position",
    },
    {
        "asset_label": "GLD",
        "asset_class": "commodity",
        "usd_invested": 300.0,
        "isin": "US78463V1070",
        "notes": "Example commodity ETF position",
    },
]


async def _seed() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    log.info("seed: ensuring schema v5 ...")
    await init_db()

    log.info("seed: truncating portfolio_holdings ...")
    removed = await truncate_holdings()
    log.info("seed: removed %d old rows", removed)

    # Best-effort: try to fetch live prices to record price_at_add for drift P&L.
    # Many of the new tickers are European-listed UCITS that yfinance may not
    # return — that's fine, we just won't have a snapshot for them.
    market_data: dict = {}
    try:
        from oracle.agents.market import collect_market_data  # noqa: PLC0415

        log.info("seed: fetching live market prices for entry snapshots ...")
        market_data, errors = await collect_market_data()
        if errors:
            log.warning("seed: %d market data errors (non-fatal): %s", len(errors), errors[:3])
    except Exception as e:
        log.warning("seed: market data fetch failed (non-fatal): %s", e)

    from oracle.portfolio import _lookup_current_price  # noqa: PLC0415

    log.info("seed: inserting %d positions ...", len(HOLDINGS))
    for h in HOLDINGS:
        # _lookup_current_price already applies the listing-currency → USD
        # conversion for EU UCITS, so price_at_add is stored in USD too.
        price_at_add = _lookup_current_price(h["asset_label"], market_data)
        try:
            await add_usd_holding(
                asset_label=h["asset_label"],
                usd_invested=float(h["usd_invested"]),
                asset_class=h["asset_class"],
                isin=h["isin"],
                notes=h["notes"],
                price_at_add=price_at_add,
            )
            tag = f" @ ${price_at_add:.2f}" if price_at_add else " (no live price)"
            log.info("  ✓ %s — $%s%s", h["asset_label"], h["usd_invested"], tag)
        except Exception as e:
            log.error("  ✗ %s — %s", h["asset_label"], e)

    # Final summary
    portfolio = await get_portfolio_with_pnl(market_data)
    print()
    print("=" * 70)
    print("PORTFOLIO AFTER SEED")
    print("=" * 70)
    print(format_portfolio_for_llm(portfolio))
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(_seed())
