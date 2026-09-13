"""Non-LLM domain services: portfolio tracking, feedback learning, price
watchlist, real-time alerts, and job scheduling.

These modules hold business logic and persistence that isn't itself an LLM
agent (that's `oracle.agents`) but does more than the thin data helpers in
`oracle.db`/`oracle.models`. Grouped here so the top level of `oracle/`
stays limited to framework wiring (`config`, `state`, `graph`, `nodes`,
`main`, `db`, `models`, `observability`).
"""

from __future__ import annotations
