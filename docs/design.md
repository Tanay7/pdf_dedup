# PDF and Photo Duplicate Finder: Design

Oct 22, 2026 · \@Tan

One Python script finds duplicate PDFs and photos by content across
whole drives such as C:\\ and D:\\. It scores every match, picks the
sharpest shot in bursts of near-identical photos, and moves the extras
you choose to the Recycle Bin from a browser report. Nothing is deleted
permanently, and nothing leaves your computer.

## Scope

The tool finds files that are the same or nearly the same in content,
whatever their names or folders. It covers PDFs and photos and runs
entirely on your own computer.

It will:

-   Scan chosen folders or whole drives, such as C:\\ and D:\\, in one
    pass.

-   Find exact copies, re-saved copies, edited versions, resized photos
    and duplicate scans.

-   Group burst shots taken seconds apart and suggest the sharpest one
    to keep.

-   Flag blurry or badly exposed photos for you to review.

-   Show a similarity score for every match.

-   Move the files you choose to the Recycle Bin from a browser report.

It won\'t:

-   Delete anything permanently, or change any file it doesn\'t move.

-   Upload anything; the report is served only on your own computer.

-   Handle other file types, such as Word documents or videos, in the
    first version.

It targets Windows 10 and 11 with Python 3.12 or newer, and should work
on macOS and Linux with small changes. Photos from a phone gallery are
scanned once they\'re copied or synced to the PC.

## How it works

![scan-to-Recycle-Bin pipeline · PDF and photo
branches](pipeline.png){width="6.0in"
height="6.946428258967629in"}

scan-to-Recycle-Bin pipeline · PDF and photo branches

PDFs and photos share every stage except fingerprinting. Files move only
after you confirm them in the report and the safety checks pass.

## Detection checks

Each file runs through cheap checks first and costly ones only when
needed. A match at an earlier check makes the later ones unnecessary for
that pair.

  ---------------------------------------------------------------------------------
  \#    Check         Applies   How it works            Catches            Cost
                      to                                                   
  ----- ------------- --------- ----------------------- ------------------ --------
  1     File          PDFs,     Group files by size,    Exact copies under Low
        fingerprint   photos    then SHA-256 the bytes  other names or     
                                of same-size files only folders            

  2     Text          PDFs with Extract the text,       Re-saved or        Medium
        fingerprint   text      normalize it, then      re-exported        
                                SHA-256 the result      copies,            
                                                        metadata-only      
                                                        changes            

  3     Pixel         Photos    SHA-256 of width,       Copies with an     Medium
        fingerprint             height and pixels after edited date,       
                                rotation; only for      stripped location  
                                pairs whose perceptual  or other metadata  
                                hashes are identical                       

  4     Text          PDFs with 5-word chunks, a        Edited versions,   Medium
        similarity    text      128-value MinHash       added cover pages, 
                                signature, an LSH       small revisions    
                                lookup, then an exact                      
                                overlap check on                           
                                candidates                                 

  5     Visual        Photos,   64-bit perceptual hash  Resized,           Medium
        similarity    scanned   (pHash), compared by    recompressed or    
                      PDF pages the number of differing format-converted   
                                bits                    copies, light      
                                                        colour edits       

  6     Burst         Photos    Same camera, taken      Quick-fire shots   Low
        grouping                within 3 seconds of the of the same scene  
                                previous shot, and                         
                                visually close                             

  7     Quality score Photos    Sharpness of the        Blurry, very dark  Low
                                sharpest image tiles,   or blown-out shots 
                                plus an exposure check                     

  8     AI similarity Photos    Embeddings from a small Bursts with        High
        (optional)              CLIP-style model,       movement, crops,   
                                compared by cosine      heavier edits      
                                similarity                                 

  9     Cross-type    Photos    Each photo\'s hash      A scan saved both  Low
        match         vs.       against each page hash  as a JPG and       
                      scanned                           inside a PDF       
                      PDF pages                                            
  ---------------------------------------------------------------------------------

Text normalization: Unicode NFKC, lowercase, rejoin words hyphenated
across line breaks, collapse whitespace, and drop lines that repeat on
most pages, such as headers and footers, plus bare page numbers.

