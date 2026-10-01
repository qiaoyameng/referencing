import itertools

import pytest

from referencing import Resource, exceptions


def pairs(choices):
    return sorted(
        itertools.combinations(choices, 2),
        key=lambda error: str(type(error)),
    )


TRUE = Resource.opaque(True)


thunks = (
    lambda: exceptions.CannotDetermineSpecification(TRUE),
    lambda: exceptions.NoSuchResource("urn:example:foo"),
    lambda: exceptions.NoInternalID(TRUE),
    lambda: exceptions.InvalidAnchor(resource=TRUE, anchor="foo", ref="a#b"),
    lambda: exceptions.NoSuchAnchor(resource=TRUE, anchor="foo", ref="a#b"),
    lambda: exceptions.PointerToNowhere(resource=TRUE, ref="urn:example:foo"),
    lambda: exceptions.Unresolvable("urn:example:foo"),
    lambda: exceptions.Unretrievable("urn:example:foo"),
    lambda: exceptions.RetrievalDenied(
        ref="urn:example:foo",
        reason="nope",
    ),
    lambda: exceptions.RetrievalQuotaExceeded(
        ref="urn:example:foo",
        reason="retrieved resources",
        limit=3,
    ),
)


@pytest.mark.parametrize("one, two", pairs(each() for each in thunks))
def test_eq_incompatible_types(one, two):
    assert one != two


@pytest.mark.parametrize("thunk", thunks)
def test_hash(thunk):
    assert thunk() in {thunk()}  # noqa: FURB171


def test_retrieval_denied_equality_and_message():
    assert exceptions.RetrievalDenied(
        ref="urn:example:foo",
        reason="nope",
    ) == exceptions.RetrievalDenied(ref="urn:example:foo", reason="nope")
    assert str(
        exceptions.RetrievalDenied(ref="urn:example:foo", reason="nope"),
    ) == (
        "Retrieval of 'urn:example:foo' was denied by the retrieval "
        "policy: nope"
    )
    assert str(exceptions.RetrievalDenied(ref="urn:example:foo")) == (
        "Retrieval of 'urn:example:foo' was denied by the retrieval policy"
    )


def test_retrieval_quota_exceeded_is_a_retrieval_denial():
    error = exceptions.RetrievalQuotaExceeded(
        ref="urn:example:foo",
        reason="retrieved resources",
        limit=2,
    )
    assert isinstance(error, exceptions.RetrievalDenied)
    assert str(error) == (
        "Retrieval of 'urn:example:foo' exceeded the quota of 2 "
        "retrieved resources"
    )
