"""Fire-and-forget MQTT notification.

A minimal MQTT 3.1.1 publisher: connect, publish one message, disconnect. That
is the whole protocol surface rne needs, and it is small enough to implement on
a plain socket rather than take a dependency for — no subscribe, no reconnect,
no session state, no MQTT 5.

QoS 0 and 1 are supported. QoS 2's four-packet handshake buys nothing here: a
duplicate "your disc is done" push is harmless, and the config layer rejects
`qos = 2` rather than silently downgrading it.

Everything below the `send()` boundary either succeeds or raises NotifyError.
`send()` itself never raises — notifying is a side quest, and a broker that is
down must not cost the user a disc rip.
"""

from __future__ import annotations

import os
import re
import socket
import ssl
import struct
from collections.abc import Mapping
from dataclasses import dataclass

from rne.config import MqttConfig

_PROTOCOL_NAME = b"\x00\x04MQTT"
_PROTOCOL_LEVEL = 4  # 3.1.1

# Control packet types (high nibble of the fixed header).
_CONNECT = 1
_CONNACK = 2
_PUBLISH = 3
_PUBACK = 4
_DISCONNECT = 14

_CLEAN_SESSION = 0x02
_USERNAME_FLAG = 0x80
_PASSWORD_FLAG = 0x40

_KEEPALIVE = 60  # seconds; the connection lives for well under one of these
_PACKET_ID = 1  # one publish per connection, so a fixed id is unambiguous

_CONNACK_ERRORS = {
    1: "broker rejected the protocol version",
    2: "broker rejected the client id",
    3: "broker unavailable",
    4: "bad username or password",
    5: "not authorised",
}


class NotifyError(Exception):
    """A notification could not be delivered."""


@dataclass(frozen=True)
class Result:
    """Outcome of a send() attempt, for the caller to report however it likes."""

    ok: bool
    detail: str


# ---------------------------------------------------------------------------
# Packet encoding (pure)
# ---------------------------------------------------------------------------


def encode_remaining_length(length: int) -> bytes:
    """Encode a fixed-header remaining-length as MQTT's 1-4 byte varint."""
    if length < 0 or length > 268_435_455:
        raise NotifyError(f"message is too large to encode ({length} bytes)")
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        out.append(byte)
        if not length:
            return bytes(out)


def _field(value: str | bytes) -> bytes:
    """Encode a length-prefixed MQTT field (UTF-8 string or binary blob)."""
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if len(raw) > 0xFFFF:
        raise NotifyError(f"field is too long ({len(raw)} bytes, max 65535)")
    return struct.pack("!H", len(raw)) + raw


def _packet(kind: int, flags: int, body: bytes) -> bytes:
    return bytes([(kind << 4) | flags]) + encode_remaining_length(len(body)) + body


def connect_packet(
    *,
    client_id: str,
    username: str | None = None,
    password: str | None = None,
    keepalive: int = _KEEPALIVE,
) -> bytes:
    flags = _CLEAN_SESSION
    if username is not None:
        flags |= _USERNAME_FLAG
        if password is not None:
            # 3.1.1 only allows the password flag alongside the username flag,
            # which the config layer also enforces.
            flags |= _PASSWORD_FLAG

    body = _PROTOCOL_NAME + bytes([_PROTOCOL_LEVEL, flags])
    body += struct.pack("!H", keepalive)
    body += _field(client_id)
    if username is not None:
        body += _field(username)
        if password is not None:
            body += _field(password)
    return _packet(_CONNECT, 0, body)


def publish_packet(
    topic: str,
    payload: bytes,
    *,
    qos: int = 0,
    retain: bool = False,
    packet_id: int = _PACKET_ID,
) -> bytes:
    flags = (qos << 1) | (1 if retain else 0)
    body = _field(topic)
    if qos:
        body += struct.pack("!H", packet_id)
    return _packet(_PUBLISH, flags, body + payload)


def disconnect_packet() -> bytes:
    return _packet(_DISCONNECT, 0, b"")


# ---------------------------------------------------------------------------
# Wire I/O
# ---------------------------------------------------------------------------