A PDF counts as scanned when it averages under 20 extracted characters
per page. Its first 30 pages are then rendered at 72 dpi and hashed
instead; with \--ocr they\'re also read with Tesseract and sent through
checks 2 and 4. PDFs with both kinds of pages get both kinds of checks,
and password-protected PDFs get check 1 only and are flagged in the
report.

Photos are decoded once, at about 1,024 pixels wide with their rotation
applied, and that copy feeds checks 5, 6 and 7.

## Similarity scores

Every file in a group shows one score against the copy being kept, using
the strongest check that matched. Scores are comparable only within the
same label, and every cutoff can be changed.

  --------------------------------------------------------------------------
  Label       Meaning                How it\'s computed       Shown when
  ----------- ---------------------- ------------------------ --------------
  100%        Byte-for-byte the same File fingerprints match  Always
  identical                                                   
  file                                                        

  100% same   Same words, different  Normalized-text          Always
  text        file                   fingerprints match       

  100% same   Same image, different  Pixel fingerprints match Always
  pixels      metadata                                        

  N% similar  Share of 5-word chunks Exact Jaccard: shared    N ≥ 85
  text        the two files have in  chunks ÷ all distinct    
              common                 chunks in either file    

  N% visual   How alike two photos   (64 − differing bits) ÷  At most 8
  match       look                   64                       differing
  (photos)                                                    bits, so N ≥
                                                              87.5

  N% visual   How alike the pages    Mean score of matched    N ≥ 85
  match       look, adjusted for     pages × matched pages ÷  
  (scanned    page count             the larger page count    
  PDFs)                                                       

  Burst · N   Taken seconds apart on Capture-time gap of 3 s  Always, for
  shots       the same camera and    or less and at most 16   photos with
              look alike             differing bits between   capture times
                                     neighbours               

  N% similar  Same scene or subject, Cosine similarity × 100  N ≥ 92; tune
  content     judged by the AI model                          per model

  Related     A photo matches a page Page-hash match within 8 Always; never
              inside a PDF           bits                     pre-selected
                                                              for trash
  --------------------------------------------------------------------------

A page counts as matched when its best match in the other PDF is within
8 differing bits.

## Matching and grouping

Comparing every pair doesn\'t scale: 10,000 files means about 50 million
pairs. So each check first finds candidate pairs cheaply, then verifies
only those.

How candidates are found:

-   **Fingerprints:** files are bucketed by hash, and files in the same
    bucket are candidates. File fingerprints are computed only for files
    that share a size with another file.

-   **Text similarity:** a MinHash LSH index tuned for an overlap of
    about 0.8 returns candidate pairs. Each pair is checked by
    extracting both texts again and computing the exact overlap, and
    that exact value is the score shown.

-   **Visual similarity:** 64-bit hashes are compared in blocks with
    NumPy (XOR, then bitwise_count). That should handle a library of
    around 100,000 photos in seconds to minutes; measure it during the
    build and switch to a BK-tree index if it\'s too slow.

-   **Bursts:** photos with capture times are sorted by camera and time,
    and consecutive shots within the gap are compared directly.

-   **Pixel fingerprints:** computed only for pairs with identical
    perceptual hashes and dimensions.

How groups form:

Every verified pair above its cutoff becomes a link, and a union-find
structure joins linked files into groups. A file can join through
another member (A matches B, and B matches C), so each member\'s score
is recomputed against the kept copy. If that direct score is below the
cutoff, the row names the member it was linked through, and it\'s never
pre-selected for trash.

Which copy is kept by default (\--keep auto), in priority order:

-   **Duplicate photos:** most pixels, then the largest file (less
    compression), then a name without copy markers such as "(1)" or "-
    Copy", then the earliest capture date.

-   **Burst shots:** the highest quality score (next section), then most
    pixels.

-   **PDFs:** a name without copy markers, then the newest modified
    date, then the most pages, then the shortest path.

-   **Any file under a \--prefer folder** wins over all of these.

Files in system folders (when included), hard-linked files and Related
matches are never pre-selected for trash.

## Burst shots and photo quality

Quick-fire shots are grouped as a burst, every photo gets a quality
score, and the best shot in each burst is pre-selected to keep. Blurry
or badly exposed photos outside bursts are only flagged, never
pre-selected.

