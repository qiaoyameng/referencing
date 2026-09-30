"""
Tests for packing & loading bundles of resources for offline use.
"""

import pytest

from referencing import (
    Bundle,
    Manifest,
    Reference,
    ReferenceKind,
    Resource,
    exceptions,
    pack,
)
from referencing.jsonschema import (
    DRAFT201909,
    DRAFT202012,
    EMPTY_REGISTRY,
)

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

ROOT_URI = "https://example.com/api/root.json"
THING_URI = "https://example.com/common/thing.json"
OTHER_URI = "https://example.com/api/other.json"
TREE_URI = "https://example.com/tree.json"
STRICT_TREE_URI = "https://example.com/strict-tree.json"

ROOT = {
    "$id": ROOT_URI,
    "$schema": SCHEMA_DIALECT,
    "properties": {
        "thing": {"$ref": "../common/thing.json"},
        "tree": {"$ref": "../strict-tree.json"},
    },
}
THING = {
    "$id": THING_URI,
    "$schema": SCHEMA_DIALECT,
    "properties": {
        "again": {"$ref": "./thing.json"},
        "other": {"$ref": "../api/other.json"},
    },
}
OTHER = {
    "$id": OTHER_URI,
    "$schema": SCHEMA_DIALECT,
    "$ref": "../common/thing.json",
}
TREE = {
    "$id": TREE_URI,
    "$schema": SCHEMA_DIALECT,
    "$dynamicAnchor": "node",
    "type": "object",
    "properties": {
        "data": True,
        "children": {
            "type": "array",
            "items": {"$dynamicRef": "#node"},
        },
    },
}
STRICT_TREE = {
    "$id": STRICT_TREE_URI,
    "$schema": SCHEMA_DIALECT,
    "$dynamicAnchor": "node",
    "$ref": "tree.json",
    "unevaluatedProperties": False,
}

ALL = [
    (ROOT_URI, ROOT),
    (THING_URI, THING),
    (OTHER_URI, OTHER),
    (TREE_URI, TREE),
    (STRICT_TREE_URI, STRICT_TREE),
]


def lookup_paths(registry):
    """
    Perform a fixed set of lookups against the given registry.

    Includes cross-document relative references as well as a recursive
    schema whose dynamic anchor is overridden by an outer schema.
    """
    resolver = registry.resolver(ROOT_URI)
    results = []

    thing = resolver.lookup("#/properties/thing")
    assert thing.contents == {"$ref": "../common/thing.json"}
    thing = thing.resolver.lookup("../common/thing.json")
    results.append(thing.contents)

    other = thing.resolver.lookup("#/properties/other")
    other = other.resolver.lookup("../api/other.json")
    results.append(other.contents)

    # Following the cycle leads back to the first resource.
    again = other.resolver.lookup("../common/thing.json")
    results.append(again.contents)

    tree = resolver.lookup("#/properties/tree")
    tree = tree.resolver.lookup("../strict-tree.json")
    results.append(tree.contents)

    items = tree.resolver.lookup("tree.json")
    items = items.resolver.lookup("#/properties/children/items")
    assert items.contents == {"$dynamicRef": "#node"}
    node = items.resolver.lookup("#node")
    results.append(node.contents)

    return results


