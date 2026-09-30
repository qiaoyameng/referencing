"""
Tests for offline packaging and loading.
"""

import json

import pytest

from referencing import Registry, Resource, exceptions
from referencing.jsonschema import (
    DRAFT201909,
    DRAFT202012,
    lookup_recursive_ref,
)
from referencing.packaging import (
    MissingResource,
    Package,
    digest_for,
    pack,
)


def _registry_of(resources):
    return Registry().with_resources(
        (resource.id(), resource) for resource in resources
    ).crawl()


class TestReferenceEnumeration:
    def test_dynamic_references_are_tagged_separately(self):
        """
        ``$dynamicRef`` is enumerated as dynamic, ``$ref`` as static.
        """
        contents = {
            "$id": "https://example.com/root",
            "$ref": "other",
            "properties": {
                "thing": {"$dynamicRef": "#meta"},
            },
        }
        references = list(DRAFT202012.references_in(contents))
        assert {(ref.kind.value, ref.target) for ref in references} == {
            ("static", "other"),
        }
        nested = contents["properties"]["thing"]
        nested_refs = [
            (ref.kind.value, ref.target)
            for ref in DRAFT202012.references_in(nested)
        ]
        assert nested_refs == [("dynamic", "#meta")]

    def test_non_schema_data_same_named_key_is_ignored(self):
        """
        A ``$ref`` which is not a schema reference is not enumerated.

        Property names and values inside non-schema keywords like ``enum``
        are data, not references, even when they literally contain ``$ref``.
        """
        contents = {
            "$id": "https://example.com/tricky",
            "properties": {"$ref": {"type": "string"}},
            "enum": [{"$ref": "https://nope.example/thing"}],
            "const": {"$dynamicRef": "#not-an-anchor"},
        }
        package = pack(DRAFT202012.create_resource(contents))
        assert package.manifest.missing == ()
        assert list(package.documents) == ["https://example.com/tricky"]

    def test_recursive_ref_is_dynamic_in_2019_09(self):
        contents = {"$id": "urn:example:r", "$recursiveRef": "#"}
        references = list(DRAFT201909.references_in(contents))
        assert len(references) == 1
        assert references[0].kind.value == "dynamic"
        assert references[0].target == "#"


class TestPack:
    def test_resources_keep_their_ids_and_are_not_merged(self):
        address = DRAFT202012.create_resource(
            {"$id": "https://example.com/schemas/address", "type": "object"},
        )
        root = DRAFT202012.create_resource(
            {"$id": "https://example.com/schemas/main", "$ref": "address"},
        )
        registry = _registry_of([address, root])
        package = pack(root, retrieve=registry.__getitem__)

        assert sorted(package.documents) == [
            "https://example.com/schemas/address",
            "https://example.com/schemas/main",
        ]
        assert package.documents["https://example.com/schemas/address"] == {
            "$id": "https://example.com/schemas/address",
            "type": "object",
        }

    def test_embedded_resources_with_ids_are_separate_documents(self):
        root = DRAFT202012.create_resource(
            {
                "$id": "https://example.com/root",
                "$defs": {
                    "inner": {"$id": "inner", "type": "integer"},
                },
                "$ref": "inner",
            },
        )
        package = pack(root)
        assert sorted(package.documents) == [
            "https://example.com/inner",
            "https://example.com/root",
        ]

    def test_cyclic_references_are_collected_once(self):
        one = DRAFT202012.create_resource(
            {"$id": "https://example.com/one", "$ref": "two"},
        )
        two = DRAFT202012.create_resource(
            {
                "$id": "https://example.com/two",
                "$ref": "one",
                "properties": {"self": {"$ref": "two"}},
            },
        )
        registry = _registry_of([one, two])
        package = pack(one, retrieve=registry.__getitem__)

        assert sorted(package.documents) == [
            "https://example.com/one",
            "https://example.com/two",
        ]
        # Exactly one manifest entry per canonical document.
        assert {entry.uri for entry in package.manifest.entries} == set(
            package.documents,
        )

    def test_self_referential_schema_collected_once(self):
        node = DRAFT202012.create_resource(
            {
                "$id": "https://example.com/node",
                "type": "object",
                "properties": {"child": {"$ref": "node"}},
            },
        )
        package = pack(node)
        assert list(package.documents) == ["https://example.com/node"]

    def test_missing_resource_recorded_in_manifest(self):
        root = DRAFT202012.create_resource(
            {
                "$id": "https://example.com/root",
                "allOf": [{"$ref": "https://missing.example/schema"}],
            },
        )

        def retrieve(uri):
            raise exceptions.NoSuchResource(ref=uri)

        package = pack(root, retrieve=retrieve)
        assert package.manifest.missing == (
            MissingResource(
                uri="https://missing.example/schema",
                referenced_from=("https://example.com/root",),
            ),
        )

    def test_manifest_records_content_digests(self):
        root = DRAFT202012.create_resource(
            {"$id": "https://example.com/root", "type": "object"},
        )
        package = pack(root)
        (entry,) = package.manifest.entries
        assert entry.uri == "https://example.com/root"
        assert entry.digest == digest_for(root.contents)
        assert entry.specification == "draft2020-12"

    def test_root_without_id_is_an_error(self):
        resource = DRAFT202012.create_resource({"$ref": "#"})
        with pytest.raises(exceptions.NoInternalID):
            pack(resource)


