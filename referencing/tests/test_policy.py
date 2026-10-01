import pytest

from referencing import Registry, Resource, exceptions
from referencing.jsonschema import DRAFT202012
from referencing.retrieval import Allow, Deny, RetrieveWith


def schema_resource(**contents):
    return DRAFT202012.create_resource(contents)


class FakeRetriever:
    """
    A retriever serving resources from an in-memory mapping and logging calls.

    Two instances stand in for the real ``file://`` and ``https://``
    retrievers, so tests can prove that a denied reference never reaches
    either one (and that routing decisions select the expected one).
    """

    def __init__(self, resources=()):
        self.calls = []
        self._resources = dict(resources)

    def __call__(self, uri):
        self.calls.append(uri)
        try:
            return self._resources[uri]
        except KeyError:
            raise exceptions.NoSuchResource(ref=uri) from None


class TestRetrievalPolicy:
    def test_request_sees_raw_ref_absolute_uri_origin_and_chain(self):
        seen = []

        root = schema_resource(**{"$id": "https://example.com/a/root"})
        child = schema_resource(**{"$id": "https://example.com/a/child"})
        grand = schema_resource(**{"$id": "https://example.com/grand"})
        https = FakeRetriever(
            resources=[
                ("https://example.com/a/child", child),
                ("https://example.com/grand", grand),
            ],
        )

        def policy(request):
            seen.append(request)
            return RetrieveWith(https)

        resolver = (root @ Registry(policy=policy)).resolver_with_root(root)

        resolved = resolver.lookup("child")
        assert resolved.contents is child.contents
        first = seen[-1]
        assert first.raw_ref == "child"
        assert first.uri == "https://example.com/a/child"
        assert first.origin is root
        assert list(first.chain) == ["https://example.com/a/root"]

        resolved = resolved.resolver.lookup("../grand")
        second = seen[-1]
        assert second.raw_ref == "../grand"
        assert second.uri == "https://example.com/grand"
        assert second.origin is child
        assert list(second.chain) == [
            "https://example.com/a/root",
            "https://example.com/a/child",
        ]

    def test_deny_happens_before_retriever_is_called(self):
        https = FakeRetriever(
            resources=[("https://example.com/secret", schema_resource())],
        )

        def policy(request):
            assert https.calls == []
            return Deny("not on the allowlist")

        registry = Registry(policy=policy, retrieve=https)
        with pytest.raises(exceptions.RetrievalDenied) as info:
            registry.get_or_retrieve("https://example.com/secret")
        assert info.value == exceptions.RetrievalDenied(
            ref="https://example.com/secret",
            reason="not on the allowlist",
        )
        assert https.calls == []

    def test_fragment_only_ref_entering_absent_document_is_gated(self):
        # A resolver whose base URI names a document not yet in memory: a
        # fragment-only reference still targets that document and is gated.
        https = FakeRetriever(
            resources=[
                ("https://example.com/doc", schema_resource(v=1)),
            ],
        )
        resolver = Registry(
            policy=lambda request: Allow(),
            retrieve=https,
        ).resolver(base_uri="https://example.com/doc")
        resolved = resolver.lookup("#")
        assert resolved.contents == {"v": 1}

        denied = Registry(
            policy=lambda request: Deny("no fragments"),
            retrieve=https,
        ).resolver(base_uri="https://example.com/doc")
        with pytest.raises(exceptions.RetrievalDenied) as info:
            denied.lookup("#")
        assert info.value.ref == "#"

    def test_fragment_ref_to_another_document_is_gated(self):
        # The base resource is in memory; the fragment-only reference points
        # at a different document which is not present, so it must be fetched
        # -- and the policy may deny it while preserving the raw fragment.
        root = schema_resource(**{"$id": "https://example.com/root"})
        target = schema_resource(**{"$id": "https://example.com/other"})
        https = FakeRetriever(
            resources=[("https://example.com/other", target)],
        )
        resolver = (
            root
            @ Registry(policy=lambda request: Allow(), retrieve=https)
        ).resolver_with_root(root)  # fmt: skip
        resolved = resolver.lookup("other#")
        assert resolved.contents is target.contents

        def policy(request):
            return Deny() if request.uri.endswith("other") else Allow()

        denied = root @ Registry(policy=policy, retrieve=https)
        with pytest.raises(exceptions.RetrievalDenied) as info:
            denied.resolver_with_root(root).lookup("other#")
        assert info.value.ref == "other#"
        # Only the allowed lookup above fetched the target.
        assert https.calls == ["https://example.com/other"]

    def test_pointer_fragment_is_subject_to_the_policy(self):
        resource = schema_resource(
            **{"$id": "https://example.com/doc", "foo": {"bar": 12}},
        )
        resolver = (
            resource
            @ Registry(policy=lambda request: Allow())
        ).resolver_with_root(resource)  # fmt: skip
        resolved = resolver.lookup("#/foo/bar")
        assert resolved.contents == 12
        # The policy is consulted for the fragment lookup too.
        denied = resource @ Registry(
            policy=lambda request: Deny(),
        )
        with pytest.raises(exceptions.RetrievalDenied):
            denied.resolver_with_root(resource).lookup("#/foo/bar")

    def test_named_anchor_fragment_is_subject_to_the_policy(self):
        resource = Resource.from_contents(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://example.com/doc",
                "$defs": {
                    "thing": {
                        "$anchor": "thing",
                        "const": 7,
                    },
                },
            },
        )
        resolver = (
            resource @ Registry(policy=lambda request: Allow())
        ).resolver_with_root(resource)
        assert resolver.lookup("#thing").contents == {
            "$anchor": "thing",
            "const": 7,
        }

        with pytest.raises(exceptions.RetrievalDenied):
            (
                resource @ Registry(policy=lambda request: Deny())
            ).resolver_with_root(resource).lookup("#thing")

    def test_deny_through_resolver_keeps_raw_reference(self):
        def policy(request):
            return Deny()

        resolver = Registry(
            policy=policy,
            retrieve=FakeRetriever(),
        ).resolver(base_uri="https://example.com/")
        with pytest.raises(exceptions.RetrievalDenied) as info:
            resolver.lookup("../elsewhere")
        assert info.value.ref == "../elsewhere"

    def test_deny_of_absolute_ref_is_unchanged(self):
        resolver = Registry(
            policy=lambda request: Deny(),
            retrieve=FakeRetriever(),
        ).resolver()
        ref = "https://example.com/absolute"
        with pytest.raises(exceptions.RetrievalDenied) as info:
            resolver.lookup(ref)
        assert info.value.ref == ref

    def test_unretrievable_through_resolver_is_wrapped(self):
        def boom(uri):
            raise RuntimeError("network down")

        resolver = Registry(
            policy=lambda request: Allow(),
            retrieve=boom,
        ).resolver()
        with pytest.raises(exceptions.Unresolvable):
            resolver.lookup("https://example.com/x")

    def test_returning_none_is_treated_as_allow(self):
        https = FakeRetriever(
            resources=[("https://example.com/schema", schema_resource())],
        )
        registry = Registry(policy=lambda request: None, retrieve=https)
        resolved = registry.resolver().lookup("https://example.com/schema")
        assert resolved.contents == {}

    def test_delegate_uses_chosen_retriever(self):
        file = FakeRetriever(
            resources=[
                ("file:///srv/schemas/thing.json", schema_resource()),
            ],
        )
        https = FakeRetriever()

        def policy(request):
            if request.uri.startswith("file:///srv/schemas/"):
                return RetrieveWith(file)
            return Deny()

        registry = Registry(policy=policy, retrieve=https)
        resolved = registry.resolver().lookup(
            "file:///srv/schemas/thing.json",
        )
        assert resolved.contents == {}
        assert file.calls == ["file:///srv/schemas/thing.json"]
        assert https.calls == []

    def test_in_memory_resources_are_gated_by_default(self):
        resource = schema_resource(**{"$id": "urn:example:in-memory"})
        https = FakeRetriever()
        registry = resource @ Registry(
            policy=lambda request: Deny(),
            retrieve=https,
        )
        with pytest.raises(exceptions.RetrievalDenied):
            registry.get_or_retrieve("urn:example:in-memory")
        assert https.calls == []

    def test_in_memory_resources_can_be_exempted(self):
        resource = schema_resource(**{"$id": "urn:example:in-memory"})
        registry = resource @ Registry(
            policy=lambda request: Deny(),
            policy_applies_to_resources=False,
        )
        got = registry.get_or_retrieve("urn:example:in-memory")
        assert got.value is resource

    def test_subresource_discovered_by_crawl_is_allowed_without_fetch(self):
        https = FakeRetriever()
        root = Resource.from_contents(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://example.com/root",
                "$defs": {
                    "child": {
                        "$id": "child",
                        "type": "integer",
                    },
                },
            },
        )
        registry = root @ Registry(
            policy=lambda request: Allow(),
            retrieve=https,
        )
        resolved = registry.resolver().lookup(
            "https://example.com/child",
        )
        assert resolved.contents["type"] == "integer"
        assert https.calls == []

    def test_exempted_memory_resource_is_returned_before_policy_runs(self):
        calls = []

        def policy(request):  # pragma: no cover - must not be consulted
            calls.append(request)
            return Deny()

        resource = schema_resource(**{"$id": "urn:example:cached"})
        registry = resource @ Registry(
            policy=policy,
            policy_applies_to_resources=False,
        )
        # Direct in-memory hit short-circuits before the policy is consulted.
        assert registry.get_or_retrieve("urn:example:cached").value is resource
        assert calls == []

    def test_delegate_can_override_an_in_memory_resource(self):
        memory = schema_resource(**{"$id": "urn:example:thing", "v": 1})
        fetched = schema_resource(**{"$id": "urn:example:thing", "v": 2})
        https = FakeRetriever(resources=[("urn:example:thing", fetched)])
        registry = memory @ Registry(
            policy=lambda request: RetrieveWith(https),
        )
        assert registry.get_or_retrieve("urn:example:thing").value is fetched
        assert https.calls == ["urn:example:thing"]

    def test_relative_path_traversal_cannot_escape_allowed_prefix(self):
        """
        Relative ``..`` jumps and percent-encoding are normalized before the
        policy decides, so they cannot smuggle a reference outside an
        allowed prefix nor trigger a retrieval for the escaped URI.
        """
        encoded_targets = [
            "https://example.com/schemas/..%2f..%2fetc%2fpasswd",
            "https://example.com/schemas/%2e%2e/%2e%2e/etc/passwd",
        ]
        allowed = FakeRetriever(
            resources=[
                ("https://example.com/schemas/a", schema_resource()),
                *(
                    (uri, schema_resource(**{"target": uri}))
                    for uri in encoded_targets
                ),
            ],
        )

        def policy(request):
            if request.uri.startswith("https://example.com/schemas/"):
                return RetrieveWith(allowed)
            return Deny("outside of /schemas/")

        resolver = Registry(policy=policy).resolver(
            base_uri="https://example.com/schemas/root",
        )

        # Literal relative traversal is normalized before the policy runs and
        # escapes the prefix, so it is denied without any retrieval.
        for raw in ["../etc/passwd", "../../etc/passwd"]:
            with pytest.raises(exceptions.RetrievalDenied):
                resolver.lookup(raw)
        assert allowed.calls == []

        # Percent-encoded separators are *not* real separators (per RFC 3986),
        # so such references stay under the prefix -- but the retriever only
        # ever sees the normalized https URI containing the encoded bytes, and
        # cannot be tricked into touching a filesystem path.
        for raw in [
            "..%2f..%2fetc%2fpasswd",
            "%2e%2e/%2e%2e/etc/passwd",
            "https://example.com/schemas/%2e%2e/%2e%2e/etc/passwd",
        ]:
            resolved = resolver.lookup(raw)
            suffix = raw.removeprefix(
                "https://example.com/schemas/",
            )
            assert resolved.contents["target"].endswith(suffix)
        assert allowed.calls == [
            "https://example.com/schemas/..%2f..%2fetc%2fpasswd",
            "https://example.com/schemas/%2e%2e/%2e%2e/etc/passwd",
            "https://example.com/schemas/%2e%2e/%2e%2e/etc/passwd",
        ]

        # And a genuinely allowed relative jump works as expected.
        resolved = resolver.lookup("a")
        assert resolved.contents == {}
        assert allowed.calls[-1] == "https://example.com/schemas/a"

    def test_encoded_scheme_and_backslashes_cannot_change_scheme(self):
        """
        Encoded slashes or backslashes cannot turn an https-relative
        reference into a file (or foreign host) reference, because the
        retrievers only ever receive the normalized absolute URI.
        """
        file = FakeRetriever(
            resources=[("file:///etc/passwd", schema_resource())],
        )
        https = FakeRetriever(
            resources=[("https://example.com/x", schema_resource())],
        )

        def policy(request):
            if request.uri.startswith("https://example.com/"):
                return RetrieveWith(https)
            if request.uri.startswith("file:///"):
                return RetrieveWith(file)
            return Deny()

        resolver = Registry(policy=policy).resolver(
            base_uri="https://example.com/root",
        )

        # A genuinely foreign scheme (here an encoded ``file:`` reference
        # which normalizes to a file URI) is denied, and the file retriever
        # is never reached.
        with pytest.raises(exceptions.RetrievalDenied):
            resolver.lookup("file:%2f%2f%2fetc%2fpasswd")
        assert file.calls == []

        # Encoded slashes/backslashes cannot forge a new scheme or authority:
        # the reference stays an https URI under example.com, so it is routed
        # to the https retriever (which does not know it) rather than ever
        # touching the file retriever or an attacker-chosen host.
        for raw in [
            "https:%2f%2fevil.example/x",
            "https:%5c%5cevil.example%5cx",
            "https:/\\evil.example/x",
        ]:
            with pytest.raises(exceptions.Unresolvable):
                resolver.lookup(raw)
        assert file.calls == []
        assert https.calls == [
            "https://example.com/%2f%2fevil.example/x",
            "https://example.com/%5c%5cevil.example%5cx",
            "https://example.com/\\evil.example/x",
        ]

        # An explicitly allowed https reference routes only to https.
        https.calls.clear()
        resolved = resolver.lookup("x")
        assert resolved.contents == {}
        assert https.calls == ["https://example.com/x"]
        assert file.calls == []


