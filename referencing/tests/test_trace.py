"""
Tests for optional resolution tracing.
"""

from __future__ import annotations

import attrs
import pytest

from referencing import Registry, Resource, Specification, exceptions
from referencing.jsonschema import DRAFT201909, DRAFT202012
from referencing.trace import (
    AnchorAdopted,
    AnchorCandidate,
    AnchorFound,
    LookupFailed,
    LookupStarted,
    LookupSucceeded,
    PointerStep,
    ReferenceResolved,
    ResolutionTrace,
    ResourceFoundInRegistry,
    RetrievalStarted,
    RetrievalSucceeded,
    TraceEvent,
)


ROOT_URI = "http://example.com"


def _dynamic_schemas():
    one = DRAFT202012.create_resource({"$anchor": "fooAnchor"})
    root = DRAFT202012.create_resource(
        {
            "$id": ROOT_URI,
            "$dynamicAnchor": "fooAnchor",
            "$defs": {
                "foo": {
                    "$id": "foo",
                    "$dynamicAnchor": "fooAnchor",
                    "$defs": {
                        "bar": True,
                        "baz": {"$dynamicAnchor": "fooAnchor"},
                    },
                },
            },
        },
    )
    return one, root


class _NestedAnchor:
    """
    An anchor whose own resolution performs another (nested) lookup.
    """

    def __init__(self, name, target):
        self.name = name
        self.target = target

    def resolve(self, resolver):
        return resolver.lookup(self.target)


def test_traced_returns_trace_on_resolved():
    resource = DRAFT202012.create_resource({"type": "integer"})
    resolver, trace = (
        Registry()
        .with_resource(ROOT_URI, resource)
        .resolver()
        .traced()
    )

    resolved = resolver.lookup(ROOT_URI)

    assert resolved.trace is trace
    started = [
        event for event in trace.events
        if isinstance(event, LookupStarted)
    ]
    assert len(started) == 1
    assert started[0].ref == ROOT_URI
    assert started[0].base_uri == ""
    assert isinstance(trace.events[1], ReferenceResolved)
    assert isinstance(trace.events[2], ResourceFoundInRegistry)
    succeeded = trace.events[-1]
    assert isinstance(succeeded, LookupSucceeded)
    assert succeeded.uri == ROOT_URI


def test_untraced_lookup_has_no_trace():
    resource = DRAFT202012.create_resource({"type": "integer"})
    resolver = Registry().with_resource(ROOT_URI, resource).resolver()

    resolved = resolver.lookup(ROOT_URI)

    assert resolved.trace is None
    assert resolved.resolver.recorder is None


def test_traced_records_relative_reference_and_base_uri():
    resource = DRAFT202012.create_resource({"type": "integer"})
    registry = Registry().with_resource("http://example.com/foo/bar", resource)
    resolver, trace = (
        registry
        .resolver(base_uri="http://example.com/foo/")
        .traced()
    )

    resolver.lookup("bar#")

    resolved_event = next(
        event for event in trace.events if isinstance(event, ReferenceResolved)
    )
    assert resolved_event.uri == "http://example.com/foo/bar"
    assert resolved_event.fragment == ""


def test_traced_records_json_pointer_steps_and_subresource_entry():
    resource = DRAFT202012.create_resource(
        {
            "$id": ROOT_URI + "/root",
            "$defs": {"child": {"$id": "child", "type": "object"}},
        },
    )
    resolver, trace = (
        Registry()
        .with_resource(ROOT_URI + "/root", resource)
        .resolver()
        .traced()
    )

    resolved = resolver.lookup(ROOT_URI + "/root#/$defs/child")

    steps = [event for event in trace.events if isinstance(event, PointerStep)]
    assert [
        (event.segment, event.path, event.subresource)
        for event in steps
    ] == [
        ("$defs", "/$defs", False),
        ("child", "/$defs/child", True),
    ]
    assert resolved.resolver._base_uri == ROOT_URI + "/child"
    assert isinstance(trace.events[-1], LookupSucceeded)


