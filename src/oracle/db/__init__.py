"""ORACLE domain database — schema, connection management, init.

This is the DOMAIN database for ORACLE: signals collected from sources, user
feedback on ideas/signals, topic timeline tracking, and user-added sources.

It is INTENTIONALLY SEPARATE from the LangGraph checkpoint database
(`oracle.db` from Step 1). Reasons:
  - Domain data must persist; checkpoint data can be safely truncated
  - Backup / restore lifecycles differ
  - Schema migrations on one don't risk the other
  - LangGraph owns its own table layout and we shouldn't share writes

The four producer/consumer relationships from the user spec:

  signals          producer: scout/market/trend/custom (Steps 4-7)
                   consumer: synthesizer (Step 9), topic_timeline writer

  feedback         producer: Telegram bot button callbacks (Step 3)
                   consumer: learning system (Step 14) — recalibrates every 30

  topic_timeline   producer: synthesizer extracts topics from signals (Step 9)
                   consumer: idea_generator lifecycle stage; /history command

  user_sources     producer: /add_source bot flow (Step 8)
                   consumer: custom collector node (Step 7)

  portfolio_holdings  producer: /add_holding bot flow (Step 20)
                      consumer: investment_analyzer (personalized advice based on
                                user's actual positions + live market prices)

This package is split into:
  - schema.py     table DDL + version migrations
  - connection.py connection pragmas, the `get_db` context manager, `init_db`
"""

from __future__ import annotations

from .connection import DB_PATH, get_db, init_db
from .schema import SCHEMA_VERSION

__all__ = ["DB_PATH", "SCHEMA_VERSION", "get_db", "init_db"]
