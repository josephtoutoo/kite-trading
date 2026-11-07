"""Fail-closed network block for offline tests.

Any TCP connect or HTTP open raises. Tests must not treat a raised block as success
unless they are asserting that a fetch was refused.
"""
import http.client
import socket
import urllib.request

ATTEMPTS = []


def _note(kind, detail):
    ATTEMPTS.append({"kind": kind, "detail": str(detail)[:120]})
    raise OSError("network blocked by offline test")


class BlockedSocket(socket.socket):
    def connect(self, address):
        _note("connect", address)

    def connect_ex(self, address):
        _note("connect_ex", address)


def install():
    socket.socket = BlockedSocket
    socket.create_connection = lambda *a, **k: _note("create_connection", a[0] if a else "")

    def _connect(self, *a, **k):
        _note("http_connect", getattr(self, "host", ""))

    http.client.HTTPConnection.connect = _connect
    http.client.HTTPSConnection.connect = _connect

    def _urlopen(*a, **k):
        _note("urlopen", a[0] if a else "")

    urllib.request.urlopen = _urlopen
