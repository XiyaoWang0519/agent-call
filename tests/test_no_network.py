"""R01: the offline test boundary.

Every provider/Twilio/HTTP dependency in the suite is faked or transport-mocked.
This module proves the autouse guard in ``tests/conftest.py`` actually rejects an
outbound non-loopback socket, so an unmocked code path fails loudly instead of
reaching a real endpoint.
"""

from __future__ import annotations

import socket

import pytest

from tests.conftest import ExternalNetworkBlocked


def test_non_loopback_connect_is_blocked() -> None:
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ExternalNetworkBlocked):
            client.connect(("192.0.2.1", 443))
    finally:
        client.close()


def test_loopback_connect_is_allowed() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(server.getsockname())
    finally:
        client.close()
        server.close()
