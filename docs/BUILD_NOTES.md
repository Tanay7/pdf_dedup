# Build notes (work paused before coding)

Status: planning finished, **no code written yet**. The design in
[design.md](design.md) is the spec; this file records the decisions made
while planning, so the build can resume without redoing them.

## Deliverable shape

- One file, `dupefinder.py`, at the repo root. It runs as
  `python dupefinder.py C:\ D:\` and, from its folder, as
  `python -m dupefinder C:\ D:\` (the design's typical runs).
- The design's modules (scanner, cache, pdf_pipeline, image_pipeline,
  matcher, grouping, safety, server) become clearly marked sections of that
  file; the report's HTML/JS/CSS is embedded as a string constant.
- Alongside it: `tests/make_test_files.py` (generator), `tests/test_*.py`
  (pytest), `requirements.txt`, `README.md` (Windows setup and use), and
  `.github/workflows/tests.yml` running the tests on `windows-latest` and
  `ubuntu-latest` (Windows can't be run in the build container).

## Environment findings

- Installed and working here: PyMuPDF 2.0 (import as `pymupdf`; `fitz` is
  deprecated, fall back to it only for old versions), Pillow 12,
  pillow-heif 1.8, ImageHash 4.3, datasketch 2.0, NumPy 2.5
  (`np.bitwise_count` present), opencv-python-headless 5.0, Send2Trash 2.1,
  pytest 9. No Tesseract, no torch.
- datasketch 2.0 `MinHash.hashvalues` is `uint32`; store signatures as
  uint64 bytes and convert consistently.
- **LSH tuning:** `MinHashLSH(threshold=0.8)` with default weights picks
  b=9, r=13, which finds only ~69% of pairs at Jaccard 0.85, so too many
  real matches would be missed. Pick (b, r) explicitly so recall at
  `--min-score` is at least 98% (e.g. b=16, r=8 gives 99.4% at 0.85); the
  exact Jaccard check removes the extra false candidates.

## Decisions beyond the design

Safety:
- `send2trash` silently deletes permanently when the Recycle Bin is
  disabled for a drive (`NukeOnDelete`, or the `NoRecycleFiles` policy) or
  the file exceeds the bin's `MaxCapacity`. On Windows, read
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume\{GUID}`
  (GUID via `GetVolumeNameForVolumeMountPointW`) and send such files to
  `_duplicates_review` instead. If the check itself fails, fall back to the
  defaults (bin enabled).
- Never fall back to deletion when a move fails; report the error.
- Always skip `_duplicates_review` folders and the cache folder (the
  thumbnails are JPEGs and would otherwise be scanned).
