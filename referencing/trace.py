"""
Optional resolution tracing.

A `ResolutionTrace` records exactly what happens during a single call (and
any nested calls) to `referencing._core.Resolver.lookup`, without performing
any additional retrieval or re-resolution:

* the input reference and the base URI it is resolved against,
* resource switches (including which resource was found in the registry and
  when a retrieval function is invoked),
* each JSON Pointer step (including entry into a new subresource),
* anchor candidates considered during dynamic resolution and the candidate
  ultimately adopted,
* and the point at which resolution failed when it does.

Enable tracing via `referencing._core.Resolver.traced`, which returns both a
resolver with recording enabled and the `ResolutionTrace` it appends to.
Traces are recorded eagerly during lookup, so they are available both on the
returned `~referencing._core.Resolved` object as well as attached to any
`referencing.exceptions.Unresolvable` raised.
"""

from __future__ import annotations

from typing import Any

import attrs
from referencing.typing import URI


@attrs.frozen
class TraceEvent:
    """
    Something which happened during reference resolution.

    Each event belongs to a single lookup, identified by ``lookup_id``.
    Nested lookups (those triggered while resolving an outer reference) have
    a distinct ``lookup_id`` and may be correlated to their parent via
    `TraceEvent.parent_lookup_id`.
    """

    lookup_id: int
    parent_lookup_id: int | None


@attrs.frozen
class LookupStarted(TraceEvent):
    """
    A lookup has begun.

    ``ref`` is the reference as passed in, while ``base_uri`` is the base
    URI the (possibly relative) reference is resolved against.
    """

    ref: URI
    base_uri: URI


@attrs.frozen
class ReferenceResolved(TraceEvent):
    """
    The input reference has been combined with the base URI.

    ``uri`` is the resulting (fragmentless) resource URI and ``fragment`` is
    any remaining fragment (an empty string when there is none).
    """

    uri: URI
    fragment: str


@attrs.frozen
class ResourceFoundInRegistry(TraceEvent):
    """
    The resource was already present (or found by crawling) in the registry.
    """

    uri: URI


@attrs.frozen
class RetrievalStarted(TraceEvent):
    """
    The configured retrieval function is about to be called.
    """

    uri: URI


@attrs.frozen
class RetrievalSucceeded(TraceEvent):
    """
    The retrieval function returned a resource.
    """

    uri: URI


@attrs.frozen
class PointerStep(TraceEvent):
    """
    A single JSON Pointer segment was followed.

    ``path`` is the accumulated pointer up to and including this step, and
    ``subresource`` is true when following the step entered a new resource
    (with its own base URI).
    """

    segment: int | str
    path: str
    subresource: bool


@attrs.frozen
class AnchorFound(TraceEvent):
    """
    An anchor was looked up from the registry.

    ``dynamic`` says whether the found anchor is a dynamic anchor. Missing
    (plain-name) anchors are represented via `AnchorCandidate` events with
    ``found`` false instead.
    """

    uri: URI
    name: str
    dynamic: bool


@attrs.frozen
class AnchorCandidate(TraceEvent):
    """
    An anchor considered while walking the dynamic scope.

    ``found`` is false when the given URI in the dynamic scope has no
    anchor with the relevant name; ``dynamic`` is false when an anchor with
    the name exists but is not itself a dynamic anchor (and therefore is
    skipped).
    """

    uri: URI
    name: str
    found: bool
    dynamic: bool


@attrs.frozen
class AnchorAdopted(TraceEvent):
    """
    The anchor whose resource resolution ultimately adopts.

    For dynamic anchors this is the outermost dynamic anchor found in the
    dynamic scope; for plain anchors it is the anchor directly named by the
    reference.
    """

    uri: URI | None
    name: str
    dynamic: bool


@attrs.frozen
class LookupSucceeded(TraceEvent):
    """
    The lookup completed successfully.

    ``uri`` is the base URI of the resolver returned alongside the resolved
    contents.
    """

    uri: URI


@attrs.frozen
class LookupFailed(TraceEvent):
    """
    The lookup failed at this point.

    ``stage`` names the stage of resolution which failed (e.g. ``retrieve``,
    ``pointer`` or ``anchor``), and ``error_type`` is the type name of the
    underlying exception.
    """

    stage: str
    error_type: str
    detail: str


class _Frame:
    __slots__ = ("lookup_id", "parent_lookup_id", "failed")

    def __init__(self, lookup_id: int, parent_lookup_id: int | None):
        self.lookup_id = lookup_id
        self.parent_lookup_id = parent_lookup_id
        self.failed = False


