"""Late binding for the historical controller module's injectable dependencies.

Tests and embedders replace helpers, clocks and provider constructors on
``supervisor.controller``. Importing their values here would silently break that
contract. Resolve on use, without copying dictionaries or rebinding function code.
This module carries dependencies only; it never carries per-run mutable state.
"""

import importlib
from typing import Any


def __getattr__(name: str) -> Any:
    return getattr(importlib.import_module("supervisor.controller"), name)
