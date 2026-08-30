import pytest

from qumidi.errors import ParameterRangeError
from qumidi.protocol.params import Fixed, FlaggedIndex
from qumidi.protocol.registry import REGISTRY
from qumidi.state import sub_index_for


@pytest.mark.parametrize("name,nrpn_id", [("dca_assign", 0x40), ("mute_group_assign", 0x5C)])
def test_assigns_are_flagged_index_with_a_fixed_07_filler(name, nrpn_id) -> None:  # type: ignore[no-untyped-def]
    spec = REGISTRY.lookup(name)
    assert spec.nrpn_id == nrpn_id
    assert isinstance(spec.vx, Fixed) and spec.vx.value == 0x07
    assert isinstance(spec.domain, FlaggedIndex)
    assert spec.domain.count == 4
    assert spec.domain.on_flag == 0x40


@pytest.mark.parametrize("name", ["dca_assign", "mute_group_assign"])
@pytest.mark.parametrize("index,on,expected", [
    (0, False, 0x00), (3, False, 0x03), (0, True, 0x40), (3, True, 0x43),
])
def test_the_documented_encoding(name, index, on, expected) -> None:  # type: ignore[no-untyped-def]
    # PRD 14.2: the DCA row's *label* is a copy-paste error, but the encoding
    # is correct as given.
    domain = REGISTRY.lookup(name).domain
    assert domain.encode(index=index, on=on) == expected  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", ["dca_assign", "mute_group_assign"])
def test_the_sub_index_comes_from_the_value_not_vx(name) -> None:  # type: ignore[no-untyped-def]
    spec = REGISTRY.lookup(name)
    assert sub_index_for(spec, vx=0x07, value=0x42) == 2
    assert sub_index_for(spec, vx=0x07, value=0x02) == 2


def test_an_out_of_range_group_index_is_rejected() -> None:
    with pytest.raises(ParameterRangeError):
        REGISTRY.lookup("dca_assign").domain.encode(index=4, on=True)  # type: ignore[attr-defined]
