"""Provider-neutral agent runtime.

Deliberately named `agent_runtime` rather than `agents`: the OpenAI Agents SDK
occupies the top-level `agents` module and is imported from inside this package.
"""

from .prompts import INSTRUCTIONS
from .runtime import HistoryTurn

__all__ = ["INSTRUCTIONS", "HistoryTurn"]