def _read_exact(sock, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise NotifyError("broker closed the connection")
        buf += chunk
    return bytes(buf)


def _read_packet(sock) -> tuple[int, bytes]:
    """Read one control packet. Returns (packet type, body)."""
    header = _read_exact(sock, 1)[0]
    length = 0
    multiplier = 1
    for _ in range(4):
        byte = _read_exact(sock, 1)[0]
        length += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            break
        multiplier *= 128
    else:
        raise NotifyError("malformed reply from broker (bad remaining length)")
    return header >> 4, (_read_exact(sock, length) if length else b"")


def _expect_connack(sock) -> None:
    kind, body = _read_packet(sock)
    if kind != _CONNACK:
        raise NotifyError(f"expected CONNACK from broker, got packet type {kind}")
    if len(body) < 2:
        raise NotifyError("truncated CONNACK from broker")
    code = body[1]
    if code:
        reason = _CONNACK_ERRORS.get(code, f"connection refused (code {code})")
        raise NotifyError(reason)


def _expect_puback(sock, packet_id: int) -> None:
    kind, body = _read_packet(sock)
    if kind != _PUBACK:
        raise NotifyError(f"expected PUBACK from broker, got packet type {kind}")
    if len(body) < 2 or struct.unpack("!H", body[:2])[0] != packet_id:
        raise NotifyError("broker acknowledged a different message")


def _wrap_tls(sock, cfg: MqttConfig):
    context = ssl.create_default_context()
    if cfg.tls_insecure:
        # check_hostname must go first: turning off verification while it is
        # still on raises ValueError.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context.wrap_socket(sock, server_hostname=cfg.host)


def publish(cfg: MqttConfig, *, topic: str, payload: str) -> None:
    """Publish one message and disconnect. Raises NotifyError on any failure.

    Every network operation is bounded by cfg.timeout, so the worst case is a
    few seconds rather than a hung CLI.
    """
    data = payload.encode("utf-8")
    client_id = cfg.client_id or f"rne-{os.getpid()}"
    sock = None
    try:
        sock = socket.create_connection((cfg.host, cfg.port), timeout=cfg.timeout)
        if cfg.tls:
            sock = _wrap_tls(sock, cfg)
        sock.settimeout(cfg.timeout)

        sock.sendall(
            connect_packet(
                client_id=client_id,
                username=cfg.username,
                password=cfg.password,
            )
        )
        _expect_connack(sock)

        sock.sendall(
            publish_packet(topic, data, qos=cfg.qos, retain=cfg.retain)
        )
        if cfg.qos:
            _expect_puback(sock, _PACKET_ID)

        try:
            sock.sendall(disconnect_packet())
        except OSError:
            pass  # the message is already accepted; a rude close is harmless
    except TimeoutError as exc:
        raise NotifyError(
            f"timed out after {cfg.timeout:g}s talking to {cfg.host}:{cfg.port}"
        ) from exc
    except OSError as exc:  # includes socket.gaierror and ssl.SSLError
        raise NotifyError(f"{cfg.host}:{cfg.port}: {exc}") from exc
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Templating and the never-fails entry point
# ---------------------------------------------------------------------------

# Deliberately not str.format(): payloads are very often JSON, and format()
# would choke on the literal braces in {"disc": "..."} unless the user doubled
# every one of them. This matches {placeholder} and nothing else.
_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def render(template: str, values: Mapping[str, object]) -> str:
    """Substitute {placeholder} tokens, leaving unknown ones as literal text.

    An unrecognised placeholder is passed through rather than raising: a typo in
    the payload should produce a slightly odd notification, not no notification.
    """

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        value = values[key]
        return "" if value is None else str(value)

    return _PLACEHOLDER_RE.sub(replace, template)


def send(cfg: MqttConfig, values: Mapping[str, object]) -> Result:
    """Render topic and payload, publish, and report what happened.

    Never raises. `values` supplies the {placeholder} substitutions; `hostname`
    is provided automatically and can be overridden by the caller.
    """
    try:
        merged: dict[str, object] = {"hostname": socket.gethostname(), **values}
        topic = render(cfg.topic, merged)
        payload = render(cfg.payload, merged)
        publish(cfg, topic=topic, payload=payload)
    except NotifyError as exc:
        return Result(False, str(exc))
    except Exception as exc:  # belt and braces: notifying must never break a rip
        return Result(False, f"{type(exc).__name__}: {exc}")
    return Result(True, f"{topic} -> {cfg.host}:{cfg.port}")
