"""最小 IMAP4rev1 客户端（仅标准库，自管套接字）。

只实现采集器所需协议子集，设计上的硬约束：

* 所有正文读取一律使用 ``BODY.PEEK``，客户端从不发送 STORE/APPEND 等会
  改动 ``\\Seen`` 标记的命令；
* FETCH 字面量按线上 transfer-encoded 字节精确计数。字面量传输中途连接
  断开时抛出 :class:`LiteralInterrupted`，已到达的原始字节随异常交回，
  因此恢复点是“编码字节偏移”，而不是解码后的字符位置；
* 调用方始终以 UID 寻址，响应里回显的 UID 必须与请求 UID 一致，否则按
  协议错误处理——绝不把隐式序号（sequence number）当作 UID。
"""
from __future__ import annotations

import socket
from typing import Any, List, Optional, Union

CRLF = b"\r\n"


def _int_of_response(token: Any) -> int:
    """解析响应码里的数字 token，剥离可能粘连的右方括号（如 ``b'101]'``）。"""
    if isinstance(token, int):
        return token
    if isinstance(token, bytes):
        return int(token.rstrip(b"]"))
    raise ImapError("numeric response token expected, got %r" % (token,))

# 标记类型：bytes=原子/引号串/字面量，int=数字，None=NIL，"("/")"=列表边界
Token = Union[bytes, int, None, str]


class ImapError(RuntimeError):
    """服务器返回 NO/BAD，或响应结构无法解释。"""


class LiteralInterrupted(ConnectionError):
    """字面量在传输中途连接断开。

    ``data`` 是本次请求已经到达的原始（仍处于传输编码状态的）字节，
    ``expected`` 是该字面量承诺的总长度。恢复时应从
    ``原偏移 + len(data)`` 这个编码字节偏移重新发起 partial FETCH。
    """

    def __init__(self, data: bytes, expected: int):
        super().__init__(
            "literal interrupted: received %d of %d encoded bytes"
            % (len(data), expected)
        )
        self.data = data
        self.expected = expected


def nest(tokens: List[Token]) -> List[Any]:
    """把含 ``(`` / ``)`` 边界标记的扁平标记流还原为嵌套列表。"""
    position = 0

    def parse_list() -> List[Any]:
        nonlocal position
        result: List[Any] = []
        while position < len(tokens):
            token = tokens[position]
            if token == "(":
                position += 1
                result.append(parse_list())
            elif token == ")":
                position += 1
                return result
            else:
                result.append(token)
                position += 1
        return result

    nested = parse_list()
    if position != len(tokens):
        raise ImapError("unbalanced parentheses in FETCH response")
    return nested


