"""Compartment - high-security, fully offline, encrypted vector memory for AI agents."""

__version__ = "1.15.2"

from . import offline_guard as _og

_og.activate_from_env()
