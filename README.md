# 邮件交付数据的UID纪元与MIME附件采集

项目保存供方接口协议、演练数据与回环地址服务。所有示例记录均使用本机演练资源，不连接实际供应平台。

运行环境为 Python 3.10 或以上，仅使用标准库。

```sh
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m src.source --port 1143
```

前两条命令分别检查供方源协议行为及语法；第三条启动本机服务，使用 Ctrl+C 结束。

隔离模式（`-I`，不读环境变量、不把当前目录加入 `sys.path`，防止数据目录里被植入的同名模块劫持）下请用根目录包装脚本，它们显式把受信任的项目根加入路径：

```sh
python -I run_source.py --port 1143
python -I run_collector.py --host 127.0.0.1 --port 1143 --account 逻辑名 \
    --user local --password local --mailbox INBOX \
    --manifest fixtures/source.json --workdir ./work --chunk-size 16
```

## 采集器模块

* `src/imap_client.py`：自管套接字的最小 IMAP4rev1 客户端。只用 UID 寻址并校验 FETCH 回显 UID（拒绝同序号替身）；正文一律 `BODY.PEEK`，不发 STORE/APPEND；字面量按线上字节精确计数，传输中断时通过 `LiteralInterrupted` 交回已到达的原始字节与编码偏移。
* `src/mime_tree.py`：把 BODYSTRUCTURE 递归解析成带 IMAP part 路径的 MIME 树，覆盖嵌套 multipart 与 `message/rfc822`；处理 RFC 2047 与 RFC 2231 非 ASCII 文件名；只选显式 `attachment` 且 content-type 在允许列表内的叶子，inline 正文不领取；显示文件名做单段路径安全处理。
* `src/transfer.py`：对完整原始片段整体做 base64 / quoted-printable / 7-8bit 解码（四元组与转义可横跨分段），未知编码抛错隔离；按供方清单核对解码后长度与 SHA256。
* `src/collector.py`：采集编排与持久化。

### 身份、纪元与轮次边界

持久身份为 **账号逻辑名 / mailbox / UIDVALIDITY / UID / part 路径**，序号从不入库。每轮先 EXAMINE 取 UIDVALIDITY，并把当时的 UIDNEXT 固定为上界，只处理 `UID 1:bound-1`；轮次中新到的邮件留给下一轮，UID 缺号视为正常。UIDVALIDITY 变化即开启独立纪元（state 中按 uidvalidity 分键），旧纪元进度不套用。邮件在 SEARCH 与 FETCH 之间被 EXPUNGE 时记录 `vanished`，不找替身；处理顺序严格按 UID 升序，不依赖收件时间。

### 分段、缓存与重启

大附件用 `BODY.PEEK[part]<offset.count>` 分段。偏移按**线上 transfer-encoded 字节**计：原始片段追加缓存到 `work/raw/.../part-<path>.raw`，检查点 `encoded_offset` 与缓存长度互相校验；解码只对全部已缓存原始字节整体进行，断点从不落在解码后的字符位置。连接中断（含字面量截断与陈旧连接）按同一编码偏移重连续传，连续失败有上界。重启后已 `delivered`/`vanished`/`quarantined` 的邮件直接跳过，同一 part 不会产生第二份有效交付。

### 交付与隔离

校验通过的附件原子写入 `work/delivered/<account>/<mailbox>/<uidvalidity>/uid<uid>/part-<path>__<显示名>`，同名部件靠 part 路径共存，附 `.json` sidecar 记录完整身份与清单核对结果。未知编码、不完整 MIME、清单缺失或长度/哈希不符都进入 `work/quarantine/...` 并写明原因，状态标记为 quarantined，绝不声称领取成功。`work/state.json` 保存纪元、轮次边界、MIME 树与每个 part 的检查点。

## 演练邮箱

IMAP服务支持CAPABILITY、LOGIN、SELECT、EXAMINE、UID SEARCH UID start:end、UID FETCH UID/BODYSTRUCTURE/BODY.PEEK[part]<offset.count>、NOOP、CLOSE和LOGOUT。任意本机演练登录名可用，不含真实凭据。make_server返回对象的mailbox提供epoch、uidnext、messages、disappear、cut_after和seen。两个同名附件位于2、3，UID为2和5。

src/source.py 是供方源实现；fixtures/source.json 保存协议示例数据；tests/test_source.py 检查供方源自身的响应；tests/test_collector.py 覆盖完整交付、重启不重复、纪元切换后低 UID 新邮件、FETCH 前消失、UID 缺号与轮次上界、分段中断续传、inline 不领取、清单不符与未知编码隔离，每个用例都核对演练邮箱 Seen 未变。
