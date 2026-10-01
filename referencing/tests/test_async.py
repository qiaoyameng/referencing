"""
Tests for asynchronous registries and resolvers.

A fake, latency-controllable store is used so that fetch timing (including
cancellation while a fetch is in flight) is deterministic without any real
I/O.
"""

from __future__ import annotations

import asyncio
import warnings

import pytest

from referencing import (
    AsyncRegistry,
    AsyncResolver,
    Registry,
    Resource,
    exceptions,
)
from referencing.jsonschema import DRAFT202012

DIALECT = "https://json-schema.org/draft/2020-12/schema"


class FakeStore:
    """
    An asynchronous store with fully controllable per-URI latency.

    Resources are only handed back after either sleeping for ``delay``
    seconds or, for URIs with a gate, once the gate is set. This lets tests
    deterministically cancel a fetch which has genuinely started but not
    finished.
    """

    def __init__(self, documents, delay=0.02):
        self._documents = dict(documents)
        self.delay = delay
        self.calls: list[str] = []
        self.cancelled: list[str] = []
        self.began = asyncio.Event()
        self._gates: dict[str, asyncio.Event] = {}

    def gate(self, uri: str) -> asyncio.Event:
        event = asyncio.Event()
        self._gates[uri] = event
        return event

    async def retrieve(self, uri: str) -> Resource:
        self.calls.append(uri)
        self.began.set()
        gate = self._gates.get(uri)
        try:
            if gate is not None:
                await gate.wait()
            else:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled.append(uri)
            raise
        try:
            return self._documents[uri]
        except KeyError:
            raise exceptions.NoSuchResource(ref=uri)


def schema(contents) -> Resource:
    return Resource.from_contents(
        {"$schema": DIALECT, **contents} if isinstance(contents, dict)
        else contents,
        default_specification=DRAFT202012,
    )


class TestAsyncCrossDocumentChain:
    def test_relative_refs_pointers_and_crawling(self):
        async def scenario():
            root = schema(
                {
                    "$id": "http://example.com/a",
                    "properties": {"b": {"$ref": "b"}},
                },
            )
            doc_b = schema(
                {
                    "$id": "b",
                    "$defs": {
                        "inner": {"$id": "nested", "type": "integer"},
                    },
                    "properties": {"c": {"$ref": "c#/definitions/target"}},
                },
            )
            doc_c = schema(
                {
                    "$id": "c",
                    "definitions": {"target": {"type": "string"}},
                },
            )
            store = FakeStore(
                {
                    "http://example.com/b": doc_b,
                    "http://example.com/c": doc_c,
                },
            )
            registry = AsyncRegistry(retrieve=store.retrieve).with_resource(
                "http://example.com/a",
                root,
            )
            resolver = registry.resolver()

            first = await resolver.lookup("http://example.com/a")
            assert store.calls == []  # already in memory, no fetch needed

            second = await first.resolver.lookup("b")
            assert second.contents is doc_b.contents
            assert store.calls == ["http://example.com/b"]

            # A subresource discovered by crawling the fetched document is
            # available without another fetch.
            inner = await second.resolver.lookup("nested")
            assert inner.contents["type"] == "integer"
            assert inner.contents["$id"] == "nested"
            assert store.calls == ["http://example.com/b"]

            # Fetching the next document and resolving a JSON pointer into it
            # reuses the existing (synchronous) pointer semantics.
            third = await second.resolver.lookup("c#/definitions/target")
            assert third.contents is doc_c.contents["definitions"]["target"]
            assert store.calls == ["http://example.com/b", "http://example.com/c"]

            # The returned registries carry the fetched resources ...
            assert "http://example.com/b" in second.resolver._registry
            assert "http://example.com/c" in third.resolver._registry
            assert "http://example.com/nested" in inner.resolver._registry
            assert "http://example.com/nested" not in second.resolver._registry

            # ... while every caller-held registry stays untouched.
            assert "http://example.com/b" not in registry
            assert "http://example.com/c" not in registry
            assert "http://example.com/c" not in second.resolver._registry

            # A resource already fetched is not fetched again.
            again = await third.resolver.lookup("http://example.com/b")
            assert again.contents is doc_b.contents
            assert store.calls == [
                "http://example.com/b",
                "http://example.com/c",
            ]

        asyncio.run(scenario())


