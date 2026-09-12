import json
import socket
import struct


HEADER_FMT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


def send_msg(sock: socket.socket, data: dict) -> None:
    payload = json.dumps(data).encode("utf-8")
    sock.sendall(struct.pack(HEADER_FMT, len(payload)) + payload)


def recv_msg(sock: socket.socket) -> dict:
    header = b""
    while len(header) < HEADER_SIZE:
        chunk = sock.recv(HEADER_SIZE - len(header))
        if not chunk:
            raise ConnectionError("connection closed")
        header += chunk
    length = struct.unpack(HEADER_FMT, header)[0]
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            raise ConnectionError("connection closed")
        payload += chunk
    return json.loads(payload)
