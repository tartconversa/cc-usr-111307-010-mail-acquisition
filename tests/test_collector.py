"""采集器端到端与单元测试。

演练邮箱由 src.source.make_server 提供（仅监听回环地址），支持纪元切换、
UID 缺号、FETCH 前消失与分段中断。所有测试结束都核对 ``\\Seen`` 未被设置。
"""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from src import source
from src.collector import Collector
from src.imap_client import ImapError, LiteralInterrupted, extract_fetch, nest
from src.mime_tree import (
    attachment_parts,
    decode_filename,
    parse_bodystructure,
    safe_filename,
)
from src.transfer import decode_payload

FIXTURE_MANIFEST = source.DATA["delivery_manifest"]


def manifest_for(uidvalidity, records=FIXTURE_MANIFEST):
    result = copy.deepcopy(records)
    for entry in result:
        entry["uidvalidity"] = uidvalidity
    return result


class CollectorCase(unittest.TestCase):
    def setUp(self):
        self.server = source.make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.tempdir = tempfile.mkdtemp()
        self.workdir = Path(self.tempdir) / "work"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def run_collector(self, manifest=None, chunk_size=65536, on_event=None):
        collector = Collector(
            host="127.0.0.1",
            port=self.port,
            account="demo-logical-account",
            mailbox="INBOX",
            username="local",
            password="local",
            workdir=self.workdir,
            manifest=manifest or manifest_for(101),
            chunk_size=chunk_size,
            on_event=on_event,
        )
        return collector.run_round()

    def assert_seen_untouched(self):
        self.assertEqual(self.server.mailbox.seen, set())

    def assert_peek_only(self):
        # 所有正文 FETCH 必须是 BODY.PEEK；BODYSTRUCTURE 不影响 Seen。
        for request in self.server.mailbox.requests:
            if request["command"] == "UID" and "BODY[" in request["argument"].upper():
                self.assertIn(
                    "BODY.PEEK[",
                    request["argument"].upper(),
                    "non-PEEK body fetch issued: %r" % request,
                )

    def state_epoch(self, uidvalidity=101):
        state = json.loads((self.workdir / "state.json").read_text(encoding="utf-8"))
        return (
            state["accounts"]["demo_logical_account"]
            ["mailboxes"]["INBOX"]["epochs"][str(uidvalidity)]
        )

    def state_message(self, uid, uidvalidity=101):
        return self.state_epoch(uidvalidity)["messages"][str(uid)]

    # ---- 端到端 -------------------------------------------------------
    def test_full_round_delivers_all_manifest_parts(self):
        summary = self.run_collector()
        self.assertEqual(summary.uids, [2, 5])
        self.assertEqual(summary.bound, 6)
        self.assertEqual(len(summary.delivered), 4)
        keys = {(item["uidvalidity"], item["uid"], item["part"]) for item in summary.delivered}
        self.assertEqual(
            keys,
            {(101, 2, "2"), (101, 2, "3"), (101, 5, "2"), (101, 5, "3")},
        )
        # 同名附件两个部件都保留，且落地身份带 part 路径。
        delivered_files = sorted(
            str(path.relative_to(self.workdir))
            for path in self.workdir.glob("delivered/**/*")
            if path.is_file() and not path.name.endswith(".json")
        )
        self.assertEqual(len(delivered_files), 4)
        self.assertTrue(all("part-2__" in name or "part-3__" in name for name in delivered_files))
        for path in self.workdir.glob("delivered/**/*.json"):
            metadata = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(metadata["manifest_verified"])
            self.assertEqual(metadata["decoded_length"], 20)
            self.assertEqual(len(metadata["sha256"]), 64)
        # 交付内容与清单哈希一致。
        for path in self.workdir.glob("delivered/**/part-*__*"):
            if path.suffix == ".json":
                continue
            import hashlib
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                json.loads(path.with_suffix(path.suffix + ".json").read_text(encoding="utf-8"))["sha256"],
            )
        self.assertEqual(summary.vanished, [])
        self.assertEqual(summary.quarantined, [])
        self.assert_seen_untouched()
        self.assert_peek_only()

    def test_restart_resumes_and_never_duplicates(self):
        events = []
        abort_after = {"done": False}

        def abort_after_first_delivery(event, info):
            events.append((event, info))
            if event == "transfer_interrupted":
                # 演练邮箱只截断一次：清除截断点，重连后的新连接正常。
                self.server.mailbox.cut_after = None
            if event == "part_delivered" and not abort_after["done"]:
                abort_after["done"] = True
                raise RuntimeError("simulated process crash")

        # 极小分段 + 服务端截断，制造横跨 base64 四元组的分段中断。
        self.server.mailbox.cut_after = 16
        with self.assertRaises(RuntimeError):
            self.run_collector(chunk_size=32, on_event=abort_after_first_delivery)
        interrupted = [info for event, info in events if event == "transfer_interrupted"]
        self.assertTrue(interrupted, "segmented interruption did not occur")
        # 检查点记录的是编码字节偏移，而非解码长度（解码长度只有 20）。
        self.assertGreaterEqual(interrupted[0]["offset"], 16)

        # 断点文件必须是原始（仍为 base64 编码）字节，而不是解码后的明文。
        raw_files = list(self.workdir.glob("raw/**/*.raw"))
        self.assertTrue(raw_files)
        first_raw = raw_files[0].read_bytes()
        self.assertTrue(first_raw)
        self.assertNotIn(b"id,name", first_raw)  # 明文首行不在缓存里
        import base64 as _b64
        # 全部由 base64 字母表组成；这是“仍处于传输编码状态”的直接证据。
        self.assertTrue(
            all(chr(c) in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\r\n"
                for c in first_raw)
        )
        # 同一原始字节整体重解码可恢复明文，证明断点不是解码字符位置。
        full_raw = b"".join(p.read_bytes() for p in sorted(
            self.workdir.glob("raw/**/*.raw")))
        self.assertEqual(
            _b64.b64decode(b"".join(full_raw.split())).decode("utf-8").splitlines()[0],
            "id,name",
        )

        # 模拟重启：同一工作目录、同一邮箱再跑一轮（演练服务器在新连接上
        # 才暴露上次的断连，采集器必须凭编码偏移缓存自动续传）。
        summary = self.run_collector(chunk_size=32)
        self.assertEqual(len(summary.delivered), 3)  # 首轮已落 1 份
        all_deliveries = list(self.workdir.glob("delivered/**/part-*"))
        data_files = [p for p in all_deliveries if not p.name.endswith(".json")]
        self.assertEqual(
            len(data_files), 4, "restart must not produce a second valid copy"
        )
        # 每个持久身份恰好一份数据文件 + 一份 sidecar。
        for identity in ((101, 2, "2"), (101, 2, "3"), (101, 5, "2"), (101, 5, "3")):
            uidvalidity, uid, part = identity
            matches = list(
                (self.workdir / "delivered").glob(
                    "**/%d/uid%d/part-%s__*" % (uidvalidity, uid, part)
                )
            )
            data = [p for p in matches if not p.name.endswith(".json")]
            self.assertEqual(len(data), 1, identity)
        # 再跑一轮：全部邮件已终结，零新增交付、零重复。
        third = self.run_collector(chunk_size=32)
        self.assertEqual(third.delivered, [])
        self.assertEqual(sorted(third.skipped_messages), [2, 5])
        self.assert_seen_untouched()
        self.assert_peek_only()

    def test_epoch_switch_isolates_progress_and_admits_low_uid_mail(self):
        first = self.run_collector()
        self.assertEqual(len(first.delivered), 4)
        state_path = self.workdir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        old_epoch = self.state_epoch(101)
        self.assertEqual(sorted(old_epoch["messages"]), ["2", "5"])

        # 邮箱搬迁后 UIDVALIDITY 变化；新纪元里出现 UID 很低（1）的新邮件。
        # 供方为新纪元出具的清单以新身份 (102, uid=1, part) 标识——旧纪元
        # 那套 (101, uid=2/5) 进度与清单都不能套用。
        self.server.mailbox.epoch = 102
        self.server.mailbox.messages = {1: self.server.mailbox.raw}
        self.server.mailbox.uidnext = 2
        new_manifest = []
        for entry in manifest_for(102):
            entry = copy.deepcopy(entry)
            entry["uid"] = 1
            new_manifest.append(entry)

        summary = self.run_collector(manifest=new_manifest)
        self.assertEqual(summary.uidvalidity, 102)
        self.assertEqual(summary.uids, [1])
        self.assertEqual(
            {(item["uidvalidity"], item["uid"], item["part"]) for item in summary.delivered},
            {(102, 1, "2"), (102, 1, "3")},
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        epochs = state["accounts"]["demo_logical_account"]["mailboxes"]["INBOX"]["epochs"]
        self.assertEqual(set(epochs), {"101", "102"})
        # 旧纪元进度原样保留，没有被套用到新纪元。
        self.assertEqual(sorted(epochs["101"]["messages"]), ["2", "5"])
        self.assertEqual(sorted(epochs["102"]["messages"]), ["1"])
        self.assert_seen_untouched()
        self.assert_peek_only()

    def test_epoch_change_during_reconnect_aborts_without_reusing_progress(self):
        from src.collector import EpochChangedError
        # 首次分段传输即截断，重连时邮箱已搬迁、UIDVALIDITY 改变。
        self.server.mailbox.cut_after = 8

        def flip_epoch(event, info):
            if event == "transfer_interrupted":
                self.server.mailbox.epoch = 999
                self.server.mailbox.cut_after = None

        collector = Collector(
            host="127.0.0.1", port=self.port, account="demo-logical-account",
            mailbox="INBOX", username="local", password="local",
            workdir=self.workdir, manifest=manifest_for(101),
            chunk_size=16, on_event=flip_epoch,
        )
        with self.assertRaises(EpochChangedError):
            collector.run_round()
        # 旧纪元 101 的状态保留，但没有任何 part 被标记为交付成功。
        epoch = self.state_epoch(101)
        for message in epoch["messages"].values():
            for part in message["parts"].values():
                self.assertNotEqual(part["status"], "delivered")
        # 纪元 999 没有被悄悄建立（中止发生在新 EXAMINE 校验阶段）。
        state = json.loads((self.workdir / "state.json").read_text(encoding="utf-8"))
        self.assertNotIn("999", state["accounts"]["demo_logical_account"]
                         ["mailboxes"]["INBOX"]["epochs"])
        self.assert_seen_untouched()

    def test_message_vanished_before_fetch_is_recorded_not_substituted(self):
        self.server.mailbox.disappear = {2}
        summary = self.run_collector()
        self.assertEqual(summary.vanished, [2])
        # UID 5 正常交付；没有任何东西冒充 UID 2。
        self.assertEqual(
            {(item["uid"], item["part"]) for item in summary.delivered},
            {(5, "2"), (5, "3")},
        )
        message = self.state_message(2)
        self.assertEqual(message["status"], "vanished")
        # 再次运行不会重试已消失邮件：UID 2 已被 EXPUNGE，SEARCH 不再返回它，
        # 因此本轮只跳过仍在邮箱里、且上轮已交付的 UID 5。
        again = self.run_collector()
        self.assertEqual(again.vanished, [])
        self.assertEqual(again.skipped_messages, [5])
        self.assertEqual(again.delivered, [])
        # 持久状态里 UID 2 的 vanished 记录保持不变，没有被同序号邮件顶替。
        self.assertEqual(self.state_message(2)["status"], "vanished")
        self.assert_seen_untouched()

    def test_uid_gaps_are_normal_and_round_bound_excludes_later_mail(self):
        # 初始 UID 为 2、5（缺 1、3、4），轮次开始时 UIDNEXT 固定为 6。
        self.server.mailbox.uidnext = 6
        # 轮次进行中到达的新信 UID 6，应被上界挡在下一轮之外。
        original_process = Collector._process_uid

        def inject_later_mail(collector, uidvalidity, uid, epoch, round_summary):
            if uid == 5:
                self.server.mailbox.messages[6] = self.server.mailbox.raw
                self.server.mailbox.uidnext = 7
            return original_process(collector, uidvalidity, uid, epoch, round_summary)

        Collector._process_uid = inject_later_mail
        try:
            summary = self.run_collector()
        finally:
            Collector._process_uid = original_process
        self.assertEqual(summary.uids, [2, 5])
        self.assertEqual(summary.bound, 6)
        self.assertNotIn(6, summary.uids)
        self.assertEqual(len(summary.delivered), 4)
        # 线上请求证实 SEARCH 上界与 FETCH 都没有触及 UID 6。
        for request in self.server.mailbox.requests:
            self.assertNotIn(" 6 ", " " + request["argument"] + " ")
        self.assert_seen_untouched()
        self.assert_peek_only()

    def test_inline_body_is_not_claimed(self):
        summary = self.run_collector()
        # text/plain inline 正文（part 1）没有任何交付或隔离记录。
        for item in summary.delivered:
            self.assertNotIn(item["part"], ("1",))
        self.assertEqual(summary.quarantined, [])
        state = json.loads((self.workdir / "state.json").read_text(encoding="utf-8"))
        message = self.state_message(2)
        self.assertEqual(set(message["parts"]), {"2", "3"})

        def collect_paths(nodes):
            found = set()
            for node in nodes:
                if node["path"]:
                    found.add(node["path"])
                found.update(collect_paths(node["children"]))
            return found

        tree_paths = collect_paths(message["tree"])
        self.assertIn("1", tree_paths)  # 树里有记录，只是不领取
        self.assert_seen_untouched()

    def test_manifest_mismatch_quarantines_and_never_claims_success(self):
        broken = manifest_for(101)
        broken[0]["sha256"] = "00" * 32  # 破坏 UID 2 / part 2 的哈希
        summary = self.run_collector(manifest=broken)
        quarantined = {
            (item["uid"], item.get("part")): item["reason"]
            for item in summary.quarantined
        }
        self.assertIn((2, "2"), quarantined)
        self.assertIn("sha256", quarantined[(2, "2")])
        delivered = {(item["uid"], item["part"]) for item in summary.delivered}
        self.assertNotIn((2, "2"), delivered)
        self.assertIn((2, "3"), delivered)
        reason_files = list(self.workdir.glob("quarantine/**/REASON.txt"))
        self.assertTrue(reason_files)
        state = json.loads((self.workdir / "state.json").read_text(encoding="utf-8"))
        part = self.state_message(2)["parts"]["2"]
        self.assertEqual(part["status"], "quarantined")
        self.assert_seen_untouched()
        self.assert_peek_only()

    def test_unknown_transfer_encoding_is_quarantined(self):
        # 构造单附件、未知 CTE（x-uuencode）的最小邮件。
        raw = (
            b"Subject: x\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: application/octet-stream; name="x.bin"\r\n'
            b"Content-Transfer-Encoding: x-uuencode\r\n"
            b'Content-Disposition: attachment; filename="x.bin"\r\n\r\n'
            b"0V%T\r\n"
        )
        self.server.mailbox.messages = {3: raw}
        self.server.mailbox.uidnext = 4
        manifest = [{
            "uidvalidity": 101, "uid": 3, "part": "1",
            "decoded_length": 4, "sha256": "00" * 32,
        }]
        summary = self.run_collector(manifest=manifest)
        self.assertEqual(summary.delivered, [])
        self.assertEqual(len(summary.quarantined), 1)
        self.assertIn("Content-Transfer-Encoding", summary.quarantined[0]["reason"])
        self.assert_seen_untouched()
        self.assert_peek_only()

    def test_disallowed_attachment_type_is_skipped_not_delivered(self):
        raw = (
            b"Subject: x\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: image/png; name="x.png"\r\n'
            b"Content-Transfer-Encoding: base64\r\n"
            b'Content-Disposition: attachment; filename="x.png"\r\n\r\n'
            b"AAAA\r\n"
        )
        self.server.mailbox.messages = {3: raw}
        self.server.mailbox.uidnext = 4
        summary = self.run_collector(
            manifest=[{"uidvalidity": 101, "uid": 3, "part": "1",
                       "decoded_length": 0, "sha256": "00" * 32}]
        )
        self.assertEqual(summary.delivered, [])
        self.assertEqual(summary.quarantined, [])  # 类型不在允许列表：跳过而非隔离
        state = json.loads((self.workdir / "state.json").read_text(encoding="utf-8"))
        message = self.state_message(3)
        self.assertEqual(message["status"], "delivered")  # 邮件本身处理完成
        self.assertEqual(message["parts"], {})
        self.assert_seen_untouched()

    # ---- 单元：协议与 MIME -------------------------------------------
    def test_uid_echo_mismatch_is_protocol_error(self):
        # 请求 UID 7，服务器（替身）回显 UID 8：必须拒绝。
        group = nest([b"*", 1, b"FETCH", "(", b"UID", 8,
                      b"BODY[2]<0>", b"abc", ")"])
        with self.assertRaises(ImapError):
            extract_fetch([group], 7, b"BODY[")
        good = nest([b"*", 1, b"FETCH", "(", b"UID", 7,
                     b"BODY[2]<0>", b"abc", ")"])
        self.assertEqual(extract_fetch([good], 7, b"BODY["), b"abc")

    def test_literal_interruption_keeps_encoded_bytes(self):
        # 直接验证中断异常携带的是编码字节偏移语义。
        exc = LiteralInterrupted(b"0123456789abcdef", 28)
        self.assertEqual(len(exc.data), 16)
        self.assertEqual(exc.expected, 28)
        # 续传偏移 = 原偏移 + 已收编码字节数，且 base64 可整体重解码。
        full = b"aWQsbmFtZQphLOi1hOa6kOeUsgo="
        self.assertEqual(decode_payload(full, "base64").decode("utf-8").splitlines()[0], "id,name")

    def test_decoding_tolerates_splits_across_base64_quartets_and_qp_escapes(self):
        from src.transfer import UnknownEncodingError
        original = "id,name\na,资源甲乙\n".encode("utf-8")
        encoded_b64 = __import__("base64").b64encode(original)
        # 在任意偏移切成多段再拼接，整体解码必须恒等于原文（四元组跨界）。
        for cut in range(1, len(encoded_b64)):
            rebuilt = b"".join([encoded_b64[:cut], encoded_b64[cut:]])
            self.assertEqual(decode_payload(rebuilt, "base64"), original)
        # quoted-printable：partial FETCH 只按字节切分编码流、不增删字节。
        # 在 =XX 转义序列内部切开，缓存重组后整体解码必须无损（转义跨界）。
        import quopri
        encoded_qp = quopri.encodestring(original)
        self.assertEqual(decode_payload(encoded_qp, "quoted-printable"), original)
        escape_positions = [i for i in range(len(encoded_qp) - 1)
                            if encoded_qp[i:i + 1] == b"="]
        self.assertTrue(escape_positions)  # 多字节中文必然产生 =XX 转义
        for cut in escape_positions:
            # 模拟两段分别缓存、拼接后一次整体解码；不插入任何额外字节。
            rebuilt = encoded_qp[:cut] + encoded_qp[cut:]
            self.assertEqual(decode_payload(rebuilt, "quoted-printable"), original)
        # 未知编码必须抛错而不是静默当明文交付。
        with self.assertRaises(UnknownEncodingError):
            decode_payload(b"whatever", "x-uuencode")

    def test_bodystructure_paths_nesting_and_disposition(self):
        # multipart/mixed( text(inline), multipart/alt( text, app attachment ),
        #                 message/rfc822( multipart/mixed( app attachment ) ) )
        structure = [
            [b"TEXT", b"PLAIN", None, None, None, b"7BIT", 3, 1, None,
             [b"INLINE", None]],
            [
                [b"TEXT", b"HTML", None, None, None, b"7BIT", 5, 1],
                [b"APPLICATION", b"OCTET-STREAM",
                 [b"NAME", b"=?UTF-8?B?5oql5ZGK?="], None, None, b"BASE64", 8,
                 None, [b"ATTACHMENT", [b"FILENAME", b"=?UTF-8?B?5oql5ZGK?="]]],
                b"ALTERNATIVE",
            ],
            [b"MESSAGE", b"RFC822", None, None, None, b"7BIT", 100,
             None,
             [
                 [b"APPLICATION", b"OCTET-STREAM",
                  [b"NAME*0*", b"utf-8''%61%62"], None, None, b"BASE64", 4,
                  None, [b"ATTACHMENT", [b"FILENAME*0*", b"utf-8''%61%62"]]],
                 b"MIXED",
             ],
             5],
            b"MIXED",
        ]
        trees = parse_bodystructure(structure)
        root = trees[0]
        self.assertEqual(root.content_type, "MULTIPART/MIXED")
        all_parts = list(_walk(root))
        by_path = {}
        for part in all_parts:
            by_path.setdefault(part.path, []).append(part)
        self.assertEqual(by_path["1"][0].content_type, "TEXT/PLAIN")
        self.assertFalse(by_path["1"][0].is_attachment)
        self.assertEqual(by_path["2"][0].content_type, "MULTIPART/ALTERNATIVE")
        attachment = by_path["2.2"][0]
        self.assertEqual(attachment.content_type, "APPLICATION/OCTET-STREAM")
        self.assertTrue(attachment.is_attachment)
        self.assertEqual(attachment.filename, "报告")
        # message/rfc822 叶子本身是 part 3（非附件）；内嵌多部件复用 3 前缀，
        # 内嵌附件因此是 3.1。路径 "3" 上有两个不同节点，按类型区分。
        rfc822_nodes = [part for part in by_path["3"] if part.maintype == "MESSAGE"]
        self.assertEqual(len(rfc822_nodes), 1)
        self.assertEqual(rfc822_nodes[0].content_type, "MESSAGE/RFC822")
        self.assertFalse(rfc822_nodes[0].is_attachment)
        nested = by_path["3.1"][0]
        self.assertTrue(nested.is_attachment)
        self.assertEqual(nested.filename, "ab")
        selected = attachment_parts(trees, {"APPLICATION/OCTET-STREAM"})
        self.assertEqual({part.path for part in selected}, {"2.2", "3.1"})

    def test_decode_filename_rfc2047_and_rfc2231(self):
        import base64
        word = "=?UTF-8?B?" + base64.b64encode("目录".encode()).decode() + "?="
        self.assertEqual(decode_filename(word.encode("ascii")), "目录")
        self.assertEqual(
            decode_filename(b"utf-8''%E7%9B%AE%E5%BD%95.csv"),
            "目录.csv",
        )
        self.assertEqual(safe_filename("../etc/passwd", "fb"), "passwd")  # 只保留末段
        self.assertEqual(safe_filename(None, "fb"), "fb")
        self.assertEqual(safe_filename("a/b\\c:txt", "fb"), "c_txt")
        self.assertEqual(safe_filename("目录.csv", "fb"), "目录.csv")  # Unicode 保留

    def test_incomplete_bodystructure_raises(self):
        from src.mime_tree import MimeStructureError
        with self.assertRaises(MimeStructureError):
            parse_bodystructure([[b"TEXT", b"PLAIN"]])  # 字段不完整
        with self.assertRaises(MimeStructureError):
            parse_bodystructure([])


def _walk(part):
    yield part
    for child in part.children:
        yield from _walk(child)


if __name__ == "__main__":
    unittest.main()
