from pathlib import Path
import socket
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'playbooks' / 'files'))
import rank_mirrors


class RankTests(unittest.TestCase):
    def test_fastest_measured_server_moves_first_and_the_rest_is_untouched(self):
        lines = ['# rated', 'Server = slow', 'Server = fast', '# note', 'Server = dead', 'Server = tail']
        self.assertEqual(rank_mirrors.rank(lines, [0.1, 20.0, 0.0]),
                         ['# rated', 'Server = fast', 'Server = slow', '# note', 'Server = dead', 'Server = tail'])

    def test_unreachable_server_measures_zero(self):
        self.assertEqual(rank_mirrors.measure('http://127.0.0.1:9/x.files', seconds=1), 0.0)

    def test_server_answering_garbage_measures_zero(self):
        server = socket.create_server(('127.0.0.1', 0))
        self.addCleanup(server.close)

        def answer():
            connection, _ = server.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(b'NOT HTTP\r\n\r\n')

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        url = 'http://127.0.0.1:%d/x.files' % server.getsockname()[1]
        self.assertEqual(rank_mirrors.measure(url, seconds=2), 0.0)
        thread.join(5)

    def test_invalid_server_url_measures_zero(self):
        self.assertEqual(rank_mirrors.measure('http://127.0.0.1:port/x.files', seconds=1), 0.0)


if __name__ == '__main__':
    unittest.main()
