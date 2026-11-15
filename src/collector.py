"""IMAP 授权数据附件采集器。

持久身份四元组：账号逻辑名 / mailbox / UIDVALIDITY / UID，再加 part 路径
定位附件。序号（sequence number）从不入库、从不参与寻址。

一轮采集的时序保证：

1. EXAMINE 只读打开 mailbox，记录 UIDVALIDITY；UIDVALIDITY 变化即开启
   独立纪元，旧纪元的进度绝不套用；
2. 轮次开始时把当时的 UIDNEXT 固定为上界 bound，只 SEARCH/处理
   ``1:bound-1`` 的 UID；轮次中途到达的新信 UID >= bound，留给下一轮；
3. UID 缺号是正常现象——SEARCH 返回什么就处理什么，不按序号补位；
4. 每封邮件先 FETCH BODYSTRUCTURE 再取部件；若检索与 FETCH 之间邮件被
   删，记录 vanished，绝不把随后同序号的邮件当替身（FETCH 回显 UID 必须
   与请求 UID 一致）；
5. 所有正文读取走 BODY.PEEK，不发送任何会设置 \\Seen 的命令，也不依赖
   收件时间排序——严格按 UID 数值升序处理。
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .imap_client import ImapConnection, ImapError, LiteralInterrupted
from .mime_tree import (
    MimePart,
    MimeStructureError,
    attachment_parts,
    parse_bodystructure,
    safe_filename,
)
from .transfer import (
    ManifestEntry,
    UnknownEncodingError,
    decode_payload,
    load_manifest,
    verify,
)

EventCallback = Callable[[str, dict], None]


class CollectorError(RuntimeError):
    pass


class EpochChangedError(CollectorError):
    """重连续传时发现 UIDVALIDITY 已变化，本轮必须中止并另立纪元。"""


class VanishedError(CollectorError):
    """邮件在检索之后、FETCH 完成之前被删除。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_write_json(path: Path, payload: object) -> None:
    _atomic_write_bytes(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


@dataclass
class RoundSummary:
    account: str
    mailbox: str
    uidvalidity: int
    bound: int
    uids: list[int] = field(default_factory=list)
    delivered: list[dict] = field(default_factory=list)
    vanished: list[int] = field(default_factory=list)
    quarantined: list[dict] = field(default_factory=list)
    skipped_messages: list[int] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "account": self.account,
            "mailbox": self.mailbox,
            "uidvalidity": self.uidvalidity,
            "uidnext_bound": self.bound,
            "uids": self.uids,
            "delivered": self.delivered,
            "vanished": self.vanished,
            "quarantined": self.quarantined,
            "skipped_messages": self.skipped_messages,
        }


