"""MQTT packet encoding, templating, and the never-fails send() boundary."""

from __future__ import annotations

import socket

import pytest

from rne import notify
from rne.config import MqttConfig

CONNACK_OK = b"\x20\x02\x00\x00"
CONNACK_BAD_AUTH = b"\x20\x02\x00\x04"
PUBACK_1 = b"\x40\x02\x00\x01"


def cfg(**overrides) -> MqttConfig:
    base = {"host": "broker.local", "topic": "rne/disc", "payload": "done"}
    return MqttConfig(**{**base, **overrides})


class FakeSocket:
    """Records what was written and replays canned broker replies."""

    def __init__(self, replies: bytes = b"") -> None:
        self.sent = bytearray()
        self.replies = bytearray(replies)
        self.closed = False
        self.timeout: float | None = None

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        self.sent += data

    def recv(self, count):
        chunk = bytes(self.replies[:count])
        del self.replies[:count]
        return chunk

    def close(self):
        self.closed = True


def split_packets(data: bytes) -> list[tuple[int, int, bytes]]:
    """Split a byte stream into (packet type, flags nibble, body) tuples."""
    packets = []
    i = 0
    while i < len(data):
        kind, flags = data[i] >> 4, data[i] & 0x0F
        i += 1
        length = 0
        multiplier = 1
        while True:
            byte = data[i]
            i += 1
            length += (byte & 0x7F) * multiplier
            if not byte & 0x80:
                break
            multiplier *= 128
        packets.append((kind, flags, data[i : i + length]))
        i += length
    return packets


def patch_socket(monkeypatch, sock, *, error: Exception | None = None) -> list:
    """Route notify's create_connection to `sock`; records the (addr, timeout)."""
    calls = []

    def fake_create_connection(address, timeout=None):
        calls.append((address, timeout))
        if error is not None:
            raise error
        return sock

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    return calls


# ---------------------------------------------------------------------------
# Remaining-length varint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "length,expected",
    [
        (0, b"\x00"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (16_383, b"\xff\x7f"),
        (16_384, b"\x80\x80\x01"),
        (268_435_455, b"\xff\xff\xff\x7f"),
    ],
)
def test_encode_remaining_length(length, expected):
    assert notify.encode_remaining_length(length) == expected


def test_encode_remaining_length_rejects_oversize():
    with pytest.raises(notify.NotifyError):
        notify.encode_remaining_length(268_435_456)


# ---------------------------------------------------------------------------
# CONNECT
# ---------------------------------------------------------------------------


def test_connect_packet_anonymous():
    (kind, flags, body), = split_packets(notify.connect_packet(client_id="rne-1"))
    assert (kind, flags) == (1, 0)
    assert body[:6] == b"\x00\x04MQTT"
    assert body[6] == 4  # protocol level 3.1.1
    assert body[7] == 0x02  # clean session only
    assert body[10:] == b"\x00\x05rne-1"


def test_connect_packet_with_username_and_password():
    (_, _, body), = split_packets(
        notify.connect_packet(client_id="c", username="ha", password="pw")
    )
    assert body[7] == 0xC2  # username + password + clean session
    assert body.endswith(b"\x00\x01c\x00\x02ha\x00\x02pw")


def test_connect_packet_username_only():
    (_, _, body), = split_packets(
        notify.connect_packet(client_id="c", username="ha")
    )
    assert body[7] == 0x82
    assert body.endswith(b"\x00\x01c\x00\x02ha")


def test_connect_packet_rejects_oversize_client_id():
    with pytest.raises(notify.NotifyError, match="too long"):
        notify.connect_packet(client_id="x" * 70_000)


# ---------------------------------------------------------------------------
# PUBLISH
# ---------------------------------------------------------------------------


def test_publish_packet_qos0_has_no_packet_id():
    (kind, flags, body), = split_packets(notify.publish_packet("a/b", b"hi"))
    assert (kind, flags) == (3, 0)
    assert body == b"\x00\x03a/b" + b"hi"


def test_publish_packet_qos1_carries_packet_id():
    (_, flags, body), = split_packets(
        notify.publish_packet("a/b", b"hi", qos=1, packet_id=7)
    )
    assert flags == 0x02
    assert body == b"\x00\x03a/b" + b"\x00\x07" + b"hi"


def test_publish_packet_retain_sets_low_bit():
    (_, flags, _), = split_packets(notify.publish_packet("a", b"", retain=True))
    assert flags == 0x01


def test_publish_packet_encodes_utf8_payload():
    (_, _, body), = split_packets(notify.publish_packet("a", "café".encode()))
    assert body == b"\x00\x01a" + "café".encode()


def test_disconnect_packet():
    assert notify.disconnect_packet() == b"\xe0\x00"


# ---------------------------------------------------------------------------
# publish() — wire conversation
# ---------------------------------------------------------------------------


def test_publish_qos0_sends_connect_publish_disconnect(monkeypatch):
    sock = FakeSocket(CONNACK_OK)
    calls = patch_socket(monkeypatch, sock)

    notify.publish(cfg(timeout=2.5), topic="rne/disc", payload="done")

    assert calls == [(("broker.local", 1883), 2.5)]
    kinds = [kind for kind, _, _ in split_packets(bytes(sock.sent))]
    assert kinds == [1, 3, 14]
    assert sock.closed


def test_publish_qos1_waits_for_puback(monkeypatch):
    sock = FakeSocket(CONNACK_OK + PUBACK_1)
    patch_socket(monkeypatch, sock)

    notify.publish(cfg(qos=1), topic="t", payload="p")

    assert not sock.replies  # both replies consumed
    assert [k for k, _, _ in split_packets(bytes(sock.sent))] == [1, 3, 14]


