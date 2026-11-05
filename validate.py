#!/usr/bin/env python3
"""Validate GKOT URLs using HEAD only. Run again to resume at the saved cursor."""
import argparse
import sqlite3

from lib import clss

PROGRESS = 'datapoint__next_to_validate'


def list_regions(db):
    return db.execute('SELECT * FROM regions ORDER BY id').fetchall()


def get_datapoint(db, datapoint_id):
    return db.execute('SELECT * FROM datapoints WHERE id=?', (datapoint_id,)).fetchone()


def current_datapoint(db):
    return clss.current(db, PROGRESS)


def get_http_code(db, url):
    """Return status and file length. HEAD never reads the point-cloud body."""
    with clss.request(db, url, 'HEAD') as response:
        code = response.getcode()
        size = response.headers.get('Content-Length', '')
        content_type = response.headers.get('Content-Type', '').lower()
        if code == 200 and ('html' in content_type or 'xml' in content_type):
            raise clss.RequestFailed(f'HTTP 200 returned an error page instead of a point cloud: {url}')
        return code, int(size) if size.isdigit() else None


def test_url(db, datapoint, regions):
    urls = ([datapoint['url']] if datapoint['url'] else []) + [
        r['placeholder_url'].replace('$1', datapoint['suffix']) for r in regions]
    for url in dict.fromkeys(urls):  # Try the known URL first; avoid checking it twice.
        code, size = get_http_code(db, url)
        if code == 200:
            return url, 200, size
        # Only confirmed 404s permit trying the next region. Other errors stop/retry.
    return None, 404, None


def reset_progress(db):
    """Keep URL leads and download progress, but recheck every validation result."""
    with db:
        db.execute('UPDATE datapoints SET http_code=NULL,size_bytes=NULL')
        db.execute(f'UPDATE {PROGRESS} SET datapoint_id=(SELECT MIN(id) FROM datapoints) WHERE singleton=1')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--status', action='store_true', help='Show progress without making requests')
    mode.add_argument('--fresh', action='store_true', help='Clear validation results and recheck from the first tile')
    args = parser.parse_args()
    db = clss.connect()
    try:
        if args.status:
            clss.validation_summary(db)
            return
        with clss.single_runner():
            if args.fresh:
                reset_progress(db)
                print('Validation reset. Rechecking from the first tile.', flush=True)
            regions = list_regions(db)
            total = db.execute('SELECT COUNT(*) FROM datapoints').fetchone()[0]
            datapoint = current_datapoint(db)
            while datapoint is not None:
                try:
                    url, code, size = test_url(db, datapoint, regions)
                except clss.RequestFailed as error:
                    if error.code is not None and error.code != 429:
                        with db:
                            db.execute('UPDATE datapoints SET http_code=? WHERE id=?', (error.code, datapoint['id']))
                    raise
                with db:
                    db.execute('UPDATE datapoints SET url=?,http_code=?,size_bytes=? WHERE id=?',
                               (url, code, size, datapoint['id']))
                    clss.advance(db, PROGRESS, datapoint['id'])
                print(f'{datapoint["id"]:,}/{total:,} {datapoint["suffix"]}: HTTP {code}', flush=True)
                datapoint = current_datapoint(db)
            print('Validation complete.', flush=True)
            clss.validation_summary(db)
    finally:
        db.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped. Run validate.py again to resume.')
    except (clss.RequestFailed, RuntimeError, sqlite3.Error) as error:
        raise SystemExit(str(error))