def test_traced_dynamic_ref_records_candidates_and_adopted_anchor():
    one, root = _dynamic_schemas()
    calls = []

    def retrieve(uri):
        calls.append(uri)
        return {
            ROOT_URI + "/foo/": one,
            ROOT_URI + "/foo/bar": root,
        }[uri]

    resolver, trace = (
        Registry(retrieve=retrieve)
        .with_resource(ROOT_URI, root)
        .resolver()
        .traced()
    )

    first = resolver.lookup(ROOT_URI)
    second = first.resolver.lookup("foo/")
    third = second.resolver.lookup("bar")
    resolved = third.resolver.lookup("#fooAnchor")

    assert resolved.contents == root.contents
    assert resolved.trace is trace

    found = [event for event in trace.events if isinstance(event, AnchorFound)]
    assert found[-1].uri == ROOT_URI + "/foo/bar"
    assert found[-1].name == "fooAnchor"
    assert found[-1].dynamic is True

    candidates = [
        event for event in trace.events if isinstance(event, AnchorCandidate)
    ]
    # Initial anchor, then dynamic scope: outer dynamic, a non-dynamic anchor
    # which must be skipped, then the outermost dynamic anchor.
    assert [
        (event.uri, event.found, event.dynamic) for event in candidates
    ] == [
        (ROOT_URI + "/foo/bar", True, True),
        (ROOT_URI + "/foo/", True, False),
        (ROOT_URI, True, True),
    ]

    adopted = [
        event for event in trace.events
        if isinstance(event, AnchorAdopted)
    ]
    assert adopted[-1].uri == ROOT_URI
    assert adopted[-1].dynamic is True

    # A counting retrieve function: exactly two external lookups, with no
    # additional retrieval performed to produce the trace.
    assert calls == [ROOT_URI + "/foo/", ROOT_URI + "/foo/bar"]


def test_traced_dynamic_ref_falls_back_to_non_dynamic_resource():
    one = DRAFT202012.create_resource({"$anchor": "fooAnchor"})
    two = DRAFT202012.create_resource(
        {
            "$id": ROOT_URI,
            "$defs": {"foo": {"$id": "foo"}},
        },
    )
    resolver, trace = (
        Registry()
        .with_resources(
            [
                (ROOT_URI, two),
                (ROOT_URI + "/foo/", one),
            ],
        )
        .resolver()
        .traced()
    )
    first = resolver.lookup(ROOT_URI)
    second = first.resolver.lookup("foo/")

    resolved = second.resolver.lookup("#fooAnchor")

    assert resolved.contents == one.contents
    adopted = [
        event for event in trace.events
        if isinstance(event, AnchorAdopted)
    ]
    assert adopted[-1].uri == ROOT_URI + "/foo/"
    assert adopted[-1].dynamic is False


def test_trace_does_not_cause_additional_retrievals():
    resource = DRAFT202012.create_resource(
        {
            "$id": ROOT_URI + "/main",
            "$defs": {"sub": {"$id": "sub"}},
        },
    )
    calls = []

    def retrieve(uri):
        calls.append(uri)
        return resource

    resolver, trace = (
        Registry(retrieve=retrieve).resolver().traced()
    )
    first = resolver.lookup(ROOT_URI + "/main")
    first.resolver.lookup(ROOT_URI + "/sub")
    first.resolver.lookup(ROOT_URI + "/sub")

    assert calls == [ROOT_URI + "/main"]
    started = [
        event for event in trace.events if isinstance(event, RetrievalStarted)
    ]
    succeeded = [
        event for event in trace.events
        if isinstance(event, RetrievalSucceeded)
    ]
    assert len(started) == len(succeeded) == 1
    assert started[0].uri == succeeded[0].uri == ROOT_URI + "/main"


def test_pointer_failure_trace_stops_at_failing_step():
    resource = DRAFT202012.create_resource({"foo": {"bar": [1, 2]}})
    uri = ROOT_URI + "/doc"
    resolver, trace = (
        Registry().with_resource(uri, resource).resolver().traced()
    )

    with pytest.raises(exceptions.PointerToNowhere) as excinfo:
        resolver.lookup(uri + "#/foo/bar/9")

    assert excinfo.value.trace is trace
    paths = [
        event.path
        for event in trace.events
        if isinstance(event, PointerStep)
    ]
    assert paths == ["/foo", "/foo/bar", "/foo/bar/9"]
    final = trace.events[-1]
    assert isinstance(final, LookupFailed)
    assert final.stage == "pointer"
    assert final.error_type == "PointerToNowhere"
    assert not any(
        isinstance(event, LookupSucceeded) for event in trace.events
    )


def test_missing_anchor_failure_trace():
    resource = DRAFT202012.create_resource({"foo": "bar"})
    uri = ROOT_URI + "/doc"
    resolver, trace = (
        Registry().with_resource(uri, resource).resolver().traced()
    )

    with pytest.raises(exceptions.NoSuchAnchor) as excinfo:
        resolver.lookup(uri + "#nope")

    assert excinfo.value.trace is trace
    final = trace.events[-1]
    assert isinstance(final, LookupFailed)
    assert final.stage == "anchor"
    assert final.error_type == "NoSuchAnchor"


def test_retrieval_failure_trace():
    def retrieve(uri):
        raise exceptions.NoSuchResource(ref=uri)

    resolver, trace = Registry(retrieve=retrieve).resolver().traced()

    with pytest.raises(exceptions.Unresolvable) as excinfo:
        resolver.lookup("http://other.example.com/x")

    assert excinfo.value.trace is trace
    assert isinstance(trace.events[-2], RetrievalStarted)
    final = trace.events[-1]
    assert isinstance(final, LookupFailed)
    assert final.stage == "retrieve"
    assert final.error_type == "Unresolvable"


