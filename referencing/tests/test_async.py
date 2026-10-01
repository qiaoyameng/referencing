import asyncio

import pytest

from referencing import AsyncRegistry, Registry, Resource, exceptions
from referencing.jsonschema import DRAFT202012


class ControllableStore:
    def __init__(self, documents):
        self.documents = documents
        self.started = {uri: asyncio.Event() for uri in documents}
        self.released = {uri: asyncio.Event() for uri in documents}
        self.calls = []
        self.cancelled = set()

    def release(self, uri):
        self.released[uri].set()

    async def wait_started(self, uri):
        await self.started[uri].wait()

    async def retrieve(self, uri):
        self.calls.append(uri)
        self.started[uri].set()
        try:
            await self.released[uri].wait()
        except asyncio.CancelledError:
            self.cancelled.add(uri)
            raise
        return DRAFT202012.create_resource(self.documents[uri])


def schema(contents):
    return DRAFT202012.create_resource(contents)


def test_in_memory_pointer_does_not_retrieve():
    async def retrieve(uri):
        raise AssertionError(uri)

    root = schema(
        {
            "$id": "urn:root",
            "properties": {
                "thing": {"properties": {"name": {"const": "ready"}}},
            },
        },
    )
    resolver = AsyncRegistry(retrieve=retrieve).resolver_with_root(root)

    resolved = asyncio.run(
        resolver.lookup(
            "#/properties/thing/properties/name/const",
        ),
    )

    assert resolved.contents == "ready"
    same = asyncio.run(resolved.resolver.lookup("#"))
    assert same.contents == root.contents


def test_anchor_in_memory_does_not_retrieve():
    async def retrieve(uri):
        raise AssertionError(uri)

    root = schema({"$id": "urn:root", "$anchor": "here", "value": 12})
    resolver = AsyncRegistry(retrieve=retrieve).resolver_with_root(root)

    resolved = asyncio.run(resolver.lookup("#here"))

    assert resolved.contents == {
        "$id": "urn:root",
        "$anchor": "here",
        "value": 12,
    }


def test_cross_document_relative_chain_and_pointer():
    documents = {
        "https://example.com/a": {
            "$id": "https://example.com/a",
            "next": "b",
        },
        "https://example.com/b": {
            "$id": "https://example.com/b",
            "target": {"name": "external"},
        },
    }
    store = ControllableStore(documents)
    root = schema(documents["https://example.com/a"])
    resolver = (
        AsyncRegistry(retrieve=store.retrieve)
        .with_resource("https://example.com/a", root)
        .resolver()
    )

    async def main():
        first = await resolver.lookup("https://example.com/a")
        second_task = asyncio.create_task(
            first.resolver.lookup("b"),
        )
        await store.wait_started("https://example.com/b")
        store.release("https://example.com/b")
        second = await second_task
        pointed = await second.resolver.lookup("#/target/name")
        return first, second, pointed

    first, second, pointed = asyncio.run(main())

    assert first.contents == documents["https://example.com/a"]
    assert second.contents == documents["https://example.com/b"]
    assert pointed.contents == "external"
    assert store.calls == ["https://example.com/b"]


def test_dynamic_anchor_across_external_documents():
    documents = {
        "https://example.com/middle": {
            "$id": "https://example.com/middle",
            "next": "leaf",
            "$dynamicAnchor": "target",
        },
        "https://example.com/leaf": {
            "$id": "https://example.com/leaf",
            "$dynamicAnchor": "target",
            "answer": "leaf",
        },
    }
    store = ControllableStore(documents)
    root = schema(
        {
            "$id": "https://example.com/root",
            "next": "middle",
            "$dynamicAnchor": "target",
        },
    )
    resolver = (
        AsyncRegistry(retrieve=store.retrieve)
        .with_resource("https://example.com/root", root)
        .resolver()
    )

    async def main():
        root_resolved = await resolver.lookup("https://example.com/root")
        middle_task = asyncio.create_task(
            root_resolved.resolver.lookup("middle"),
        )
        await store.wait_started("https://example.com/middle")
        store.release("https://example.com/middle")
        middle = await middle_task

        leaf_task = asyncio.create_task(middle.resolver.lookup("leaf"))
        await store.wait_started("https://example.com/leaf")
        store.release("https://example.com/leaf")
        leaf = await leaf_task
        return await leaf.resolver.lookup("#target")

    resolved = asyncio.run(main())

    assert resolved.contents == root.contents
    assert store.calls == [
        "https://example.com/middle",
        "https://example.com/leaf",
    ]


def test_external_retrieval_returns_new_immutable_registry():
    external = Resource.opaque({"source": "external"})

    async def retrieve(uri):
        return external

    initial = AsyncRegistry(retrieve=retrieve)
    retrieved = asyncio.run(initial.get_or_retrieve("urn:external"))

    assert retrieved.value is external
    assert retrieved.registry.contents("urn:external") == external.contents
    assert "urn:external" not in initial
    assert retrieved.registry is not initial


def test_two_branches_evolve_independently():
    documents = {
        "urn:a": {"name": "a"},
        "urn:b": {"name": "b"},
    }
    store = ControllableStore(documents)
    root = schema({"$id": "urn:root"})
    initial = (
        AsyncRegistry(retrieve=store.retrieve)
        .with_resource("urn:root", root)
    )
    resolver = initial.resolver()

    async def main():
        task_a = asyncio.create_task(resolver.lookup("urn:a"))
        await store.wait_started("urn:a")
        task_b = asyncio.create_task(resolver.lookup("urn:b"))
        await store.wait_started("urn:b")

        store.release("urn:a")
        store.release("urn:b")
        return await task_a, await task_b

    resolved_a, resolved_b = asyncio.run(main())

    assert resolved_a.contents == documents["urn:a"]
    assert resolved_b.contents == documents["urn:b"]
    assert "urn:a" not in resolved_b.resolver._registry
    assert "urn:b" not in resolved_a.resolver._registry
    assert "urn:a" not in initial
    assert "urn:b" not in initial


