import ipaddress
import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TMDB_API_KEY", "test")


_connect = socket.socket.connect


@pytest.fixture(autouse=True)
def no_internet(monkeypatch):
    """Tests never reach the internet: their result must not depend on the network
    (CI's servers reach sites this machine can't, and the other way round). Local
    servers (the browser tests' fake TMDB and TorrServer) are fine."""

    def connect(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost"
        if not local:
            raise OSError(f"a test tried to reach the internet ({host})")
        return _connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