class TestAsyncDynamicAnchors:
    def test_dynamic_anchor_across_fetched_documents(self):
        async def scenario():
            root_contents = {
                "$schema": DIALECT,
                "$id": "http://example.com",
                "$dynamicAnchor": "fooAnchor",
                "$defs": {
                    "foo": {
                        "$id": "foo",
                        "$dynamicAnchor": "fooAnchor",
                        "$defs": {"baz": {"$dynamicAnchor": "fooAnchor"}},
                    },
                },
            }
            root = Resource.from_contents(root_contents)
            true_resource = DRAFT202012.create_resource(True)

            store = FakeStore(
                {
                    # Both intermediate documents are fetched asynchronously.
                    "http://example.com/foo/": true_resource,
                    "http://example.com/foo/bar": root,
                },
            )
            registry = AsyncRegistry(retrieve=store.retrieve).with_resource(
                "http://example.com",
                root,
            )
            resolver = registry.resolver()

            first = await resolver.lookup("http://example.com")
            second = await first.resolver.lookup("foo/")
            assert second.contents is True
            fetched_bar = await second.resolver.lookup("bar")

            # The plain-name fragment is resolved entirely in memory, and the
            # dynamic anchor walks the dynamic scope exactly like the
            # synchronous resolver.
            fourth = await fetched_bar.resolver.lookup("#fooAnchor")
            assert fourth.contents == root_contents

            # It matches a fully in-memory synchronous resolution.
            sync_registry = Registry().with_resources(
                [
                    ("http://example.com", root),
                    ("http://example.com/foo/", true_resource),
                    ("http://example.com/foo/bar", root),
                ],
            )
            sync_resolver = sync_registry.resolver()
            sync_first = sync_resolver.lookup("http://example.com")
            sync_second = sync_first.resolver.lookup("foo/")
            sync_third = sync_second.resolver.lookup("bar")
            sync_fourth = sync_third.resolver.lookup("#fooAnchor")
            assert fourth.contents == sync_fourth.contents

        asyncio.run(scenario())


class TestAsyncCancellation:
    def test_cancel_propagates_into_the_fetch(self):
        resource = schema({"$id": "urn:s", "type": "string"})
        store = FakeStore({"urn:s": resource})

        async def scenario():
            registry = AsyncRegistry(retrieve=store.retrieve)
            gate = store.gate("urn:s")
            store.began.clear()
            began = store.began

            task = asyncio.create_task(registry.get_or_retrieve("urn:s"))
            await began.wait()  # the real fetch is now pending
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # The actual fetch operation saw the cancellation ...
            assert store.cancelled == ["urn:s"]
            # ... and no unfinished resource leaked into any registry.
            assert "urn:s" not in registry

            # Releasing the gate and retrying performs a fresh, successful
            # fetch, returning a new immutable registry.
            gate.set()
            retrieved = await registry.get_or_retrieve("urn:s")
            assert retrieved.value is resource
            assert "urn:s" in retrieved.registry
            assert "urn:s" not in registry

        asyncio.run(scenario())

    def test_completed_resources_survive_a_later_cancellation(self):
        doc_b = schema({"$id": "b", "type": "object"})
        doc_c = schema({"$id": "c", "type": "number"})
        store = FakeStore({"http://example.com/b": doc_b}, delay=0.01)

        async def scenario():
            root = schema({"$id": "http://example.com/a"})
            registry = AsyncRegistry(retrieve=store.retrieve).with_resource(
                "http://example.com/a",
                root,
            )
            first = await registry.resolver().lookup("http://example.com/a")
            second = await first.resolver.lookup("b")  # fully fetched

            # A *later*, unfinished fetch for a different URI.
            store._documents["http://example.com/c"] = doc_c
            gate = store.gate("http://example.com/c")
            store.began.clear()
            began = store.began
            task = asyncio.create_task(second.resolver.lookup("c"))
            await began.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            gate.set()

            # The fully fetched resource is still present ...
            assert "http://example.com/b" in second.resolver._registry
            # ... the unfinished one is not, anywhere.
            assert "http://example.com/c" not in second.resolver._registry
            assert "http://example.com/c" not in registry
            assert "http://example.com/b" not in registry
            assert store.cancelled == ["http://example.com/c"]

        asyncio.run(scenario())

    def test_cancel_in_resolver_lookup_is_not_normalized(self):
        store = FakeStore({})

        async def scenario():
            registry = AsyncRegistry(retrieve=store.retrieve)
            gate = store.gate("urn:slow")
            store.began.clear()
            began = store.began
            task = asyncio.create_task(registry.resolver().lookup("urn:slow"))
            await began.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            gate.set()

        asyncio.run(scenario())


class TestAsyncBranchesEvolveIndependently:
    def test_two_branches_do_not_share_fetches_or_state(self):
        doc_b = schema({"$id": "b", "type": "object"})
        doc_c = schema({"$id": "c", "type": "boolean"})
        store = FakeStore(
            {"http://example.com/b": doc_b, "http://example.com/c": doc_c},
            delay=0.02,
        )

        async def scenario():
            root = schema({"$id": "http://example.com/a"})
            registry = AsyncRegistry(retrieve=store.retrieve).with_resource(
                "http://example.com/a",
                root,
            )
            first = await registry.resolver().lookup("http://example.com/a")

            branch_b, branch_c = await asyncio.gather(
                asyncio.create_task(first.resolver.lookup("b")),
                asyncio.create_task(first.resolver.lookup("c")),
            )

            # Each branch only observes the resource it fetched.
            assert "http://example.com/b" in branch_b.resolver._registry
            assert "http://example.com/c" not in branch_b.resolver._registry
            assert "http://example.com/c" in branch_c.resolver._registry
            assert "http://example.com/b" not in branch_c.resolver._registry

            # The shared starting point is never mutated.
            assert len(registry) == 1
            assert "http://example.com/b" not in registry
            assert "http://example.com/c" not in registry

            # The branches continue to evolve independently afterwards.
            merged = await branch_b.resolver.lookup("c")
            assert "http://example.com/c" in merged.resolver._registry
            assert "http://example.com/b" in merged.resolver._registry
            assert "http://example.com/c" not in branch_b.resolver._registry
            assert "http://example.com/b" not in branch_c.resolver._registry

        asyncio.run(scenario())


