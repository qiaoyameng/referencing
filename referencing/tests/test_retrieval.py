from functools import lru_cache
import json

import pytest

from referencing import Registry, Resource, exceptions
from referencing.jsonschema import DRAFT202012
from referencing.retrieval import (
    Allow,
    Deny,
    RetrieveWith,
    to_cached_resource,
)


def blow_up(uri):  # pragma: no cover
    raise RuntimeError(f"Unexpected retrieval of {uri}")


class TestToCachedResource:
    def test_it_caches_retrieved_resources(self):
        contents = {"$schema": "https://json-schema.org/draft/2020-12/schema"}
        stack = [json.dumps(contents)]

        @to_cached_resource()
        def retrieve(uri):
            return stack.pop()

        registry = Registry(retrieve=retrieve)

        expected = Resource.from_contents(contents)

        got = registry.get_or_retrieve("urn:example:schema")
        assert got.value == expected

        # And a second time we get the same value.
        again = registry.get_or_retrieve("urn:example:schema")
        assert again.value is got.value

    def test_custom_loader(self):
        contents = {"$schema": "https://json-schema.org/draft/2020-12/schema"}
        stack = [json.dumps(contents)[::-1]]

        @to_cached_resource(loads=lambda s: json.loads(s[::-1]))
        def retrieve(uri):
            return stack.pop()

        registry = Registry(retrieve=retrieve)

        expected = Resource.from_contents(contents)

        got = registry.get_or_retrieve("urn:example:schema")
        assert got.value == expected

        # And a second time we get the same value.
        again = registry.get_or_retrieve("urn:example:schema")
        assert again.value is got.value

    def test_custom_from_contents(self):
        contents = {}
        stack = [json.dumps(contents)]

        @to_cached_resource(from_contents=DRAFT202012.create_resource)
        def retrieve(uri):
            return stack.pop()

        registry = Registry(retrieve=retrieve)

        expected = DRAFT202012.create_resource(contents)

        got = registry.get_or_retrieve("urn:example:schema")
        assert got.value == expected

        # And a second time we get the same value.
        again = registry.get_or_retrieve("urn:example:schema")
        assert again.value is got.value

    def test_custom_cache(self):
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema"}
        mapping = {
            "urn:example:1": dict(schema, foo=1),
            "urn:example:2": dict(schema, foo=2),
            "urn:example:3": dict(schema, foo=3),
        }

        resources = {
            uri: Resource.from_contents(contents)
            for uri, contents in mapping.items()
        }

        @to_cached_resource(cache=lru_cache(maxsize=2))
        def retrieve(uri):
            return json.dumps(mapping.pop(uri))

        registry = Registry(retrieve=retrieve)

        got = registry.get_or_retrieve("urn:example:1")
        assert got.value == resources["urn:example:1"]
        assert registry.get_or_retrieve("urn:example:1").value is got.value
        assert registry.get_or_retrieve("urn:example:1").value is got.value

        got = registry.get_or_retrieve("urn:example:2")
        assert got.value == resources["urn:example:2"]
        assert registry.get_or_retrieve("urn:example:2").value is got.value
        assert registry.get_or_retrieve("urn:example:2").value is got.value

        # This still succeeds, but evicts the first URI
        got = registry.get_or_retrieve("urn:example:3")
        assert got.value == resources["urn:example:3"]
        assert registry.get_or_retrieve("urn:example:3").value is got.value
        assert registry.get_or_retrieve("urn:example:3").value is got.value

        # And now this fails (as we popped the value out of `mapping`)
        with pytest.raises(exceptions.Unretrievable):
            registry.get_or_retrieve("urn:example:1")


