"""S6.8 balanced plan: application-layer SSRF / DNS-rebinding guards.

These tests pin the security invariant added on 2026-11-15:
an allowlisted hostname may only pin globally routable addresses.  Private,
link-local, CGNAT, reserved, multicast and cloud metadata addresses must never
become authorised outbound peers, even if DNS returns them.
"""

from __future__ import annotations

import pytest

from app import external_guard as eg


@pytest.fixture(autouse=True)
def _clean_authorized_ips():
    with eg._lock:
        eg._authorized_ips.clear()
    yield
    with eg._lock:
        eg._authorized_ips.clear()


def test_is_public_ip_rejects_private_and_metadata():
    assert eg._is_public_ip("8.8.8.8") is True
    assert eg._is_public_ip("2001:4860:4860::8888") is True
    for value in (
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.10",
        "172.16.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "::1",
        "fd00::1",
        "fe80::1",
        "::ffff:10.0.0.5",
    ):
        assert eg._is_public_ip(value) is False, value


def test_record_authorized_ip_skips_private_and_metadata():
    eg._record_authorized_ip(("10.0.0.5", 80))
    eg._record_authorized_ip(("169.254.169.254", 80))
    assert eg._ip_authorized("10.0.0.5") is False
    assert eg._ip_authorized("169.254.169.254") is False

    eg._record_authorized_ip(("8.8.8.8", 443))
    assert eg._ip_authorized("8.8.8.8") is True


def test_guard_outbound_denies_private_literal(monkeypatch):
    monkeypatch.setattr(eg, "enabled", lambda: True)
    eg._record_authorized_ip(("10.0.0.5", 80))
    with pytest.raises(eg.ExternalFetchBlocked) as exc:
        eg._guard_outbound("connect", ("10.0.0.5", 80))
    assert exc.value.reason_code == "EGRESS_DESTINATION_DENIED"


def test_guard_outbound_denies_cloud_metadata(monkeypatch):
    monkeypatch.setattr(eg, "enabled", lambda: True)
    eg._record_authorized_ip(("169.254.169.254", 80))
    with pytest.raises(eg.ExternalFetchBlocked) as exc:
        eg._guard_outbound("connect", ("169.254.169.254", 80))
    assert exc.value.reason_code == "EGRESS_DESTINATION_DENIED"


def test_guard_outbound_allows_authorized_public_ip(monkeypatch):
    monkeypatch.setattr(eg, "enabled", lambda: True)
    eg._record_authorized_ip(("8.8.8.8", 443))
    assert eg._guard_outbound("connect", ("8.8.8.8", 443)) == ("8.8.8.8", 443)


class _FakeSock:
    def __init__(self, peer):
        self._peer = peer
        self.family = None

    def getpeername(self):
        return self._peer


def test_connected_stream_denies_private_peer(monkeypatch):
    monkeypatch.setattr(eg, "enabled", lambda: True)
    eg._record_authorized_ip(("10.0.0.5", 80))
    with pytest.raises(eg.ExternalFetchBlocked) as exc:
        eg._assert_stream_peer_allowed(_FakeSock(("10.0.0.5", 80)), "sendall")
    assert exc.value.reason_code == "NETWORK_STREAM_SEND_BLOCKED"


def test_connected_stream_allows_public_peer(monkeypatch):
    monkeypatch.setattr(eg, "enabled", lambda: True)
    eg._record_authorized_ip(("8.8.8.8", 443))
    eg._assert_stream_peer_allowed(_FakeSock(("8.8.8.8", 443)), "sendall")