class TestSynchronousLookupWithAsyncRetriever:
    def test_registry_raises_a_clear_error(self):
        async def retrieve(uri):  # pragma: no cover - never awaited
            raise AssertionError("should not be called synchronously")

        registry = Registry(retrieve=retrieve)
        with pytest.raises(exceptions.AsyncRetrievalRequired) as exc_info:
            registry.get_or_retrieve("urn:missing")
        assert exc_info.value.ref == "urn:missing"
        assert "AsyncRegistry" in str(exc_info.value)

    def test_resolver_raises_with_the_original_reference(self):
        async def retrieve(uri):  # pragma: no cover - never awaited
            raise AssertionError("should not be called synchronously")

        registry = Registry(retrieve=retrieve)
        resolver = registry.resolver(base_uri="http://example.com/base/")
        with pytest.raises(exceptions.AsyncRetrievalRequired) as exc_info:
            resolver.lookup("missing")
        assert exc_info.value.ref == "missing"

    def test_in_memory_lookups_remain_synchronous(self):
        async def retrieve(uri):  # pragma: no cover - never awaited
            raise AssertionError("should not be called synchronously")

        resource = schema({"$id": "urn:here", "properties": {"x": {}}})
        registry = Registry(retrieve=retrieve).with_resource("urn:here", resource)
        resolved = registry.resolver().lookup("urn:here#/properties/x")
        assert resolved.contents == {}

    def test_callable_instance_returning_a_coroutine(self):
        class Store:
            def __call__(self, uri):
                return self._retrieve(uri)

            async def _retrieve(self, uri):
                raise AssertionError("the coroutine must not run synchronously")

        with warnings.catch_warnings(record=True) as record:
            warnings.simplefilter("always")
            with pytest.raises(exceptions.AsyncRetrievalRequired):
                Registry(retrieve=Store()).get_or_retrieve("urn:missing")
        assert not [
            warning
            for warning in record
            if "never awaited" in str(warning.message)
        ]

        async def scenario():
            class FullStore:
                def __call__(self, uri):
                    return self._retrieve(uri)

                async def _retrieve(self, uri):
                    return Resource.opaque({"fetched": True})

            retrieved = await AsyncRegistry(retrieve=FullStore()).get_or_retrieve(
                "urn:ok",
            )
            assert retrieved.value.contents == {"fetched": True}

        asyncio.run(scenario())

    def test_async_registry_in_memory_lookups(self):
        async def retrieve(uri):  # pragma: no cover - never awaited
            raise AssertionError("should not be called synchronously")

        resource = schema({"$id": "urn:here"})
        registry = AsyncRegistry(retrieve=retrieve).with_resource(
            "urn:here",
            resource,
        )
        assert registry["urn:here"] is resource
        assert registry.contents("urn:here") == resource.contents

    def test_matmul_returns_a_new_async_registry(self):
        async def retrieve(uri):  # pragma: no cover - never awaited
            raise AssertionError("should not be called synchronously")

        resource = schema({"$id": "urn:new"})
        registry = AsyncRegistry(retrieve=retrieve)
        evolved = resource @ registry
        assert isinstance(evolved, AsyncRegistry)
        assert "urn:new" in evolved
        assert "urn:new" not in registry


class TestAsyncRetrievalErrors:
    def test_no_such_resource_is_unresolvable(self):
        store = FakeStore({}, delay=0)

        async def scenario():
            resolver = AsyncRegistry(retrieve=store.retrieve).resolver()
            with pytest.raises(exceptions.Unresolvable) as exc_info:
                await resolver.lookup("urn:nope")
            assert exc_info.value.ref == "urn:nope"

        asyncio.run(scenario())

    def test_arbitrary_exception_is_unretrievable(self):
        async def broken(uri):
            raise RuntimeError("boom")

        async def scenario():
            registry = AsyncRegistry(retrieve=broken)
            with pytest.raises(exceptions.Unretrievable):
                await registry.get_or_retrieve("urn:boom")
            assert "urn:boom" not in registry

        asyncio.run(scenario())

    def test_from_registry_preserves_in_memory_resources(self):
        async def retrieve(uri):
            raise exceptions.NoSuchResource(ref=uri)

        async def scenario():
            resource = schema({"$id": "urn:existing"})
            sync_registry = Registry().with_resource("urn:existing", resource)
            async_registry = AsyncRegistry.from_registry(
                sync_registry,
                retrieve=retrieve,
            )
            retrieved = await async_registry.get_or_retrieve("urn:existing")
            assert retrieved.value is resource

            with pytest.raises(exceptions.Unresolvable):
                await async_registry.resolver().lookup("urn:missing")

        asyncio.run(scenario())
