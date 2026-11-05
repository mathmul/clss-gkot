"""Small shared helpers. Python standard library only; no file downloads on import."""
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import http.client
from pathlib import Path
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent.parent
DATABASE = ROOT / 'db' / 'clss.sqlite3'
REQUEST_INTERVAL = 1.2  # The server rate-limited faster requests during testing.
ATTEMPTS = 5


class RequestFailed(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Report redirects; never turn validation into a GET request.


OPENER = build_opener(NoRedirects())


def connect():
    if not DATABASE.is_file():
        raise RuntimeError('Database missing. Restore the supplied db/clss.sqlite3 file.')
    db = sqlite3.connect(DATABASE, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    return db


@contextmanager
def single_runner():
    """Allow one validator or downloader at a time; the OS releases the lock on exit."""
    with DATABASE.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('A validator or downloader is already running.') from None
        yield


def setting(db, key, default=None):
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def set_setting(db, key, value):
    with db:
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))


def request(db, url, method):
    """Return a 200/404 response; retry temporary errors without moving any cursor."""
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.netloc != 'assets.flycom.si' or not parts.path.startswith('/clss/raw/'):
        raise RequestFailed(f'Unexpected download host or path: {url}')
    for attempt in range(ATTEMPTS):
        wait_until = float(setting(db, 'next_request_at', '0'))
        time.sleep(max(0, wait_until - time.time()))
        set_setting(db, 'next_request_at', time.time() + REQUEST_INTERVAL)
        response, code, delay = None, None, None
        try:
            response = OPENER.open(Request(url, method=method,
                headers={'User-Agent': 'CLSS-GKOT-tools/1.0', 'Accept-Encoding': 'identity'}), timeout=60)
        except HTTPError as error:
            response = error
        except (URLError, OSError, http.client.HTTPException) as error:
            message = str(error)
        if response is not None:
            code = response.getcode()
            if code in (200, 404):
                return response  # The caller closes it, including for HEAD and 404.
            message = f'HTTP {code} for {url}'
            retry_after = response.headers.get('Retry-After', '')
            response.close()
            if code not in (429, 500, 502, 503, 504):
                raise RequestFailed(message, code)
            if retry_after:
                try:
                    delay = max(1, float(retry_after))
                except ValueError:
                    try:
                        delay = max(1, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError):
                        pass
        delay = delay or (60 if code == 429 else 5) * 2 ** attempt
        set_setting(db, 'next_request_at', max(time.time() + delay,
                    float(setting(db, 'next_request_at', '0'))))
        if attempt + 1 == ATTEMPTS:
            raise RequestFailed(f'{message}; retries exhausted. Progress is saved.', code)
        print(f'{message}. Retrying after {delay:.0f} seconds.', flush=True)
    raise AssertionError('Unreachable')


def current(db, table):
    if table not in ('datapoint__next_to_validate', 'datapoint__next_to_download'):
        raise ValueError('Unknown progress table')
    return db.execute(f'SELECT d.* FROM {table} p JOIN datapoints d ON d.id=p.datapoint_id WHERE singleton=1').fetchone()


def advance(db, table, datapoint_id):
    # Called inside the same transaction as the result update.
    if table not in ('datapoint__next_to_validate', 'datapoint__next_to_download'):
        raise ValueError('Unknown progress table')
    db.execute(f'UPDATE {table} SET datapoint_id=(SELECT MIN(id) FROM datapoints WHERE id>?) WHERE singleton=1',
               (datapoint_id,))


def validation_summary(db):
    total = db.execute('SELECT COUNT(*) FROM datapoints').fetchone()[0]
    confirmed = db.execute('SELECT COUNT(*) FROM datapoints WHERE http_code=200').fetchone()[0]
    failed = [r[0] for r in db.execute('SELECT suffix FROM datapoints WHERE http_code=404 ORDER BY suffix')]
    print(f'Validation: {total:,} tiles | confirmed: {confirmed:,} | not found in all regions: {len(failed):,}')
    pending = current(db, 'datapoint__next_to_validate')
    if pending:
        print(f'Next to validate: {pending["suffix"]} (ID {pending["id"]}).')
    elif failed:
        print('Not found: ' + ', '.join(failed))


def readable_size(value):
    if value is None:
        return 'unknown'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB', 'PB'):
        if value < 1000 or unit == 'PB':
            return f'{value:,.2f} {unit}'
        value /= 1000


def download_summary(db):
    total, done, size, known, known_count, remaining_size = db.execute('''
        SELECT COUNT(*), COUNT(downloaded_bytes), COALESCE(SUM(downloaded_bytes),0),
               SUM(size_bytes), COUNT(size_bytes),
               COALESCE(SUM(CASE WHEN downloaded_bytes IS NULL THEN size_bytes ELSE 0 END),0)
        FROM datapoints WHERE http_code=200
    ''').fetchone()
    average = size / done if done else None
    print(f'Downloaded: {done:,}/{total:,} | remaining: {total-done:,} | saved: {readable_size(size)} '
          f'| average: {readable_size(average)}', flush=True)
    if known_count == total and total:
        percent = size / known * 100 if known else 100
        print(f'Bytes: {readable_size(size)} / {readable_size(known)} ({percent:.2f}%) '
              f'| left: {readable_size(remaining_size)} | total from validated file sizes', flush=True)
    else:
        estimated = average * total if average is not None else None
        print(f'Estimated total from downloaded-file average: {readable_size(estimated)}', flush=True)
