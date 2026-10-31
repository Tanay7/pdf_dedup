# DupeFinder

One Python script that finds duplicate and near-duplicate **PDFs and photos**
by their content, not their names. Supported photo formats:
- JPG, PNG and HEIC/HEIF;
- WebP, AVIF, GIF, BMP and TIFF;
- camera RAW files: DNG, Canon CR2/CR3, Nikon NEF, Sony ARW, Fujifilm RAF,
  Olympus ORF, Panasonic RW2, Pentax PEF, Samsung SRW and others.

RAW files are compared through the full-size preview the camera stores
inside them. A RAW file and its JPEG twin are grouped, but neither is
pre-selected. It scans folders or whole
drives such as `C:\` and `D:\` and catches these cases:

- exact copies;
- re-saved or edited PDFs;
- resized, recompressed or re-rotated photos;
- duplicate scans, including a scan kept both as a JPG and inside a PDF.

It also groups burst shots, suggests the sharpest one to keep, and flags blurry
or badly exposed photos. Every match has a similarity score. You review the
results in a browser report and move the extra copies to the Recycle Bin.

- **Nothing is deleted permanently.** Files go to the Recycle Bin. On USB
  sticks and network drives, or drives where the Recycle Bin is turned off or
  too small, they go to a `_duplicates_review` folder on the same drive
  instead. `--undo-review` moves those back.
- **Nothing leaves your computer.** The tool blocks every network connection
  that isn't to your own PC, including any its libraries might try. The report
  is served only on `127.0.0.1` and loads nothing from the web. All packages
  ship inside the offline zip, so installing needs no internet either.
- **System and program files are left alone.** These folders are skipped by
  default:
  - Windows, Program Files and ProgramData;
  - every user's AppData;
  - `$Recycle.Bin`, System Volume Information and Recovery;
  - `.git`, `node_modules`, `venv`, `.venv` and `site-packages`, wherever they
    are.

  Links and junctions aren't followed. OneDrive online-only files are
  skipped without being downloaded.

The full design, scoring rules and test plan are in [docs/design.md](docs/design.md).
Decisions made while building it are in [docs/BUILD_NOTES.md](docs/BUILD_NOTES.md).

## Install (Windows 10/11, nothing else needed)

1. Download `DupeFinder-<version>-windows.zip` from the repository's
   **Releases** page or from the latest **Actions** run (artifact
   *DupeFinder-windows*).
2. Unzip it anywhere, for example to `C:\Tools\DupeFinder`. GitHub's Actions
   download is a zip around the zip; unzip both.
3. Double-click **`run.bat`**.

The zip carries its own private copy of Python with every package
pre-installed, in the `python` folder. You don't need Python on your PC, and
nothing is downloaded or installed. If Windows shows "Windows protected your
PC" the first time, click **More info**, then **Run anyway**. That warning
appears for any batch file downloaded from the internet.

People who prefer their own Python 3.12–3.14 can build the smaller
`-windows-wheels.zip` with `python tools/build_offline_zip.py` and run its
`install.bat`.

## Use

Double-click **`run.bat`**. A small window opens:
- **Add folders:** click **Add folder...** for any folder, or a drive button
  (`C:\`, `D:\`...) to add a whole drive.
- **Choose file types:** tick **PDFs** and/or **Photos**.
- **Start:** press **Scan**.

You can also give folders directly from a Command Prompt; use quotes around
paths with spaces:

```
run.bat C:\ D:\
run.bat D:\Photos --types image
run.bat "C:\Users\Tan\Documents\Scans" E:\Backup --prefer D:\Photos
run.bat D:\Docs --types pdf --min-score 90 --report-only
run.bat --undo-review
```

You can also run the script directly with `python dupefinder.py ...`, or run
`python -m dupefinder ...` from the folder that contains it.

The script scans the folders, then opens the report in your browser. Leave the
console window open while you review, because the report moves files through it.
Close the report with its **Quit** button or press Ctrl+C in the console.

### The report

- **Summary:** files scanned, duplicate groups, bursts, space you could free,
  skipped items and scan time.
- **Filters:** PDFs or photos, match type (identical, same content, similar,
  burst, related, possibly poor), minimum score and folder, plus sorting by
  space saved.
- **Groups:** each group shows every file with its thumbnail, size,
  resolution or page count, date, its score against the kept copy, a
  Keep/Trash toggle, and Open and Show in folder buttons.
- **Suggestions:** Keep/Trash choices start from the keep rules (see Keep
  rules).
- **Bursts:** shots are listed in time order, with each shot's sharpness
  relative to the best one.
- **Photo view:** clicking a photo opens it full size, with 100% crops of its
  sharpest and blurriest areas.
- **Moving files:** the **Move N files to Recycle Bin** button asks for
  confirmation. The script then checks each file again before moving it:
  - the file must still be part of the scan;
  - its group must keep at least one file;
  - its size and date must be unchanged since the scan;
  - it must not be hard-linked.

  Any file it refuses shows the reason on its row. Every move is logged to
  `actions.csv` in the cache folder.

`--report-only` writes a static HTML report and a CSV instead, with no server
and no move button.

### Scores

| Label | Meaning |
|---|---|
| 100% identical file | Byte-for-byte the same |
| 100% same text | Same words, different file (e.g. re-saved PDF) |
| 100% same pixels | Same image, different metadata or format |
| N% similar text | Share of 5-word chunks two PDFs have in common (shown from 85%) |
| N% visual match | How alike two photos or scanned PDFs look (photos: at most 8 of 64 hash bits differ). "(rotated)", "(mirrored)" or "(cropped)" marks a copy found in that form; mirrored and cropped ones are never pre-selected |
| Burst · N shots | Same camera, taken seconds apart, looking alike |
| Related | A photo matches a page inside a scanned PDF; never pre-selected |

### How it decides

- **Photos:** each photo gets a 64-bit perceptual hash, made from its
  brightness pattern at 32×32. The same calculation also gives the hashes
  of the photo turned 90°, 180° or 270° and mirrored, plus one hash of its
  centre.

  That finds the following, even after renaming, re-saving or a change of
  format:
  - resized and recompressed copies;
  - copies rotated in their pixels;
  - mirrored copies;
  - square and centre crops.

  The hash ignores colour, so a separate colour check stops photos with the
  same layout but different colours from matching. Black-and-white versions
  still match their colour originals.
- **Blur:** a photo's sharpness score (0–100) comes from a published
  "re-blur" measure (Crete et al., 2007). It is taken on the sharpest
  quarter of the photo after filtering out sensor noise. So:
  - a sharp subject on a soft background scores well;
  - darkness doesn't count as blur;
  - motion blur is caught;
  - noisy night shots don't pass as sharp.

  Below 45 counts as blurry.
- **Tested on real photographs:** the test suite checks these behaviours
  on real public-domain photos, not just generated ones.

### Keep rules (`--keep auto`)

- **Duplicate photos:** keep the one with the most pixels, then the largest
  file, then a name without copy markers such as "(1)" or "- Copy", then the
  earliest capture date.
- **Bursts:** keep the shot with the highest quality score (sharpness, halved
  if the exposure is flagged).
- **PDFs:** keep a name without copy markers, then the newest file, then the
  one with the most pages, then the shortest path.
- **`--prefer FOLDER`:** files under this folder win over all of the above.

System-folder files, hard-linked files, Related matches and photos that are
only flagged as poor are never pre-selected for the Recycle Bin.

## Options

| Option | Default | Purpose |
|---|---|---|
| `ROOTS` | required | Folders or drives to scan |
| `--types` | `pdf,image` | Limit the scan to `pdf` or `image` |
| `--exclude PATH` | built-in list | Another folder to skip; repeatable |
| `--include-system` | off | Also scan system folders; their files are never pre-selected |
| `--min-score` | 85 | Lowest % for text and scanned-PDF matches |
| `--max-bits` | 8 | Most differing bits (of 64) for a photo visual match |
| `--burst-gap` | 3 | Most seconds between shots in a burst |
| `--blur-cutoff` | 45 | Sharpness score (0–100) below which a photo is flagged blurry; the report shows your photos' range to help tune it |
| `--quick` | off | Skip the search for turned, mirrored and cropped copies (faster on huge libraries) |
| `--dark-cutoff` / `--clip-cutoff` | 40 / 25 | Exposure flags: median brightness, and the % of pixels near pure black or white |
| `--ocr` | off | Read scanned PDFs with a locally installed Tesseract |
| `--max-pages` | 30 | Pages rendered per scanned PDF |
| `--ai` | off | AI similarity from a local model file (see below) |
| `--keep` | `auto` | `auto`, `newest`, `oldest`, `largest`, `highest-res` or `shortest-path` |
| `--prefer PATH` | none | Prefer keeping files under this folder; repeatable |
| `--workers N` | CPU cores − 1 | Worker processes |
| `--timeout` | 60 | Most seconds spent on one file |
| `--cache PATH` | `%LOCALAPPDATA%\DupeFinder` | Cache, thumbnails, logs and the action log |
| `--rescan` | off | Ignore cached fingerprints |
| `--report-only` | off | Static HTML and CSV; no server and no move button |
| `--csv PATH` | none | Also export all groups to CSV |
| `--port N` | random | Port for the local server |
| `--no-browser` | off | Don't open the browser automatically |
| `--pick` | off | Choose folders in a window (what `run.bat` does with no arguments) |
| `--undo-review` | off | Move files from `_duplicates_review` folders back |

Later scans reuse the fingerprints cached in `cache.sqlite`, so they only
process new or changed files.

## Optional extras

- **OCR (`--ocr`):** install Tesseract for Windows yourself, for example the
  UB Mannheim build. DupeFinder finds `tesseract.exe` on `PATH` or in its
  usual install folder. It then reads scanned PDFs and compares them by text
  as well.
- **AI similarity (`--ai`):** this needs `torch` and `open_clip_torch`
  installed in `.venv`, and a CLIP weights file at
  `%LOCALAPPDATA%\DupeFinder\models\clip.pt` (or give `--ai-model PATH` and
  `--ai-arch`). These aren't included in the zip because they add about
  1 GB. DupeFinder never downloads a model; without one, it says so and
  carries on.

## Development

```
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q                      # the design's test plan on generated files
python tests/make_test_files.py OUT      # just build the test folder (add --check)
node dev/check_report.js                 # browser checks of the report page (Playwright)
python tools/build_offline_zip.py --portable C:\Python313   # portable Windows zip (run on Windows)
```

GitHub Actions runs the tests on Windows (Python 3.12–3.14) and Linux. It then
builds the portable zip, unzips it on Windows with no other Python available,
and scans the generated test files with only what is inside. Pushing a `v*` tag attaches the zip to a
release.

## Notes

- **Platforms:** built for Windows 10/11. It also runs on macOS and Linux,
  where files go to the system Trash, and mounted drives under `/media`,
  `/run/media`, `/mnt` or `/Volumes` use the review folder instead.
- **Phone photos:** copy them to the PC as original files, by cable or the
  photo app's "download originals". Copies shared through messaging apps
  often lose their capture time, which the burst check needs.
- **Quality check limits:** it can't tell a shaky shot from intentional
  motion blur, fog or a noisy night scene. It doesn't judge closed eyes or
  framing.
- **PyMuPDF licence:** PyMuPDF is AGPL-3.0, which is fine for personal use.
