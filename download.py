#!/usr/bin/env python3
"""Download confirmed GKOT files, saving the cursor after every complete file."""
import argparse
import hashlib
import http.client
from pathlib import Path
import shutil
import sqlite3
import time
from urllib.error import URLError

from lib import clss

PROGRESS = 'datapoint__next_to_download'


def valid_file(path, size):
    if size is None or path.stat().st_size != size:
        return False
    with path.open('rb') as stream:
        return stream.read(4) == b'LASF'


def download_one(db, datapoint, directory):
    destination = directory / f'GKOT_{datapoint["suffix"]}.laz'
    if destination.exists():
        if valid_file(destination, datapoint['size_bytes']):
            print(f'Already present: {destination.name}', flush=True)
            return destination.stat().st_size
        raise RuntimeError(f'Existing file has an unexpected size/format: {destination}. Keeping it intact.')
    partial = destination.with_suffix('.laz.part')
    for attempt in range(clss.ATTEMPTS):
        try:
            with clss.request(db, datapoint['url'], 'GET') as response:
                if response.getcode() != 200:
                    raise clss.RequestFailed(f'HTTP {response.getcode()} while downloading {destination.name}. '
                                             'The download cursor has not moved.', response.getcode())
                length = response.headers.get('Content-Length', '')
                expected_size = int(length) if length.isdigit() else datapoint['size_bytes']
                checksum = response.headers.get('X-Amz-Meta-Checksum-Sha256')
                if expected_size and shutil.disk_usage(directory).free < expected_size:
                    raise RuntimeError(f'Not enough free space for {destination.name}. Progress is saved.')
                digest, size = hashlib.sha256(), 0
                with partial.open('wb') as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                if expected_size is not None and size != expected_size:
                    raise OSError(f'Incomplete response: received {size} of {expected_size} bytes')
                with partial.open('rb') as stream:
                    if stream.read(4) != b'LASF':
                        raise OSError('The response is not a LAS/LAZ file')
                if checksum and digest.hexdigest().lower() != checksum.lower():
                    raise OSError('Server SHA-256 checksum does not match')
            if destination.exists():
                raise RuntimeError(f'{destination} appeared during the download; keeping both files.')
            partial.rename(destination)
            return size
        except (OSError, URLError, http.client.HTTPException) as error:
            if attempt + 1 == clss.ATTEMPTS:
                raise RuntimeError(f'{destination.name}: {error}. Progress is saved.') from error
            delay = 5 * 2 ** attempt
            print(f'{destination.name}: {error}; retrying in {delay}s.', flush=True)
            time.sleep(delay)
    raise AssertionError('Unreachable')


def reset_progress(db):
    """Reset tracking and destination binding; preserve validation and all files."""
    with db:
        db.execute('UPDATE datapoints SET downloaded_bytes=NULL')
        db.execute(f'UPDATE {PROGRESS} SET datapoint_id=(SELECT MIN(id) FROM datapoints) WHERE singleton=1')
        db.execute("DELETE FROM settings WHERE key='download_directory'")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=clss.ROOT / 'downloads')
    parser.add_argument('--limit', type=int, default=0, help='Complete at most this many files this run; 0 means all')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--status', action='store_true', help='Show results without downloading')
    mode.add_argument('--fresh', action='store_true', help='Reset download tracking and start from the first tile; keep existing files')
    args = parser.parse_args()
    if args.limit < 0:
        parser.error('--limit must be nonnegative')
    db = clss.connect()
    try:
        clss.validation_summary(db)
        if clss.current(db, 'datapoint__next_to_validate') is not None:
            print('Validation is not complete. Run validate.py first.')
            return
        if args.status:
            clss.download_summary(db)
            return
        with clss.single_runner():
            if args.fresh:
                reset_progress(db)
                print('Download tracking reset. Existing files will be checked and reused.', flush=True)
            clss.download_summary(db)
            datapoint = clss.current(db, PROGRESS)
            if datapoint is None:
                print('All confirmed files have been recorded as downloaded.')
                return
            directory = args.directory.expanduser().resolve()
            previous = clss.setting(db, 'download_directory')
            if previous:
                previous_path = Path(previous)
                if not previous_path.is_absolute():
                    previous_path = clss.ROOT / previous_path
                if previous_path.resolve() != directory:
                    raise RuntimeError(f'Resume using the original download directory: {previous_path}')
            directory.mkdir(parents=True, exist_ok=True)
            try:
                stored_directory = directory.relative_to(clss.ROOT.resolve())
            except ValueError:
                stored_directory = directory
            clss.set_setting(db, 'download_directory', stored_directory)
            completed = 0
            while datapoint is not None:
                if datapoint['http_code'] == 200:
                    print(f'Downloading GKOT_{datapoint["suffix"]}.laz', flush=True)
                    size = download_one(db, datapoint, directory)
                    with db:
                        db.execute('UPDATE datapoints SET size_bytes=?,downloaded_bytes=? WHERE id=?',
                                   (size, size, datapoint['id']))
                        clss.advance(db, PROGRESS, datapoint['id'])
                    completed += 1
                    clss.download_summary(db)
                elif datapoint['http_code'] == 404:
                    with db:
                        clss.advance(db, PROGRESS, datapoint['id'])
                else:
                    raise RuntimeError('Unexpected unvalidated datapoint; run validation first.')
                datapoint = clss.current(db, PROGRESS)
                if args.limit and completed >= args.limit and datapoint is not None:
                    print('Run limit reached. Run the same command again to resume.')
                    return
            print('Download complete.', flush=True)
            clss.download_summary(db)
    finally:
        db.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped. Run download.py again with the same directory to resume.')
    except (clss.RequestFailed, RuntimeError, sqlite3.Error) as error:
        raise SystemExit(str(error))
