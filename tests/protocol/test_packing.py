import pytest

from qumixer_py.protocol.sysex import decode_meter_db, pack8to7, unpack7to8


def test_the_specs_worked_example() -> None:
    # packed 20 7C 00 -> unpacked 7C 80 -> 0x7C80 - 0x8000 = -896
    # -> /256.0 = -3.5 dB. Make this an explicit unit test.
    unpacked = unpack7to8(bytes([0x20, 0x7C, 0x00]))
    assert unpacked == bytes([0x7C, 0x80])
    value = int.from_bytes(unpacked, "big")
    assert value == 0x7C80
    assert decode_meter_db(value) == pytest.approx(-3.5)


def test_bit_order_regression_a_reversed_convention_must_fail() -> None:
    # A is bit 6 and belongs to the FIRST following byte. A reversed
    # convention also produces valid-looking output and would corrupt every
    # meter above the first.
    packed = bytes([0b1000000, 0x00, 0x00])   # only A set
    assert unpack7to8(packed) == bytes([0x80, 0x00])
    packed = bytes([0b0000001, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])  # only G set
    assert unpack7to8(packed) == bytes([0, 0, 0, 0, 0, 0, 0x80])


def test_a_full_group_of_seven() -> None:
    packed = bytes([0b1111111, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07])
    assert unpack7to8(packed) == bytes([0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87])


@pytest.mark.parametrize("length", [1, 2, 3, 4, 5, 6])
def test_every_partial_group_length(length) -> None:  # type: ignore[no-untyped-def]
    # handle every partial-group length 1-6, not just the example.
    payload = bytes(0x80 + i for i in range(length))
    assert unpack7to8(pack8to7(payload)) == payload


def test_pack_and_unpack_round_trip_over_many_lengths() -> None:
    for length in range(40):
        payload = bytes((i * 37) % 256 for i in range(length))
        assert unpack7to8(pack8to7(payload)) == payload


def test_the_high_bit_byte_of_a_partial_group_is_left_aligned() -> None:
    # with 2 remaining bytes it is 0AB00000 ...
    assert pack8to7(bytes([0x80, 0x00])) == bytes([0b1000000, 0x00, 0x00])


def test_decode_is_signed_and_unbounded_above() -> None:
    # do not clamp to a plausible-looking range -- an out-of-range
    # reading is diagnostic information about a layout error.
    assert decode_meter_db(0x8000) == pytest.approx(0.0)
    assert decode_meter_db(0x0000) == pytest.approx(-128.0)
    assert decode_meter_db(0xFFFF) == pytest.approx(127.99609375)


def test_unpack_of_an_empty_payload_is_empty() -> None:
    assert unpack7to8(b"") == b""