- Skip zero-byte files (they'd all be "identical"); count them.
- Symlinked files are skipped as well as symlinked or junctioned folders.
- On Windows `DirEntry.stat()` has zero `st_ino`/`st_dev`/`st_nlink`, so
  call `os.stat` per candidate file. Check the OneDrive attributes from
  `DirEntry.stat()` first, so placeholders are never opened.
- Pass plain paths to send2trash (it handles long paths itself); use the
  `\\?\` prefix for our own `open`, `os.stat`, `pymupdf.open`, moves and
  `makedirs`.
- Removable or remote drives (and RAM disk or unknown types) use the
  review folder; CD-ROM is refused. On POSIX, treat mounts under
  `/media`, `/run/media` and `/Volumes` as removable.
- Server: `127.0.0.1`, random port, token on every request (`?token=`
  for GETs, `X-Token` header for POSTs), check the `Host` header (against
  DNS rebinding) and the `Origin` header, answer no `OPTIONS` request,
  shut down after 30 idle minutes or on Quit.

Matching:
- Pixel fingerprints are computed for photo pairs within **4 bits** (not
  only identical hashes) and with equal rotated dimensions. JPEGs are
  decoded with `draft()` but PNGs aren't, so a PNG converted from a JPG can
  differ by a bit or two, and the test plan needs "100% same pixels"
  there.
- Pixel fingerprint = SHA-256 of `f"{w}x{h}:{mode}"` plus the pixels after
  `exif_transpose`. RGBA is reduced to RGB only when alpha is fully opaque.
- "Flat" images and pages (grey std below 2 on the 32×32 copy, or a hash
  with fewer than 8 or more than 56 set bits) are left out of visual and
  cross-type candidates. Otherwise blank pages and solid-colour images
  would all match each other.
- Scanned-PDF score: per page, the best match in the other PDF; matched
  when within `--max-bits`. Compute it in both directions and take the
  minimum, so a PDF that repeats one page doesn't score 100%. When both
  PDFs are fully scanned, extrapolate past the `--max-pages` window
  (`est = matched / min(hA, hB) * min(nA, nB)`,
  `score = mean × est / max(nA, nB)`). For mixed PDFs use
  `matched / max(nA, nB)`, so a shared blank or cover page can't group
  unrelated PDFs.
- Text: tokens are `\w+` after NFKC, lowercase, header/footer removal
  (lines on more than half the pages when there are at least 3 pages),
  bare page numbers dropped, and hyphenation re-joined. The text
  fingerprint is SHA-256 of the normalized string. Similarity needs at
  least 5 shingles. Shingle sets for verification are recomputed in the
  worker pool only for PDFs in candidate pairs (or keeper/member pairs).
- Bursts: chain shots within the gap that are ≤16 bits apart (or AI ≥0.85).
  Shots with the same camera and the exact same capture time that are
  already visual duplicates are copies, not burst frames: no burst link,
  and the chain continues. An empty camera name counts as its own camera.
- Quality score = sharpness × 0.5 when an exposure flag is set. Sharpness =
  mean Laplacian variance of the 4 sharpest of 16 tiles, on a copy at most
  1,024 px on its long side. Store the tile values in `meta` for the
  sharpest/blurriest 100% crops. Flags: blurry (< `--blur-cutoff`), dark
  (median < 40), clipped (> 25% of pixels ≤5 or ≥250). Add
  `--dark-cutoff`, `--clip-cutoff` and `--ai-min-score`, since the design
  says every cutoff can be changed. The default blur cutoff is still to be
  chosen; the report shows sharpness percentiles to help tune it.

Grouping and keep rules:
- One union-find over all links, Related included. Mixed groups (PDF and
  photo) take their keeper from the kind with more members (ties: PDF), so
  copies within that kind still get suggestions, and the other kind shows
  as Related (never pre-selected).
- Label order for "score against the kept copy": identical > same text >
  same pixels > similar text > burst (same chain) > visual > scanned visual
  > AI > related > "linked through X" (from a BFS tree rooted at the
  keeper).
- Group type: burst if it has any burst link, else related if any related
  link, else identical if every label is identical, else same (all
  100%-type labels), else similar.
- "Possibly poor shots" = flagged photos not in any group, never
  pre-selected. They may still be moved if the user chooses (the
  keep-one-copy rule applies only to real groups); the confirm dialog says
  how many have no other copy.
- Hard-linked (`st_nlink` > 1), system-folder and Related files are never
  pre-selected; the server refuses hard-linked files.

Work pool:
- Per-file 60 s timeout needs killable workers: N `multiprocessing.Process`
  workers, each with its own `Pipe`, polled with
  `multiprocessing.connection.wait`. An overdue worker is killed, the file
  gets a timeout error, and a new worker starts. Use one pipe per worker,
  not a shared queue, because killing a process mid-write can corrupt a
  shared queue's lock. All worker code is top-level, for Windows spawn.
- Each task does the file SHA (when its size collides), then the analysis.
  Pixel fingerprints, shingle sets and AI embeddings are separate later
  passes, run only where needed.

## Report data contract (embedded as `window.DF_DATA = __DF_DATA__;`)

The same page serves both modes; Python replaces the single placeholder
with JSON (escape `<` as `\u003c`).

```
{ mode: "server"|"static", token, generated,
  summary: { files_scanned, pdfs, photos, groups, bursts, poor,
             space_to_free, scan_seconds, roots[],
             settings{min_score,max_bits,burst_gap,blur_cutoff,dark_cutoff,clip_cutoff},
             sharpness_percentiles{p10,p50,p90}|null },
  skipped: { counts{label: n}, folders[{path,reason}], errors[{path,error}], protected[path] },
  groups: [{ id, type: identical|same|similar|burst|related|poor,
             kind: pdf|image|mixed, title, space, min_score|null, burst_shots|null,
             members: [{ fid, name, folder, path, kind, ext, size, dims|null, pages|null,
                         date, date_kind: taken|modified, keeper, suggest: keep|trash,
                         label, label_kind, score|null, detail,
                         sharpness|null, rel_sharpness|null, exposure|null,
                         flags[], locked, lock_reason,
                         thumb/file_url/folder_url (static only), crops }] }] }
```

Server endpoints (all need the token): `GET /`, `GET /thumb/<fid>`
(256 px JPEG, cached), `GET /view/<fid>` (HEIC converted to JPEG, PDF
served inline), `GET /crop/<fid>/sharp|blur`, `POST /api/move {ids}` →
`{results:[{fid,ok,action:recycle|review,dest,reason}]}`,
`POST /api/open {fid,reveal}`, `POST /api/quit`.

The page must load nothing from the internet: no CDNs and no web fonts.

## Test files (generator layout)

```
pdf/report.pdf                 5 pages, ~2,500 words, header/footer + "Page N of 5"
pdf/archive/report renamed.pdf byte copy                      → identical
pdf/report_resaved.pdf         new title/author, re-saved     → same text
pdf/report_edited.pdf          one ~70-word paragraph changed → similar > 90%
pdf/report_cover.pdf           extra cover page               → similar > 85%
pdf/other_topic.pdf            same vocabulary, other text    → not grouped
pdf/scan.pdf                   3 image-only pages
pdf/scan_recompressed.pdf      same pages, JPEG q≈35          → visual, grouped
pdf/protected.pdf (+ protected_copy.pdf byte copy) → flagged; identical via check 1 only
pdf/corrupt.pdf, photos/corrupt.jpg                → logged, scan continues
photos/beach.jpg               EXIF time, Make/Model, GPS
photos/backup/beach copy.jpg   byte copy                      → identical
photos/beach_small.jpg         50 %, q70                      → visual ≥ 90 %
photos/beach_exif_edited.jpg   APP1 spliced: new date, no GPS → same pixels
photos/beach.png               from beach.jpg                 → same pixels
photos/beach.heic              from beach.jpg                 → visual
photos/rotated_flag.jpg + rotated_pixels.jpg                  → visual, grouped
photos/burst/burst_1..5.jpg    0.4 s apart, subsec; 2 and 4 motion-blurred → one burst, sharpest kept
photos/portrait_bokeh.jpg      sharp centre 50 % × 50 %       → not flagged
photos/blurry.jpg, dark.jpg, overexposed.jpg                  → possibly poor, not pre-selected
photos/scene_minute_a/b.jpg    60 s apart                     → not a burst
photos/scan_page.jpg           page 1 image of scan.pdf       → related, never pre-selected
photos/unrelated_1..3.jpg                                     → never grouped
photos/long_twin.jpg + long/<nested past 260 chars>/deep_copy.jpg → identical
links/original.jpg + links/hardlink.jpg (os.link)             → not duplicates
links/real_dir/inner.jpg + links/dir_link (symlink/junction)  → seen once
onedrive/online_only.pdf       copy of report.pdf; tests fake the attributes → skipped, counted
projects/node_modules/pkg/beach_dup.jpg                       → skipped; with --include-system grouped, never pre-selected
```

Server tests: trash every copy (refused), file edited after the scan
(refused), USB drive (fake drive type → `_duplicates_review`), undo-review,
and an unchanged rescan served from the cache. Point `XDG_DATA_HOME` at a
temp folder so Linux tests use a throwaway trash.

## Plan for agents when resuming

Write `dupefinder.py` in one context, because its sections share data
structures. Two parts can go to agents in parallel, since each has its own
contract:

1. The report page (`report_template.html`) built against the data
   contract above, checked with Playwright screenshots in both modes,
   then embedded into the script.
2. `tests/make_test_files.py` with the layout above, deterministic, with
   self-checks (hash distances, sharpness ordering).

Then one independent review of the finished script, and the Windows CI
run.

## Build order (from the design)

1. Scanner, cache, file fingerprints, CSV → 2. photo checks → 3. bursts and
quality → 4. PDF text checks → 5. scanned PDFs and related → 6. grouping and
keep rules → 7. server and read-only report → 8. moves, checks, review
folders, log, undo → 9. whole-drive hardening → 10. tests, then a trial
folder → 11. OCR and AI extras.
