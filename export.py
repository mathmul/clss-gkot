#!/usr/bin/env python3
"""Export confirmed URLs: CSV has one per row; TXT has one comma-separated line."""
import argparse
import csv
from pathlib import Path
import sqlite3

from lib import clss


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yes', action='store_true', help='Export without the already-downloaded prompt')
    parser.add_argument('--directory', type=Path, default=clss.ROOT / 'exports',
                        help='Export destination; default: exports/ beside the scripts')
    args = parser.parse_args()
    db = clss.connect()
    try:
        if clss.current(db, 'datapoint__next_to_validate') is not None:
            print('Validation is not complete. Run validate.py first; no export was written.')
            return
        clss.validation_summary(db)
        if clss.current(db, 'datapoint__next_to_download') is None and not args.yes:
            answer = input('All confirmed files are recorded as downloaded. Export the URLs anyway? [y/N] ')
            if answer.strip().lower() not in ('y', 'yes'):
                print('Export cancelled.')
                return
        urls = [row[0] for row in db.execute('SELECT url FROM datapoints WHERE http_code=200 AND url IS NOT NULL ORDER BY url')]
        directory = args.directory.expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        csv_path, txt_path = directory / 'validated-urls.csv', directory / 'validated-urls.txt'
        with csv_path.open('w', newline='') as stream:
            csv.writer(stream).writerows([url] for url in urls)
        txt_path.write_text(','.join(urls) + '\n')
        print(f'CSV: {csv_path} — done ({len(urls):,} URLs, one per row, no header).')
        print(f'TXT: {txt_path} — done (one comma-separated line).')
    finally:
        db.close()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, sqlite3.Error, EOFError) as error:
        raise SystemExit(str(error))