class TestTenantSchemaIsolation:
    def test_tenant_schema_cannot_reference_local_files(self):
        """
        A tenant-supplied (in-memory) schema must not be able to make the
        service read arbitrary local files or addresses via a ``$ref``.
        """
        file = FakeRetriever(
            resources=[("file:///etc/passwd", schema_resource())],
        )
        https = FakeRetriever(
            resources=[("https://schemas.example/schemas/base", schema_resource())],
        )

        def policy(request):
            # Only pre-approved https schemas may be fetched; file:// is
            # never allowed, and the policy decides on the resolved URI.
            if request.uri.startswith("https://schemas.example/schemas/"):
                return RetrieveWith(https)
            return Deny(f"{request.uri} is not an allowed schema location")

        tenant_root = Resource.from_contents(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://schemas.example/schemas/tenant",
                "$defs": {
                    "evil_file": {"$ref": "file:///etc/passwd"},
                    "evil_relative": {"$ref": "../../../../etc/passwd"},
                },
            },
        )
        registry = tenant_root @ Registry(
            policy=policy,
            retrieve=file,
        )
        resolver = registry.resolver_with_root(tenant_root)

        # An explicitly allowed https reference works via the https getter.
        allowed = resolver.lookup("base")
        assert allowed.contents == {}
        assert https.calls == ["https://schemas.example/schemas/base"]
        assert file.calls == []

        # A file: reference is denied before the (file) retriever runs.
        with pytest.raises(exceptions.RetrievalDenied):
            resolver.lookup("file:///etc/passwd")

        # A relative escape normalizes outside the allowed https prefix and
        # is likewise denied -- the file getter is configured as the default
        # retriever but is never reached.
        with pytest.raises(exceptions.RetrievalDenied):
            resolver.lookup("../../../../etc/passwd")
        assert file.calls == []


