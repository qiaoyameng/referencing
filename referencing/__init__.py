"""
Cross-specification, implementation-agnostic JSON referencing.
"""

from referencing._core import (
    Anchor,
    Registry,
    ResolutionTrace,
    Resource,
    Specification,
    TraceEvent,
)

__all__ = [
    "Anchor",
    "Registry",
    "ResolutionTrace",
    "Resource",
    "Specification",
    "TraceEvent",
]
