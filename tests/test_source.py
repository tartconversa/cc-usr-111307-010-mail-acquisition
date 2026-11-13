import imaplib
import threading
import unittest
from src import source

class MailSourceTests(unittest.TestCase):
    def test_uid_body_peek_and_epoch(self):
        with source.make_server() as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                client = imaplib.IMAP4('127.0.0.1', server.server_address[1], timeout=5)
                client.login('local', 'local'); client.select('INBOX')
                self.assertEqual(client.response('UIDVALIDITY')[1], [b'101'])
                self.assertEqual(client.uid('SEARCH', None, 'UID', '1:5')[1], [b'2 5'])
                typ, data = client.uid('FETCH', '2', '(UID BODY.PEEK[2]<0.5>)')
                self.assertEqual(typ, 'OK'); self.assertEqual(len(data[0][1]), 5)
                self.assertEqual(server.mailbox.seen, set())
                server.mailbox.epoch = 102
                client.select('INBOX'); self.assertEqual(client.response('UIDVALIDITY')[1], [b'102'])
                client.logout()
            finally:
                server.shutdown(); thread.join()

if __name__ == '__main__': unittest.main()
