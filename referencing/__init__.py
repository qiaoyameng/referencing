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
from referencing.packaging import (
    Manifest,
    ManifestEntry,
    MissingResource,
    Package,
    pack,
)

__all__ = [
    "Anchor",
    "Manifest",
    "ManifestEntry",
    "MissingResource",
    "Package",
    "Reference",
    "ReferenceKind",
    "Registry",
    "Resource",
    "Specification",
    "pack",
]
