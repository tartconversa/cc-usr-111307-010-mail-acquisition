"""仅监听回环地址的IMAP交付邮箱。"""
from __future__ import annotations
import argparse
import base64
from email.message import EmailMessage
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import re
import socketserver
import threading

DATA = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/source.json').read_text(encoding='utf-8'))


def build_message():
    message = EmailMessage()
    message['Subject'] = '授权数据交付'
    message['From'] = 'supplier@example.invalid'
    message['To'] = 'receiver@example.invalid'
    message.set_content('请按交付清单核对附件。', cte='base64')
    for content in DATA['attachments']:
        message.add_attachment(content['content'].encode(), maintype='application', subtype='octet-stream',
                               filename=content['filename'], cte='base64')
    return message.as_bytes(policy=policy.SMTP)


def part_at(message, path):
    node = message
    for index in map(int, path.split('.')):
        children = list(node.iter_parts()) if node.is_multipart() else [node]
        node = children[index - 1]
    return node


def quoted(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def structure(node):
    if node.is_multipart():
        return '(' + ''.join(structure(child) for child in node.iter_parts()) + ' ' + quoted(node.get_content_subtype().upper()) + ')'
    content = node.get_payload(decode=False).encode('ascii')
    filename = node.get_filename()
    # 参数使用RFC 2047表示供方旧系统的中文文件名。
    if filename:
        filename = '=?UTF-8?B?' + base64.b64encode(filename.encode()).decode() + '?='
    params = 'NIL' if not filename else '("NAME" ' + quoted(filename) + ')'
    disposition = 'NIL' if not filename else '("ATTACHMENT" ("FILENAME" ' + quoted(filename) + '))'
    result = '(' + quoted(node.get_content_maintype().upper()) + ' ' + quoted(node.get_content_subtype().upper())
    result += ' ' + params + ' NIL NIL ' + quoted(node.get('Content-Transfer-Encoding', '7bit').upper()) + ' ' + str(len(content))
    if node.get_content_maintype() == 'text':
        result += ' ' + str(content.count(b'\n'))
    return result + ' NIL ' + disposition + ' NIL NIL)'


class Mailbox:
    def __init__(self):
        self.epoch = 101
        self.raw = build_message()
        self.messages = {2: self.raw, 5: self.raw}
        self.uidnext = 6
        self.seen = set()
        self.requests = []
        self.disappear = set()
        self.cut_after = None
        self.lock = threading.RLock()


def make_server(port=0):
    mailbox = Mailbox()
    class Handler(socketserver.StreamRequestHandler):
        def line(self, value):
            self.wfile.write(value.encode('ascii') + b'\r\n'); self.wfile.flush()
        def handle(self):
            self.line('* OK [CAPABILITY IMAP4rev1] 仅本机演练邮箱'.encode('ascii', 'replace').decode())
            while True:
                raw = self.rfile.readline()
                if not raw:
                    return
                text = raw.decode('ascii').rstrip('\r\n')
                tag, command, *rest = text.split(' ', 2)
                argument = rest[0] if rest else ''
                with mailbox.lock:
                    mailbox.requests.append({'command': command, 'argument': '[local-login]' if command.upper() == 'LOGIN' else argument})
                    command = command.upper()
                    if command == 'CAPABILITY':
                        self.line('* CAPABILITY IMAP4rev1')
                    elif command == 'LOGIN':
                        pass
                    elif command in ('SELECT', 'EXAMINE'):
                        self.line('* %d EXISTS' % len(mailbox.messages))
                        self.line('* OK [UIDVALIDITY %d]' % mailbox.epoch)
                        self.line('* OK [UIDNEXT %d]' % mailbox.uidnext)
                        self.line('* FLAGS (\\Seen)')
                    elif command == 'UID' and argument.upper().startswith('SEARCH'):
                        match = re.search(r'UID\s+(\d+):(\d+)', argument, re.I)
                        lo, hi = map(int, match.groups()) if match else (1, mailbox.uidnext - 1)
                        self.line('* SEARCH' + ''.join(' %d' % uid for uid in sorted(mailbox.messages) if lo <= uid <= hi))
                    elif command == 'UID' and argument.upper().startswith('FETCH'):
                        match = re.match(r'FETCH\s+(\d+)\s+(.+)', argument, re.I)
                        if not match:
                            self.line(tag + ' BAD malformed FETCH'); continue
                        uid, request = int(match[1]), match[2]
                        if uid in mailbox.disappear:
                            mailbox.messages.pop(uid, None)
                        if uid in mailbox.messages:
                            sequence = sorted(mailbox.messages).index(uid) + 1
                            message = BytesParser(policy=policy.default).parsebytes(mailbox.messages[uid])
                            if 'BODYSTRUCTURE' in request.upper():
                                self.line('* %d FETCH (UID %d BODYSTRUCTURE %s)' % (sequence, uid, structure(message)))
                            else:
                                part = re.search(r'BODY(\.PEEK)?\[([0-9.]+)\](?:<(\d+)\.(\d+)>)?', request, re.I)
                                if not part:
                                    self.line(tag + ' BAD unsupported body section'); continue
                                if not part[1]:
                                    mailbox.seen.add(uid)
                                encoded = part_at(message, part[2]).get_payload(decode=False).encode('ascii')
                                offset = int(part[3] or 0); count = int(part[4] or len(encoded))
                                payload = encoded[offset:offset + count]
                                suffix = '<%d>' % offset if part[3] else ''
                                self.line('* %d FETCH (UID %d BODY[%s]%s {%d}' % (sequence, uid, part[2], suffix, len(payload)))
                                if mailbox.cut_after is not None:
                                    self.wfile.write(payload[:mailbox.cut_after]); self.wfile.flush(); return
                                self.wfile.write(payload + b')\r\n'); self.wfile.flush()
                    elif command == 'LOGOUT':
                        self.line('* BYE local mailbox'); self.line(tag + ' OK LOGOUT'); return
                    elif command not in ('NOOP', 'CLOSE'):
                        self.line(tag + ' BAD unsupported command'); continue
                    self.line(tag + ' OK completed')
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    server = Server(('127.0.0.1', port), Handler)
    server.mailbox = mailbox
    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='启动本机邮件交付源')
    parser.add_argument('--port', type=int, default=1143)
    args = parser.parse_args()
    with make_server(args.port) as server:
        print('127.0.0.1:%d' % server.server_address[1], flush=True)
        server.serve_forever()
