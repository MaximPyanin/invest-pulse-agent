"""LLM system-prompt constants for every ORACLE agent.

Each module here holds exactly the prompt text a given agent sends to the
LLM as its system message. Keeping prompt text out of `agents/*.py` lets
the agent modules focus on orchestration (building the request, calling the
model, parsing the response) while the prompt copy lives somewhere it can
be read and edited on its own.

Nothing in this package imports from `oracle.agents` — the dependency runs
one way, agents -> prompts.
"""

from __future__ import annotations