def test_nested_lookups_are_correlated():
    nested_spec = Specification(
        name="nested",
        id_of=lambda contents: contents.get("ID"),
        subresources_of=lambda contents: [],
        maybe_in_subresource=(
            lambda segments, resolver, subresource: resolver
        ),
        anchors_in=lambda specification, contents: (
            [_NestedAnchor("jump", ROOT_URI + "/target")]
            if contents.get("ID") == ROOT_URI + "/start"
            else []
        ),
    )
    start = Resource(
        contents={"ID": ROOT_URI + "/start"},
        specification=nested_spec,
    )
    target = Resource(
        contents={"ID": ROOT_URI + "/target", "ok": True},
        specification=nested_spec,
    )
    resolver, trace = (
        Registry()
        .with_resources(
            [(ROOT_URI + "/start", start), (ROOT_URI + "/target", target)],
        )
        .resolver()
        .traced()
    )

    resolved = resolver.lookup(ROOT_URI + "/start#jump")
    assert resolved.contents == target.contents

    starts = [
        (event.lookup_id, event.parent_lookup_id, event.ref)
        for event in trace.events
        if isinstance(event, LookupStarted)
    ]
    assert starts == [
        (0, None, ROOT_URI + "/start#jump"),
        (1, 0, ROOT_URI + "/target"),
    ]
    assert sorted(
        event.lookup_id
        for event in trace.events
        if isinstance(event, LookupSucceeded)
    ) == [0, 1]
    assert {event.lookup_id for event in trace.for_lookup(0)} == {0}
    assert {event.lookup_id for event in trace.for_lookup(1)} == {1}


def test_trace_frame_is_left_balanced_after_failure():
    resource = DRAFT202012.create_resource({"foo": "bar"})
    uri = ROOT_URI + "/doc"
    resolver, trace = (
        Registry().with_resource(uri, resource).resolver().traced()
    )

    with pytest.raises(exceptions.PointerToNowhere):
        resolver.lookup(uri + "#/nope")
    # A subsequent lookup still records a fresh, top-level frame.
    resolver.lookup(uri)
    starts = [
        event for event in trace.events if isinstance(event, LookupStarted)
    ]
    assert [event.parent_lookup_id for event in starts] == [None, None]
    assert [event.lookup_id for event in starts] == [0, 1]


def test_unresolvable_equality_ignores_trace():
    resource = DRAFT202012.create_resource({"foo": "bar"})
    uri = ROOT_URI + "/doc"
    resolver, trace = (
        Registry().with_resource(uri, resource).resolver().traced()
    )

    with pytest.raises(exceptions.PointerToNowhere) as excinfo:
        resolver.lookup(uri + "#/nope")
    traced_error = excinfo.value

    plain = exceptions.PointerToNowhere(
        ref="/nope",
        resource=resource,
    )
    assert traced_error == plain
    assert hash(traced_error) == hash(plain)
    assert traced_error.trace is trace


def test_traced_with_existing_trace_continues_recording():
    resource = DRAFT202012.create_resource({"type": "integer"})
    trace = ResolutionTrace()
    resolver = Registry().with_resource(ROOT_URI, resource).resolver()
    traced_resolver, returned = resolver.traced(trace)

    assert returned is trace
    traced_resolver.lookup(ROOT_URI)
    traced_resolver.lookup(ROOT_URI)

    assert [
        event.lookup_id
        for event in trace.events
        if isinstance(event, LookupStarted)
    ] == [0, 1]


def test_events_are_immutable():
    resource = DRAFT202012.create_resource({})
    resolver, trace = (
        Registry().with_resource(ROOT_URI, resource).resolver().traced()
    )
    resolver.lookup(ROOT_URI)

    with pytest.raises(attrs.exceptions.FrozenInstanceError):
        trace.events[0].ref = "changed"
    assert isinstance(trace.events[0], TraceEvent)


def test_recursive_ref_records_nested_lookup_frames():
    inner = DRAFT201909.create_resource(
        {"$id": ROOT_URI + "/inner", "$recursiveAnchor": True},
    )
    outer = DRAFT201909.create_resource(
        {"$id": ROOT_URI + "/outer", "$recursiveAnchor": True},
    )
    registry = Registry().with_resources(
        [(ROOT_URI + "/inner", inner), (ROOT_URI + "/outer", outer)],
    )
    resolver, trace = registry.resolver().traced()

    first = resolver.lookup(ROOT_URI + "/inner")
    from referencing.jsonschema import lookup_recursive_ref

    lookup_recursive_ref(resolver=first.resolver)

    # The recursive-ref helper calls `lookup` after the outer lookup has
    # completed, so each call is a top-level lookup (none are nested).
    starts = [
        event for event in trace.events if isinstance(event, LookupStarted)
    ]
    assert len(starts) >= 2
    assert all(event.parent_lookup_id is None for event in starts)
