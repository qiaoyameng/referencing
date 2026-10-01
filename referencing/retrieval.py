"""
Helpers related to (dynamic) resource retrieval.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING, Generic, TypeVar

from attrs import field

from referencing import Resource
from referencing._attrs import frozen
from referencing.typing import D

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from referencing.typing import URI, Retrieve

#: A serialized document (e.g. a JSON string)
_T = TypeVar("_T", default=str)


@frozen
class RetrievalRequest(Generic[D]):
    """
    A request to access a resource, presented to a retrieval policy.

    A policy receives the *raw* reference as it appeared in the originating
    document, the *absolute* URI it was resolved to (relative to the
    originating resource), the resource which initiated the resolution (or
    ``None`` for a direct registry lookup), and the chain of resources already
    traversed during the current nested resolution.

    All URIs in ``chain`` are given as they were resolved at each hop of the
    chain, so a policy can reason about where a reference is coming from
    regardless of how it was encoded or made relative.
    """

    #: The reference exactly as it appeared in the originating document.
    raw_ref: URI

    #: The absolute URI the reference was resolved against.
    uri: URI

    #: The resource the reference originated from, if there was one.
    origin: Resource[D] | None = None

    #: The chain of absolute URIs already traversed, root-first.
    chain: Sequence[URI] = field(default=())


@frozen
class Allow:
    """
    A policy decision allowing the requested retrieval to proceed normally.
    """


@frozen
class Deny:
    """
    A policy decision refusing the requested retrieval.

    An optional human-readable ``reason`` is attached to the resulting
    `referencing.exceptions.RetrievalDenied`.
    """

    reason: str | None = None


@frozen
class RetrieveWith(Generic[D]):
    """
    A policy decision handing the request to a specific retriever.

    The given retriever is called *instead of* the registry's configured
    retriever, even if the requested URI was already present in-memory.
    """

    retrieve: "Retrieve[D]"


if TYPE_CHECKING:
    #: Allow the request, deny it, or route it to a chosen retriever.
    Decision = Allow | Deny | RetrieveWith[D]

#: A reusable allow decision.
ALLOW = Allow()

#: A reusable deny decision without a reason.
DENY = Deny()


def to_cached_resource(
    cache: Callable[[Retrieve[D]], Retrieve[D]] | None = None,
    loads: Callable[[_T], D] = json.loads,
    from_contents: Callable[[D], Resource[D]] = Resource.from_contents,
) -> Callable[[Callable[[URI], _T]], Retrieve[D]]:
    """
    Create a retriever which caches its return values from a simpler callable.

    Takes a function which returns things like serialized JSON (strings) and
    returns something suitable for passing to `Registry` as a retrieve
    function.

    This decorator both reduces a small bit of boilerplate for a common case
    (deserializing JSON from strings and creating `Resource` objects from the
    result) as well as makes the probable need for caching a bit easier.
    Retrievers which otherwise do expensive operations (like hitting the
    network) might otherwise be called repeatedly.

    Examples
    --------

    .. testcode::

        from referencing import Registry
        from referencing.typing import URI
        import referencing.retrieval


        @referencing.retrieval.to_cached_resource()
        def retrieve(uri: URI):
            print(f"Retrieved {uri}")

            # Normally, go get some expensive JSON from the network, a file ...
            return '''
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "foo": "bar"
                }
            '''

        one = Registry(retrieve=retrieve).get_or_retrieve("urn:example:foo")
        print(one.value.contents["foo"])

        # Retrieving the same URI again reuses the same value (and thus doesn't
        # print another retrieval message here)
        two = Registry(retrieve=retrieve).get_or_retrieve("urn:example:foo")
        print(two.value.contents["foo"])

    .. testoutput::

        Retrieved urn:example:foo
        bar
        bar

    """
    if cache is None:
        cache = lru_cache(maxsize=None)

    def decorator(retrieve: Callable[[URI], _T]):
        @cache
        def cached_retrieve(uri: URI):
            response = retrieve(uri)
            contents = loads(response)
            return from_contents(contents)

        return cached_retrieve

    return decorator