class TestRetrievalQuotas:
    def _chain(self, https, **kwargs):
        for uri in "abcd":
            https._resources[
                f"https://example.com/{uri}"
            ] = schema_resource(**{"$id": f"https://example.com/{uri}"})
        return Registry(
            policy=lambda request: Allow(),
            retrieve=https,
            **kwargs,
        )

    def test_depth_quota_is_shared_across_nested_chain(self):
        https = FakeRetriever()
        registry = self._chain(https, max_depth=3)
        first = registry.resolver(
            base_uri="https://example.com/a",
        ).lookup("b")
        second = first.resolver.lookup("c")
        with pytest.raises(exceptions.RetrievalQuotaExceeded) as info:
            second.resolver.lookup("d")
        assert info.value.limit == 3
        assert https.calls == [
            "https://example.com/b",
            "https://example.com/c",
        ]

    def test_depth_quota_counts_fragment_entry_to_a_new_document(self):
        # Entering a *new* document even via a fragment-only reference counts
        # against the shared depth quota.
        a = schema_resource(**{"$id": "https://example.com/a"})
        https = FakeRetriever(
            resources=[
                ("https://example.com/b", schema_resource(
                    **{"$id": "https://example.com/b"},
                )),
            ],
        )
        registry = a @ Registry(
            policy=lambda request: Allow(),
            retrieve=https,
            max_depth=2,
        )
        resolved = registry.resolver_with_root(a).lookup("b")
        with pytest.raises(exceptions.RetrievalQuotaExceeded):
            resolved.resolver.lookup("c#")  # would be a 3rd document

    def test_retrieved_resource_quota_is_shared_across_nested_chain(self):
        https = FakeRetriever()
        registry = self._chain(https, max_retrieved_resources=2)
        first = registry.resolver(
            base_uri="https://example.com/a",
        ).lookup("b")
        second = first.resolver.lookup("c")
        with pytest.raises(exceptions.RetrievalQuotaExceeded) as info:
            second.resolver.lookup("d")
        assert info.value.limit == 2
        assert https.calls == [
            "https://example.com/b",
            "https://example.com/c",
        ]

    def test_quota_error_message_is_grammatical(self):
        one = exceptions.RetrievalQuotaExceeded(
            ref="u",
            reason="retrieved resources",
            limit=1,
        )
        assert "quota of 1 retrieved resource" in str(one)
        many = exceptions.RetrievalQuotaExceeded(
            ref="u",
            reason="documents in the resolution chain",
            limit=3,
        )
        assert "quota of 3 documents in the resolution chain" in str(many)

    def test_in_memory_resources_do_not_consume_retrieval_quota(self):
        https = FakeRetriever()
        b = schema_resource(**{"$id": "https://example.com/b", "v": 1})
        c = schema_resource(**{"$id": "https://example.com/c", "v": 2})
        https._resources["https://example.com/c"] = c
        registry = b @ Registry(
            policy=lambda request: Allow(),
            retrieve=https,
            max_retrieved_resources=1,
        )
        first = registry.resolver(
            base_uri="https://example.com/a",
        ).lookup("b")
        second = first.resolver.lookup("c")
        assert second.contents["v"] == 2
        with pytest.raises(exceptions.RetrievalQuotaExceeded):
            second.resolver.lookup("missing")
        assert https.calls == ["https://example.com/c"]

    def test_quotas_do_not_apply_to_direct_registry_retrieval(self):
        # A direct get_or_retrieve call has no nested chain, so chain quotas
        # do not bound it (they are shared only within a resolution chain).
        https = FakeRetriever(
            resources=[("https://example.com/only", schema_resource())],
        )
        registry = Registry(
            policy=lambda request: Allow(),
            retrieve=https,
            max_depth=0,
            max_retrieved_resources=0,
        )
        got = registry.get_or_retrieve("https://example.com/only")
        assert got.value.contents == {}

    def test_failed_retrieval_leaves_no_partial_registry(self):
        def boom(uri):
            raise RuntimeError("network down")

        registry = Registry(policy=lambda request: Allow(), retrieve=boom)
        with pytest.raises(exceptions.Unretrievable):
            registry.get_or_retrieve("https://example.com/missing")
        assert "https://example.com/missing" not in registry
        assert len(registry) == 0

    def test_retriever_raised_denial_is_propagated(self):
        def retrieve(uri):
            raise exceptions.RetrievalDenied(ref=uri, reason="downstream")

        registry = Registry(retrieve=retrieve)
        with pytest.raises(exceptions.RetrievalDenied) as info:
            registry.get_or_retrieve("https://example.com/x")
        assert info.value.reason == "downstream"

    def test_denied_retrieval_leaves_no_partial_registry(self):
        https = FakeRetriever(
            resources=[("https://example.com/x", schema_resource())],
        )
        registry = Registry(policy=lambda request: Deny(), retrieve=https)
        with pytest.raises(exceptions.RetrievalDenied):
            registry.get_or_retrieve("https://example.com/x")
        assert "https://example.com/x" not in registry
        assert https.calls == []


