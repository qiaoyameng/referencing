"""
Offline packaging of a `Resource` together with everything it references.

This module supports handing a JSON Schema (or any other `Resource`) to an
offline service along with a self-contained bundle of the resources it
references:

    * `pack` walks one or more root resources using their `Specification`'s
      reference-enumeration capability (`Specification.references_in`),
      distinguishing normal references from dynamic ones and ignoring
      same-named keys that appear in non-schema data.

    * Every reachable resource is kept as its own document under its
      original (e.g. ``$id``) URI -- documents are *not* merged into a single
      ``definitions`` object. Cyclic references are collected only once.

    * The resulting `Package` carries a `Manifest` recording a content digest
      for each document as well as any referenced-but-missing resources.

    * `Package.registry` (or the `Package.load` convenience) verifies every
      digest and builds a fully crawled `Registry` which is prohibited from
      fetching anything externally.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Generic
from urllib.parse import urldefrag, urljoin

from attrs import field

from referencing._attrs import frozen
from referencing._core import (
    Registry,
    Resource,
    Specification,
    _fail_to_retrieve,  # type: ignore[reportPrivateUsage]
)
from referencing import exceptions
from referencing.typing import URI, D, Retrieve

#: Format version for serialized packages.
FORMAT_VERSION = 1


@frozen
class ManifestEntry:
    """
    Manifest metadata for a single packaged document.
    """

    #: The document's canonical URI (its original ``$id``).
    uri: URI

    #: A SHA-256 digest of the document's canonical JSON serialization.
    digest: str

    #: The short name of the `Specification` the document is interpreted under
    #: (or ``"opaque"`` for opaque resources).
    specification: str | None


@frozen
class MissingResource:
    """
    A resource referenced by the package which could not be collected.
    """

    #: The URI of the missing referenced resource.
    uri: URI

    #: The URIs of packaged documents which reference the missing resource.
    referenced_from: tuple[URI, ...] = field(converter=tuple)


@frozen
class Manifest:
    """
    A package manifest: per-document digests and missing referenced resources.
    """

    entries: tuple[ManifestEntry, ...] = field(converter=tuple)
    missing: tuple[MissingResource, ...] = field(converter=tuple)

    def digest_for(self, uri: URI) -> str | None:
        """
        Return the recorded digest for the given URI, if present.
        """
        for entry in self.entries:
            if entry.uri == uri:
                return entry.digest
        return None


def _to_native(value: Any) -> Any:
    """
    Convert rpds (or other mapping/sequence) values to plain JSON types.
    """
    if isinstance(value, Mapping):
        return {str(key): _to_native(each) for key, each in value.items()}
    if isinstance(value, (str, bytes, bool, int, float)) or value is None:
        return value
    if isinstance(value, Iterable):
        return [_to_native(each) for each in value]
    return value


def _canonical_bytes(contents: Any) -> bytes:
    """
    Serialize contents deterministically for digesting.
    """
    native = _to_native(contents)
    text = json.dumps(
        native,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return text.encode("utf-8")


def digest_for(contents: Any) -> str:
    """
    Compute the SHA-256 content digest used by package manifests.
    """
    return hashlib.sha256(_canonical_bytes(contents)).hexdigest()


@frozen
class Package(Generic[D]):
    """
    A self-contained bundle of documents plus a `Manifest`.

    Packages serialize to plain JSON (via `to_dict`, `to_json` or `to_path`)
    and can be loaded offline (via `from_dict`, `from_json`, `from_path` and
    `registry`, or the `load` convenience) without any external retrieval.
    """

    #: Mapping of canonical URI to the original document contents.
    documents: Mapping[URI, D]

    #: The package manifest.
    manifest: Manifest

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize the package to a JSON-compatible dictionary.
        """
        return {
            "version": FORMAT_VERSION,
            "documents": {
                uri: _to_native(contents)
                for uri, contents in self.documents.items()
            },
            "manifest": {
                "entries": [
                    {
                        "uri": entry.uri,
                        "digest": entry.digest,
                        "specification": entry.specification,
                    }
                    for entry in self.manifest.entries
                ],
                "missing": [
                    {
                        "uri": missing.uri,
                        "referencedFrom": list(missing.referenced_from),
                    }
                    for missing in self.manifest.missing
                ],
            },
        }

    def to_json(self, **kwargs: Any) -> str:
        """
        Serialize the package to a JSON string.
        """
        return json.dumps(self.to_dict(), **kwargs)

    def to_path(self, path: str | Path, **kwargs: Any) -> None:
        """
        Write the package as JSON to a file path.
        """
        Path(path).write_text(self.to_json(**kwargs), encoding="utf-8")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Package[D]:
        """
        Reconstruct a package from a JSON-compatible dictionary.
        """
        if data.get("version") != FORMAT_VERSION:
            raise ValueError(
                f"Unsupported package format version: {data.get('version')!r}",
            )
        raw_documents = data.get("documents")
        if not isinstance(raw_documents, Mapping):
            raise ValueError("Package is missing a 'documents' mapping.")
        raw_manifest = data.get("manifest")
        if not isinstance(raw_manifest, Mapping):
            raise ValueError("Package is missing a 'manifest' mapping.")

        entries = tuple(
            ManifestEntry(
                uri=each["uri"],
                digest=each["digest"],
                specification=each.get("specification"),
            )
            for each in raw_manifest.get("entries", ())
        )
        missing = tuple(
            MissingResource(
                uri=each["uri"],
                referenced_from=tuple(each.get("referencedFrom", ())),
            )
            for each in raw_manifest.get("missing", ())
        )
        return cls(
            documents=dict(raw_documents),
            manifest=Manifest(entries=entries, missing=missing),
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> Package[D]:
        """
        Reconstruct a package from a JSON string.
        """
        return cls.from_dict(json.loads(value))

    @classmethod
    def from_path(cls, path: str | Path) -> Package[D]:
        """
        Read a package from a JSON file.
        """
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def load(
        cls,
        source: str | bytes | Path | Mapping[str, Any],
        *,
        default_specification: Specification[D] = Specification,
    ) -> Registry[D]:
        """
        Load a serialized package and return its offline, verified registry.

        ``source`` may be a JSON string, a path to a JSON file, or an already
        deserialized package dictionary.
        """
        if isinstance(source, Mapping):
            package = cls.from_dict(source)
        elif isinstance(source, Path):
            package = cls.from_path(source)
        else:
            package = cls.from_json(source)
        return package.registry(default_specification=default_specification)

    def registry(
        self,
        *,
        default_specification: Specification[D] = Specification,
    ) -> Registry[D]:
        """
        Verify the package and build its fully crawled offline `Registry`.

        Every manifest digest is checked against the packaged contents, and
        the returned registry has no retrieval callable configured, so it is
        incapable of fetching external resources.

        Raises:

            `exceptions.DigestMismatch`

                if a document's contents don't match their recorded digest

            `ValueError`

                if the manifest and documents disagree about which URIs
                are present
        """
        from referencing.jsonschema import specification_named

        uris = set(self.documents)
        entry_uris = {entry.uri for entry in self.manifest.entries}
        if uris != entry_uris:
            missing_in_manifest = sorted(uris - entry_uris)
            missing_documents = sorted(entry_uris - uris)
            raise ValueError(
                "Package manifest does not match its documents: "
                f"undocumented {missing_in_manifest!r}, "
                f"missing {missing_documents!r}",
            )

        resources: list[tuple[URI, Resource[D]]] = []
        for entry in self.manifest.entries:
            contents = self.documents[entry.uri]
            digest = digest_for(contents)
            if digest != entry.digest:
                raise exceptions.DigestMismatch(
                    uri=entry.uri,
                    expected=entry.digest,
                    got=digest,
                )
            if entry.specification == "opaque":
                specification: Specification[D] = Specification.OPAQUE
            elif entry.specification is None:
                specification = default_specification
            else:
                specification = specification_named(
                    entry.specification,
                    default=default_specification,
                )
            resources.append(
                (entry.uri, specification.create_resource(contents)),
            )

        # No retrieve callable: lookups of anything not packaged fail rather
        # than causing external (e.g. network) access.
        return Registry().with_resources(resources).crawl()


def pack(
    roots: Resource[D] | Iterable[Resource[D]],
    *,
    retrieve: Retrieve[D] = _fail_to_retrieve,
) -> Package[D]:
    """
    Package one or more root resources together with their references.

    Each reachable document is stored under its original internal identifier
    (its ``$id``); documents with their own identifiers are never merged
    together. References -- including those forming cycles -- are enumerated
    via each document's `Specification`, and each canonical document is
    collected at most once. References to resources which cannot be retrieved
    are recorded in the manifest instead of causing failure.

    Raises:

        `exceptions.NoInternalID`

            if a root resource has no internal identifier
    """
    if isinstance(roots, Resource):
        roots = (roots,)

    documents: dict[URI, Resource[D]] = {}
    attempted: set[URI] = set()
    referrers: dict[URI, set[URI]] = {}
    pending_documents: list[tuple[URI, Resource[D]]] = []

    for root in roots:
        root_id = root.id()
        if root_id is None:
            raise exceptions.NoInternalID(resource=root)
        pending_documents.append((root_id, root))

    while pending_documents:
        document_uri, root = pending_documents.pop()
        if document_uri in documents:
            continue

        external: list[tuple[URI, Resource[D]]] = []
        nodes: list[tuple[URI, Resource[D]]] = [(document_uri, root)]
        while nodes:
            node_uri, node = nodes.pop()
            documents.setdefault(node_uri, node)

            for reference in node._specification.references_in(
                node.contents,
            ):
                target, _fragment = urldefrag(
                    urljoin(node_uri, reference.target),
                )
                referrers.setdefault(target, set()).add(node_uri)
                if target in documents or target in attempted:
                    continue
                attempted.add(target)
                try:
                    fetched = retrieve(target)
                except (
                    exceptions.CannotDetermineSpecification,
                    exceptions.NoSuchResource,
                ):
                    continue
                except Exception as error:
                    raise exceptions.Unretrievable(ref=target) from error
                fetched_id = fetched.id()
                canonical = (
                    urljoin(target, fetched_id) if fetched_id else target
                )
                external.append((canonical, fetched))

            for subresource in node.subresources():
                sub_id = subresource.id()
                if sub_id is None:
                    nodes.append((node_uri, subresource))
                else:
                    nodes.append((urljoin(node_uri, sub_id), subresource))

        pending_documents.extend(external)

    entries = tuple(
        ManifestEntry(
            uri=uri,
            digest=digest_for(resource.contents),
            specification=resource._specification.name,
        )
        for uri, resource in sorted(documents.items())
    )
    missing = tuple(
        MissingResource(
            uri=uri,
            referenced_from=tuple(sorted(referrers.get(uri, ()))),
        )
        for uri in sorted(uri for uri in attempted if uri not in documents)
    )
    manifest = Manifest(entries=entries, missing=missing)
    return Package(
        documents={
            uri: _to_native(resource.contents)
            for uri, resource in documents.items()
        },
        manifest=manifest,
    )