class TestLoad:
    @pytest.mark.parametrize("via", ["json", "path", "dict"])
    def test_roundtrip_and_lookup_equivalence(self, tmp_path, via):
        address = DRAFT202012.create_resource(
            {
                "$id": "https://example.com/schemas/address",
                "type": "object",
                "properties": {"country": {"$ref": "country"}},
            },
        )
        country = DRAFT202012.create_resource(
            {"$id": "https://example.com/schemas/country", "type": "string"},
        )
        root = DRAFT202012.create_resource(
            {
                "$id": "https://example.com/schemas/main",
                "$ref": "address#/properties/country",
            },
        )
        online = _registry_of([address, country, root])
        package = pack(root, retrieve=online.__getitem__)

        path = tmp_path / "package.json"
        package.to_path(path)
        if via == "json":
            offline = Package.from_json(package.to_json()).registry()
        elif via == "path":
            offline = Package.from_path(path).registry()
        else:
            offline = Package.load(json.loads(package.to_json()))

        resolver_before = online.resolver(
            base_uri="https://example.com/schemas/main",
        )
        resolver_after = offline.resolver(
            base_uri="https://example.com/schemas/main",
        )
        for ref in (
            "address",
            "address#",
            "address#/properties/country",
            "country",
            "https://example.com/schemas/country",
        ):
            before = resolver_before.lookup(ref).contents
            after = resolver_after.lookup(ref).contents
            assert before == after, ref

    def test_loaded_registry_is_fully_crawled(self):
        root = DRAFT202012.create_resource(
            {"$id": "https://example.com/root", "type": "object"},
        )
        offline = pack(root).registry()
        assert set(offline) == {"https://example.com/root"}

    def test_external_retrieval_is_prohibited_after_loading(self):
        root = DRAFT202012.create_resource(
            {
                "$id": "https://example.com/root",
                "$ref": "https://missing.example/schema",
            },
        )

        def retrieve(uri):
            raise exceptions.NoSuchResource(ref=uri)

        offline = pack(root, retrieve=retrieve).registry()
        with pytest.raises(exceptions.NoSuchResource):
            offline.get_or_retrieve("https://missing.example/schema")

    def test_lookup_of_missing_ref_is_unresolvable_after_loading(self):
        root = DRAFT202012.create_resource(
            {
                "$id": "https://example.com/root",
                "$ref": "https://missing.example/schema",
            },
        )

        def retrieve(uri):
            raise exceptions.NoSuchResource(ref=uri)

        offline = pack(root, retrieve=retrieve).registry()
        with pytest.raises(exceptions.Unresolvable):
            offline.resolver(base_uri="https://example.com/root").lookup(
                "https://missing.example/schema",
            )

    def test_tampered_contents_raise_digest_mismatch(self):
        root = DRAFT202012.create_resource(
            {"$id": "https://example.com/root", "type": "object"},
        )
        package = pack(root)
        data = package.to_dict()
        data["documents"]["https://example.com/root"]["type"] = "array"
        with pytest.raises(exceptions.DigestMismatch) as exc_info:
            Package.from_dict(data).registry()
        assert exc_info.value.uri == "https://example.com/root"

    def test_manifest_entries_must_match_documents(self):
        root = DRAFT202012.create_resource(
            {"$id": "https://example.com/root"},
        )
        package = pack(root)
        data = package.to_dict()
        del data["documents"]["https://example.com/root"]
        with pytest.raises(ValueError, match="manifest"):
            Package.from_dict(data).registry()

    def test_load_accepts_path_object(self, tmp_path):
        root = DRAFT202012.create_resource(
            {"$id": "https://example.com/root", "type": "null"},
        )
        path = tmp_path / "package.json"
        pack(root).to_path(path)
        registry = Package.load(path)
        assert registry.contents("https://example.com/root") == root.contents

    def test_multiple_roots(self):
        one = DRAFT202012.create_resource(
            {"$id": "https://example.com/one"},
        )
        two = DRAFT202012.create_resource(
            {"$id": "https://example.com/two"},
        )
        package = pack([one, two])
        assert sorted(package.documents) == [
            "https://example.com/one",
            "https://example.com/two",
        ]