class TestCombinePolicies:
    def test_combine_with_common_policy(self):
        def policy(request):
            return Allow()

        one = Registry(policy=policy).with_resource(
            "urn:example:one",
            Resource.opaque({}),
        )
        two = Registry(policy=policy).with_resource(
            "urn:example:two",
            Resource.opaque({}),
        )
        assert one.combine(two)._policy is policy

    def test_combine_conflicting_policy(self):
        with pytest.raises(ValueError):
            Registry(policy=lambda request: Allow()).combine(
                Registry(policy=lambda request: Deny()),
            )

    def test_combine_conflicting_depth_quota(self):
        with pytest.raises(ValueError):
            Registry(max_depth=1).combine(Registry(max_depth=2))

    def test_combine_takes_policy_from_other_registry(self):
        def policy(request):
            return Allow()

        combined = Registry().combine(Registry(policy=policy))
        assert combined._policy is policy

    def test_combine_takes_quotas_from_other_registry(self):
        combined = Registry().combine(
            Registry(max_depth=4, max_retrieved_resources=7),
        )
        assert combined._max_depth == 4
        assert combined._max_retrieved_resources == 7

    def test_combine_with_matching_quotas(self):
        combined = Registry(max_depth=4, max_retrieved_resources=7).combine(
            Registry(max_depth=4, max_retrieved_resources=7),
        )
        assert combined._max_depth == 4
        assert combined._max_retrieved_resources == 7

    def test_combine_memory_gating_is_intersected(self):
        resource = schema_resource(**{"$id": "urn:example:one"})
        strict = resource @ Registry(policy=lambda request: Allow())
        lax = Registry(policy_applies_to_resources=False)
        combined = strict.combine(lax)
        assert combined._policy_applies_to_resources is False
        assert combined.get_or_retrieve("urn:example:one").value is resource

    def test_combine_conflicting_resource_quota(self):
        with pytest.raises(ValueError):
            Registry(max_retrieved_resources=1).combine(
                Registry(max_retrieved_resources=2),
            )