**Burst grouping**

-   Read the capture time (EXIF DateTimeOriginal, plus
    SubSecTimeOriginal when present) and the camera (Make and Model).

-   Sort each camera\'s photos by time, and chain shots taken within 3
    seconds of the previous one (\--burst-gap).

-   Keep a shot in the chain only if it looks like its neighbour: at
    most 16 of 64 hash bits differ, or, with \--ai, a similarity of at
    least 85%.

-   Photos without a capture time, such as many screenshots and
    messaging-app downloads, are never grouped as bursts.

Copy the phone gallery to the PC as original files, by cable or the
photo app\'s "download originals" option. Copies shared through
messaging apps often lose their capture time, and the burst check
depends on it.

**Quality score**

  -------------------------------------------------------------------------
  Measure     How it\'s computed                   Flagged when
  ----------- ------------------------------------ ------------------------
  Sharpness   Split the reduced copy into a 4 × 4  Below the blur cutoff
              grid, measure edge contrast in each  (\--blur-cutoff), tuned
              tile (variance of the Laplacian),    on your own photos
              and average the sharpest quarter of  
              tiles                                

  Exposure    Median brightness, and the share of  Median below 40 of 255,
              pixels near pure black or pure white or over 25% of pixels
                                                   clipped; both defaults
                                                   to tune
  -------------------------------------------------------------------------

Scoring only the sharpest tiles means a sharp subject on a deliberately
blurred background still scores well. Within a burst, sharpness is shown
relative to the sharpest shot (100), which is more reliable than a fixed
cutoff.

**In the report**

-   **Bursts** appear in time order with each shot\'s sharpness and
    exposure. The best shot is set to Keep and the rest to Trash.

-   **Possibly poor shots** lists single photos below the blur cutoff or
    with an exposure flag. Nothing there is pre-selected.

-   Tapping a photo opens it full size, with 100% crops of its sharpest
    and blurriest areas, because blur is hard to judge from thumbnails.

**Limits:** the checks can\'t tell a shaky shot from intentional motion
blur, fog or a noisy night scene. They also don\'t judge closed eyes,
expressions or framing, which would need a face model (see Open
questions).

## Whole-drive safeguards

Scanning entire drives adds risks that a single folder doesn\'t have,
and each one gets a specific safeguard.

  -----------------------------------------------------------------------
  Risk                              Safeguard
  --------------------------------- -------------------------------------
  Trashing files that programs rely Skip by default: Windows, Program
  on                                Files, Program Files (x86),
                                    ProgramData, every user\'s AppData,
                                    \$Recycle.Bin, System Volume
                                    Information and Recovery, plus .git,
                                    node_modules, venv, .venv and
                                    site-packages anywhere.
                                    \--include-system scans them, but
                                    their files are never pre-selected.

  One file showing up at two paths  Don\'t follow junctions or symbolic
                                    links (DirEntry.is_junction() needs
                                    Python 3.12). Treat files with the
                                    same volume and file ID (st_dev,
                                    st_ino) as one file, and never offer
                                    hard-linked files (st_nlink above 1)
                                    for trash.

  Folders the account can\'t open   Skip them on permission errors and
                                    list them in the report. No admin
                                    rights are needed.

  Downloading OneDrive online-only  Skip files whose st_file_attributes
  files                             include offline (0x1000), recall on
                                    open (0x40000) or recall on data
                                    access (0x400000), and show how many
                                    were skipped.

  Drives with no Recycle Bin, such  Check the drive type with
  as USB sticks and network drives  GetDriveTypeW. On removable (2) or
                                    remote (4) drives, move files to a
                                    \_duplicates_review folder on the
                                    same drive instead.

  A file changing after the scan    Re-check size and modified time just
                                    before moving, and refuse the move if
                                    either changed.

  Trashing every copy in a group    The server rejects any request that
                                    would leave a group with no kept
                                    file.

  Locked, corrupt or                Give each file at most 60 seconds,
  password-protected files          log any error and carry on. Protected
                                    PDFs get the file fingerprint only.

  Paths longer than 260 characters  Open files with the \\\\?\\
                                    extended-length prefix.

  Other software calling the        Listen only on 127.0.0.1 at a random
  report\'s server                  port, require a random session token,
                                    reject requests from other origins,
                                    accept file IDs rather than paths,
                                    and shut down on exit or after 30
                                    idle minutes.
  -----------------------------------------------------------------------