class ImapConnection:
    """一条 IMAP 连接。线程模型为单线程串行请求，不做命令流水线。"""

    def __init__(self, host: str, port: int, timeout: float = 30.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buffer = bytearray()
        self._tag_sequence = 0

    # ---- 连接生命周期 -------------------------------------------------
    def connect(self) -> None:
        self._sock = socket.create_connection(
            (self.host, self.port), timeout=self.timeout
        )
        greeting = self._read_line()
        if not greeting.startswith(b"* OK"):
            raise ImapError("unexpected greeting: %r" % greeting)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        self._buffer.clear()

    def __enter__(self) -> "ImapConnection":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def logout(self) -> None:
        try:
            self._command(["LOGOUT"])
        except (ConnectionError, OSError, ImapError):
            pass
        finally:
            self.close()

    # ---- 底层字节读取 -------------------------------------------------
    def _recv(self, size: int) -> bytes:
        assert self._sock is not None
        try:
            return self._sock.recv(size)
        except socket.timeout:
            raise ConnectionError("socket timed out") from None

    def _recv_exact(self, size: int) -> bytes:
        """读取恰好 size 个字节，优先消化行读取时预取到 ``_buffer`` 的字节。

        服务器可能把响应头行与字面量数据放在同一个 TCP 段里；那些字节
        已经在 ``_buffer`` 中，必须先取走，否则既丢失数据又会在已关闭的
        套接字上空等。
        """
        pieces: List[bytes] = []
        remaining = size
        if self._buffer:
            take = bytes(self._buffer[:remaining])
            del self._buffer[: len(take)]
            pieces.append(take)
            remaining -= len(take)
        while remaining > 0:
            chunk = self._recv(min(65536, remaining))
            if not chunk:
                break
            pieces.append(chunk)
            remaining -= len(chunk)
        return b"".join(pieces)

    def _read_line(self) -> bytes:
        """读取一整行（不含 CRLF）；对端关闭则抛 ConnectionError。"""
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[:newline]).rstrip(b"\r")
                del self._buffer[: newline + 1]
                return line
            chunk = self._recv(4096)
            if not chunk:
                raise ConnectionError("connection closed while reading line")
            self._buffer.extend(chunk)

    def read_group(self) -> List[Token]:
        """读取一个逻辑响应组（一条 untagged 响应或一条带标签完结响应）。

        ``{n}`` 字面量后的 n 个字节作为一个 bytes 标记原样保留；若连接在
        字面量中途断开，抛 :class:`LiteralInterrupted`，已收字节不丢失。
        """
        tokens: List[Token] = []
        depth = 0
        while True:
            line = self._read_line()
            index = 0
            length = len(line)
            while index < length:
                char = line[index]
                if char in (0x20, 0x09):  # 空格 / TAB
                    index += 1
                elif char == 0x28:  # (
                    depth += 1
                    tokens.append("(")
                    index += 1
                elif char == 0x29:  # )
                    depth -= 1
                    tokens.append(")")
                    index += 1
                elif char == 0x22:  # 引号串
                    index, value = self._read_quoted(line, index)
                    tokens.append(value)
                elif char == 0x7B:  # 字面量 {n}
                    end = line.find(b"}", index + 1)
                    if end < 0:
                        raise ImapError("malformed literal: %r" % line)
                    literal_size = int(line[index + 1 : end])
                    index = end + 1
                    data = self._recv_exact(literal_size)
                    if len(data) < literal_size:
                        raise LiteralInterrupted(data, literal_size)
                    tokens.append(data)
                else:  # 原子 / 数字 / NIL / 方括号响应码
                    start = index
                    while index < length and line[index] not in b" \t()":
                        index += 1
                    atom = line[start:index]
                    if atom.isdigit():
                        tokens.append(int(atom))
                    elif atom.upper() == b"NIL":
                        tokens.append(None)
                    else:
                        tokens.append(atom)
            if depth <= 0:
                return tokens

    @staticmethod
    def _read_quoted(line: bytes, start: int) -> tuple[int, bytes]:
        index = start + 1
        output = bytearray()
        while index < len(line):
            if line[index] == 0x5C and index + 1 < len(line):  # 反斜杠转义
                output.append(line[index + 1])
                index += 2
            elif line[index] == 0x22:
                return index + 1, bytes(output)
            else:
                output.append(line[index])
                index += 1
        raise ImapError("unterminated quoted string: %r" % line)

    # ---- 命令与响应 ---------------------------------------------------
    def _new_tag(self) -> bytes:
        self._tag_sequence += 1
        return b"A%04d" % self._tag_sequence

    def _send(self, data: bytes) -> None:
        assert self._sock is not None
        self._sock.sendall(data + CRLF)

    @staticmethod
    def quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _command(self, parts: List[str]) -> List[List[Any]]:
        tag = self._new_tag()
        self._send(tag + b" " + " ".join(parts).encode("utf-8"))
        untagged: List[List[Any]] = []
        while True:
            group = nest(self.read_group())
            if isinstance(group[0], bytes) and group[0] == tag:
                status = group[1] if len(group) > 1 else None
                if status != b"OK":
                    raise ImapError("server replied %r: %r" % (status, group))
                return untagged
            untagged.append(group)

    # ---- 采集器使用的协议操作 -----------------------------------------
    def login(self, username: str, password: str) -> None:
        self._command(["LOGIN", self.quote(username), self.quote(password)])

    def examine(self, mailbox: str) -> dict:
        """以只读方式打开 mailbox（EXAMINE 不会改动邮件标记）。"""
        untagged = self._command(["EXAMINE", self.quote(mailbox)])
        info = {"uidvalidity": None, "uidnext": None, "exists": None}
        for group in untagged:
            if len(group) >= 3 and group[0] == b"*" and group[1] == b"EXISTS":
                info["exists"] = int(group[2])  # type: ignore[arg-type]
            for index, token in enumerate(group):
                if isinstance(token, bytes) and token.upper().startswith(b"[UIDVALIDITY"):
                    info["uidvalidity"] = _int_of_response(group[index + 1])
                if isinstance(token, bytes) and token.upper().startswith(b"[UIDNEXT"):
                    info["uidnext"] = _int_of_response(group[index + 1])
        return info

    def uid_search_range(self, low: int, high: int) -> List[int]:
        """UID SEARCH UID low:high，返回升序 UID 列表（缺号即不在列表中）。"""
        if low > high:
            return []
        untagged = self._command(["UID", "SEARCH", "UID", "%d:%d" % (low, high)])
        uids: List[int] = []
        for group in untagged:
            if len(group) >= 2 and group[0] == b"*" and group[1] == b"SEARCH":
                uids.extend(token for token in group[2:] if isinstance(token, int))
        return sorted(uids)

    def uid_fetch_bodystructure(self, uid: int) -> Optional[List[Any]]:
        """取 UID 对应邮件的 BODYSTRUCTURE；邮件已消失则返回 None。"""
        untagged = self._command(
            ["UID", "FETCH", str(uid), "(BODYSTRUCTURE)"]
        )
        return extract_fetch(untagged, uid, b"BODYSTRUCTURE")

    def uid_fetch_part(
        self, uid: int, part_path: str, offset: int, count: int
    ) -> Optional[bytes]:
        """``UID FETCH uid (BODY.PEEK[part]<offset.count>)``。

        偏移与长度都是线上传输编码字节。成功返回该段原始字节；邮件已消失
        返回 None；字面量中途断连抛 :class:`LiteralInterrupted`。
        """
        section = "BODY.PEEK[%s]<%d.%d>" % (part_path, offset, count)
        untagged = self._command(
            ["UID", "FETCH", str(uid), "(" + section + ")"]
        )
        return extract_fetch(untagged, uid, b"BODY[")


def extract_fetch(
    untagged: List[List[Any]], uid: int, attribute: bytes
) -> Optional[Any]:
    """从 FETCH 的 untagged 响应中取属性值，并校验 UID 回显。

    ``attribute`` 为 ``b"BODY["`` 时按前缀匹配（PEEK 回显通常去掉 PEEK）。
    邮件不存在（没有任何 FETCH 响应）时返回 None。UID 回显不一致按协议
    错误处理，防止把同序号的另一封邮件当替身。
    """
    for group in untagged:
        if not (len(group) >= 4 and group[0] == b"*" and group[2] == b"FETCH"):
            continue
        attributes = group[3]
        if not isinstance(attributes, list):
            raise ImapError("malformed FETCH attributes: %r" % (group,))
        echoed_uid = None
        for index, token in enumerate(attributes):
            if token == b"UID" and index + 1 < len(attributes):
                echoed_uid = attributes[index + 1]
        if echoed_uid != uid:
            raise ImapError(
                "UID echo mismatch: requested %d got %r" % (uid, echoed_uid)
            )
        for index, token in enumerate(attributes):
            if isinstance(token, bytes) and (
                token == attribute or token.startswith(attribute)
            ):
                return attributes[index + 1]
    return None
