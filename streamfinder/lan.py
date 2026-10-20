"""This machine's address on the home network (for links to the app).

No side effects on import: windows/control.py uses it too (imported inside a function,
like its other imports from the package, after it took the services' environment).
"""

import socket


def lan_ip() -> str:
    """The address other devices reach this machine at; 127.0.0.1 without a network."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # no packet is sent; just picks the address
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
