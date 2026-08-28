from qumidi import errors


def test_every_exception_derives_from_qu_error() -> None:
    names = [
        "QuConnectionError",
        "QuConnectionLostError",
        "SyncTimeoutError",
        "NotSyncedError",
        "UnsupportedOnModelError",
        "ParameterRangeError",
        "UnknownParameterError",
        "ReadOnlyParameterError",
        "ValueUnknownError",
        "ShutdownNotPermittedError",
        "MeterLayoutError",
    ]
    for name in names:
        cls = getattr(errors, name)
        assert issubclass(cls, errors.QuError), name


def test_connection_lost_is_a_connection_error() -> None:
    assert issubclass(errors.QuConnectionLostError, errors.QuConnectionError)


def test_qu_error_is_a_plain_exception_not_a_base_exception() -> None:
    # Catching Exception must catch everything this library raises; nothing
    # here should escape a caller's blanket handler the way KeyboardInterrupt
    # does.
    assert issubclass(errors.QuError, Exception)
    assert not issubclass(errors.QuError, (SystemExit, KeyboardInterrupt))


def test_errors_carry_their_message() -> None:
    exc = errors.ParameterRangeError("pan on Input 6: raw 0x7F exceeds maximum 0x4A")
    assert "Input 6" in str(exc)
    assert "0x4A" in str(exc)
