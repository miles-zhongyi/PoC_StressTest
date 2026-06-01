"""
Wire protocol shared by UE, RU and DU.

Every message is a JSON object framed by a 4-byte big-endian length prefix:

    [ 4 bytes length N ][ N bytes UTF-8 JSON ]

This is a deliberately tiny stand-in for the real 5G control plane. The message
*names* mirror the RRC / F1AP procedures they emulate so the flow is recognisable
to anyone who knows the stack, but the payloads are simplified.

Signalling path (proxy chain):   UE  <-->  RU  <-->  DU

Every UE->DU message gets exactly one DU->UE reply, so the RU can act as a simple
synchronous transparent proxy (recv from UE, augment, forward to DU, recv reply,
forward to UE).
"""
import json
import struct

# ---- message types -------------------------------------------------------

# UE -> DU (carried through the RU)
RRC_SETUP_REQUEST = "RRC_SETUP_REQUEST"   # UE wants to attach
MEASUREMENT_REPORT = "MEASUREMENT_REPORT"  # UE moved / RF changed
DATA = "DATA"                              # UE sends user-plane traffic
RRC_RELEASE = "RRC_RELEASE"                # UE leaving

# DU -> UE (carried back through the RU)
RRC_SETUP = "RRC_SETUP"                    # attach accepted, PRBs granted
RRC_REJECT = "RRC_REJECT"                  # attach refused (no resources / no coverage)
RRC_RECONFIG = "RRC_RECONFIG"             # PRB grant updated after mobility
DATA_ACK = "DATA_ACK"                      # ack with achievable throughput
RRC_RELEASE_COMPLETE = "RRC_RELEASE_COMPLETE"

_HEADER = struct.Struct("!I")  # 4-byte unsigned big-endian length


def send_msg(sock, obj):
    """Serialize and send one framed JSON message."""
    payload = json.dumps(obj).encode("utf-8")
    sock.sendall(_HEADER.pack(len(payload)) + payload)


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed connection")
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(sock):
    """Receive exactly one framed JSON message (blocking)."""
    (length,) = _HEADER.unpack(_recv_exact(sock, _HEADER.size))
    return json.loads(_recv_exact(sock, length).decode("utf-8"))


# ---- asyncio variants (used by the scalable DU/RU/UE-sim) ----------------

async def async_send_msg(writer, obj):
    """Send one framed JSON message on an asyncio StreamWriter."""
    payload = json.dumps(obj).encode("utf-8")
    writer.write(_HEADER.pack(len(payload)) + payload)
    await writer.drain()


async def async_recv_msg(reader):
    """Receive one framed JSON message from an asyncio StreamReader.

    Raises asyncio.IncompleteReadError on EOF (callers treat that as a close).
    """
    header = await reader.readexactly(_HEADER.size)
    (length,) = _HEADER.unpack(header)
    payload = await reader.readexactly(length)
    return json.loads(payload.decode("utf-8"))
