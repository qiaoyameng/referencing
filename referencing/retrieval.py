"""
Helpers related to (dynamic) resource retrieval.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from referencing import Resource
from referencing._attrs import frozen
from referencing.typing import URI, D, Retrieve

if TYPE_CHECKING:
    from collections.abc import Callable

#: A serialized document (e.g. a JSON string)
_T = TypeVar("_T", default=str)


@frozen
class RetrievalRequest(Generic[D]):
    """
    Context made available to a retrieval policy.

    ``chain`` contains the URIs of the resources which led to this retrieval,
    ordered from the root resource toward the resource containing the current
    reference. ``retrieved_resources`` de-duplicates that chain, while
    ``external_resources`` contains only resources which required retrieval.
    ``depth`` is the depth the requested resource would have if retrieval is
    allowed.
    """

    ref: URI
    uri: URI
    source: Resource[D] | None
    chain: tuple[URI, ...]
    retrieved_resources: tuple[URI, ...] = ()
    external_resources: tuple[URI, ...] = ()
    depth: int = 1


@frozen
class Allow:
    """
    Allow retrieval using the registry's configured retriever.
    """


@frozen
class Deny:
    """
    Deny retrieval without calling any retriever.
    """

    reason: str | None = None


@frozen
class RetrieveWith(Generic[D]):
    """
    Allow retrieval, but use the given retriever rather than the default one.
    """

    retrieve: Retrieve[D]


class RetrievalPolicy(Protocol[D]):
    """
    Decide whether a resource reference may be retrieved.
    """

    def __call__(
        self,
        request: RetrievalRequest[D],
    ) -> Allow | Deny | RetrieveWith[D]:
        """
        Return an `Allow`, `Deny`, or `RetrieveWith` decision.
        """


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