## Scan cache

Fingerprints live in a SQLite file, by default
%LOCALAPPDATA%\\DupeFinder\\cache.sqlite, so later scans only process
new or changed files.

  -----------------------------------------------------------------------
  Field                   What it holds
  ----------------------- -----------------------------------------------
  path                    Full path; the primary key

  volume_id, file_id      st_dev and st_ino, to spot one file at two
                          paths

  size, mtime_ns          Size in bytes and modified time, to detect
                          changes

  kind                    pdf or image

  file_sha256             File fingerprint; filled only when needed

  content_sha256          Normalized-text or pixel fingerprint

  minhash                 128-value MinHash signature (PDFs with text)

  phash                   64-bit perceptual hash (photos)

  page_hashes             One 64-bit hash per rendered page (scanned
                          PDFs)

  taken_at, camera        Capture time and camera model, for bursts

  sharpness, exposure     Quality measures (photos)

  embedding               Optional AI vector, stored as float16

  meta                    JSON: page count, width × height, characters
                          per page, and flags such as scanned or
                          encrypted

  error                   The last error for this file, if any

  algo_version            Fingerprint version; raising it forces
                          recomputation
  -----------------------------------------------------------------------

Two more tables sit alongside it. scans records each run\'s start time,
roots, options and counts. actions records every move: time, path,
action, destination, group, kept file and file fingerprint, and it\'s
exported as actions.csv after each run.

Rescan rules:

-   Reuse a row when the path, size, modified time and algo_version all
    match.

-   Recompute when any of them differs, or for every file with
    \--rescan.

-   Delete rows for files that weren\'t found during a full scan of
    their folder.

## Report and deletion

After scanning, the script starts a small web server on your computer
and opens the report in your default browser. The script keeps running
until you close it, so the report can act on files.

What the report shows:

-   **Summary bar:** files scanned, duplicate groups, bursts, space you
    could free, skipped items (expandable) and scan time.

-   **Filters:** PDFs, photos or both; match type (identical, same
    content, similar, burst, related, possibly poor); minimum score;
    folder; and sorting by space saved.

-   **Group card:** group number, match type, space to free and one row
    per file.

-   **File row:** thumbnail, name, folder, size, pages or resolution,
    date modified or taken, score against the kept copy, a Keep/Trash
    toggle, and Open and Show in folder buttons.

-   **Bulk actions:** accept all suggestions for exact duplicates, or
    clear all choices.

-   **Action bar:** "Move N files (X GB) to Recycle Bin".

Thumbnails are made on demand at 256 pixels and cached.

Moving files, step by step:

1.  You review the groups and change any toggles; suggestions come
    pre-filled from the keep rules.

2.  You press the action bar button, and a dialog confirms the file
    count and total size.

3.  The page sends the chosen file IDs and the session token to the
    server.

4.  The server checks each file: it belongs to this scan, its group
    keeps at least one file, its size and modified time haven\'t
    changed, and it isn\'t hard-linked.

5.  Files on drives with a Recycle Bin go there through send2trash;
    files on USB or network drives move to \_duplicates_review.

6.  Every action is written to the actions table and to actions.csv.

7.  The page marks each row as moved, or shows why the server refused
    it.

To undo, restore files from the Recycle Bin as usual. \--undo-review
moves files from \_duplicates_review folders back to their original
paths using the log.

\--report-only writes a static HTML report and a CSV instead, with no
server and no trash button.

## Command-line options

Typical runs:

