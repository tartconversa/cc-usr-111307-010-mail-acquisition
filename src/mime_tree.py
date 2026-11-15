"""把 IMAP BODYSTRUCTURE 解析成带 part 路径的 MIME 树。

part 路径按 RFC 3501 编号：多部件子节点 1..N 依次编号，嵌套多部件用
点号拼接；``message/rfc822`` 叶子的内嵌 MIME 结构以该节点路径为前缀
继续编号（如 ``4.1.2``）。路径字符串即持久身份的一部分，因此同名附件
也能靠路径区分，绝不互相覆盖。

文件名参数同时支持 RFC 2047 编码字（``=?UTF-8?B?...?=``）和 RFC 2231
延续/百分号编码（``filename*0*``、``filename*=utf-8''...``）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from email import policy
from email.parser import HeaderParser
from typing import Any, List, Optional
from urllib.parse import unquote_to_bytes

import re

_ENCODED_WORD = re.compile(rb"=\?[^?]*\?[bBqQ]\?[^?]*\?=")


class MimeStructureError(ValueError):
    """BODYSTRUCTURE 不完整或字段布局无法识别。"""


@dataclass
class MimePart:
    path: str                      # IMAP part 路径，根多部件的叶子形如 "2"、"1.3"
    maintype: str                 # 大写，如 APPLICATION
    subtype: str                  # 大写，如 OCTET-STREAM
    cte: str                      # 大写 Content-Transfer-Encoding；未知编码保留原文
    encoded_size: Optional[int]   # 线上 transfer-encoded 字节数（BODYSTRUCTURE 给出）
    filename: Optional[str]       # 解码后的文件名，仅作显示用途
    disposition: Optional[str]    # 大写 disposition，如 ATTACHMENT / INLINE
    children: List["MimePart"] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        return self.maintype + "/" + self.subtype

    @property
    def is_attachment(self) -> bool:
        # 只有显式 Content-Disposition: attachment 才领取；inline 正文不领取。
        return self.disposition == "ATTACHMENT"

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "content_type": self.content_type,
            "content_transfer_encoding": self.cte,
            "encoded_size": self.encoded_size,
            "filename": self.filename,
            "disposition": self.disposition,
            "children": [child.to_json() for child in self.children],
        }


def decode_filename(raw: bytes) -> str:
    """解码非 ASCII 文件名参数：RFC 2231 优先，RFC 2047 编码字兜底。"""
    if _ENCODED_WORD.fullmatch(raw.strip()):
        parser = HeaderParser(policy=policy.default)
        message = parser.parsestr("X-Name: " + raw.decode("ascii", "replace"))
        return str(message["X-Name"])
    # RFC 2231：可能带 charset 段，如 utf-8''%E7%9B%AE；也可能是裸百分号编码。
    # 无引号延续参数（filename*0*）只有一个分隔单引号，裸百分号参数没有单引号。
    if b"'" in raw:
        _, _, rest = raw.partition(b"'")
        if b"'" in rest:  # charset'language'value 形态，再剥一次
            _, _, rest = rest.partition(b"'")
    else:
        rest = raw
    return unquote_to_bytes(rest).decode("utf-8", "replace")


def _parse_parameters(tokens: Any) -> dict:
    """把 (NAME value NAME value ...) 参数列表解析成 dict（键统一小写）。"""
    if tokens is None:
        return {}
    if not isinstance(tokens, list):
        raise MimeStructureError("parameter list expected, got %r" % (tokens,))
    result = {}
    index = 0
    while index + 1 < len(tokens):
        key = tokens[index]
        value = tokens[index + 1]
        if isinstance(key, bytes):
            result[key.decode("ascii", "replace").lower()] = (
                value if isinstance(value, bytes) else b""
            )
        index += 2
    if index != len(tokens):
        raise MimeStructureError("odd number of parameter tokens: %r" % (tokens,))
    return result


def _join_rfc2231(parameters: dict, name: str) -> Optional[bytes]:
    """合并 RFC 2231 延续参数（``name*0*``、``name*1*`` 与 ``name*``）。"""
    star = parameters.get(name + "*")
    if star is not None:
        return star
    chunks: list[tuple[int, bytes]] = []
    for key, value in parameters.items():
        match = re.fullmatch(re.escape(name) + r"\*(\d+)\*?", key)
        if match:
            chunks.append((int(match.group(1)), value))
    if not chunks:
        return None
    return b"".join(value for _, value in sorted(chunks))


def _filename_from(parameters: dict, disposition: Any) -> Optional[str]:
    disposition_params = _parse_parameters(disposition[1]) if disposition else {}
    for table, name in ((disposition_params, "filename"), (parameters, "name")):
        joined = _join_rfc2231(table, name)
        if joined is not None:
            return decode_filename(joined)
        plain = table.get(name)
        if plain is not None:
            return decode_filename(plain)
    return None


def _disposition_of(tokens: Any) -> tuple[Optional[str], Any]:
    """解析 disposition 字段：``(ATTACHMENT (FILENAME ...))`` 或 NIL。"""
    if tokens is None:
        return None, None
    if not isinstance(tokens, list) or not tokens or not isinstance(tokens[0], bytes):
        raise MimeStructureError("malformed disposition: %r" % (tokens,))
    return tokens[0].decode("ascii", "replace").upper(), tokens


def parse_bodystructure(tokens: Any) -> List[MimePart]:
    """解析完整 BODYSTRUCTURE，返回树的顶层节点列表（通常只有一个根）。"""
    if not isinstance(tokens, list) or not tokens:
        raise MimeStructureError("empty BODYSTRUCTURE")
    return [_parse(tokens, "")]


def _is_message_rfc822_leaf(node: Any) -> bool:
    """基本部件叶子的判据：前两个 token 是 content-type 的 bytes。

    普通叶子与 ``message/rfc822`` 叶子（内部还嵌一个 BODYSTRUCTURE 列表）
    都满足；多部件首元素是子节点列表，不满足。
    """
    return (
        isinstance(node, list)
        and len(node) >= 2
        and isinstance(node[0], bytes)
        and isinstance(node[1], bytes)
    )


def _is_multipart(node: Any) -> bool:
    """区分多部件节点与基本部件（含 message/rfc822 叶子）。"""
    if not (isinstance(node, list) and node):
        return False
    return not _is_message_rfc822_leaf(node)


def _parse(node: Any, prefix: str) -> MimePart:
    if _is_multipart(node):
        return _parse_multipart(node, prefix)
    return _parse_leaf(node, prefix)


def _parse_multipart(node: list, prefix: str) -> MimePart:
    index = 0
    children: List[MimePart] = []
    part_index = 0
    # 多部件布局：子结构列表（叶子或嵌套多部件都是列表）... 之后是 subtype
    # bytes；只按“下一个 token 仍是列表”判定子节点。
    while index < len(node) and isinstance(node[index], list):
        part_index += 1
        child_prefix = str(part_index) if not prefix else prefix + "." + str(part_index)
        children.append(_parse(node[index], child_prefix))
        index += 1
    if index >= len(node) or not isinstance(node[index], bytes):
        raise MimeStructureError("multipart subtype missing in %r" % (node,))
    subtype = node[index].decode("ascii", "replace").upper()
    parent = MimePart(
        path=prefix,  # 根多部件前缀为空串，容器本身没有可 FETCH 的 part 号
        maintype="MULTIPART",
        subtype=subtype,
        cte="",
        encoded_size=None,
        filename=None,
        disposition=None,
        children=children,
    )
    return parent


def _parse_leaf(node: list, prefix: str) -> MimePart:
    if len(node) < 7 or not all(isinstance(node[i], bytes) for i in (0, 1)):
        raise MimeStructureError("basic body part header missing: %r" % (node,))
    maintype = node[0].decode("ascii", "replace").upper()
    subtype = node[1].decode("ascii", "replace").upper()
    parameters = _parse_parameters(node[2])
    cte = node[5].decode("ascii", "replace").upper() if isinstance(node[5], bytes) else ""
    encoded_size = node[6] if isinstance(node[6], int) else None

    # 叶子固定字段：基础 7 个；text 多 1 行数字；message/rfc822 多 3 个字段
    # （envelope、内嵌 BODYSTRUCTURE、行数）。扩展字段从它们之后开始。
    cursor = 7
    if maintype == "TEXT":
        cursor += 1
    embedded_children: List[MimePart] = []
    if maintype == "MESSAGE" and subtype.upper() == "RFC822":
        if len(node) < cursor + 3:
            raise MimeStructureError("incomplete message/rfc822 part: %r" % (node,))
        embedded_structure = node[cursor + 1]
        line_count = node[cursor + 2]
        if not isinstance(line_count, int):
            raise MimeStructureError("rfc822 line count missing: %r" % (node,))
        if _is_multipart(embedded_structure) or isinstance(embedded_structure, list):
            embedded_children.append(_parse(embedded_structure, prefix or "1"))
        cursor += 3

    extension = node[cursor:]
    disposition = None
    disposition_token = None
    # 扩展字段布局：[MD5 [disposition [language [location ...]]]]
    if len(extension) >= 2:
        disposition, disposition_token = _disposition_of(extension[1])
    filename = _filename_from(parameters, disposition_token)

    part = MimePart(
        path=prefix or "1",
        maintype=maintype,
        subtype=subtype,
        cte=cte,
        encoded_size=encoded_size,
        filename=filename,
        disposition=disposition,
        children=embedded_children,
    )
    return part


def walk(parts: List[MimePart]) -> List[MimePart]:
    """深度优先展开全部叶子与容器节点。"""
    output: List[MimePart] = []

    def visit(part: MimePart) -> None:
        output.append(part)
        for child in part.children:
            visit(child)

    for part in parts:
        visit(part)
    return output


def attachment_parts(
    parts: List[MimePart], allowed_types: set[str]
) -> List[MimePart]:
    """挑选允许类型的 attachment 叶子；inline 正文与容器节点一律不领取。"""
    selected: List[MimePart] = []
    for part in walk(parts):
        if part.children:
            continue
        if not part.is_attachment:
            continue
        if part.content_type not in allowed_types:
            continue
        selected.append(part)
    return selected


_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._一-鿿㐀-䶿]+")


def safe_filename(filename: Optional[str], fallback: str) -> str:
    """把显示用文件名处理成单段安全路径：去目录成分、去控制字符、限长。

    文件名只用于展示与落地文件名，持久身份始终以 part 路径为准。
    """
    if not filename:
        return fallback
    # 只取最后一段，杜绝目录穿越；同时过滤掉 Windows 盘符与设备名前缀。
    segment = filename.replace("\\", "/").rsplit("/", 1)[-1]
    segment = segment.replace("\x00", "")
    segment = _SAFE_SEGMENT.sub("_", segment).strip("._ ")
    if not segment:
        segment = fallback
    return segment[:120]
