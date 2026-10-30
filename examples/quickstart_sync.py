"""Quickstart -- the same session through the sync facade.

Requires a real Qu console reachable at the host below; this is not exercised
by the test suite. Behaviour is identical to
examples/quickstart.py: no ``await``, and the property assignment does not
return until the bytes are on the wire.
"""

from qumixer_py import QuMixer

with QuMixer("192.168.1.60") as mixer:
    mixer.wait_synced()
    mixer.input(6).fader.db = -6.0
    print(mixer.input(6).fader.db)
