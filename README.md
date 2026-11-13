# 邮件交付数据的UID纪元与MIME附件采集

项目保存供方接口协议、演练数据与回环地址服务。所有示例记录均使用本机演练资源，不连接实际供应平台。

运行环境为 Python 3.10 或以上，仅使用标准库。

```sh
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m src.source --port 1143
```

前两条命令分别检查供方源协议行为及语法；第三条启动本机服务，使用 Ctrl+C 结束。

IMAP服务支持CAPABILITY、LOGIN、SELECT、EXAMINE、UID SEARCH UID start:end、UID FETCH UID/BODYSTRUCTURE/BODY.PEEK[part]<offset.count>、NOOP、CLOSE和LOGOUT。任意本机演练登录名可用，不含真实凭据。make_server返回对象的mailbox提供epoch、uidnext、messages、disappear、cut_after和seen。两个同名附件位于2、3，UID为2和5。

src/source.py 是供方源实现；fixtures/source.json 保存协议示例数据；tests/test_source.py 检查供方源自身的响应。
