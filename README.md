# CLSS GKOT validator and downloader

Small, standard-library Python scripts for the 2023–2025 Slovenian LiDAR
point clouds. Requirements: **Python 3.10+ on macOS or Linux**. No packages
need to be installed.

Validation is complete in **`db/clss.sqlite3`**: 21,430 unique suffixes,
**21,429 confirmed URLs** and one missing tile, **`575_189`**. The validation
cursor is NULL, and the download cursor remains at ID 1. You can export or
download immediately. Your validation results have been preserved.

Only the README and the three main scripts live at the root:

```text
README.md
validate.py
download.py
export.py
db/          Validated SQLite database and process lock
lib/         Shared Python helpers
scripts/     Optional background validation launcher
tests/       Offline tests
logs/        Validation log and PID
downloads/   Default point-cloud destination
exports/     Default validated URL export destination
archive/     Local unrelated files, excluded from Git
```

Output directories are created when needed. All default paths are relative
to the project folder, regardless of the shell's current directory.

## 1. Check validation or resume an unfinished run

The current database has already completed validation. Check its results:

```sh
python3 validate.py --status
```

For a fresh database or an unfinished validation, use the optional background
launcher from this folder:

```sh
./scripts/start_validate.sh
```

It continues after the terminal closes. Check progress with:

```sh
python3 validate.py --status
tail -f logs/validate.log
```

Ctrl+C exits `tail` without stopping the background validator. At completion,
the log says `Validation complete.` and the validation cursor becomes NULL.
At 1.2 seconds between requests, a clean run takes roughly seven hours;
retries and region searches add time.

For a foreground run instead:

```sh
python3 -u validate.py
```

The same command resumes after an interruption or error. A process lock
prevents simultaneous validation/download runs. An exhausted retry stops on
the current tile; inspect the last log message before resuming.

To deliberately revalidate every tile:

```sh
python3 validate.py --fresh
```

`--fresh` clears HTTP statuses and validated file sizes, retains URL leads,
and sets the validation cursor to ID 1 before making HEAD requests. Existing
download tracking and files are preserved. Until validation completes again,
downloads and exports remain blocked. For an ordinary resume, omit `--fresh`.

## 2. Export validated URLs

After validation completes:

```sh
python3 export.py
```

Only HTTP 200 URLs are exported, alphabetically:

- `exports/validated-urls.csv`: one URL per row, without a header.
- `exports/validated-urls.txt`: one line of comma-separated URLs.

Use `--directory /another/path` to choose a different export folder.

If downloads are already complete, the script asks before exporting anyway.
`python3 export.py --yes` skips that question. Export does not download files.

## 3. Download GKOT files

Choose a destination with enough space for the dataset. The earlier sample
suggested several terabytes nationally; validation records actual file sizes
from HEAD responses and the downloader reports their sum when available.

Try one confirmed file first:

```sh
python3 download.py --directory /path/to/large/drive/GKOT --limit 1
```

Then resume all remaining files using the **same directory**:

```sh
python3 download.py --directory /path/to/large/drive/GKOT
```

The default destination, when `--directory` is omitted, is `downloads/` beside
the scripts. `python3 download.py --status` only displays results.

The downloader refuses to start before validation finishes. It skips tiles
that returned 404 in every region. After every successful file it prints the
completed/remaining counts, downloaded bytes, average file size, and estimated
total only when some validated sizes are missing. With complete HEAD sizes,
it instead shows the validated byte total, byte-weighted percentage and
remaining bytes. The current validated total is **8.47 TB**. Progress updates
after each completed file; an unfinished `.part` file is not counted yet.

Files are written to `.part` paths, checked for length and LAS/LAZ signature,
and checked against the server's SHA-256 checksum when supplied. Only then
does the final filename appear and the download cursor advance. An interrupted
file is restarted from the beginning; already completed files are retained.
An existing final file is accepted only if its size matches validation and
its LAS/LAZ signature is present. Unexpected existing files are left intact.

Keep the database in `db/` and resume with the same download directory.
Destinations inside the project are stored relatively, so the project can be
moved or renamed together with its downloads. External destinations stay
absolute and continue to point to the same drive.
The database tracks completed files; deleting them does not reset that
progress automatically.

