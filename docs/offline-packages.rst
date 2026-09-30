Offline Packages
================

If you need to hand a schema (or any other `Resource`) to an offline service along with the resources it references, use the :func:`referencing.pack` function to produce a `referencing.packaging.Package`.

Packaging walks your root resource(s) using their `Specification`'s reference-enumeration behavior, so:

* every referenced document is kept separate under its original identifier (its :kw:`$id`) rather than being merged into a single ``definitions`` object;
* normal references are distinguished from dynamic ones (like :kw:`$dynamicRef`), and keys which only coincidentally share their name but appear in non-schema data are not mistaken for references;
* documents which are part of a reference cycle are collected only once;
* resources which are referenced but unavailable are recorded in the package manifest instead of causing an error.

The resulting `~referencing.packaging.Package` is plain JSON-serializable data. Its `~referencing.packaging.Manifest` records a SHA-256 content digest for each document as well as any missing resources.

Loading a package verifies every digest against its contents and returns a fully crawled `Registry` with no retrieval configured, so no lookup can cause external (e.g. network) access:

.. testcode::

    from referencing import Registry, pack
    from referencing.exceptions import NoSuchResource
    import referencing.jsonschema

    root = referencing.jsonschema.DRAFT202012.create_resource(
        {
            "$id": "https://example.com/schemas/main",
            "$ref": "address",
        },
    )
    address = referencing.jsonschema.DRAFT202012.create_resource(
        {"$id": "https://example.com/schemas/address", "type": "object"},
    )
    registry = Registry().with_resources(
        [
            ("https://example.com/schemas/main", root),
            ("https://example.com/schemas/address", address),
        ],
    )

    def retrieve(uri):  # Stand-in for e.g. reading from disk or a cache.
        return registry[uri]

    package = pack(root, retrieve=retrieve)

    # Hand the serialized package to the offline service:
    serialized = package.to_json()

    # On the receiving side, verify and build a fully offline registry:
    offline = package.registry()
    assert (
        offline.resolver(
            base_uri="https://example.com/schemas/main",
        ).lookup("address").contents
        == {"$id": "https://example.com/schemas/address", "type": "object"}
    )

    assert package.manifest.missing == ()

    try:
        offline.get_or_retrieve("https://example.com/not-packaged")
    except NoSuchResource:
        pass  # External retrieval is not possible.