python -m dupefinder C:\\ D:\\\
python -m dupefinder D:\\Photos \--types image \--ai\
python -m dupefinder D:\\Docs \--types pdf \--min-score 90
\--report-only

  -------------------------------------------------------------------------------
  Option              Default                      Purpose
  ------------------- ---------------------------- ------------------------------
  ROOTS               required                     Folders or drives to scan

  \--types            pdf,image                    Limit the scan to pdf or image

  \--exclude PATH     built-in list                Another folder to skip;
                                                   repeatable

  \--include-system   off                          Also scan system folders;
                                                   their files are never
                                                   pre-selected

  \--min-score        85                           Lowest % reported for text and
                                                   scanned-PDF matches

  \--max-bits         8                            Most differing bits, out of
                                                   64, for a photo visual match

  \--burst-gap        3                            Most seconds between shots in
                                                   a burst

  \--blur-cutoff      set after a trial            Sharpness below which a single
                                                   photo is flagged

  \--ocr              off                          Read scanned PDFs with
                                                   Tesseract

  \--max-pages        30                           Pages rendered per scanned PDF

  \--ai               off                          AI similarity for bursts with
                                                   movement, crops and heavy
                                                   edits

  \--keep             auto                         Keep rule: auto, newest,
                                                   oldest, largest, highest-res
                                                   or shortest-path

  \--prefer PATH      none                         Prefer keeping files under
                                                   this folder; repeatable

  \--workers N        CPU cores − 1                Worker processes

  \--cache PATH       %LOCALAPPDATA%\\DupeFinder   Cache, thumbnails and logs

  \--rescan           off                          Ignore cached fingerprints

  \--report-only      off                          Static HTML and CSV; no server
                                                   and no trash

  \--csv PATH         none                         Also export all groups to CSV

  \--port N           random                       Port for the local server

  \--no-browser       off                          Don\'t open the browser
                                                   automatically

  \--undo-review      off                          Move files from review folders
                                                   back, using the log
  -------------------------------------------------------------------------------

## Dependencies and code layout

The core needs eight packages plus the Python standard library; OCR and
AI similarity are optional extras.

  ------------------------------------------------------------------------
  Package                Used for                       Notes
  ---------------------- ------------------------------ ------------------
  PyMuPDF                PDF text extraction and page   AGPL-3.0 licence;
                         rendering                      fine for personal
                                                        use

  Pillow                 Decoding photos, applying      Image.draft()
                         rotation                       speeds up JPEG
                         (ImageOps.exif_transpose),     decoding
                         reading EXIF, thumbnails       

  pillow-heif            iPhone HEIC and HEIF photos    

  ImageHash              64-bit perceptual hash         

  datasketch             MinHash signatures and the LSH 
                         index                          

  NumPy 2.x              Block hash comparisons         
                         (bitwise_count)                

  OpenCV (headless)      Laplacian for the sharpness    Pure NumPy is a
                         score                          fallback

  Send2Trash             Moving files to the Recycle    Works on Windows,
                         Bin                            macOS and Linux

  Standard library       sqlite3, hashlib, http.server, No web framework
                         concurrent.futures, argparse   needed

  pytesseract +          Optional OCR (\--ocr)          Tesseract is
  Tesseract                                             installed
                                                        separately

  open_clip_torch +      Optional AI similarity (\--ai) Large download; a
  PyTorch                                               GPU helps but
                                                        isn\'t required
  ------------------------------------------------------------------------

The code splits into one module per stage:

  -----------------------------------------------------------------------
  Module                 Responsibility
  ---------------------- ------------------------------------------------
  \_\_main\_\_.py        Command-line options and the overall run

  scanner.py             Walking folders, exclusions, links and OneDrive
                         placeholders

  cache.py               SQLite schema, lookups and rescan rules

  pdf_pipeline.py        PDF fingerprints, text normalization, MinHash
                         and page hashes

  image_pipeline.py      Photo fingerprints, rotation, pHash, EXIF,
                         quality measures and optional embeddings

  matcher.py             Candidate search, verification and scores

  grouping.py            Union-find groups, bursts, keep rules and
                         related links

  safety.py              Drive types, Recycle Bin checks and pre-move
                         validation

  server.py              Local server, session token, report API and move
                         actions

  report/                index.html, app.js and styles.css for the
                         browser report

  tests/                 Test-file generator and tests
  -----------------------------------------------------------------------

## Performance

A first full scan of both drives should take from several minutes to
about an hour. The time depends mostly on how many photos and PDFs there
are, and on whether a drive is a hard disk. Rescans should take minutes,
mostly the folder walk; all of these are estimates to confirm during the
build.

How it stays fast:

-   Walk folders with os.scandir, and prune excluded folders before
    descending into them.

-   Fingerprint file bytes only for files that share a size with another
    file, reading 1 MB at a time.

