"""
Cross-specification, implementation-agnostic JSON referencing.
"""

from referencing._core import (
    Anchor,
    Reference,
    ReferenceKind,
    Registry,
    Resource,
    Specification,
)
from referencing._packaging import Bundle, Manifest, pack

__all__ = [
    "Anchor",
    "Bundle",
    "Manifest",
    "Reference",
    "ReferenceKind",
    "Registry",
    "Resource",
    "Specification",
    "pack",
]
