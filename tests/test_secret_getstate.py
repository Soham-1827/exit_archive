"""T4.3.4: an explicit __getstate__ call must not hand out the raw value."""

import copy
import pickle

import pytest

from exit_archive.secrets import HarvestToken, Secret

VALUE = "tok-GETSTATE-1a2b3c"


@pytest.mark.parametrize("cls", [Secret, HarvestToken])
def test_getstate_exposes_nothing(cls: type[Secret]) -> None:
    s = cls(VALUE)
    assert s.__getstate__() is None
    assert getattr(s, "__getstate__")() is None  # noqa: B009
    for state in (s.__getstate__(), getattr(s, "__getstate__")()):  # noqa: B009
        assert VALUE not in repr(state)


@pytest.mark.parametrize("cls", [Secret, HarvestToken])
def test_pickle_copy_and_repr_unchanged(cls: type[Secret]) -> None:
    s = cls(VALUE)
    for op in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            op(s)
    assert VALUE not in repr(s) and VALUE not in str(s)
    assert s.reveal() == VALUE


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer%20{v}",
        "GET /x?h=Bearer%20{v}&a=1",
        "GET /x?h=bearer%20{v}",
        "GET /x?h=BEARER+{v}",
        "GET /x?h=Bearer+{v}",
        "Bearer%20%20{v}",
        "Bearer {v}",
    ],
)
def test_bearer_variants_scrubbed(text: str) -> None:
    from exit_archive.secrets import redact

    assert VALUE not in redact(text.format(v=VALUE), None)
