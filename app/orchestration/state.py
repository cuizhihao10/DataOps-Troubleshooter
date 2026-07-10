"""Canonical orchestration state export.

Keeping this import boundary allows LangGraph integration to reuse the validated
domain state without passing loose dictionaries between nodes.
"""

from app.domain.models import AgentState

__all__ = ["AgentState"]