class TestPackLoadRoundTrip:
    def test_lookup_results_are_identical_before_and_after_packing(self):
        registry = EMPTY_REGISTRY.with_contents(ALL)
        expected = lookup_paths(registry)

        bundle = pack(ROOT, registry=registry)

        assert bundle.root == ROOT_URI
        assert set(bundle.resources) == {uri for uri, _ in ALL}
        for uri, contents in ALL:
            assert bundle.resources[uri] == contents
        assert set(bundle.manifest.digests) == set(bundle.resources)
        assert bundle.manifest.missing == frozenset()

        loaded = bundle.load()
        assert lookup_paths(loaded) == expected

    def test_dynamic_anchor_is_overridden_by_outer_scope(self):
        registry = EMPTY_REGISTRY.with_contents(ALL)
        loaded = pack(ROOT, registry=registry).load()

        resolver = loaded.resolver(STRICT_TREE_URI)
        items = resolver.lookup("tree.json").resolver.lookup(
            "#/properties/children/items",
        )
        node = items.resolver.lookup("#node")
        assert node.contents == STRICT_TREE

    def test_circular_references_are_packed_once(self):
        registry = EMPTY_REGISTRY.with_contents(ALL)
        bundle = pack(THING, registry=registry)
        assert bundle.root == THING_URI
        assert set(bundle.resources) == {THING_URI, OTHER_URI}

    def test_loaded_registry_does_not_retrieve_externally(self):
        registry = EMPTY_REGISTRY.with_contents(ALL)
        loaded = pack(ROOT, registry=registry).load()
        with pytest.raises(exceptions.NoSuchResource):
            loaded.get_or_retrieve("https://example.com/elsewhere.json")
        with pytest.raises(exceptions.Unresolvable):
            loaded.resolver(ROOT_URI).lookup(
                "https://example.com/elsewhere.json",
            )

    def test_missing_resources_are_recorded_in_the_manifest(self):
        root = {
            "$id": "https://example.com/has-missing",
            "$schema": SCHEMA_DIALECT,
            "$ref": "https://example.com/absent",
        }
        bundle = pack(root, registry=EMPTY_REGISTRY)
        assert bundle.manifest.missing == {"https://example.com/absent"}
        assert set(bundle.resources) == {"https://example.com/has-missing"}

        loaded = bundle.load()
        with pytest.raises(exceptions.Unresolvable):
            loaded.resolver("https://example.com/has-missing").lookup(
                "https://example.com/absent",
            )

    def test_load_verifies_digests(self):
        registry = EMPTY_REGISTRY.with_contents(ALL)
        bundle = pack(ROOT, registry=registry)

        tampered = Bundle(
            root=bundle.root,
            resources={**bundle.resources, THING_URI: {"not": "thing"}},
            manifest=bundle.manifest,
        )
        with pytest.raises(exceptions.DigestMismatch) as excinfo:
            tampered.load()
        assert excinfo.value.uri == THING_URI

        dropped_digest = Bundle(
            root=bundle.root,
            resources=bundle.resources,
            manifest=Manifest(
                digests={
                    uri: digest
                    for uri, digest in bundle.manifest.digests.items()
                    if uri != THING_URI
                },
                missing=bundle.manifest.missing,
            ),
        )
        with pytest.raises(exceptions.DigestMismatch):
            dropped_digest.load()

    def test_non_schema_data_with_reference_like_keys_is_ignored(self):
        root = {
            "$id": "https://example.com/tricky",
            "$schema": SCHEMA_DIALECT,
            "const": {"$ref": "https://example.com/nope1"},
            "examples": [{"$dynamicRef": "https://example.com/nope2"}],
            "properties": {"$ref": {"const": 12}},
        }
        bundle = pack(root)
        assert set(bundle.resources) == {"https://example.com/tricky"}
        assert bundle.manifest.missing == frozenset()

    def test_pack_accepts_resources_and_explicit_specifications(self):
        resource = Resource.from_contents(ROOT)
        registry = EMPTY_REGISTRY.with_contents(ALL)
        assert pack(resource, registry=registry) == pack(
            ROOT,
            registry=registry,
        )
        assert pack(
            ROOT,
            registry=registry,
            specification=DRAFT202012,
        ) == pack(ROOT, registry=registry)


class TestReferencesIn:
    def test_distinguishes_kinds_and_ignores_non_schema_data(self):
        schema = {
            "$id": "https://example.com/root",
            "$ref": "other",
            "properties": {
                "foo": {"$dynamicRef": "tree#node"},
                "$ref": {"const": 1},
            },
            "const": {"$ref": "not-a-reference"},
            "examples": [{"$dynamicRef": "also-not-a-reference"}],
            "$defs": {
                "inner": {"$id": "inner", "$ref": "more"},
            },
        }
        assert set(DRAFT202012.references_in(schema)) == {
            Reference(uri="https://example.com/other"),
            Reference(
                uri="https://example.com/tree#node",
                kind=ReferenceKind.DYNAMIC,
            ),
            Reference(uri="https://example.com/more"),
        }

    def test_recursive_references_in_2019(self):
        schema = {
            "$id": "https://example.com/root",
            "$recursiveRef": "#",
            "items": {"$ref": "other"},
        }
        assert set(DRAFT201909.references_in(schema)) == {
            Reference(
                uri="https://example.com/root",
                kind=ReferenceKind.RECURSIVE,
            ),
            Reference(uri="https://example.com/other"),
        }

    def test_boolean_schemas_have_no_references(self):
        assert list(DRAFT202012.references_in(True)) == []

    def test_resource_references(self):
        resource = DRAFT202012.create_resource(
            {"$id": "https://example.com/root", "$ref": "other"},
        )
        assert list(resource.references()) == [
            Reference(uri="https://example.com/other"),
        ]

    def test_opaque_specification_has_no_references(self):
        resource = Resource.opaque({"$ref": "not-a-reference"})
        assert list(resource.references()) == []