class Collector:
    def __init__(
        self,
        host: str,
        port: int,
        account: str,
        mailbox: str,
        workdir: Path,
        manifest: object,
        username: Optional[str] = None,
        password: Optional[str] = None,
        chunk_size: int = 65536,
        allowed_types: Optional[set[str]] = None,
        timeout: float = 30.0,
        max_retries: int = 5,
        on_event: Optional[EventCallback] = None,
    ):
        self.host = host
        self.port = port
        self.account = safe_filename(account, "account")
        self.mailbox_name = mailbox
        self.mailbox_slug = safe_filename(mailbox, "mailbox")
        self.username = username if username is not None else account
        self.password = password if password is not None else ""
        self.chunk_size = chunk_size
        self.max_retries = max_retries
        self.allowed_types = allowed_types or {"APPLICATION/OCTET-STREAM", "TEXT/CSV"}
        self.timeout = timeout
        self.on_event = on_event or (lambda event, info: None)
        self.workdir = Path(workdir)
        if isinstance(manifest, (str, Path)):
            manifest = json.loads(Path(manifest).read_text(encoding="utf-8"))
        self.manifest = load_manifest(manifest)
        self.state_path = self.workdir / "state.json"
        self.state = self._load_state()
        # 当前轮次的活动连接；分段中断重连后整体替换，所有方法共用。
        self.conn: Optional[ImapConnection] = None

    # ---- 持久化 -------------------------------------------------------
    def _load_state(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"version": 1, "accounts": {}}

    def _save_state(self) -> None:
        _atomic_write_json(self.state_path, self.state)

    def _epoch_state(self, uidvalidity: int) -> dict:
        account_node = self.state["accounts"].setdefault(self.account, {"mailboxes": {}})
        mailbox_node = account_node["mailboxes"].setdefault(self.mailbox_slug, {"epochs": {}})
        epochs = mailbox_node["epochs"]
        key = str(uidvalidity)
        if key not in epochs:
            # 独立纪元：全新状态，不引用任何旧纪元进度。
            epochs[key] = {
                "uidvalidity": uidvalidity,
                "mailbox": self.mailbox_name,
                "rounds": [],
                "messages": {},
            }
        return epochs[key]

    def _object_dir(self, kind: str, uidvalidity: int, uid: int) -> Path:
        return (
            self.workdir / kind / self.account / self.mailbox_slug
            / str(uidvalidity) / ("uid%d" % uid)
        )

    def _raw_path(self, uidvalidity: int, uid: int, part_path: str) -> Path:
        return self._object_dir("raw", uidvalidity, uid) / ("part-%s.raw" % part_path)

    # ---- 连接 ---------------------------------------------------------
    def _open_connection(self) -> None:
        conn = ImapConnection(self.host, self.port, timeout=self.timeout)
        conn.connect()
        conn.login(self.username, self.password)
        self.conn = conn

    def _reconnect(self, uidvalidity: int, mailbox: str) -> None:
        if self.conn is not None:
            self.conn.close()
        self._open_connection()
        assert self.conn is not None
        info = self.conn.examine(mailbox)
        if info["uidvalidity"] != uidvalidity:
            self.conn.close()
            self.conn = None
            raise EpochChangedError(
                "UIDVALIDITY changed mid-round: expected %d got %r"
                % (uidvalidity, info["uidvalidity"])
            )

    # ---- 轮次入口 -----------------------------------------------------
    def run_round(self) -> RoundSummary:
        self._open_connection()
        assert self.conn is not None
        try:
            info = self.conn.examine(self.mailbox_name)
        except BaseException:
            self.conn.close()
            self.conn = None
            raise
        uidvalidity = info["uidvalidity"]
        uidnext = info["uidnext"]
        if not isinstance(uidvalidity, int) or not isinstance(uidnext, int):
            conn.close()
            raise CollectorError("server did not return UIDVALIDITY/UIDNEXT: %r" % info)

        epoch = self._epoch_state(uidvalidity)
        # 本轮上界在轮次开始时固定；中途新信 UID >= bound，留给下一轮。
        bound = uidnext
        round_record = {"bound": bound, "started_at": _utc_now(), "finished_at": None}
        epoch["rounds"].append(round_record)
        self._save_state()
        self.on_event("round_start", {"uidvalidity": uidvalidity, "bound": bound})

        summary = RoundSummary(
            account=self.account,
            mailbox=self.mailbox_name,
            uidvalidity=uidvalidity,
            bound=bound,
        )
        try:
            assert self.conn is not None
            uids = self.conn.uid_search_range(1, bound - 1)
            summary.uids = list(uids)
            for uid in uids:
                self._process_uid(uidvalidity, uid, epoch, summary)
        finally:
            round_record["finished_at"] = _utc_now()
            self._save_state()
            # 重连校验失败（如纪元变化）时 self.conn 可能已被置空。
            if self.conn is not None:
                try:
                    self.conn.logout()
                except (ConnectionError, OSError, ImapError):
                    self.conn.close()
            self.conn = None
        return summary

    # ---- 单封邮件 -----------------------------------------------------
    def _message_state(self, epoch: dict, uid: int) -> dict:
        return epoch["messages"].setdefault(
            str(uid), {"status": "new", "tree": None, "parts": {}, "reason": None}
        )

    def _process_uid(
        self, uidvalidity: int, uid: int, epoch: dict, summary: RoundSummary
    ) -> None:
        message = self._message_state(epoch, uid)
        if message["status"] in ("delivered", "vanished", "quarantined"):
            # 重启后已终结的邮件直接跳过，part 不会被重复交付。
            summary.skipped_messages.append(uid)
            self.on_event("message_skip", {"uid": uid, "status": message["status"]})
            return

        self.on_event("message_start", {"uid": uid})
        assert self.conn is not None
        structure = self.conn.uid_fetch_bodystructure(uid)
        if structure is None:
            # 检索与 FETCH 之间邮件被删除：记录消失，不找同序号替身。
            self._mark_vanished(epoch, uid, message, summary, "expunged before FETCH")
            return
        try:
            tree = parse_bodystructure(structure)
        except MimeStructureError as exc:
            self._quarantine_message(uidvalidity, uid, message, "incomplete-mime: %s" % exc)
            summary.quarantined.append({"uid": uid, "scope": "message", "reason": str(exc)})
            self._save_state()
            return

        message["tree"] = [node.to_json() for node in tree]
        selected = attachment_parts(tree, self.allowed_types)
        self.on_event(
            "message_tree",
            {"uid": uid, "attachments": [part.path for part in selected]},
        )

        message_failed = False
        for part in selected:
            status = self._process_part(uidvalidity, uid, part, message, epoch, summary)
            if status == "vanished":
                return
            if status == "quarantined":
                message_failed = True

        message["status"] = "quarantined" if message_failed else "delivered"
        self._save_state()
        self.on_event("message_done", {"uid": uid, "status": message["status"]})

    def _mark_vanished(
        self, epoch: dict, uid: int, message: dict, summary: RoundSummary, reason: str
    ) -> None:
        message["status"] = "vanished"
        message["reason"] = reason
        summary.vanished.append(uid)
        self._save_state()
        self.on_event("message_vanished", {"uid": uid, "reason": reason})

    def _quarantine_message(
        self, uidvalidity: int, uid: int, message: dict, reason: str
    ) -> None:
        message["status"] = "quarantined"
        message["reason"] = reason
        quarantine_dir = self._object_dir("quarantine", uidvalidity, uid) / "message"
        _atomic_write_text(quarantine_dir / "REASON.txt", reason)
        _atomic_write_json(
            quarantine_dir / "metadata.json",
            {
                "identity": self._identity(uidvalidity, uid),
                "reason": reason,
                "at": _utc_now(),
            },
        )

    def _identity(self, uidvalidity: int, uid: int) -> dict:
        return {
            "account": self.account,
            "mailbox": self.mailbox_name,
            "uidvalidity": uidvalidity,
            "uid": uid,
        }

    # ---- 单个附件部件 -------------------------------------------------
    def _process_part(
        self,
        uidvalidity: int,
        uid: int,
        part: MimePart,
        message: dict,
        epoch: dict,
        summary: RoundSummary,
    ) -> str:
        part_state = message["parts"].setdefault(
            part.path,
            {
                "status": "in_progress",
                "content_type": part.content_type,
                "filename_display": part.filename,
                "encoded_size": part.encoded_size,
                "encoded_offset": 0,
                "delivery": None,
                "reason": None,
            },
        )
        if part_state["status"] == "delivered":
            return "delivered"

        entry = self.manifest.get((uidvalidity, uid, part.path))
        if entry is None:
            return self._quarantine_part(
                uidvalidity, uid, part, part_state, summary,
                "attachment part absent from supplier manifest",
            )

        try:
            raw_path = self._fetch_encoded(uidvalidity, uid, part, part_state)
        except VanishedError as exc:
            self._mark_vanished(epoch, uid, message, summary, str(exc))
            return "vanished"

        encoded = raw_path.read_bytes()
        if part.encoded_size is not None and len(encoded) != part.encoded_size:
            return self._quarantine_part(
                uidvalidity, uid, part, part_state, summary,
                "encoded size mismatch: BODYSTRUCTURE=%d cached=%d"
                % (part.encoded_size, len(encoded)),
                raw_path,
            )

        try:
            # 始终对全部已缓存原始字节整体解码；断点只存在于编码偏移上。
            decoded = decode_payload(encoded, part.cte)
        except UnknownEncodingError as exc:
            return self._quarantine_part(
                uidvalidity, uid, part, part_state, summary, str(exc), raw_path
            )

        result = verify(decoded, entry)
        if not result.ok:
            return self._quarantine_part(
                uidvalidity, uid, part, part_state, summary,
                "verification failed: %s" % result.reason, raw_path,
            )

        delivery = self._deliver(uidvalidity, uid, part, decoded, entry)
        part_state["status"] = "delivered"
        part_state["reason"] = None
        part_state["delivery"] = delivery
        self._save_state()
        summary.delivered.append(delivery)
        self.on_event("part_delivered", {"uid": uid, "part": part.path, **delivery})
        return "delivered"

    def _fetch_encoded(
        self,
        uidvalidity: int,
        uid: int,
        part: MimePart,
        part_state: dict,
    ) -> Path:
        raw_path = self._raw_path(uidvalidity, uid, part.path)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        # 续传偏移以线上编码字节为准：缓存文件有多长就从哪里续。
        offset = raw_path.stat().st_size if raw_path.exists() else 0
        if offset != part_state.get("encoded_offset", 0):
            raise CollectorError(
                "raw cache offset %d disagrees with checkpoint %d (uid=%d part=%s)"
                % (offset, part_state.get("encoded_offset", 0), uid, part.path)
            )
        if part.encoded_size is not None and offset >= part.encoded_size:
            return raw_path

        with open(raw_path, "ab") as cache:
            # 连续失败的重连次数有上界；一旦成功收到一块就清零，允许长期
            # 慢速但最终可恢复的传输，同时杜绝对持续不可用连接的死循环。
            consecutive_failures = 0
            while part.encoded_size is None or offset < part.encoded_size:
                want = self.chunk_size
                if part.encoded_size is not None:
                    want = min(want, part.encoded_size - offset)
                assert self.conn is not None
                try:
                    chunk = self.conn.uid_fetch_part(uid, part.path, offset, want)
                except LiteralInterrupted as exc:
                    # 已到达的原始字节先落盘，再以新的编码偏移重连续传。
                    consecutive_failures += 1
                    if consecutive_failures > self.max_retries:
                        raise CollectorError(
                            "transfer retry budget exhausted at encoded offset %d "
                            "(uid=%d part=%s)" % (offset, uid, part.path)
                        ) from exc
                    cache.write(exc.data)
                    cache.flush()
                    os.fsync(cache.fileno())
                    offset += len(exc.data)
                    part_state["encoded_offset"] = offset
                    self._save_state()
                    self.on_event(
                        "transfer_interrupted",
                        {"uid": uid, "part": part.path, "offset": offset,
                         "expected": exc.expected},
                    )
                    self._reconnect(uidvalidity, self.mailbox_name)
                    if part.encoded_size is not None and offset >= part.encoded_size:
                        break
                    continue
                except (ConnectionError, OSError, ImapError) as exc:
                    # 上一条请求后服务端已断开（演练服务器在新请求上才暴露），
                    # 或传输中途连接重置：未收新字节，偏移不变，重连同点重试。
                    consecutive_failures += 1
                    if consecutive_failures > self.max_retries:
                        raise CollectorError(
                            "transfer retry budget exhausted at encoded offset %d "
                            "(uid=%d part=%s)" % (offset, uid, part.path)
                        ) from exc
                    self.on_event(
                        "transfer_interrupted",
                        {"uid": uid, "part": part.path, "offset": offset,
                         "expected": part.encoded_size, "reason": str(exc)},
                    )
                    self._reconnect(uidvalidity, self.mailbox_name)
                    continue
                if chunk is None:
                    raise VanishedError(
                        "uid %d vanished while fetching part %s" % (uid, part.path)
                    )
                if not chunk:
                    raise CollectorError(
                        "empty FETCH chunk at encoded offset %d" % offset
                    )
                consecutive_failures = 0  # 成功收到一块，失败计数清零
                cache.write(chunk)
                cache.flush()
                os.fsync(cache.fileno())
                offset += len(chunk)
                part_state["encoded_offset"] = offset
                self._save_state()
                self.on_event(
                    "part_chunk",
                    {"uid": uid, "part": part.path, "offset": offset},
                )
                if part.encoded_size is None and len(chunk) < want:
                    break
        return raw_path

    def _quarantine_part(
        self,
        uidvalidity: int,
        uid: int,
        part: MimePart,
        part_state: dict,
        summary: RoundSummary,
        reason: str,
        raw_path: Optional[Path] = None,
    ) -> str:
        part_state["status"] = "quarantined"
        part_state["reason"] = reason
        quarantine_dir = self._object_dir("quarantine", uidvalidity, uid) / (
            "part-%s" % part.path
        )
        _atomic_write_text(quarantine_dir / "REASON.txt", reason)
        metadata = {
            "identity": {**self._identity(uidvalidity, uid), "part": part.path},
            "content_type": part.content_type,
            "content_transfer_encoding": part.cte,
            "filename_display": part.filename,
            "reason": reason,
            "at": _utc_now(),
        }
        _atomic_write_json(quarantine_dir / "metadata.json", metadata)
        if raw_path is not None and raw_path.exists():
            _atomic_write_bytes(
                quarantine_dir / raw_path.name, raw_path.read_bytes()
            )
        summary.quarantined.append(
            {"uid": uid, "part": part.path, "scope": "part", "reason": reason}
        )
        self._save_state()
        self.on_event("part_quarantined", {"uid": uid, "part": part.path, "reason": reason})
        return "quarantined"

    def _deliver(
        self, uidvalidity: int, uid: int, part: MimePart, decoded: bytes,
        entry: ManifestEntry,
    ) -> dict:
        display = safe_filename(part.filename, "part-%s.bin" % part.path)
        message_dir = self._object_dir("delivered", uidvalidity, uid)
        # part 路径参与落地身份，同名两个部件各自保留，互不覆盖。
        data_name = "part-%s__%s" % (part.path, display)
        data_path = message_dir / data_name
        sidecar_path = message_dir / (data_name + ".json")
        delivery = {
            **self._identity(uidvalidity, uid),
            "part": part.path,
            "filename_display": part.filename,
            "stored_as": data_name,
            "decoded_length": entry.decoded_length,
            "sha256": entry.sha256,
            "manifest_verified": True,
            "delivered_at": _utc_now(),
        }
        if not data_path.exists() or data_path.read_bytes() != decoded:
            _atomic_write_bytes(data_path, decoded)
        _atomic_write_json(sidecar_path, delivery)
        return delivery


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


# ---- CLI --------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="IMAP UID/MIME 授权附件采集器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1143)
    parser.add_argument("--account", required=True, help="账号逻辑名（持久身份组成）")
    parser.add_argument("--mailbox", default="INBOX")
    parser.add_argument("--user", default=None, help="IMAP 登录名，默认取账号逻辑名")
    parser.add_argument("--password", default="")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--chunk-size", type=int, default=65536)
    parser.add_argument(
        "--allow",
        default="application/octet-stream,text/csv",
        help="允许领取的 content-type 逗号列表（大小写不敏感）",
    )
    args = parser.parse_args(argv)
    allowed = {item.strip().upper() for item in args.allow.split(",") if item.strip()}
    collector = Collector(
        host=args.host,
        port=args.port,
        account=args.account,
        mailbox=args.mailbox,
        username=args.user,
        password=args.password,
        workdir=args.workdir,
        manifest=args.manifest,
        chunk_size=args.chunk_size,
        allowed_types=allowed,
    )
    summary = collector.run_round()
    print(json.dumps(summary.to_json(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