def test_publish_qos0_does_not_wait_for_puback(monkeypatch):
    # No PUBACK queued: a QoS 0 publish must not block reading for one.
    sock = FakeSocket(CONNACK_OK)
    patch_socket(monkeypatch, sock)

    notify.publish(cfg(qos=0), topic="t", payload="p")


def test_publish_uses_pid_client_id_by_default(monkeypatch):
    sock = FakeSocket(CONNACK_OK)
    patch_socket(monkeypatch, sock)
    monkeypatch.setattr(notify.os, "getpid", lambda: 4242)

    notify.publish(cfg(), topic="t", payload="p")

    connect_body = split_packets(bytes(sock.sent))[0][2]
    assert connect_body.endswith(b"rne-4242")


def test_publish_honours_configured_client_id(monkeypatch):
    sock = FakeSocket(CONNACK_OK)
    patch_socket(monkeypatch, sock)

    notify.publish(cfg(client_id="ripper"), topic="t", payload="p")

    assert split_packets(bytes(sock.sent))[0][2].endswith(b"ripper")


def test_publish_raises_on_rejected_credentials(monkeypatch):
    patch_socket(monkeypatch, FakeSocket(CONNACK_BAD_AUTH))
    with pytest.raises(notify.NotifyError, match="bad username or password"):
        notify.publish(cfg(), topic="t", payload="p")


def test_publish_raises_when_broker_hangs_up(monkeypatch):
    patch_socket(monkeypatch, FakeSocket(b""))
    with pytest.raises(notify.NotifyError, match="closed the connection"):
        notify.publish(cfg(), topic="t", payload="p")


def test_publish_raises_on_wrong_reply_packet(monkeypatch):
    patch_socket(monkeypatch, FakeSocket(PUBACK_1))
    with pytest.raises(notify.NotifyError, match="expected CONNACK"):
        notify.publish(cfg(), topic="t", payload="p")


def test_publish_raises_on_mismatched_puback(monkeypatch):
    patch_socket(monkeypatch, FakeSocket(CONNACK_OK + b"\x40\x02\x00\x09"))
    with pytest.raises(notify.NotifyError, match="different message"):
        notify.publish(cfg(qos=1), topic="t", payload="p")


def test_publish_raises_on_refused_connection(monkeypatch):
    patch_socket(monkeypatch, None, error=ConnectionRefusedError("refused"))
    with pytest.raises(notify.NotifyError, match="broker.local:1883"):
        notify.publish(cfg(), topic="t", payload="p")


def test_publish_reports_timeouts_with_the_budget(monkeypatch):
    patch_socket(monkeypatch, None, error=TimeoutError())
    with pytest.raises(notify.NotifyError, match="timed out after 5s"):
        notify.publish(cfg(), topic="t", payload="p")


def test_publish_closes_socket_on_failure(monkeypatch):
    sock = FakeSocket(CONNACK_BAD_AUTH)
    patch_socket(monkeypatch, sock)
    with pytest.raises(notify.NotifyError):
        notify.publish(cfg(), topic="t", payload="p")
    assert sock.closed


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------


def test_render_substitutes_known_placeholders():
    assert notify.render("{title} done", {"title": "Initial D"}) == "Initial D done"


def test_render_leaves_json_braces_alone():
    template = '{"event": "rip", "disc": "{disc}", "n": {count}}'
    assert notify.render(template, {"disc": "D1", "count": 7}) == (
        '{"event": "rip", "disc": "D1", "n": 7}'
    )


def test_render_passes_through_unknown_placeholders():
    assert notify.render("{title} {nope}", {"title": "A"}) == "A {nope}"


def test_render_renders_none_as_empty_string():
    assert notify.render("s{season}e", {"season": None}) == "se"


def test_render_repeats_a_placeholder():
    assert notify.render("{a}/{a}", {"a": "x"}) == "x/x"


def test_render_ignores_non_placeholder_braces():
    assert notify.render("{ spaced } {UPPER}", {"spaced": "x"}) == "{ spaced } {UPPER}"


# ---------------------------------------------------------------------------
# send() — never raises
# ---------------------------------------------------------------------------


def test_send_renders_topic_and_payload(monkeypatch):
    sock = FakeSocket(CONNACK_OK)
    patch_socket(monkeypatch, sock)

    result = notify.send(
        cfg(topic="rne/{hostname}/disc", payload="{title} done"),
        {"hostname": "ripper", "title": "Initial D"},
    )

    assert result.ok
    _, publish_body, _ = [body for _, _, body in split_packets(bytes(sock.sent))]
    assert publish_body == b"\x00\x0frne/ripper/disc" + b"Initial D done"


def test_send_supplies_hostname_by_default(monkeypatch):
    sock = FakeSocket(CONNACK_OK)
    patch_socket(monkeypatch, sock)
    monkeypatch.setattr(socket, "gethostname", lambda: "rip-box")

    assert notify.send(cfg(payload="{hostname}"), {}).ok
    assert split_packets(bytes(sock.sent))[1][2].endswith(b"rip-box")


def test_send_returns_failure_instead_of_raising(monkeypatch):
    patch_socket(monkeypatch, None, error=ConnectionRefusedError("nope"))

    result = notify.send(cfg(), {})

    assert not result.ok
    assert "broker.local:1883" in result.detail


def test_send_survives_an_unexpected_error(monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(notify, "publish", explode)

    result = notify.send(cfg(), {})

    assert not result.ok
    assert "RuntimeError" in result.detail


def test_send_detail_names_the_destination(monkeypatch):
    patch_socket(monkeypatch, FakeSocket(CONNACK_OK))
    result = notify.send(cfg(topic="rne/disc"), {})
    assert result.detail == "rne/disc -> broker.local:1883"