class ResolutionTrace:
    """
    A mutable record of the events of one or more related lookups.

    Instances are produced by `referencing._core.Resolver.traced`; they are
    not normally constructed directly.
    """

    def __init__(self) -> None:
        self._events: list[TraceEvent] = []
        self._stack: list[_Frame] = []
        self._next_id = 0

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        """
        All recorded events, in the order they occurred.
        """
        return tuple(self._events)

    def for_lookup(self, lookup_id: int) -> tuple[TraceEvent, ...]:
        """
        Return the events belonging to the given lookup only.
        """
        return tuple(
            event for event in self._events if event.lookup_id == lookup_id
        )

    def begin(self, ref: URI, base_uri: URI) -> int:
        lookup_id = self._next_id
        self._next_id += 1
        parent_lookup_id = self._stack[-1].lookup_id if self._stack else None
        self._stack.append(_Frame(lookup_id, parent_lookup_id))
        self._append(
            LookupStarted(
                lookup_id=lookup_id,
                parent_lookup_id=parent_lookup_id,
                ref=ref,
                base_uri=base_uri,
            ),
        )
        return lookup_id

    def reference_resolved(self, uri: URI, fragment: str) -> None:
        self._append(
            ReferenceResolved(
                lookup_id=self._current(),
                parent_lookup_id=self._parent(),
                uri=uri,
                fragment=fragment,
            ),
        )

    def found_in_registry(self, uri: URI) -> None:
        self._append(
            ResourceFoundInRegistry(
                lookup_id=self._current(),
                parent_lookup_id=self._parent(),
                uri=uri,
            ),
        )

    def retrieval_started(self, uri: URI) -> None:
        self._append(
            RetrievalStarted(
                lookup_id=self._current(),
                parent_lookup_id=self._parent(),
                uri=uri,
            ),
        )

    def retrieval_succeeded(self, uri: URI) -> None:
        self._append(
            RetrievalSucceeded(
                lookup_id=self._current(),
                parent_lookup_id=self._parent(),
                uri=uri,
            ),
        )

    def pointer_step(
        self,
        segment: int | str,
        path: str,
        subresource: bool,
    ) -> None:
        self._append(
            PointerStep(
                lookup_id=self._current(),
                parent_lookup_id=self._parent(),
                segment=segment,
                path=path,
                subresource=subresource,
            ),
        )

    def anchor_found(self, uri: URI, name: str, dynamic: bool) -> None:
        self._append(
            AnchorFound(
                lookup_id=self._current(),
                parent_lookup_id=self._parent(),
                uri=uri,
                name=name,
                dynamic=dynamic,
            ),
        )

    def anchor_candidate(
        self,
        uri: URI,
        name: str,
        found: bool,
        dynamic: bool,
    ) -> None:
        self._append(
            AnchorCandidate(
                lookup_id=self._current(),
                parent_lookup_id=self._parent(),
                uri=uri,
                name=name,
                found=found,
                dynamic=dynamic,
            ),
        )

    def anchor_adopted(
        self,
        uri: URI | None,
        name: str,
        dynamic: bool,
    ) -> None:
        self._append(
            AnchorAdopted(
                lookup_id=self._current(),
                parent_lookup_id=self._parent(),
                uri=uri,
                name=name,
                dynamic=dynamic,
            ),
        )

    def succeeded(self, uri: URI) -> None:
        self._append(
            LookupSucceeded(
                lookup_id=self._current(),
                parent_lookup_id=self._parent(),
                uri=uri,
            ),
        )
        self._stack.pop()

    def failed(self, stage: str, error: Exception) -> None:
        if self._stack and not self._stack[-1].failed:
            self._stack[-1].failed = True
            self._append(
                LookupFailed(
                    lookup_id=self._current(),
                    parent_lookup_id=self._parent(),
                    stage=stage,
                    error_type=type(error).__name__,
                    detail=str(error),
                ),
            )
        self._stack.pop()

    def _append(self, event: TraceEvent) -> None:
        self._events.append(event)

    def _current(self) -> int:
        return self._stack[-1].lookup_id

    def _parent(self) -> int | None:
        return self._stack[-1].parent_lookup_id


class _NullTrace:
    """
    A trace-shaped object used when recording is disabled.

    Every method is a no-op, and attribute lookup on instances is cheap
    (a single shared class attribute), so untraced resolution pays only
    for passing the object around. Frame tracking methods exist so that the
    same lookup code runs in both modes.
    """

    __slots__ = ()

    def begin(self, ref: URI, base_uri: URI) -> int:
        return -1

    def succeeded(self, uri: URI) -> None: ...

    def failed(self, stage: str, error: Exception) -> None: ...

    def reference_resolved(self, uri: URI, fragment: str) -> None: ...

    def found_in_registry(self, uri: URI) -> None: ...

    def retrieval_started(self, uri: URI) -> None: ...

    def retrieval_succeeded(self, uri: URI) -> None: ...

    def pointer_step(
        self,
        segment: int | str,
        path: str,
        subresource: bool,
    ) -> None: ...

    def anchor_found(self, uri: URI, name: str, dynamic: bool) -> None: ...

    def anchor_candidate(
        self,
        uri: URI,
        name: str,
        found: bool,
        dynamic: bool,
    ) -> None: ...

    def anchor_adopted(
        self,
        uri: URI | None,
        name: str,
        dynamic: bool,
    ) -> None: ...


NULL_TRACE: Any = _NullTrace()