class TestRetrievalPolicy:
    def test_policy_receives_reference_context(self):
        root_uri = "https://example.com/allowed/root.json"
        child_uri = "https://example.com/allowed/child.json"
        root = DRAFT202012.create_resource(
            {"$id": root_uri, "$ref": "child.json"},
        )
        child = DRAFT202012.create_resource({"$id": child_uri})
        requests = []
        calls = []

        def retrieve(uri):
            calls.append(uri)
            return child

        def policy(request):
            requests.append(request)
            return RetrieveWith(retrieve)

        registry = Registry(
            retrieval_policy=policy,
            retrieve=blow_up,
        ).with_resource(root_uri, root)

        resolved = registry.resolver(root_uri).lookup("child.json")

        assert resolved.contents == child.contents
        assert calls == [child_uri]
        request = requests[0]
        assert request.ref == "child.json"
        assert request.uri == child_uri
        assert request.source == root
        assert request.chain == (root_uri,)
        assert request.retrieved_resources == (root_uri,)
        assert request.external_resources == ()
        assert request.depth == 2

    def test_denial_happens_before_calling_a_retriever(self):
        requests = []
        calls = []

        def retrieve(uri):
            calls.append(uri)
            return Resource.opaque({})

        def policy(request):
            requests.append(request)
            return Deny(reason="not allowed")

        registry = Registry(retrieval_policy=policy, retrieve=retrieve)

        with pytest.raises(exceptions.RetrievalDenied) as error:
            registry.get_or_retrieve("file:///etc/passwd")

        assert error.value == exceptions.RetrievalDenied(
            ref="file:///etc/passwd",
            reason="not allowed",
        )
        assert "not allowed" in str(error.value)
        assert len(requests) == 1
        assert calls == []

    def test_retriever_is_selected_by_scheme(self):
        uri = "file:///allowed/schema.json"
        resource = Resource.opaque({"from": "file"})
        default_calls = []
        selected_calls = []

        def default_retrieve(uri):
            default_calls.append(uri)
            return Resource.opaque({})

        def file_retrieve(uri):
            selected_calls.append(uri)
            return resource

        def policy(request):
            if request.uri == uri:
                return RetrieveWith(file_retrieve)
            return Deny()

        registry = Registry(
            retrieve=default_retrieve,
            retrieval_policy=policy,
        )

        resolved = registry.resolver().lookup(uri)
        assert resolved.contents == {"from": "file"}
        assert selected_calls == [uri]
        assert default_calls == []

    def test_in_memory_resources_are_unrestricted_by_default(self):
        uri = "urn:example:in-memory"
        resource = Resource.opaque({"where": "memory"})
        requests = []
        registry = Registry(
            retrieve=blow_up,
            retrieval_policy=lambda request: requests.append(request)
            or Deny(),
        ).with_resource(uri, resource)

        assert registry.get_or_retrieve(uri).value is resource
        assert requests == []

    def test_in_memory_resources_can_be_restricted(self):
        uri = "urn:example:in-memory"
        resource = Resource.opaque({"where": "memory"})
        replacement = Resource.opaque({"where": "retriever"})
        calls = []

        def retrieve(uri):
            calls.append(uri)
            return replacement

        denied = Registry(
            retrieve=retrieve,
            retrieval_policy=lambda request: Deny(),
            restrict_in_memory_resources=True,
        ).with_resource(uri, resource)
        with pytest.raises(exceptions.RetrievalDenied):
            denied.get_or_retrieve(uri)
        assert calls == []

        allowed = Registry(
            retrieve=retrieve,
            retrieval_policy=lambda request: Allow(),
            restrict_in_memory_resources=True,
        ).with_resource(uri, resource)
        assert allowed.get_or_retrieve(uri).value is resource
        assert calls == []

        selected = Registry(
            retrieve=blow_up,
            retrieval_policy=lambda request: RetrieveWith(retrieve),
            restrict_in_memory_resources=True,
        ).with_resource(uri, resource)
        assert selected.get_or_retrieve(uri).value is replacement
        assert calls == [uri]

    def test_nested_lookups_share_resource_and_depth_budgets(self):
        root_uri = "https://example.com/root.json"
        child_uri = "https://example.com/child.json"
        grandchild_uri = "https://example.com/grandchild.json"
        resources = {
            root_uri: DRAFT202012.create_resource(
                {"$id": root_uri, "$ref": "child.json"},
            ),
            child_uri: DRAFT202012.create_resource(
                {"$id": child_uri, "$ref": "grandchild.json"},
            ),
            grandchild_uri: DRAFT202012.create_resource(
                {"$id": grandchild_uri},
            ),
        }
        calls = []

        def retrieve(uri):
            calls.append(uri)
            return resources[uri]

        class QuotaPolicy:
            def __init__(self, max_resources, max_depth):
                self.max_resources = max_resources
                self.max_depth = max_depth
                self.requests = []

            def __call__(self, request):
                self.requests.append(request)
                if len(request.retrieved_resources) >= self.max_resources:
                    return Deny(reason="resource quota exhausted")
                if request.depth > self.max_depth:
                    return Deny(reason="depth quota exhausted")
                return RetrieveWith(retrieve)

        policy = QuotaPolicy(max_resources=2, max_depth=3)
        resolver = Registry(retrieval_policy=policy).resolver()
        first = resolver.lookup(root_uri)
        second = first.resolver.lookup("child.json")

        with pytest.raises(exceptions.RetrievalDenied, match="resource quota"):
            second.resolver.lookup("grandchild.json")

        assert calls == [root_uri, child_uri]
        request = policy.requests[-1]
        assert request.uri == grandchild_uri
        assert request.chain == (root_uri, child_uri)
        assert request.retrieved_resources == (root_uri, child_uri)
        assert request.depth == 3

        policy = QuotaPolicy(max_resources=3, max_depth=2)
        resolver = Registry(retrieval_policy=policy).resolver()
        calls.clear()
        first = resolver.lookup(root_uri)
        second = first.resolver.lookup("child.json")

        with pytest.raises(exceptions.RetrievalDenied, match="depth quota"):
            second.resolver.lookup("grandchild.json")

        assert calls == [root_uri, child_uri]
        assert grandchild_uri not in second.resolver._registry
        assert second.resolver._external_resources == (root_uri, child_uri)

    def test_nested_in_memory_resources_share_budgets(self):
        root_uri = "https://example.com/allowed/root.json"
        child_uri = "https://example.com/allowed/child.json"
        grandchild_uri = "https://example.com/allowed/grandchild.json"
        root = DRAFT202012.create_resource(
            {
                "$id": root_uri,
                "$defs": {
                    "child": {
                        "$id": "child.json",
                        "$defs": {"grandchild": {"$id": "grandchild.json"}},
                    },
                },
            },
        )
        requests = []

        def policy(request):
            requests.append(request)
            if request.depth > 2:
                return Deny(reason="depth quota exhausted")
            return Allow()

        registry = (
            root
            @ Registry(
                retrieval_policy=policy,
                restrict_in_memory_resources=True,
                retrieve=blow_up,
            )
        )
        resolver = registry.resolver(root_uri)
        child = resolver.lookup(child_uri)

        with pytest.raises(exceptions.RetrievalDenied, match="depth quota"):
            child.resolver.lookup(grandchild_uri)

        assert requests[-1].retrieved_resources == (root_uri, child_uri)
        assert requests[-1].external_resources == ()

    def test_explicit_self_reference_uses_another_depth(self):
        uri = "urn:example:self"
        resource = Resource.opaque({})
        requests = []

        def policy(request):
            requests.append(request)
            if request.depth > 4:
                return Deny(reason="depth quota exhausted")
            return Allow()

        registry = Registry(
            retrieve=blow_up,
            retrieval_policy=policy,
            restrict_in_memory_resources=True,
        ).with_resource(uri, resource)

        first = registry.resolver(uri).lookup(uri)
        second = first.resolver.lookup(uri)
        third = second.resolver.lookup(uri)

        assert first.resolver._chain == (uri, uri)
        assert second.resolver._chain == (uri, uri, uri)
        assert third.resolver._chain == (uri, uri, uri, uri)
        with pytest.raises(exceptions.RetrievalDenied, match="depth quota"):
            third.resolver.lookup(uri)

        assert requests[-1].chain == (uri, uri, uri, uri)
        assert requests[-1].retrieved_resources == (uri,)
        assert requests[-1].depth == 5

    def test_combining_registries_shares_policy(self):
        policy = lambda request: Deny()
        in_memory = Resource.opaque({})
        first = Registry(retrieve=blow_up, retrieval_policy=policy)
        second = Registry().with_resource("urn:example:in-memory", in_memory)

        combined = first.combine(second)
        assert combined.get_or_retrieve("urn:example:in-memory").value is (
            in_memory
        )
        with pytest.raises(exceptions.RetrievalDenied):
            combined.get_or_retrieve("urn:example:missing")

        restricted = first.combine(
            Registry(restrict_in_memory_resources=True),
        ).with_resource("urn:example:in-memory", in_memory)
        with pytest.raises(exceptions.RetrievalDenied):
            restricted.get_or_retrieve("urn:example:in-memory")

        other_policy = lambda request: Allow()
        with pytest.raises(Exception, match="conflict.*polic"):
            first.combine(Registry(retrieval_policy=other_policy))

    def test_encoded_and_relative_references_cannot_bypass_policy(self):
        root_uri = "https://example.com/allowed/root.json"
        child_uri = "https://example.com/allowed/child.json"
        file_uri = "file:///allowed/child.json"
        root = DRAFT202012.create_resource({"$id": root_uri})
        https_child = DRAFT202012.create_resource({"$id": child_uri})
        file_child = DRAFT202012.create_resource({"$id": file_uri})
        https_calls = []
        file_calls = []

        def https_retrieve(uri):
            https_calls.append(uri)
            return https_child

        def file_retrieve(uri):
            file_calls.append(uri)
            return file_child

        def policy(request):
            from urllib.parse import unquote, urlsplit
            import posixpath

            parts = urlsplit(request.uri)
            path = posixpath.normpath(unquote(parts.path))
            if parts.scheme == "file" and path.startswith("/allowed/"):
                return RetrieveWith(file_retrieve)
            if (
                parts.scheme == "https"
                and parts.netloc == "example.com"
                and path.startswith("/allowed/")
            ):
                return RetrieveWith(https_retrieve)
            return Deny(reason="outside the allowed root")

        registry = Registry(
            retrieval_policy=policy,
            retrieve=blow_up,
        ).with_resource(root_uri, root)
        resolver = registry.resolver(root_uri)

        assert resolver.lookup(child_uri).contents == https_child.contents
        assert resolver.lookup(file_uri).contents == file_child.contents

        blocked = [
            "../forbidden.json",
            "..%2fforbidden.json",
            "%2e%2e%2fetc/passwd",
            "file:///etc/passwd",
            "https://evil.example.com/allowed/child.json",
        ]
        for ref in blocked:
            https_calls.clear()
            file_calls.clear()
            with pytest.raises(exceptions.RetrievalDenied):
                resolver.lookup(ref)
            assert https_calls == []
            assert file_calls == []

    def test_failed_crawl_does_not_add_a_partial_resource(self):
        from referencing import Specification

        def subresources(contents):
            raise RuntimeError("broken discovered resource")

        broken = Specification(
            name="broken",
            id_of=lambda contents: contents.get("ID"),
            subresources_of=subresources,
            anchors_in=lambda specification, contents: [],
            maybe_in_subresource=lambda segments, resolver, resource: (
                resolver
            ),
        )

        def retrieve(uri):
            return broken.create_resource({"ID": uri})

        registry = Registry(
            retrieve=retrieve,
            retrieval_policy=lambda request: Allow(),
        )

        with pytest.raises(exceptions.Unretrievable):
            registry.get_or_retrieve("urn:example:broken")
        assert len(registry) == 0

    def test_invalid_policy_decision(self):
        registry = Registry(
            retrieval_policy=lambda request: "allow?",
        )

        with pytest.raises(TypeError, match="Allow, Deny, or RetrieveWith"):
            registry.get_or_retrieve("urn:example")
