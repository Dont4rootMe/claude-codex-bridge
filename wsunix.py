"""Минимальный WebSocket-клиент поверх unix-сокета (только стандартная библиотека).

Нужен, чтобы говорить с общим `codex app-server --listen unix://...`: сокет ждёт
HTTP Upgrade на /rpc и JSON-RPC в текстовых кадрах WebSocket.
"""
from __future__ import annotations

import base64
import os
import socket
import struct
import threading


class WSUnix:
    def __init__(self, sock_path: str, resource: str = "/rpc", timeout: float = 15):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(timeout)
        self.s.connect(sock_path)
        self._wlock = threading.Lock()
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {resource} HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.s.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("сокет закрыт во время рукопожатия")
            buf += chunk
        head, self._buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raise ConnectionError(f"нет WebSocket upgrade: {status!r}")
        self.s.settimeout(None)

    def _recvn(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.s.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("соединение закрыто")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    @staticmethod
    def _mask(data: bytes, mask: bytes) -> bytes:
        n = len(data)
        if n == 0:
            return data
        m = (mask * (n // 4 + 1))[:n]
        return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")

    def _send_frame(self, opcode: int, payload: bytes):
        hdr = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        with self._wlock:
            self.s.sendall(bytes(hdr) + self._mask(payload, mask))

    def send_text(self, text: str):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        parts = []
        while True:
            b1, b2 = self._recvn(2)
            fin, op, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recvn(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recvn(8))[0]
            mask = self._recvn(4) if b2 & 0x80 else None
            data = self._recvn(n)
            if mask:
                data = self._mask(data, mask)
            if op == 0x9:          # ping
                self._send_frame(0xA, data)
                continue
            if op == 0xA:          # pong
                continue
            if op == 0x8:          # close
                raise ConnectionError("сервер закрыл соединение")
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8")

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.s.close()
        except Exception:
            pass
