"""
Cross-specification, implementation-agnostic JSON referencing.
"""

from referencing._core import Anchor, Registry, Resource, Specification
from referencing.trace import (
    AnchorAdopted,
    AnchorCandidate,
    AnchorFound,
    LookupFailed,
    LookupStarted,
    LookupSucceeded,
    PointerStep,
    ReferenceResolved,
    ResolutionTrace,
    ResourceFoundInRegistry,
    RetrievalStarted,
    RetrievalSucceeded,
    TraceEvent,
)

__all__ = [
    "Anchor",
    "AnchorAdopted",
    "AnchorCandidate",
    "AnchorFound",
    "LookupFailed",
    "LookupStarted",
    "LookupSucceeded",
    "PointerStep",
    "ReferenceResolved",
    "Registry",
    "ResolutionTrace",
    "Resource",
    "ResourceFoundInRegistry",
    "RetrievalStarted",
    "RetrievalSucceeded",
    "Specification",
    "TraceEvent",
]