To reset download tracking and restart at ID 1:

```sh
python3 download.py --fresh
```

`--fresh` clears completed-download counters and the saved destination,
while preserving validation and file sizes. Existing files in the selected
destination are checked and reused; no files are deleted. A different
destination may be supplied with `--directory`. `--fresh` and `--status` are
mutually exclusive in both scripts: a status check never resets anything.

## Database

`db/clss.sqlite3` has the requested tables:

| Table | Contents |
|---|---|
| `regions` | `id` and `placeholder_url`; the 12 base-region templates using `$1` |
| `datapoints` | `id`, unique indexed `suffix`, nullable `url` and `http_code` |
| `datapoint__next_to_validate` | The next datapoint ID, or NULL when complete |
| `datapoint__next_to_download` | The next datapoint ID, or NULL when complete |

The two cursor tables each have a fixed `singleton=1` primary key to enforce
one row. Results and cursor changes are committed in one transaction.

Two extra datapoint columns, `size_bytes` and `downloaded_bytes`, support file
checks and progress totals. A small `settings` table persists server retry
deadlines and the chosen download directory.

The validator tries the seeded URL first, including an annex URL if that is
the selected survey. If it returns 404, it tries the 12 base regions. Only a
successful HEAD response is recorded as 200, and only exhausting the regions
with 404 responses is recorded as a final 404. HTTP 429 and temporary server
errors are retried up to five times, respecting `Retry-After`. Unexpected
responses or exhausted retries stop without advancing the cursor. A terminal
error's HTTP code may be recorded for diagnosis; 429 is not stored as a final
tile result.

## Files to share

Required files to share:

- `db/clss.sqlite3`: the database, including completed validation results.
- `validate.py`, `download.py`, `export.py`: the three workflows.
- `lib/clss.py`: shared database, pacing and reporting helpers.
- `README.md`: these instructions.

Optional supporting files are `scripts/start_validate.sh` and
`tests/test_tools.py`. The supplied SQLite database is the data source;
there is no separate seed CSV or database-builder script. Keep a copy of it.
The current package does not depend on `archive/` or `logs/`. Obsolete
generated archives and logs have been removed. Unrelated existing files
remain under `archive/`, excluded from Git.

Folder-level `.gitignore` files exclude downloaded point clouds, logs, local
archives, process locks and Python caches without adding another root file.
The validated SQLite database is retained for sharing. URL exports can be
regenerated at any time with `python3 export.py`.

To run the offline tests:

```sh
python3 -B -m unittest discover -s tests -v
```

## Data sources and URL selection

The suffixes come from the official JGP `LISTI CLSS` grid (`CLSS_mreza.zip`),
which contains 21,430 unique entries. URLs come from the public CLSS map's
tile catalogue, supplemented with 100 URLs found by HEAD checks across the
base regions. `575_189` is the one suffix without a seeded URL; the completed
validation confirmed HTTP 404 in all twelve base regions.

Overlapping surveys are reduced to one URL per suffix. The seed chooses the
newest published capture date (the latest date in a published date range).
Ties prefer a base region over an annex, then use URL order for determinism.
The resulting selected URLs are stored in `db/clss.sqlite3`. Use the
`--fresh` options to restart an existing database's workflows; the database
is never silently recreated or replaced.

Official sources:

- [GURS remote sensing overview and CLSS grid guidance](https://www.e-prostor.gov.si/podrocja/drzavni-topografski-sistem/daljinsko-zaznavanje/)
- [JGP catalogue](https://ipi.eprostor.gov.si/jgp/): Ostalo → Koordinatne mreže listov → Mreže listov in sekcij → LISTI CLSS
- [Stable grid download-metadata endpoint](https://ipi.eprostor.gov.si/jgp-service-api/display-views/groups/132/files/301)
- [CLSS public map](https://clss.si/)
- [Maribor technical report](https://assets.flycom.si/clss/tehnicna_porocila/Maribor_porocilo.pdf)

Source snapshots were collected on 5 November 2026. No credentials, third-party
dependencies, or background services are required to use this package.