class TestDynamicAnchorRecursion:
    def _tree_documents(self):
        tree = DRAFT202012.create_resource(
            {
                "$id": "https://example.com/tree",
                "$dynamicAnchor": "meta",
                "type": "object",
                "properties": {
                    "data": {"$dynamicRef": "#meta"},
                    "children": {
                        "type": "array",
                        "items": {"$ref": "tree"},
                    },
                },
            },
        )
        strict_tree = DRAFT202012.create_resource(
            {
                "$schema": (
                    "https://json-schema.org/draft/2020-12/schema"
                ),
                "$id": "https://example.com/strict-tree",
                "$ref": "tree",
                "$defs": {
                    "meta": {
                        "$dynamicAnchor": "meta",
                        "type": "integer",
                        "minimum": 0,
                    },
                },
            },
        )
        return tree, strict_tree

    def test_dynamic_override_lookup_is_identical_after_packaging(self):
        tree, strict_tree = self._tree_documents()
        online = _registry_of([tree, strict_tree])
        package = pack(
            [tree, strict_tree],
            retrieve=online.__getitem__,
        )
        offline = package.registry()

        def dynamic_meta(registry, strict):
            base_uri = (
                "https://example.com/strict-tree"
                if strict
                else "https://example.com/tree"
            )
            resolver = registry.resolver(base_uri=base_uri)
            pointer = (
                "tree#/properties/data"
                if strict
                else "#/properties/data"
            )
            resolved = resolver.lookup(pointer)
            return resolved.resolver.lookup("#meta").contents

        plain_before = dynamic_meta(online, strict=False)
        plain_after = dynamic_meta(offline, strict=False)
        overridden_before = dynamic_meta(online, strict=True)
        overridden_after = dynamic_meta(offline, strict=True)

        assert plain_before == plain_after
        assert plain_after["type"] == "object"
        assert overridden_before == overridden_after
        assert overridden_after == {
            "$dynamicAnchor": "meta",
            "type": "integer",
            "minimum": 0,
        }

    def test_recursive_tree_children_resolve_offline(self):
        tree, strict_tree = self._tree_documents()
        online = _registry_of([tree, strict_tree])
        offline = pack(
            [tree, strict_tree],
            retrieve=online.__getitem__,
        ).registry()

        items_ref = "#/properties/children/items"
        before = (
            online.resolver(base_uri="https://example.com/tree")
            .lookup(items_ref)
            .contents
        )
        after = (
            offline.resolver(base_uri="https://example.com/tree")
            .lookup(items_ref)
            .contents
        )
        assert before == after
        # And following the recursive ref gets the tree itself offline.
        followed = (
            offline.resolver(base_uri="https://example.com/tree")
            .lookup("tree")
            .contents
        )
        assert followed == tree.contents

    def test_recursive_anchor_2019_09(self):
        inner = DRAFT201909.create_resource(
            {
                "$id": "https://example.com/2019/inner",
                "$recursiveAnchor": True,
                "$recursiveRef": "#",
                "type": "integer",
            },
        )
        outer = DRAFT201909.create_resource(
            {
                "$id": "https://example.com/2019/outer",
                "$recursiveAnchor": True,
                "type": "string",
                "properties": {"wrapped": {"$ref": "inner"}},
            },
        )
        online = _registry_of([inner, outer])
        package = pack([inner, outer], retrieve=online.__getitem__)
        offline = package.registry()

        def recurse(registry):
            resolver = registry.resolver(
                base_uri="https://example.com/2019/outer",
            )
            resolved = resolver.lookup(
                "https://example.com/2019/inner",
            )
            # The recursive ref overrides to the outermost recursive
            # resource (the string-typed outer schema).
            recursive = lookup_recursive_ref(resolved.resolver)
            return recursive.contents

        assert recurse(online) == recurse(offline) == outer.contents


def test_opaque_external_resources_can_be_packaged():
    # Opaque resources have no notion of an internal ID, so they're collected
    # under the URI they were retrieved at.
    data = Resource.opaque({"hello": "world"})
    root = DRAFT202012.create_resource(
        {
            "$id": "urn:example:root",
            "$ref": "urn:example:data",
        },
    )
    resources = {
        "urn:example:data": data,
        "urn:example:root": root,
    }
    package = pack(root, retrieve=resources.__getitem__)
    assert sorted(package.documents) == [
        "urn:example:data",
        "urn:example:root",
    ]
    opaque_entry = package.manifest.digest_for("urn:example:data")
    assert opaque_entry == digest_for(data.contents)

    offline = package.registry()
    assert offline.contents("urn:example:data") == {"hello": "world"}
    assert (
        offline.resolver(base_uri="urn:example:root")
        .lookup("urn:example:data")
        .contents
        == {"hello": "world"}
    )