-   Decode each photo once at about 1,024 pixels wide (JPEGs via
    Image.draft()) for hashing, bursts and quality; decode at full size
    only for pixel fingerprints of candidate pairs.

-   Extract PDF text and render pages in a process pool, with a
    60-second limit per file.

-   Write results to SQLite in batches from the main process.

-   Keep signatures small: 8 bytes per photo hash and about 1 KB per PDF
    MinHash.

-   Make thumbnails only when a group is shown.

## Test plan

A generator script builds a test folder with known duplicates, and every
case below must give its expected result before the tool touches real
drives.

  -----------------------------------------------------------------------
  Test file                             Expected result
  ------------------------------------- ---------------------------------
  Renamed copy of a PDF and of a JPG in 100% identical file
  another folder                        

  PDF re-saved with a new title and     100% same text
  author                                

  PDF with one paragraph rewritten      Similar text above 90%, grouped

  PDF with an extra cover page          Similar text above 85%, grouped

  Two different PDFs on the same topic  Not grouped

  Image-only PDF and a recompressed     Visual match, grouped
  copy                                  

  JPG resized to 50% and saved at       Visual match of 90% or more
  quality 70                            

  JPG with an edited date and stripped  100% same pixels
  location                              

  PNG converted from a JPG              100% same pixels

  JPG rotated by its rotation flag vs.  Visual match, grouped
  a copy rotated in pixels              

  Five-shot burst with two              One burst; the sharpest shot
  motion-blurred frames                 kept, blurred frames set to Trash

  Portrait photo with a blurred         Not flagged as blurry
  background                            

  Single blurry photo                   Listed under Possibly poor shots,
                                        not pre-selected

  Two photos of the same scene taken a  Not a burst
  minute apart                          

  JPG identical to a page of an         Related; never pre-selected
  image-only PDF                        

  Hard link and junction to the same    Not reported as duplicates
  file                                  

  OneDrive online-only file             Skipped and counted, not
                                        downloaded

  Password-protected or corrupt file    Logged; the scan continues

  Request to trash every copy in a      Refused
  group                                 

  File edited after the scan, then      Refused for that file
  trashed                               

  File on a USB stick                   Moved to \_duplicates_review, not
                                        trashed

  Rescan with no changes                Uses the cache and finishes
                                        quickly
  -----------------------------------------------------------------------

## Build order

Each step ends with something testable, so problems show up early.

1.  Scanner, cache and file fingerprints with CSV output, so exact
    duplicates work end to end.

2.  Photo checks: rotation, perceptual hash, NumPy matching and
    on-demand pixel fingerprints.

3.  Bursts and quality: EXIF capture times, burst chains, sharpness and
    exposure.

4.  PDF checks: text extraction and normalization, text fingerprint,
    MinHash, LSH and exact verification.

5.  Scanned PDFs: page rendering, page hashes, scanned-PDF scores and
    related matches.

6.  Grouping, keep rules and scores against the kept copy.

7.  Local server and a read-only browser report.

8.  Moving to the Recycle Bin, with every check, review folders, the
    action log and undo.

9.  Whole-drive hardening: exclusions, links, OneDrive, long paths,
    timeouts and the worker pool.

10. The test suite on generated files, then a trial on one real folder
    before whole drives.

11. Optional extras: OCR and AI similarity.

Estimated build time with Claude: roughly 1 to 2 hours for steps 1 to
10, with the optional extras on top.

## Open questions

-   [ ] Default keep rule for PDFs: the newest file, or the one without
    copy markers in its name?

-   [ ] Default cutoffs (85% for text, 8 bits for photos, 3 seconds for
    bursts), confirmed after a trial on real files

-   [ ] Blur cutoff and exposure flags, tuned on your own gallery

-   [ ] Mixed PDFs: compare by text, by page images, or both?

-   [ ] Should closed eyes or poor framing be judged? That needs a face
    model.

-   [ ] Camera RAW files (.CR2, .NEF, .ARW), which would need rawpy

-   [ ] AI step: which model, and whether a GPU is available

-   [ ] Scan external and USB drives too, or only C:\\ and D:\\?

-   [ ] Where to keep the cache, thumbnails and logs