def test_cancellation_reaches_retrieval_and_keeps_completed_resources():
    documents = {
        "urn:done": {"$id": "urn:done", "state": "complete"},
        "urn:pending": {"$id": "urn:pending", "state": "pending"},
    }
    store = ControllableStore(documents)
    initial = AsyncRegistry(retrieve=store.retrieve)

    async def main():
        completed_task = asyncio.create_task(
            initial.get_or_retrieve("urn:done"),
        )
        await store.wait_started("urn:done")
        store.release("urn:done")
        completed = await completed_task
        pending = asyncio.create_task(
            completed.registry.get_or_retrieve("urn:pending"),
        )
        await store.wait_started("urn:pending")
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        return completed

    completed = asyncio.run(main())

    assert completed.registry.contents("urn:done") == documents["urn:done"]
    assert "urn:pending" not in completed.registry
    assert "urn:done" not in initial
    assert "urn:pending" not in initial
    assert store.calls == ["urn:done", "urn:pending"]
    assert store.cancelled == {"urn:pending"}


def test_synchronous_lookup_with_async_retriever_is_explicit():
    async def retrieve(uri):
        return Resource.opaque({})

    registry = Registry(retrieve=retrieve)

    with pytest.raises(TypeError, match="AsyncRegistry"):
        registry.get_or_retrieve("urn:external")

    with pytest.raises(TypeError, match="AsyncRegistry"):
        registry.resolver().lookup("urn:external")


def test_synchronous_retriever_type_error_is_retrieval_failure():
    def retrieve(uri):
        raise TypeError("storage client misuse")

    with pytest.raises(exceptions.Unretrievable):
        Registry(retrieve=retrieve).get_or_retrieve("urn:external")


def test_synchronous_retriever_returning_non_resource_is_unretrievable():
    with pytest.raises(exceptions.Unretrievable):
        Registry(retrieve=lambda uri: {}).get_or_retrieve("urn:external")


def test_async_retriever_failure_is_wrapped():
    async def retrieve(uri):
        raise RuntimeError("storage unavailable")

    retrieved = (
        AsyncRegistry(retrieve=retrieve)
        .get_or_retrieve("urn:external")
    )

    with pytest.raises(exceptions.Unretrievable):
        asyncio.run(retrieved)


def test_async_retriever_returning_non_resource_is_unretrievable():
    def retrieve(uri):
        return {"not": "a resource"}

    with pytest.raises(exceptions.Unretrievable):
        asyncio.run(
            AsyncRegistry(retrieve=retrieve).get_or_retrieve("urn:external"),
        )


def test_async_no_such_resource():
    async def retrieve(uri):
        raise exceptions.NoSuchResource(ref=uri)

    with pytest.raises(exceptions.NoSuchResource):
        asyncio.run(AsyncRegistry(retrieve=retrieve).get_or_retrieve("urn:no"))


def test_async_registry_collection_and_immutable_additions():
    one = Resource.opaque({"one": 1})
    two = Resource.opaque({"two": 2})

    initial = AsyncRegistry()
    added = initial.with_resource("urn:one", one)
    added_many = added.with_resources([("urn:two", two)])

    assert len(initial) == 0
    assert added["urn:one"] is one
    assert added_many["urn:two"] is two
    assert set(added_many) == {"urn:one", "urn:two"}
    assert repr(added).startswith("Async<Registry")


def test_async_matmul_preserves_retriever():
    async def retrieve(uri):
        raise exceptions.NoSuchResource(ref=uri)

    resource = schema({"$id": "urn:resource"})
    registry = resource @ AsyncRegistry(retrieve=retrieve)

    assert registry["urn:resource"] == resource
    assert registry._retrieve is retrieve


def test_async_remove_crawl_and_combine():
    one = schema({"$id": "urn:one", "$anchor": "a"})
    two = Resource.opaque({"two": 2})

    first = AsyncRegistry().with_resource("urn:one", one).crawl()
    second = AsyncRegistry().with_resource("urn:two", two)
    combined = first.combine(second)
    removed = combined.remove("urn:two")

    assert first.anchor("urn:one", "a").value.name == "a"
    assert combined["urn:two"] is two
    assert "urn:two" not in removed
    assert first.combine(first) is first


def test_async_combine_conflicting_retrievers():
    async def retrieve_one(uri):
        raise exceptions.NoSuchResource(ref=uri)

    async def retrieve_two(uri):
        raise exceptions.NoSuchResource(ref=uri)

    with pytest.raises(ValueError, match="conflict.*retriev"):
        AsyncRegistry(retrieve=retrieve_one).combine(
            AsyncRegistry(retrieve=retrieve_two),
        )


def test_async_registry_does_not_inherit_synchronous_retriever():
    def synchronous_retrieve(uri):
        raise AssertionError(uri)

    async def asynchronous_retrieve(uri):
        raise exceptions.NoSuchResource(ref=uri)

    synchronous = Registry(retrieve=synchronous_retrieve)
    asynchronous = AsyncRegistry(
        synchronous,
        retrieve=asynchronous_retrieve,
    )

    assert asynchronous._registry._retrieve is not synchronous_retrieve
    assert asynchronous._retrieve is asynchronous_retrieve
    with pytest.raises(exceptions.NoSuchResource):
        asyncio.run(asynchronous.get_or_retrieve("urn:external"))
