"""传输编码解码与交付校验。

关键约束：解码的输入永远是**完整的线上原始片段缓存**（transfer-encoded
字节），断点只记录编码字节偏移；base64 四元组与 quoted-printable 转义
允许横跨 partial FETCH 分段，因此不做逐段解码，而是把已缓存的全部原始
字节重新整体解码。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import quopri
from dataclasses import dataclass
from typing import Optional


class UnknownEncodingError(ValueError):
    """BODYSTRUCTURE 声明了本采集器不支持的 Content-Transfer-Encoding。"""


@dataclass
class ManifestEntry:
    uidvalidity: int
    uid: int
    part: str
    decoded_length: int
    sha256: str

    @classmethod
    def from_json(cls, payload: dict) -> "ManifestEntry":
        return cls(
            uidvalidity=int(payload["uidvalidity"]),
            uid=int(payload["uid"]),
            part=str(payload["part"]),
            decoded_length=int(payload["decoded_length"]),
            sha256=str(payload["sha256"]).lower(),
        )

    def key(self) -> tuple[int, int, str]:
        return self.uidvalidity, self.uid, self.part


def load_manifest(payload: object) -> dict[tuple[int, int, str], ManifestEntry]:
    """兼容两种清单形态：裸列表，或带 ``delivery_manifest`` 键的对象。"""
    if isinstance(payload, dict) and "delivery_manifest" in payload:
        items = payload["delivery_manifest"]
    else:
        items = payload
    if not isinstance(items, list):
        raise ValueError("manifest must be a list of delivery entries")
    return {
        entry.key(): entry for entry in (ManifestEntry.from_json(item) for item in items)
    }


def decode_payload(encoded: bytes, cte: str) -> bytes:
    """对完整原始字节做一次整体解码。

    base64 不使用 validate 模式：标准允许分段处的 CRLF 与填充差异，整体
    解码才能正确处理横跨分段的四元组。未知编码直接抛错，由上层隔离。
    """
    encoding = cte.upper()
    if encoding in ("7BIT", "8BIT", "BINARY"):
        return encoded
    if encoding == "BASE64":
        try:
            return base64.b64decode(b"".join(encoded.split()), validate=False)
        except (binascii.Error, ValueError) as exc:
            raise UnknownEncodingError("invalid base64 payload: %s" % exc) from exc
    if encoding == "QUOTED-PRINTABLE":
        return quopri.decodestring(encoded)
    raise UnknownEncodingError("unsupported Content-Transfer-Encoding: %r" % cte)


@dataclass
class VerificationResult:
    ok: bool
    reason: Optional[str]
    decoded_length: int
    sha256: str


def verify(decoded: bytes, entry: Optional[ManifestEntry]) -> VerificationResult:
    """按供方清单核对解码后长度与 SHA256；清单缺项视为不可交付。"""
    digest = hashlib.sha256(decoded).hexdigest()
    if entry is None:
        return VerificationResult(False, "not-in-manifest", len(decoded), digest)
    if len(decoded) != entry.decoded_length:
        return VerificationResult(
            False,
            "length-mismatch: expected %d got %d" % (entry.decoded_length, len(decoded)),
            len(decoded),
            digest,
        )
    if digest != entry.sha256:
        return VerificationResult(False, "sha256-mismatch", len(decoded), digest)
    return VerificationResult(True, None, len(decoded), digest)
