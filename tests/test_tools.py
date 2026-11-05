"""Offline workflow tests. Run: python3 -B -m unittest discover -s tests -v"""
from contextlib import ExitStack, closing, redirect_stdout
import hashlib
import io
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from lib import clss
import download
import export
import validate


PAYLOAD = b'LASF-test-data'


def response(code=200, checksum=None):
    stream = io.BytesIO(PAYLOAD)
    stream.getcode = lambda: code
    stream.headers = {'Content-Length': str(len(PAYLOAD)), 'Content-Type': 'application/octet-stream'}
    if checksum:
        stream.headers['X-Amz-Meta-Checksum-Sha256'] = checksum
    return stream


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / 'clss.sqlite3'
        with closing(sqlite3.connect(clss.DATABASE)) as source, closing(sqlite3.connect(self.database)) as db:
            source.backup(db)
            db.execute('DELETE FROM datapoints WHERE id>2')
            db.execute("UPDATE datapoints SET url='https://assets.flycom.si/clss/raw/01-koper/zls/gkot/GKOT_'||suffix||'.laz',http_code=NULL,size_bytes=NULL,downloaded_bytes=NULL")
            db.execute('UPDATE datapoint__next_to_validate SET datapoint_id=1')
            db.execute('UPDATE datapoint__next_to_download SET datapoint_id=1')
            db.execute('DELETE FROM settings')
            db.commit()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(clss, 'ROOT', self.root))
        self.stack.enter_context(patch.object(clss, 'DATABASE', self.database))
        self.sleep = self.stack.enter_context(patch.object(clss.time, 'sleep'))
        self.network = self.stack.enter_context(patch.object(clss.OPENER, 'open'))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def db(self, sql, parameters=()):
        with closing(sqlite3.connect(self.database)) as db:
            result = db.execute(sql, parameters)
            rows = result.fetchall()
            db.commit()
            return rows

    def one_tile(self):
        self.db('DELETE FROM datapoints WHERE id=2')

    def call(self, module, *args):
        with patch('sys.argv', [module.__name__ + '.py', *args]), redirect_stdout(io.StringIO()):
            module.main()

    def ready_for_download(self):
        self.db('UPDATE datapoints SET http_code=200,size_bytes=?', (len(PAYLOAD),))
        self.db('UPDATE datapoint__next_to_validate SET datapoint_id=NULL')

    def test_validation_uses_only_head_and_advances(self):
        self.network.side_effect = lambda *a, **k: response()
        self.call(validate)
        self.assertEqual([call.args[0].get_method() for call in self.network.call_args_list], ['HEAD', 'HEAD'])
        self.assertEqual(self.db('SELECT datapoint_id FROM datapoint__next_to_validate'), [(None,)])
        self.assertEqual(self.db('SELECT http_code FROM datapoints'), [(200,), (200,)])
        self.assertEqual(self.db('SELECT datapoint_id FROM datapoint__next_to_download'), [(1,)])

    def test_fresh_validation_rechecks_every_tile_and_preserves_download_tracking(self):
        self.ready_for_download()
        self.db('UPDATE datapoints SET downloaded_bytes=? WHERE id=1', (len(PAYLOAD),))
        self.db('UPDATE datapoint__next_to_download SET datapoint_id=2')
        calls = []

        def fetch(req, **kwargs):
            if not calls:
                self.assertEqual(self.db('SELECT http_code,size_bytes FROM datapoints'), [(None, None), (None, None)])
                self.assertEqual(self.db('SELECT datapoint_id FROM datapoint__next_to_validate'), [(1,)])
            calls.append(req.get_method())
            return response()

        self.network.side_effect = fetch
        self.call(validate, '--fresh')
        self.assertEqual(calls, ['HEAD', 'HEAD'])
        self.assertEqual(self.db('SELECT datapoint_id FROM datapoint__next_to_validate'), [(None,)])
        self.assertEqual(self.db('SELECT datapoint_id FROM datapoint__next_to_download'), [(2,)])
        self.assertEqual(self.db('SELECT downloaded_bytes FROM datapoints WHERE id=1'), [(len(PAYLOAD),)])

    def test_404_requires_all_twelve_regions(self):
        self.one_tile()
        self.db('UPDATE datapoints SET url=NULL')
        self.network.side_effect = lambda req, **k: (_ for _ in ()).throw(HTTPError(req.full_url, 404, 'Missing', {}, None))
        self.call(validate)
        self.assertEqual(self.network.call_count, 12)
        regions = {call.args[0].full_url.split('/')[5] for call in self.network.call_args_list}
        self.assertEqual(len(regions), 12)
        self.assertEqual(self.db('SELECT url,http_code FROM datapoints'), [(None, 404)])

    def test_429_then_success_obeys_retry_after(self):
        self.one_tile()
        self.network.side_effect = [HTTPError('https://assets.flycom.si/', 429, 'Wait', {'Retry-After': '9'}, None), response()]
        self.call(validate)
        self.assertTrue(any(call.args[0] > 8 for call in self.sleep.call_args_list))
        self.assertEqual(self.db('SELECT http_code FROM datapoints'), [(200,)])

    def test_repeated_429_leaves_current_tile_unvalidated(self):
        self.one_tile()
        self.network.side_effect = HTTPError('https://assets.flycom.si/', 429, 'Wait', {'Retry-After': '1'}, None)
        with self.assertRaises(clss.RequestFailed):
            self.call(validate)
        self.assertEqual(self.db('SELECT http_code FROM datapoints'), [(None,)])
        self.assertEqual(self.db('SELECT datapoint_id FROM datapoint__next_to_validate'), [(1,)])

    def test_server_errors_do_not_become_404_or_advance(self):
        self.one_tile()
        self.network.side_effect = HTTPError('https://assets.flycom.si/', 503, 'Unavailable', {}, None)
        with self.assertRaises(clss.RequestFailed):
            self.call(validate)
        self.assertEqual(self.network.call_count, clss.ATTEMPTS)
        self.assertEqual(self.db('SELECT http_code FROM datapoints'), [(503,)])
        self.assertEqual(self.db('SELECT datapoint_id FROM datapoint__next_to_validate'), [(1,)])

    def test_export_is_gated_and_outputs_both_formats(self):
        self.call(export, '--yes')
        self.assertFalse((self.root / 'exports' / 'validated-urls.csv').exists())
        self.ready_for_download()
        self.call(export, '--yes')
        expected = sorted(row[0] for row in self.db('SELECT url FROM datapoints'))
        self.assertEqual((self.root / 'exports' / 'validated-urls.csv').read_text().splitlines(), expected)
        self.assertEqual((self.root / 'exports' / 'validated-urls.txt').read_text(), ','.join(expected) + '\n')
        custom = self.root / 'custom-exports'
        self.call(export, '--yes', '--directory', str(custom))
        self.assertEqual((custom / 'validated-urls.csv').read_text().splitlines(), expected)

    def test_download_gate_limit_and_resume_existing_file(self):
        directory = self.root / 'downloads'
        self.call(download)
        self.network.assert_not_called()
        self.assertFalse(directory.exists())
        self.ready_for_download()
        self.network.side_effect = lambda *a, **k: response(checksum=hashlib.sha256(PAYLOAD).hexdigest())
        self.call(download, '--limit', '1')
        self.assertEqual(self.network.call_args.args[0].get_method(), 'GET')
        self.assertEqual(self.db('SELECT datapoint_id FROM datapoint__next_to_download'), [(2,)])
        suffix = self.db('SELECT suffix FROM datapoints WHERE id=2')[0][0]
        (directory / f'GKOT_{suffix}.laz').write_bytes(PAYLOAD)
        self.call(download)
        self.assertEqual(self.network.call_count, 1)
        self.assertEqual(self.db('SELECT datapoint_id FROM datapoint__next_to_download'), [(None,)])
        self.assertEqual(self.db('SELECT downloaded_bytes FROM datapoints'), [(len(PAYLOAD),), (len(PAYLOAD),)])
        self.assertEqual(self.db("SELECT value FROM settings WHERE key='download_directory'"), [('downloads',)])

    def test_fresh_download_rechecks_existing_files_without_downloading_them(self):
        self.ready_for_download()
        self.network.side_effect = lambda *a, **k: response()
        self.call(download)
        with patch.object(download, 'download_one', wraps=download.download_one) as scan:
            self.call(download, '--fresh')
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(self.network.call_count, 2)  # Only the original two GETs.
        self.assertEqual(self.db('SELECT datapoint_id FROM datapoint__next_to_download'), [(None,)])
        self.assertEqual(len(list((self.root / 'downloads').glob('*.laz'))), 2)

    def test_fresh_download_resets_test_destination_but_keeps_validation(self):
        self.ready_for_download()
        self.network.side_effect = lambda *a, **k: response()
        test_directory = self.root / 'test-download'
        self.call(download, '--directory', str(test_directory), '--limit', '1')
        self.call(download, '--fresh', '--limit', '1')
        self.assertEqual(self.network.call_count, 2)
        self.assertEqual(self.db('SELECT datapoint_id FROM datapoint__next_to_download'), [(2,)])
        self.assertEqual(self.db('SELECT http_code FROM datapoints'), [(200,), (200,)])
        self.assertEqual(self.db('SELECT downloaded_bytes FROM datapoints WHERE id=2'), [(None,)])
        self.assertEqual(self.db("SELECT value FROM settings WHERE key='download_directory'"), [('downloads',)])
        self.assertEqual(len(list(test_directory.glob('*.laz'))), 1)
        self.assertEqual(len(list((self.root / 'downloads').glob('*.laz'))), 1)

    def test_progress_uses_validated_sizes_for_unequal_files(self):
        self.ready_for_download()
        self.db('UPDATE datapoints SET size_bytes=1000,downloaded_bytes=1000 WHERE id=1')
        self.db('UPDATE datapoints SET size_bytes=9000 WHERE id=2')
        output = io.StringIO()
        with clss.connect() as db, redirect_stdout(output):
            clss.download_summary(db)
        db.close()
        self.assertIn('10.00 KB (10.00%)', output.getvalue())
        self.assertIn('left: 9.00 KB', output.getvalue())
        self.assertNotIn('Estimated total', output.getvalue())

    def test_progress_estimates_only_when_a_validated_size_is_missing(self):
        self.ready_for_download()
        self.db('UPDATE datapoints SET size_bytes=1000,downloaded_bytes=1000 WHERE id=1')
        self.db('UPDATE datapoints SET size_bytes=NULL WHERE id=2')
        output = io.StringIO()
        with clss.connect() as db, redirect_stdout(output):
            clss.download_summary(db)
        db.close()
        self.assertIn('Estimated total from downloaded-file average: 2.00 KB', output.getvalue())
        self.assertNotIn('%', output.getvalue())

    def test_default_download_directory_can_move_with_project(self):
        self.ready_for_download()
        self.network.side_effect = lambda *a, **k: response()
        self.call(download, '--limit', '1')
        moved_root = self.root / 'moved-project'
        moved_root.mkdir()
        moved_database = moved_root / 'clss.sqlite3'
        shutil.copyfile(self.database, moved_database)
        shutil.copytree(self.root / 'downloads', moved_root / 'downloads')
        with patch.object(clss, 'ROOT', moved_root), patch.object(clss, 'DATABASE', moved_database):
            self.call(download)
        with closing(sqlite3.connect(moved_database)) as db:
            self.assertIsNone(db.execute('SELECT datapoint_id FROM datapoint__next_to_download').fetchone()[0])
        self.assertEqual(len(list((moved_root / 'downloads').glob('*.laz'))), 2)

    def test_bad_checksum_never_completes_download(self):
        self.one_tile()
        self.ready_for_download()
        self.network.side_effect = lambda *a, **k: response(checksum='0' * 64)
        directory = self.root / 'files'
        with self.assertRaises(RuntimeError):
            self.call(download, '--directory', str(directory))
        self.assertEqual(self.db('SELECT datapoint_id FROM datapoint__next_to_download'), [(1,)])
        self.assertEqual(self.db('SELECT downloaded_bytes FROM datapoints'), [(None,)])
        self.assertFalse(list(directory.glob('*.laz')))


if __name__ == '__main__':
    unittest.main()
