"""
Packing resources, along with everything they reference, for offline use.
"""

from __future__ import annotations

from collections.abc import Mapping, Set
from typing import Any, Generic
from urllib.parse import urldefrag, urljoin
import hashlib
import json

from referencing import exceptions
from referencing._attrs import frozen
from referencing._core import (
    _UNSET,  # type: ignore[reportPrivateUsage]
    Registry,
    Resource,
    Specification,
    _Unset,  # type: ignore[reportPrivateUsage]
)
from referencing.typing import D, URI


def _digest_of(contents: Any) -> str:
    serialized = json.dumps(contents, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


@frozen
class Manifest:
    """
    A record of what a `Bundle` contains, and of what it could not find.
    """

    #: A digest of the contents of each resource in the bundle, by URI.
    digests: Mapping[URI, str]

    #: URIs which were referenced from the bundle but could not be found.
    missing: Set[URI] = frozenset()


@frozen
class Bundle(Generic[D]):
    """
    A root resource along with everything it (transitively) references.

    Bundles are intended to be serialized and handed off to environments
    where no external retrieval is possible, then turned back into a
    `Registry` there via `Bundle.load`.

    Resources are kept as-is, each under its own URI (with any internal
    IDs, e.g. :kw:`$id`, left intact) -- nothing is inlined into the root
    document.
    """

    #: The URI of the root resource the bundle was packed from.
    root: URI

    #: The contents of each resource in the bundle, by URI.
    resources: Mapping[URI, D]

    #: The bundle's manifest.
    manifest: Manifest

    def load(
        self,
        default_specification: (
            type[Specification[D]] | Specification[D]
        ) = Specification,
    ) -> Registry[D]:
        """
        Verify the bundle against its manifest, then build a registry.

        The returned registry contains exactly the resources present in
        the bundle, and will not attempt to retrieve anything further --
        lookups of URIs which are not present (e.g. ones recorded as
        missing in the manifest) fail with `exceptions.NoSuchResource`.

        Raises:

            `exceptions.DigestMismatch`

                if a resource's contents do not match the manifest

        """
        for uri, contents in self.resources.items():
            if self.manifest.digests.get(uri) != _digest_of(contents):
                raise exceptions.DigestMismatch(uri=uri)
        return Registry().with_contents(
            self.resources.items(),
            default_specification=default_specification,
        )


def pack(
    root: D | Resource[D],
    registry: Registry[D] | None = None,
    specification: Specification[D] | _Unset = _UNSET,
) -> Bundle[D]:
    """
    Pack a root resource and everything it references into a `Bundle`.

    The root may be given either as a `Resource` or as raw contents, in
    which case its specification is detected (or the given ``specification``
    is used).

    Referenced resources are looked up in -- or retrieved via -- the given
    registry. Each distinct resource is included exactly once, even when
    references are circular, and any references which cannot be found are
    recorded in the bundle's manifest.

    Raises:

        `exceptions.CannotDetermineSpecification`

            if the root was given as raw contents and its specification
            could not be detected

    """
    if isinstance(root, Resource):
        resource = root
    elif specification is _UNSET:
        resource = Resource.from_contents(root)
    else:
        resource = specification.create_resource(root)

    if registry is None:
        registry = Registry()

    resources: dict[URI, Resource[D]] = {}
    missing: set[URI] = set()
    seen: set[URI] = set()
    frontier: list[tuple[URI, Resource[D]]] = []

    def enqueue(uri: URI, resource: Resource[D]) -> None:
        id = resource.id()
        if id is not None:
            uri = urljoin(uri, id)
        uri = uri.rstrip("#")
        if uri not in seen:
            seen.add(uri)
            frontier.append((uri, resource))

    enqueue("", resource)
    while frontier:
        uri, resource = frontier.pop()
        resources[uri] = resource
        for reference in resource.references():
            target = urldefrag(urljoin(uri, reference.uri)).url
            if target in seen or target in missing:
                continue
            try:
                retrieved = registry.get_or_retrieve(target)
            except (exceptions.NoSuchResource, exceptions.Unretrievable):
                missing.add(target)
            else:
                registry = retrieved.registry
                enqueue(target, retrieved.value)

    return Bundle(
        root=next(iter(resources)),
        resources={
            uri: each.contents for uri, each in resources.items()
        },
        manifest=Manifest(
            digests={
                uri: _digest_of(each.contents)
                for uri, each in resources.items()
            },
            missing=frozenset(missing),
        ),
    )
