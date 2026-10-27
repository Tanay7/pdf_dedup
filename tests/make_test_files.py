#!/usr/bin/env python3
r"""Deterministic test-file generator for DupeFinder.

Builds a folder of PDFs and photos with known duplicates (the layout in
docs/BUILD_NOTES.md, "Test files (generator layout)") and checks that the
folder has the properties the tests rely on.

Usage:
    python tests/make_test_files.py OUTDIR [--check]

    Always (re)generates OUTDIR and writes OUTDIR/manifest.json, which holds
    every created path, which optional items were created or skipped and why,
    and the self-check metrics. With --check it also prints the metrics table
    and exits with status 1 when any self-check fails.

API:
    make_test_files(outdir) -> dict     the manifest (also written to disk)
    check(outdir) -> dict               self-check metrics and assertions
    phash(img) / sharpness(img) / exposure(img)
                                        the reference measures (DupeFinder
                                        implements the same ones)

Only the standard library, NumPy, Pillow, pillow_heif and PyMuPDF are used.
Everything is seeded, so every run produces the same structure (and, apart
from JPEG/HEIC encoder versions and PDF encryption salts, the same bytes).

Reference measures (on a "reduced" copy: decode, ImageOps.exif_transpose,
convert to RGB, then if the long side is above 1,024 px resize with LANCZOS
to round(w * s) x round(h * s), s = 1024 / long side):

* pHash: reduced.convert("L").resize((32, 32), LANCZOS) as a float array X;
  C[k, n] = cos(pi * k * (2n + 1) / 64); D = C @ X @ C.T; low = D[:8, :8];
  bits = low > median(low); 64-bit int, row-major, first bit = MSB.
  Distance = popcount(a ^ b).
* Sharpness: grey = reduced.convert("L") as float; Laplacian
  [[0,1,0],[1,-4,1],[0,1,0]] on the interior ((H-2) x (W-2)); split into a
  4 x 4 grid at rows i*(H-2)//4 and columns j*(W-2)//4; variance per tile;
  mean of the 4 highest tile variances.
* Exposure: median grey of the reduced copy; clipped share = share of grey
  pixels <= 5 or >= 250.
* PDF pages: page.get_pixmap(dpi=72), then the same pHash.
* Text similarity: tokens = re.findall(r"\w+", text.lower()) from page.get_text()
  after NFKC, without header/footer lines (lines that, digits masked, are on
  more than half of >= 3 pages) and bare page numbers; Jaccard of the sets of
  5-word shingles.

manifest.json keys: files (every created file, relative, "/"-separated),
dir_links, optional (heic, hardlink, dir_link, long_path: created or skipped
and why), byte_copies, scenes (files showing the same picture; "#pN" = PDF
page N), passwords, long_path, info, checks (the result of check()).

On Windows, delete an output folder with remove_tree(), which handles the
long path and the junction.
"""

from __future__ import annotations

import argparse
import calendar
import copy
import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import time
import unicodedata
from collections import Counter

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFilter, ImageOps, TiffImagePlugin

try:  # HEIC support is optional: without it beach.heic is skipped.
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - depends on the install
    pillow_heif = None
    HEIF_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

GENERATOR_VERSION = 1
SEED = 20240601
MANIFEST_NAME = "manifest.json"
TOP_LEVEL_DIRS = ("pdf", "photos", "onedrive", "links", "long", "projects")
LANCZOS = Image.Resampling.LANCZOS

PROTECTED_USER_PW = "user-secret"
PROTECTED_OWNER_PW = "owner-secret"

# Self-check limits (bits for pHash distances).
LIMITS = {
    "beach_small_bits": 4,
    "beach_heic_bits": 6,
    "rotated_bits": 4,
    "burst_consecutive_bits": 12,
    "scan_page_vs_pdf_bits": 6,
    "scan_recompressed_bits": 6,
    "distinct_scene_min_bits": 20,
    "sharp_vs_blurry_ratio": 3.0,
    "dark_median_max": 40,
    "overexposed_clipped_min": 0.25,
    "edited_jaccard_min": 0.90,
    "cover_jaccard_min": 0.85,
    "other_topic_jaccard_max": 0.05,
    "long_path_min_chars": 280,
    "burst_gap_seconds": 3.0,
}

# The long-path copy: the relative part alone is longer than 280 characters,
# so the absolute path is too, wherever OUTDIR is.
LONG_SEGMENTS = [f"nested_folder_level_{i:02d}_abcdefghijklmnopqrstuvwxyz0123" for i in range(1, 7)]
LONG_REL = "long/" + "/".join(LONG_SEGMENTS) + "/deep_copy.jpg"

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def win_long_path(path: str) -> str:
    """Return the Windows extended-length form (\\\\?\\C:\\...) of path; unchanged elsewhere."""
    if os.name != "nt":
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def remove_tree(path: str) -> None:
    """Delete a generated folder, long paths and links included (safe on Windows)."""
    if not os.path.lexists(path):
        return
    link = os.path.join(path, "links", "dir_link")
    if os.path.lexists(link):
        _remove_link(link)
    shutil.rmtree(win_long_path(path))


def _remove_link(p: str) -> None:
    try:
        os.unlink(p)
    except OSError:
        os.rmdir(p)  # Windows directory symlinks and junctions


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(win_long_path(path), "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _winansi(text: str) -> str:
    """Map text to WinAnsi code points: PyMuPDF's base-14 'helv' uses
    /WinAnsiEncoding but writes each character's code point, so e.g. an em
    dash must be passed as U+0097 to come out as an em dash."""
    return text.encode("cp1252").decode("latin-1")


class _Writer:
    """Creates files under outdir and records every created path."""

    def __init__(self, outdir: str):
        self.outdir = os.path.abspath(outdir)
        self.files: list[str] = []
        self.dir_links: dict[str, str] = {}

    def path(self, rel: str) -> str:
        p = os.path.join(self.outdir, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def add(self, rel: str) -> str:
        if rel not in self.files:
            self.files.append(rel)
        return os.path.join(self.outdir, *rel.split("/"))

    def write_bytes(self, rel: str, data: bytes) -> str:
        p = self.path(rel)
        with open(p, "wb") as f:
            f.write(data)
        return self.add(rel)

    def copy(self, src_rel: str, dst_rel: str) -> str:
        shutil.copyfile(self.path(src_rel), self.path(dst_rel))
        return self.add(dst_rel)


# --------------------------------------------------------------------------
# Reference measures
# --------------------------------------------------------------------------

_K = np.arange(32, dtype=np.float64)
DCT32 = np.cos(np.pi * _K[:, None] * (2 * _K[None, :] + 1) / 64.0)


def reduce_image(img: Image.Image, max_side: int = 1024) -> Image.Image:
    """exif_transpose, RGB, and LANCZOS-resize so the long side is <= max_side."""
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        s = max_side / max(w, h)
        img = img.resize((max(1, round(w * s)), max(1, round(h * s))), LANCZOS)
    return img


def load_reduced(path: str) -> Image.Image:
    with Image.open(win_long_path(path)) as im:
        im.load()
        return reduce_image(im)


def phash(img: Image.Image) -> int:
    """64-bit pHash of an already reduced image (see the module docstring)."""
    x = np.asarray(img.convert("L").resize((32, 32), LANCZOS), dtype=np.float64)
    d = DCT32 @ x @ DCT32.T
    low = d[:8, :8]
    bits = (low > np.median(low)).ravel()
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    return value


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def sharpness(img: Image.Image) -> float:
    """Mean Laplacian variance of the 4 sharpest of 16 tiles (reduced image)."""
    g = np.asarray(img.convert("L"), dtype=np.float64)
    lap = g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:] - 4.0 * g[1:-1, 1:-1]
    h, w = lap.shape
    rows = [i * h // 4 for i in range(5)]
    cols = [j * w // 4 for j in range(5)]
    tiles = [float(lap[rows[i]:rows[i + 1], cols[j]:cols[j + 1]].var())
             for i in range(4) for j in range(4)]
    tiles.sort(reverse=True)
    return float(np.mean(tiles[:4]))


def exposure(img: Image.Image) -> dict:
    g = np.asarray(img.convert("L"))
    return {
        "median": float(np.median(g)),
        "clipped": float(np.mean((g <= 5) | (g >= 250))),
        "white": float(np.mean(g >= 250)),
    }


def pdf_page_images(path: str, max_pages: int = 30) -> list[Image.Image]:
    out = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pix = page.get_pixmap(dpi=72)
            out.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    return out


def pdf_body_tokens(path: str) -> list[str]:
    """Words of a text PDF without header/footer lines (lines that, with digits
    masked, appear on more than half the pages when there are >= 3 pages) and
    without bare page numbers."""
    with pymupdf.open(path) as doc:
        pages = [p.get_text() for p in doc]
    norm = [[unicodedata.normalize("NFKC", ln).strip().lower() for ln in t.splitlines()]
            for t in pages]
    norm = [[ln for ln in lines if ln] for lines in norm]
    drop: set[str] = set()
    if len(pages) >= 3:
        counts = Counter()
        for lines in norm:
            counts.update({re.sub(r"\d+", "#", ln) for ln in lines})
        drop = {k for k, c in counts.items() if c > len(pages) / 2}
    tokens: list[str] = []
    for lines in norm:
        for ln in lines:
            if re.sub(r"\d+", "#", ln) in drop or re.fullmatch(r"\d+", ln):
                continue
            tokens.extend(re.findall(r"\w+", ln))
    return tokens


def shingles(tokens: list[str], k: int = 5) -> set:
    return {tuple(tokens[i:i + k]) for i in range(len(tokens) - k + 1)}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 1.0


# --------------------------------------------------------------------------
# Text synthesis
# --------------------------------------------------------------------------

FUNCTION_WORDS = """
the of and to in for with on by as at from our its their this that these
which while across through during under over into within between after
before also more most new all each both will would could should has have
had was were is are be been not than further only well still now such other
many several some every where when because although since toward about
around beyond
""".split()

CONTENT_WORDS = """
company corporation business revenue growth margin profit earnings income
expense cost budget forecast investment investor shareholder stakeholder
board director management executive committee strategy market segment
region customer client supplier partner product service portfolio brand
channel distribution operation facility plant warehouse logistics supply
chain inventory capacity production output quality safety compliance
regulation policy risk exposure liability asset equity debt capital cash
flow liquidity dividend share price value return performance efficiency
productivity innovation technology platform system process program project
initiative plan target objective goal milestone outcome result review
analysis assessment audit control governance oversight transparency
accountability sustainability environment emission energy resource
community employee workforce talent culture leadership training
development research engineering design manufacturing procurement sales
marketing finance treasury tax accounting reporting disclosure statement
quarter year period fiscal outlook guidance trend demand pricing
competition competitor acquisition merger integration divestiture
restructuring transformation digital data infrastructure network security
cloud software hardware contract agreement obligation commitment
opportunity challenge headwind tailwind volatility inflation currency
interest rate economy sector industry retail wholesale consumer enterprise
government public private international domestic global local regional
national pipeline backlog order volume shipment unit division subsidiary
headquarters office branch store footprint presence expansion scale scope
synergy savings saving efficiency benchmark metric indicator ratio
leverage coverage rating credit facility bond loan refinancing maturity
pension benefit compensation incentive bonus remuneration succession
diversity inclusion wellbeing health engagement retention recruitment
skill expertise knowledge insight intelligence automation analytics
model framework roadmap architecture modernization migration upgrade
maintenance reliability availability resilience continuity recovery
incident cyber privacy protection license patent property trademark
litigation dispute settlement claim provision reserve impairment
amortization depreciation goodwill valuation estimate assumption
judgment sensitivity scenario uncertainty
strong solid robust steady stable modest significant substantial
considerable notable positive negative favorable unfavorable higher lower
improved expanded consistent disciplined prudent balanced sustainable
resilient competitive innovative operational financial strategic organic
adjusted underlying recurring annual quarterly total net gross core key
primary additional ongoing continued future current previous prior recent
early initial final overall broad diverse efficient effective critical
essential important major minor long short medium premium flexible
scalable secure responsible ambitious challenging dynamic
increased decreased delivered achieved generated reported recorded
recognized invested reduced launched completed acquired divested
maintained strengthened accelerated supported enabled drove reflected
offset exceeded remained continued declined grew rose fell returned
allocated approved announced implemented optimized streamlined enhanced
focused prioritized managed monitored mitigated addressed secured extended
renewed signed opened closed integrated transformed positioned leveraged
captured sustained outperformed expects anticipates plans intends
believes targets seeks aims continues remains
significantly steadily consistently primarily largely partially fully
strongly modestly slightly broadly globally ultimately approximately
effectively successfully
""".split()


def _dedupe(words):
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


FUNCTION_WORDS = _dedupe(FUNCTION_WORDS)
CONTENT_WORDS = [w for w in _dedupe(CONTENT_WORDS) if w not in set(FUNCTION_WORDS)]
# keep a 400-word vocabulary: a fixed, evenly mixed subset of the content words
_keep = set(random.Random(SEED).sample(range(len(CONTENT_WORDS)), 400 - len(FUNCTION_WORDS)))
CONTENT_WORDS = [w for i, w in enumerate(CONTENT_WORDS) if i in _keep]
VOCABULARY = FUNCTION_WORDS + CONTENT_WORDS
assert len(VOCABULARY) == 400


def _sentence(rng: random.Random) -> str:
    n = rng.randint(8, 20)
    words: list[str] = []
    while len(words) < n:
        pool = FUNCTION_WORDS if (words and rng.random() < 0.3) else CONTENT_WORDS
        w = rng.choice(pool)
        if words and w == words[-1]:
            continue
        words.append(w)
    if n >= 12 and rng.random() < 0.4:
        k = rng.randint(4, n - 4)
        words[k] += ","
    s = " ".join(words)
    return s[0].upper() + s[1:] + "."


def _paragraph(rng: random.Random) -> str:
    return " ".join(_sentence(rng) for _ in range(rng.randint(4, 7)))


def _words(text: str) -> int:
    return len(text.split())


def _document(seed: int, target_words: int) -> list[str]:
    rng = random.Random(seed)
    paras: list[str] = []
    total = 0
    while total < target_words:
        p = _paragraph(rng)
        paras.append(p)
        total += _words(p)
    return paras


def _paginate(paras: list[str], n_pages: int) -> list[list[str]]:
    total = sum(_words(p) for p in paras)
    pages: list[list[str]] = [[] for _ in range(n_pages)]
    cum = 0
    for p in paras:
        c = _words(p)
        idx = min(n_pages - 1, int((cum + c / 2) * n_pages / total))
        pages[idx].append(p)
        cum += c
    assert all(pages), "pagination left an empty page"
    return pages


A4_W, A4_H = 595, 842
HEADER_RECT = pymupdf.Rect(56, 30, 539, 46)
BODY_RECT = pymupdf.Rect(56, 62, 539, 796)
FOOTER_RECT = pymupdf.Rect(56, 806, 539, 822)
REPORT_HEADER = "ACME Corporation — Annual Report 2024"
OTHER_HEADER = "ACME Corporation — Strategy Review 2024"
PROTECTED_HEADER = "ACME Corporation — Payroll Summary 2024"
FOOTER_FMT = "Confidential · Page {n} of {total}"
FIXED_DATE = "D:20240301120000+00'00'"


def _textbox(page, rect, text, fontsize=10.0, align=pymupdf.TEXT_ALIGN_LEFT, lineheight=1.25):
    rc = page.insert_textbox(rect, _winansi(text), fontname="helv", fontsize=fontsize,
                             align=align, lineheight=lineheight)
    if rc < 0:
        raise RuntimeError(f"text overflows its box by {-rc:.1f} pt: {text[:60]!r}")
    return rc


def _text_pdf(pages: list[list[str]], header: str, cover: tuple[str, str] | None = None,
              metadata: dict | None = None) -> pymupdf.Document:
    doc = pymupdf.open()
    total = len(pages) + (1 if cover else 0)
    if cover:
        title, blurb = cover
        page = doc.new_page(width=A4_W, height=A4_H)
        _textbox(page, pymupdf.Rect(56, 250, 539, 330), title, fontsize=26,
                 align=pymupdf.TEXT_ALIGN_CENTER, lineheight=1.2)
        _textbox(page, pymupdf.Rect(96, 360, 499, 520), blurb, fontsize=12,
                 align=pymupdf.TEXT_ALIGN_CENTER)
    for i, paras in enumerate(pages):
        n = i + 1 + (1 if cover else 0)
        page = doc.new_page(width=A4_W, height=A4_H)
        _textbox(page, HEADER_RECT, header, fontsize=9)
        _textbox(page, BODY_RECT, "\n\n".join(paras), fontsize=10)
        _textbox(page, FOOTER_RECT, FOOTER_FMT.format(n=n, total=total), fontsize=9,
                 align=pymupdf.TEXT_ALIGN_CENTER)
    meta = {"creator": "make_test_files.py", "producer": "PyMuPDF",
            "creationDate": FIXED_DATE, "modDate": FIXED_DATE}
    meta.update(metadata or {})
    doc.set_metadata(meta)
    return doc


def _save_pdf(doc: pymupdf.Document, path: str, **kw) -> None:
    """Save with stable bytes: no random /ID, and PyMuPDF's reproducible mode if present."""
    try:
        doc.save(path, reproducible=True, no_new_id=True, **kw)
    except TypeError:  # older PyMuPDF without `reproducible`
        doc.save(path, no_new_id=True, **kw)


# --------------------------------------------------------------------------
# Image synthesis
# --------------------------------------------------------------------------


def _field(rng: np.random.Generator, w: int, h: int, cell: float, channels: int = 1) -> np.ndarray:
    """Smooth random field (std ~0.8): one N(0,1) value per `cell` px, bicubic-upsampled."""
    gw = max(2, int(math.ceil(w / cell)) + 1)
    gh = max(2, int(math.ceil(h / cell)) + 1)
    planes = []
    for _ in range(channels):
        g = rng.standard_normal((gh, gw)).astype(np.float32)
        planes.append(np.asarray(Image.fromarray(g).resize((w, h), Image.Resampling.BICUBIC)))
    return np.stack(planes, axis=-1) if channels > 1 else planes[0]


def _ramp(w: int, h: int, angle: float) -> np.ndarray:
    gx = np.linspace(-1.0, 1.0, w, dtype=np.float32)
    gy = np.linspace(-1.0, 1.0, h, dtype=np.float32)
    return math.cos(angle) * gx[None, :] + math.sin(angle) * gy[:, None]


def _paste_shape(arr: np.ndarray, rng: np.random.Generator, colour, kind: str,
                 box: tuple[int, int, int, int], soft: float = 1.0, shade: float = 22.0) -> None:
    """Composite a soft-edged, shaded shape into arr (float32 HxWx3) inside box."""
    h, w = arr.shape[:2]
    x0, y0, x1, y1 = box
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    bw, bh = x1 - x0, y1 - y0
    if bw < 8 or bh < 8:
        return
    mask = Image.new("L", (bw, bh), 0)
    d = ImageDraw.Draw(mask)
    pad = 3
    if kind == "ellipse":
        d.ellipse([pad, pad, bw - pad, bh - pad], fill=255)
    elif kind == "rect":
        d.rectangle([pad, pad, bw - pad, bh - pad], fill=255)
    else:  # polygon
        n = int(rng.integers(5, 9))
        ang = np.sort(rng.uniform(0, 2 * np.pi, n))
        rad = rng.uniform(0.55, 1.0, n)
        pts = [(bw / 2 + (bw / 2 - pad) * r * math.cos(a), bh / 2 + (bh / 2 - pad) * r * math.sin(a))
               for a, r in zip(ang, rad)]
        d.polygon(pts, fill=255)
    if soft > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(soft))
    m = np.asarray(mask, dtype=np.float32)[..., None] / 255.0
    col = np.asarray(colour, dtype=np.float32) + shade * _field(rng, bw, bh, max(bw, bh) / 2.5)[..., None]
    sub = arr[y0:y1, x0:x1]
    col -= sub
    col *= m
    sub += col  # sub is a view: arr is updated in place


def _upsample_rgb(small: np.ndarray, w: int, h: int) -> np.ndarray:
    """Bicubic-upsample a smooth float field (h x w x 3, built at low resolution) to w x h."""
    return np.asarray(_to_image(small).resize((w, h), Image.Resampling.BICUBIC), dtype=np.float32)


def _detail(rng: np.random.Generator, w: int, h: int, amount: float = 1.0,
            strength_small: np.ndarray | None = None) -> np.ndarray:
    """Luminance texture (octaves at ~2.5, 7 and 28 px, scaled by a strength map)
    plus per-pixel grain. Returns HxW float32."""
    gw, gh = int(math.ceil(w / 2.5)) + 1, int(math.ceil(h / 2.5)) + 1
    tex = 10.0 * rng.standard_normal((gh, gw), dtype=np.float32)
    tex += 11.0 * _field(rng, gw, gh, 7 / 2.5)
    tex += 13.0 * _field(rng, gw, gh, 28 / 2.5)
    if strength_small is None:
        strength = np.clip(0.6 + 0.5 * _field(rng, gw, gh, max(gw, gh) / 4), 0.2, 1.0)
    else:
        strength = np.asarray(Image.fromarray(strength_small.astype(np.float32)).resize(
            (gw, gh), Image.Resampling.BICUBIC))
    tex *= strength * amount
    lum = np.asarray(Image.fromarray(tex).resize((w, h), Image.Resampling.BICUBIC))
    return lum + (5.5 * amount) * rng.standard_normal((h, w), dtype=np.float32)


def _scene(seed: int, w: int, h: int, detail: float = 1.0) -> np.ndarray:
    """A synthetic photo: colour field, gradient, shaded shapes, texture. Float HxWx3."""
    rng = np.random.default_rng(seed)
    big = max(w, h)
    sw, sh = max(16, w // 8), max(16, h // 8)
    mean = rng.uniform(70, 180, 3).astype(np.float32)
    small = mean + 60.0 * _field(rng, sw, sh, max(sw, sh) / rng.uniform(1.8, 3.2), 3)
    small += (40.0 * _ramp(sw, sh, rng.uniform(0, 2 * np.pi)))[..., None]
    arr = _upsample_rgb(small, w, h)
    for _ in range(int(rng.integers(5, 10))):
        bw = int(big * rng.uniform(0.08, 0.40))
        bh = int(bw * rng.uniform(0.5, 1.6))
        x0 = int(rng.uniform(-0.1, 0.95) * w)
        y0 = int(rng.uniform(-0.1, 0.95) * h)
        kind = ("ellipse", "rect", "poly")[int(rng.integers(0, 3))]
        _paste_shape(arr, rng, rng.uniform(15, 240, 3), kind, (x0, y0, x0 + bw, y0 + bh),
                     soft=float(rng.uniform(0.8, 2.0)))
    arr += _detail(rng, w, h, detail)[..., None]
    return arr


def _beach_scene(seed: int, w: int, h: int) -> np.ndarray:
    """Sky, sea and sand bands with a few objects on the sand."""
    rng = np.random.default_rng(seed)
    sw, sh = w // 4, h // 4
    ys = np.linspace(0, 1, sh, dtype=np.float32)[:, None]
    xs = np.linspace(0, 1, sw, dtype=np.float32)[None, :]
    horizon = 0.40 + 0.02 * (xs - 0.5)
    shore = 0.60 + 0.03 * np.sin(xs * 9.0 + 1.3) + 0.015 * np.sin(xs * 23.0)
    sky_t = np.clip(ys / 0.40, 0, 1)[..., None]
    sky = (np.array([70, 125, 205], np.float32) * (1 - sky_t)
           + np.array([185, 210, 235], np.float32) * sky_t)
    sea = np.array([25, 95, 135], np.float32) + 20.0 * _field(rng, sw, sh, 40, 3)
    sand = np.array([214, 188, 140], np.float32) + 18.0 * _field(rng, sw, sh, 55, 3)
    is_sky = _sigmoid((horizon - ys) * sh)
    is_sand = _sigmoid((ys - shore) * sh / 1.5)
    is_sea = np.clip(1.0 - is_sky - is_sand, 0, 1)
    small = sky * is_sky[..., None] + sea * is_sea[..., None] + sand * is_sand[..., None]
    clouds = np.clip(_field(rng, sw, sh, 65) - 0.2, 0, 1) * is_sky
    small += (70.0 * clouds)[..., None]
    arr = _upsample_rgb(small, w, h)
    # objects on the sand: umbrella, towels, a person; a sail on the sea
    for colour, kind, box in [
        ((200, 40, 45), "ellipse", (int(0.12 * w), int(0.58 * h), int(0.36 * w), int(0.72 * h))),
        ((245, 215, 40), "rect", (int(0.55 * w), int(0.78 * h), int(0.75 * w), int(0.86 * h))),
        ((40, 120, 200), "rect", (int(0.20 * w), int(0.85 * h), int(0.33 * w), int(0.97 * h))),
        ((60, 45, 40), "poly", (int(0.66 * w), int(0.55 * h), int(0.72 * w), int(0.80 * h))),
        ((235, 240, 245), "poly", (int(0.80 * w), int(0.30 * h), int(0.86 * w), int(0.46 * h))),
    ]:
        _paste_shape(arr, rng, colour, kind, box, soft=1.2)
    strength = 0.15 * is_sky + 0.55 * is_sea + 1.15 * is_sand
    arr += _detail(rng, w, h, 1.0, strength_small=strength)[..., None]
    return arr


def _to_image(arr: np.ndarray) -> Image.Image:
    if arr.dtype == np.uint8:
        return Image.fromarray(arr)
    out = arr + np.float32(0.5)
    np.clip(out, 0.0, 255.0, out=out)
    return Image.fromarray(out.astype(np.uint8))


def _motion_blur_h(arr: np.ndarray, length: int) -> np.ndarray:
    """Horizontal box blur of `length` px (odd) - camera shake. 8-bit in, float32 out."""
    pad = length // 2
    a = np.asarray(_to_image(arr))
    a = np.pad(a, ((0, 0), (pad, pad), (0, 0)), mode="edge")
    c = np.zeros((a.shape[0], a.shape[1] + 1, a.shape[2]), np.int32)
    np.cumsum(a, axis=1, dtype=np.int32, out=c[:, 1:])
    return (c[:, length:] - c[:, :-length]).astype(np.float32) / length


def _gauss(arr: np.ndarray, radius: float) -> np.ndarray:
    """Pillow Gaussian blur (8-bit RGB), returned as float32."""
    return np.asarray(_to_image(arr).filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.tanh(0.5 * x))


def _grey(arr: np.ndarray) -> np.ndarray:
    a = np.clip(arr, 0, 255)
    return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]


# --------------------------------------------------------------------------
# Scanned-document pages
# --------------------------------------------------------------------------

SCAN_W, SCAN_H = 1240, 1754
INK = 255


def _text_lines(d: ImageDraw.ImageDraw, rng: random.Random, x0: int, y0: int, x1: int,
                y1: int, line_h: int = 30, size: int = 12, wrap: list | None = None,
                para_lines: tuple[int, int] = (4, 8)) -> int:
    """Draw glyph-like marks as lines of text; returns the y after the block."""
    y = y0
    left_in_para = rng.randint(*para_lines)
    first = True
    while y + line_h <= y1:
        xa, xb = x0, x1
        for (rx0, ry0, rx1, ry1) in wrap or []:
            if y + line_h > ry0 and y < ry1:
                if rx0 > (x0 + x1) / 2:
                    xb = min(xb, rx0 - 30)
                else:
                    xa = max(xa, rx1 + 30)
        x = xa + (40 if first else 0)
        last = left_in_para == 1
        end = xb - (rng.randint(80, 400) if last else 0)
        base = y + line_h - 8
        while True:
            nch = rng.randint(2, 10)
            cw = rng.randint(8, 11)
            if x + nch * cw > end:
                break
            for _ in range(nch):
                tall = rng.random() < 0.28
                gh = int(size * (1.45 if tall else 1.0))
                kind = rng.random()
                if kind < 0.45:
                    d.rectangle([x, base - gh, x + cw - 3, base], outline=INK, width=2)
                elif kind < 0.8:
                    d.rectangle([x + 1, base - gh, x + 3, base], fill=INK)
                    d.rectangle([x + 1, base - 3, x + cw - 3, base], fill=INK)
                else:
                    d.ellipse([x, base - size, x + cw - 3, base], outline=INK, width=2)
                x += cw
            x += rng.randint(9, 13)
        y += line_h
        first = False
        left_in_para -= 1
        if left_in_para == 0:
            y += line_h // 2
            left_in_para = rng.randint(*para_lines)
            first = True
    return y


def _heading(d: ImageDraw.ImageDraw, rng: random.Random, x: int, y: int, words: int,
             size: int = 30) -> None:
    for _ in range(words):
        ww = rng.randint(60, 190)
        d.rectangle([x, y, x + ww, y + size], fill=INK)
        x += ww + 22


def _scan_page(page_no: int) -> Image.Image:
    seed = SEED + 1000 * SCAN_SEEDS[page_no]
    rng = np.random.default_rng(seed)
    trng = random.Random(seed + 1)
    w, h = SCAN_W, SCAN_H
    pw, ph = w // 8, h // 8
    paper = np.array([238, 235, 226], np.float32) + 4.0 * _field(rng, pw, ph, 60, 3)
    paper += (-7.0 * np.abs(_ramp(pw, ph, 0.3)))[..., None]
    paper_img = _to_image(paper).resize((w, h), Image.Resampling.BICUBIC)
    ink = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(ink)
    photos: list[tuple[tuple[int, int, int, int], int]] = []
    if page_no == 1:  # letter: logo, title, body wrapping round a photo, signature
        d.rectangle([90, 90, 230, 230], fill=INK)
        _heading(d, trng, 270, 110, 3, 34)
        _heading(d, trng, 270, 170, 5, 16)
        _heading(d, trng, 100, 310, 4, 30)
        photo = (680, 470, 1140, 860)
        photos.append((photo, seed + 2))
        _text_lines(d, trng, 100, 390, 1140, 1500, wrap=[photo])
        d.line([(110, 1620), (180, 1590), (240, 1625), (300, 1580), (380, 1615)], fill=INK, width=4)
        d.rectangle([100, 1640, 520, 1643], fill=INK)
    elif page_no == 2:  # two-column report with a table
        _heading(d, trng, 330, 100, 4, 36)
        _text_lines(d, trng, 100, 200, 600, 1000, para_lines=(5, 12))
        _text_lines(d, trng, 660, 200, 1140, 1000, para_lines=(5, 12))
        tx0, ty0, tx1, ty1 = 100, 1080, 1140, 1650
        rows, cols = 7, 4
        d.rectangle([tx0, ty0, tx1, ty0 + (ty1 - ty0) // rows], fill=int(INK * 0.55))
        for r in range(rows + 1):
            yy = ty0 + r * (ty1 - ty0) // rows
            d.rectangle([tx0, yy, tx1, yy + 3], fill=INK)
        for c in range(cols + 1):
            xx = tx0 + c * (tx1 - tx0) // cols
            d.rectangle([xx, ty0, xx + 3, ty1], fill=INK)
        for r in range(1, rows):
            for c in range(cols):
                cx = tx0 + c * (tx1 - tx0) // cols + 20
                cy = ty0 + r * (ty1 - ty0) // rows + 25
                d.rectangle([cx, cy, cx + trng.randint(60, 200), cy + 14], fill=INK)
    else:  # photo sheet: big photo, caption, short text, bar chart
        photo = (100, 110, 1140, 760)
        photos.append((photo, seed + 2))
        _heading(d, trng, 100, 800, 6, 16)
        _text_lines(d, trng, 100, 870, 1140, 1130, para_lines=(3, 5))
        base_y = 1640
        d.rectangle([100, base_y, 1140, base_y + 4], fill=INK)
        d.rectangle([100, 1200, 104, base_y], fill=INK)
        for i in range(8):
            bh = trng.randint(80, 420)
            bx = 150 + i * 120
            d.rectangle([bx, base_y - bh, bx + 70, base_y], fill=int(INK * (0.55 + 0.4 * (i % 2))))
    mask = ink.point([int(v * 0.9 + 0.5) for v in range(256)])
    page = Image.composite(Image.new("RGB", (w, h), (32, 32, 40)), paper_img, mask)
    for (x0, y0, x1, y1), pseed in photos:
        pic = _scene(pseed, x1 - x0, y1 - y0)
        pic = 0.85 * pic + 0.15 * _grey(pic)[..., None]  # printed: a bit desaturated
        page.paste(_to_image(pic * 0.9 + 10), (x0, y0))
    page = page.filter(ImageFilter.GaussianBlur(0.6))  # scanner optics
    noise = np.rint(2.5 * rng.standard_normal((h, w, 1), dtype=np.float32)).astype(np.int16)
    arr = np.asarray(page, dtype=np.int16) + noise
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def _jpeg_bytes(img: Image.Image, quality: int, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, **kw)
    return buf.getvalue()


def _image_pdf(jpegs: list[bytes], page_w: float, page_h: float) -> pymupdf.Document:
    doc = pymupdf.open()
    for data in jpegs:
        page = doc.new_page(width=page_w, height=page_h)
        page.insert_image(page.rect, stream=data)
    doc.set_metadata({"creator": "make_test_files.py (scanner)", "producer": "PyMuPDF",
                      "creationDate": FIXED_DATE, "modDate": FIXED_DATE})
    return doc


# --------------------------------------------------------------------------
# EXIF
# --------------------------------------------------------------------------


def _dms(value: float) -> tuple:
    d = int(value)
    m = int((value - d) * 60)
    s = round(((value - d) * 60 - m) * 60 * 100)
    R = TiffImagePlugin.IFDRational
    return (R(d, 1), R(m, 1), R(s, 100))


def build_exif(make: str | None = None, model: str | None = None, orientation: int | None = 1,
               dto: str | None = None, subsec: str | None = None,
               gps: tuple[float, float] | None = None) -> Image.Exif:
    exif = Image.Exif()
    if make:
        exif[0x010F] = make
    if model:
        exif[0x0110] = model
    if orientation is not None:
        exif[0x0112] = orientation
    sub = {}
    if dto:
        sub[0x9003] = dto
    if subsec is not None:
        sub[0x9291] = subsec
    if sub:
        exif[0x8769] = sub
    if gps:
        lat, lon = gps
        exif[0x8825] = {
            0: b"\x02\x03\x00\x00",
            1: "N" if lat >= 0 else "S",
            2: _dms(abs(lat)),
            3: "E" if lon >= 0 else "W",
            4: _dms(abs(lon)),
        }
    return exif


def splice_exif(jpeg: bytes, exif_payload: bytes) -> bytes:
    """Replace the APP1/Exif segment of a JPEG without touching any other bytes."""
    if jpeg[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    if not exif_payload.startswith(b"Exif\x00\x00") or len(exif_payload) > 65533:
        raise ValueError("bad Exif payload")
    out = [jpeg[:2]]
    i, replaced = 2, False
    while True:
        if jpeg[i] != 0xFF:
            raise ValueError(f"marker expected at {i}")
        marker = jpeg[i + 1]
        if marker == 0xDA:  # start of scan: copy the rest verbatim
            out.append(jpeg[i:])
            break
        seglen = struct.unpack(">H", jpeg[i + 2:i + 4])[0]
        seg = jpeg[i:i + 2 + seglen]
        if marker == 0xE1 and seg[4:10] == b"Exif\x00\x00" and not replaced:
            out.append(b"\xff\xe1" + struct.pack(">H", len(exif_payload) + 2) + exif_payload)
            replaced = True
        else:
            out.append(seg)
        i += 2 + seglen
    if not replaced:
        raise ValueError("no APP1/Exif segment")
    return b"".join(out)


def _sos_offset(jpeg: bytes) -> int:
    i = 2
    while jpeg[i + 1] != 0xDA:
        i += 2 + struct.unpack(">H", jpeg[i + 2:i + 4])[0]
    return i


def read_exif(path: str) -> dict:
    with Image.open(win_long_path(path)) as im:
        ex = im.getexif()
        sub = ex.get_ifd(0x8769)
        gps = ex.get_ifd(0x8825)
        return {
            "make": ex.get(0x010F), "model": ex.get(0x0110), "orientation": ex.get(0x0112),
            "datetime_original": sub.get(0x9003), "subsec": sub.get(0x9291),
            "has_gps": bool(gps), "gps_lat_ref": gps.get(1) if gps else None,
        }


# Capture metadata per photo: (make, model, DateTimeOriginal, SubSec, Orientation, GPS).
BEACH_GPS = (36.720139, -4.419500)
PHOTO_EXIF = {
    "photos/beach.jpg": ("TestCam", "Model A", "2023:07:15 14:30:00", None, 1, BEACH_GPS),
    "photos/rotated_flag.jpg": ("TestCam", "Model C", "2023:08:02 10:15:00", None, 6, None),
    "photos/rotated_pixels.jpg": ("TestCam", "Model C", "2023:08:05 17:40:00", None, None, None),
    "photos/portrait_bokeh.jpg": ("TestCam", "Model D", "2023:05:20 16:45:10", None, 1, None),
    "photos/blurry.jpg": ("TestCam", "Model A", "2023:04:02 11:11:11", None, 1, None),
    "photos/dark.jpg": ("TestCam", "Model A", "2023:03:03 22:05:00", None, 1, None),
    "photos/overexposed.jpg": ("TestCam", "Model A", "2023:02:04 13:20:00", None, 1, None),
    "photos/scene_minute_a.jpg": ("TestCam", "Model B", "2023:09:10 10:00:00", None, 1, None),
    "photos/scene_minute_b.jpg": ("TestCam", "Model B", "2023:09:10 10:01:00", None, 1, None),
    "photos/unrelated_1.jpg": ("OtherCam", "X1", "2022:12:24 18:00:00", None, 1, None),
    "photos/unrelated_3.jpg": ("OtherCam", "X1", "2022:11:11 09:30:00", None, 1, None),
    "photos/long_twin.jpg": ("TestCam", "Model A", "2022:10:10 10:10:10", None, 1, None),
}
BEACH_EDITED_DTO = "2023:07:18 09:12:00"
BURST_TIMES = [("2024:06:01 12:00:00", "000"), ("2024:06:01 12:00:00", "400"),
               ("2024:06:01 12:00:00", "800"), ("2024:06:01 12:00:01", "200"),
               ("2024:06:01 12:00:01", "600")]
for _i, (_t, _s) in enumerate(BURST_TIMES, 1):
    PHOTO_EXIF[f"photos/burst/burst_{_i}.jpg"] = ("TestCam", "Burst Cam", _t, _s, 1, None)

# Scenes: files that show the same picture. Files in different scenes must be
# >= 20 bits apart. "#pN" marks page N of a PDF.
SCENES = {
    "beach": ["photos/beach.jpg", "photos/beach_small.jpg", "photos/beach_exif_edited.jpg",
              "photos/beach.png", "photos/beach.heic"],
    "rotated": ["photos/rotated_flag.jpg", "photos/rotated_pixels.jpg"],
    "burst": [f"photos/burst/burst_{i}.jpg" for i in range(1, 6)],
    "portrait": ["photos/portrait_bokeh.jpg"],
    "blurry": ["photos/blurry.jpg"],
    "dark": ["photos/dark.jpg"],
    "overexposed": ["photos/overexposed.jpg"],
    "minute": ["photos/scene_minute_a.jpg", "photos/scene_minute_b.jpg"],
    "unrelated_1": ["photos/unrelated_1.jpg"],
    "unrelated_2": ["photos/unrelated_2.png"],
    "unrelated_3": ["photos/unrelated_3.jpg"],
    "long_twin": ["photos/long_twin.jpg"],
    "links_original": ["links/original.jpg"],
    "links_inner": ["links/real_dir/inner.jpg"],
    "scan_p1": ["photos/scan_page.jpg", "pdf/scan.pdf#p1", "pdf/scan_recompressed.pdf#p1"],
    "scan_p2": ["pdf/scan.pdf#p2", "pdf/scan_recompressed.pdf#p2"],
    "scan_p3": ["pdf/scan.pdf#p3", "pdf/scan_recompressed.pdf#p3"],
}

# Byte copies: copy -> original.
BYTE_COPIES = {
    "pdf/archive/report renamed.pdf": "pdf/report.pdf",
    "onedrive/online_only.pdf": "pdf/report.pdf",
    "pdf/protected_copy.pdf": "pdf/protected.pdf",
    "photos/backup/beach copy.jpg": "photos/beach.jpg",
    "projects/node_modules/pkg/beach_dup.jpg": "photos/beach.jpg",
    LONG_REL: "photos/long_twin.jpg",
}

# Scene seeds, chosen so every pair of scenes is >= 26 bits apart (the check needs 20).
SCENE_SEEDS = {
    "beach": 11, "rotated": 12, "burst_bg": 113, "burst_subject": 14, "portrait_bg": 15,
    "portrait_subject": 16, "blurry": 17, "dark": 718, "overexposed": 19, "minute": 20,
    "unrelated_1": 21, "unrelated_2": 22, "unrelated_3": 223, "long_twin": 24,
    "links_original": 225, "links_inner": 226,
}


SCAN_SEEDS = {1: 571, 2: 272, 3: 73}


def _seed(name: str) -> int:
    return SEED + 1000 * SCENE_SEEDS[name]


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def _prepare_outdir(outdir: str) -> None:
    os.makedirs(outdir, exist_ok=True)
    entries = os.listdir(outdir)
    if not entries:
        return
    manifest = os.path.join(outdir, MANIFEST_NAME)
    if not os.path.isfile(manifest):
        raise RuntimeError(f"{outdir} is not empty and has no {MANIFEST_NAME} from an earlier "
                           "run; refusing to overwrite it")
    link = os.path.join(outdir, "links", "dir_link")
    if os.path.lexists(link):
        _remove_link(link)
    for name in TOP_LEVEL_DIRS:
        p = os.path.join(outdir, name)
        if os.path.lexists(p):
            shutil.rmtree(win_long_path(p))
    os.remove(manifest)


def _make_pdfs(wr: _Writer, info: dict) -> None:
    # report.pdf: 5 pages, ~2,500 words
    paras = _document(SEED + 1, 2500)
    pages = _paginate(paras, 5)
    meta = {"title": "Annual Report 2024", "author": "ACME Finance", "subject": "Annual report"}
    doc = _text_pdf(pages, REPORT_HEADER, metadata=meta)
    _save_pdf(doc, wr.path("pdf/report.pdf"), garbage=1)
    wr.add("pdf/report.pdf")
    info["report_words"] = sum(_words(p) for p in paras)
    info["report_paragraphs"] = len(paras)

    wr.copy("pdf/report.pdf", "pdf/archive/report renamed.pdf")
    wr.copy("pdf/report.pdf", "onedrive/online_only.pdf")

    # report_resaved.pdf: new title/author, garbage collection + deflate
    with pymupdf.open(wr.path("pdf/report.pdf")) as src:
        m = dict(src.metadata)
        m.update({"title": "ACME Annual Report 2024 (final)", "author": "J. Smith",
                  "modDate": "D:20240415093000+00'00'"})
        m.pop("format", None)
        m.pop("encryption", None)
        src.set_metadata(m)
        _save_pdf(src, wr.path("pdf/report_resaved.pdf"), garbage=4, deflate=True)
    wr.add("pdf/report_resaved.pdf")

    # report_edited.pdf: one ~70-word paragraph on page 3 replaced
    page_idx = 2
    j = min(range(len(pages[page_idx])), key=lambda k: abs(_words(pages[page_idx][k]) - 70))
    old = pages[page_idx][j]
    rng = random.Random(SEED + 2)
    new = None
    for _ in range(5000):
        cand = _paragraph(rng)
        if abs(_words(cand) - _words(old)) <= 3:
            new = cand
            break
    if new is None:
        raise RuntimeError("could not make a replacement paragraph")
    edited = copy.deepcopy(pages)
    edited[page_idx][j] = new
    doc = _text_pdf(edited, REPORT_HEADER, metadata=meta)
    _save_pdf(doc, wr.path("pdf/report_edited.pdf"), garbage=1)
    wr.add("pdf/report_edited.pdf")
    info["edited_paragraph"] = {"page": page_idx + 1, "index_on_page": j,
                                "old_words": _words(old), "new_words": _words(new)}

    # report_cover.pdf: an extra cover page, footers "Page N of 6"
    crng = random.Random(SEED + 3)
    blurb = []
    while sum(_words(s) for s in blurb) < 38:
        blurb.append(_sentence(crng))
    doc = _text_pdf(pages, REPORT_HEADER, cover=("ACME Corporation\nAnnual Report 2024", " ".join(blurb)),
                    metadata=meta)
    _save_pdf(doc, wr.path("pdf/report_cover.pdf"), garbage=1)
    wr.add("pdf/report_cover.pdf")

    # other_topic.pdf: same vocabulary, other seed, ~2,000 words
    oparas = _document(SEED + 4, 2000)
    doc = _text_pdf(_paginate(oparas, 4), OTHER_HEADER,
                    metadata={"title": "Strategy Review 2024", "author": "ACME Strategy"})
    _save_pdf(doc, wr.path("pdf/other_topic.pdf"), garbage=1)
    wr.add("pdf/other_topic.pdf")

    # protected.pdf (AES-256, user password) and a byte copy
    pparas = _document(SEED + 5, 220)
    doc = _text_pdf([pparas], PROTECTED_HEADER,
                    metadata={"title": "Payroll Summary 2024", "author": "ACME HR"})
    doc.save(wr.path("pdf/protected.pdf"), garbage=1, encryption=pymupdf.PDF_ENCRYPT_AES_256,
             owner_pw=PROTECTED_OWNER_PW, user_pw=PROTECTED_USER_PW)
    wr.add("pdf/protected.pdf")
    wr.copy("pdf/protected.pdf", "pdf/protected_copy.pdf")

    # corrupt files
    brng = np.random.default_rng(SEED + 6)
    wr.write_bytes("pdf/corrupt.pdf", b"%PDF-1.4\n" + brng.integers(0, 256, 4096, dtype=np.uint8).tobytes())
    junk = brng.integers(0, 256, 4096, dtype=np.uint8)
    junk[0] = 0x13  # make sure no marker follows SOI
    wr.write_bytes("photos/corrupt.jpg", b"\xff\xd8" + junk.tobytes())

    # scanned PDFs: 3 image-only pages, and a recompressed copy
    page_imgs = [_scan_page(n) for n in (1, 2, 3)]
    jpeg85 = [_jpeg_bytes(im, 85) for im in page_imgs]
    pw, ph = SCAN_W / 2, SCAN_H / 2
    _save_pdf(_image_pdf(jpeg85, pw, ph), wr.path("pdf/scan.pdf"), garbage=3)
    wr.add("pdf/scan.pdf")
    small = [im.resize((round(SCAN_W * 0.7), round(SCAN_H * 0.7)), LANCZOS) for im in page_imgs]
    jpeg35 = [_jpeg_bytes(im, 35) for im in small]
    _save_pdf(_image_pdf(jpeg35, pw, ph), wr.path("pdf/scan_recompressed.pdf"), garbage=3)
    wr.add("pdf/scan_recompressed.pdf")
    wr.write_bytes("photos/scan_page.jpg", jpeg85[0])


def _save_photo(wr: _Writer, rel: str, img: Image.Image, quality: int = 92) -> str:
    meta = PHOTO_EXIF.get(rel)
    kw = {}
    if meta:
        make, model, dto, subsec, orientation, gps = meta
        kw["exif"] = build_exif(make, model, orientation, dto, subsec, gps).tobytes()
    img.save(wr.path(rel), "JPEG", quality=quality, **kw)
    return wr.add(rel)


PHOTO_W, PHOTO_H = 2000, 1500
PLAIN_SIZES = {"unrelated_1": (1600, 1200), "unrelated_2": (1200, 900), "unrelated_3": (1500, 2000),
               "long_twin": (1600, 1200), "links_original": (1280, 960), "links_inner": (1280, 960)}


def render_scene(name: str, info: dict | None = None):
    """Render one scene in memory: an Image, or a list of Images for multi-shot scenes
    (burst: 5 frames; minute: a, b; rotated: the upright picture)."""
    W, H = PHOTO_W, PHOTO_H
    info = {} if info is None else info
    if name == "beach":
        return _to_image(_beach_scene(_seed("beach"), W, H))
    if name == "rotated":
        return _to_image(_scene(_seed("rotated"), W, H))
    if name == "burst":
        m = 24
        bg = _scene(_seed("burst_bg"), W + 2 * m, H + 2 * m)
        sw, sh = 520, 760
        subject = _scene(_seed("burst_subject"), sw, sh, detail=1.3)
        smask = Image.new("L", (sw, sh), 0)
        ImageDraw.Draw(smask).ellipse([6, 6, sw - 6, sh - 6], fill=255)
        smask_a = np.asarray(smask.filter(ImageFilter.GaussianBlur(2.0)), np.float32)[..., None] / 255.0
        cam = [(0, 0), (1, 0), (1, 1), (2, 1), (2, 2)]  # camera shake, px
        frames = []
        for i in range(5):
            cx, cy = cam[i]
            frame = bg[m + cy:m + cy + H, m + cx:m + cx + W].copy()
            x0, y0 = 760 + 5 * i, 520 + 2 * i  # the subject moves a few px per frame
            frame[y0:y0 + sh, x0:x0 + sw] = (frame[y0:y0 + sh, x0:x0 + sw] * (1 - smask_a)
                                             + subject * smask_a)
            if i in (1, 3):  # frames 2 and 4: camera shake
                frame = _motion_blur_h(frame, 31)
            elif i in (0, 4):  # frames 1 and 5: very slightly soft
                frame = _gauss(frame, 1.0)
            frames.append(_to_image(frame))
        return frames
    if name == "portrait":
        bgp = _gauss(_scene(_seed("portrait_bg"), W, H), 18.0)
        x0, y0, x1, y1 = int(0.24 * W), int(0.24 * H), int(0.76 * W), int(0.76 * H)
        subj = _scene(_seed("portrait_subject"), x1 - x0, y1 - y0, detail=1.5)
        pm = Image.new("L", (x1 - x0, y1 - y0), 0)
        ImageDraw.Draw(pm).rounded_rectangle([4, 4, x1 - x0 - 4, y1 - y0 - 4], radius=60, fill=255)
        pm_a = np.asarray(pm.filter(ImageFilter.GaussianBlur(2.5)), np.float32)[..., None] / 255.0
        bgp[y0:y1, x0:x1] = bgp[y0:y1, x0:x1] * (1 - pm_a) + subj * pm_a
        return _to_image(bgp)
    if name == "blurry":
        return _to_image(_gauss(_scene(_seed("blurry"), W, H), 6.0))
    if name == "dark":
        dark = _scene(_seed("dark"), W, H)
        k = 20.0 / float(np.median(_grey(dark[::3, ::3])))
        dark *= k
        drng = np.random.default_rng(_seed("dark") + 1)
        for _ in range(40):  # a few lit windows and lamps
            lx, ly = int(drng.integers(0, W - 60)), int(drng.integers(0, H - 60))
            lw, lh = int(drng.integers(10, 60)), int(drng.integers(10, 60))
            dark[ly:ly + lh, lx:lx + lw] = drng.uniform(170, 250, 3)
        info["dark_gain"] = round(k, 4)
        return _to_image(dark)
    if name == "overexposed":
        over = _scene(_seed("overexposed"), W, H)
        lo, hi = 0.0, 255.0
        probe = over[::3, ::3]
        for _ in range(24):  # the lift that saturates ~45% of the pixels
            mid = (lo + hi) / 2
            share = float(np.mean(_grey(probe * 1.25 + mid) >= 252))
            lo, hi = (mid, hi) if share < 0.45 else (lo, mid)
        over *= 1.25
        over += hi
        info["overexposed_lift"] = round(hi, 2)
        return _to_image(over)
    if name == "minute":
        mm = 20
        base = _scene(_seed("minute"), W + 2 * mm, H + 2 * mm)
        a = _to_image(base[mm:mm + H, mm:mm + W])
        b = _to_image(base[mm + 4:mm + 4 + H, mm + 9:mm + 9 + W] * 1.02)  # 9 px / 4 px shift
        return [a, b]
    if name in PLAIN_SIZES:
        return _to_image(_scene(_seed(name), *PLAIN_SIZES[name]))
    raise KeyError(name)


def _make_photos(wr: _Writer, optional: dict, info: dict) -> None:
    W, H = PHOTO_W, PHOTO_H

    # --- beach and its copies -------------------------------------------
    p_beach = _save_photo(wr, "photos/beach.jpg", render_scene("beach"))
    wr.copy("photos/beach.jpg", "photos/backup/beach copy.jpg")
    wr.copy("photos/beach.jpg", "projects/node_modules/pkg/beach_dup.jpg")
    with Image.open(p_beach) as im:
        beach_exif = im.info.get("exif")
        decoded = im.convert("RGB")
    beach_px = np.asarray(decoded)

    decoded.resize((W // 2, H // 2), LANCZOS).save(
        wr.path("photos/beach_small.jpg"), "JPEG", quality=70, exif=beach_exif)
    wr.add("photos/beach_small.jpg")

    with open(p_beach, "rb") as f:
        beach_bytes = f.read()
    new_exif = build_exif("TestCam", "Model A", 1, BEACH_EDITED_DTO, None, None).tobytes()
    edited = splice_exif(beach_bytes, new_exif)
    assert edited[_sos_offset(edited):] == beach_bytes[_sos_offset(beach_bytes):]
    p_edit = wr.write_bytes("photos/beach_exif_edited.jpg", edited)
    with Image.open(p_edit) as im:
        assert np.array_equal(np.asarray(im.convert("RGB")), beach_px), "EXIF splice changed pixels"

    decoded.save(wr.path("photos/beach.png"), "PNG", compress_level=1)
    p_png = wr.add("photos/beach.png")
    with Image.open(p_png) as im:
        assert np.array_equal(np.asarray(im.convert("RGB")), beach_px), "PNG pixels differ"

    if pillow_heif is None:
        optional["heic"] = {"created": False, "reason": f"pillow_heif not importable ({HEIF_IMPORT_ERROR})"}
    else:
        try:
            p_heic = wr.path("photos/beach.heic")
            try:
                decoded.save(p_heic, format="HEIF", quality=90, exif=beach_exif,
                             enc_params={"preset": "ultrafast"})
            except Exception:  # encoder without a "preset" parameter
                decoded.save(p_heic, format="HEIF", quality=90, exif=beach_exif)
            with Image.open(p_heic) as im:
                im.load()
                ok = im.size == decoded.size
            if not ok:
                raise RuntimeError("HEIC read back with the wrong size")
            wr.add("photos/beach.heic")
            optional["heic"] = {"created": True, "reason": None}
        except Exception as exc:  # no HEVC encoder in this pillow_heif build
            if os.path.exists(wr.path("photos/beach.heic")):
                os.remove(wr.path("photos/beach.heic"))
            optional["heic"] = {"created": False, "reason": f"HEIC encoding failed: {type(exc).__name__}: {exc}"}
    del decoded, beach_px

    # --- rotated by flag vs rotated in pixels ----------------------------
    upright = render_scene("rotated")
    _save_photo(wr, "photos/rotated_flag.jpg", upright.transpose(Image.Transpose.ROTATE_90))
    _save_photo(wr, "photos/rotated_pixels.jpg", upright, quality=90)

    # --- burst, portrait, poor shots, a minute apart, unrelated ------------
    for i, frame in enumerate(render_scene("burst"), 1):
        _save_photo(wr, f"photos/burst/burst_{i}.jpg", frame)
    _save_photo(wr, "photos/portrait_bokeh.jpg", render_scene("portrait"))
    _save_photo(wr, "photos/blurry.jpg", render_scene("blurry"))
    _save_photo(wr, "photos/dark.jpg", render_scene("dark", info))
    _save_photo(wr, "photos/overexposed.jpg", render_scene("overexposed", info))
    a, b = render_scene("minute")
    _save_photo(wr, "photos/scene_minute_a.jpg", a)
    _save_photo(wr, "photos/scene_minute_b.jpg", b)
    _save_photo(wr, "photos/unrelated_1.jpg", render_scene("unrelated_1"))
    render_scene("unrelated_2").save(wr.path("photos/unrelated_2.png"), "PNG", compress_level=1)
    wr.add("photos/unrelated_2.png")
    _save_photo(wr, "photos/unrelated_3.jpg", render_scene("unrelated_3"))

    # --- long path twin --------------------------------------------------
    _save_photo(wr, "photos/long_twin.jpg", render_scene("long_twin"))
    dst = os.path.join(wr.outdir, *LONG_REL.split("/"))
    try:
        os.makedirs(win_long_path(os.path.dirname(dst)), exist_ok=True)
        shutil.copyfile(wr.path("photos/long_twin.jpg"), win_long_path(dst))
        wr.add(LONG_REL)
        optional["long_path"] = {"created": True, "reason": None, "path_length": len(dst),
                                 "extended_prefix": os.name == "nt"}
    except OSError as exc:
        optional["long_path"] = {"created": False, "reason": f"{type(exc).__name__}: {exc}",
                                 "path_length": len(dst), "extended_prefix": os.name == "nt"}

    # --- links -----------------------------------------------------------
    _save_photo(wr, "links/original.jpg", render_scene("links_original"))
    try:
        os.link(wr.path("links/original.jpg"), wr.path("links/hardlink.jpg"))
        wr.add("links/hardlink.jpg")
        optional["hardlink"] = {"created": True, "reason": None}
    except (OSError, NotImplementedError, AttributeError) as exc:
        optional["hardlink"] = {"created": False, "reason": f"{type(exc).__name__}: {exc}"}

    _save_photo(wr, "links/real_dir/inner.jpg", render_scene("links_inner"))
    optional["dir_link"] = _make_dir_link(wr)


def _make_dir_link(wr: _Writer) -> dict:
    target = os.path.join(wr.outdir, "links", "real_dir")
    link = os.path.join(wr.outdir, "links", "dir_link")
    errors = []
    if os.name == "nt":
        try:
            r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and os.path.isdir(link):
                wr.dir_links["links/dir_link"] = "junction"
                return {"created": True, "type": "junction", "reason": None}
            errors.append(f"mklink /J failed ({r.returncode}): {(r.stderr or r.stdout).strip()}")
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"mklink /J failed: {exc}")
        try:
            os.symlink(target, link, target_is_directory=True)
            wr.dir_links["links/dir_link"] = "symlink"
            return {"created": True, "type": "symlink", "reason": "; ".join(errors) or None}
        except (OSError, NotImplementedError) as exc:
            errors.append(f"symlink failed: {exc}")
        return {"created": False, "type": None, "reason": "; ".join(errors)}
    try:
        os.symlink("real_dir", link, target_is_directory=True)
        wr.dir_links["links/dir_link"] = "symlink"
        return {"created": True, "type": "symlink", "reason": None}
    except (OSError, NotImplementedError) as exc:
        return {"created": False, "type": None, "reason": f"symlink failed: {exc}"}


def make_test_files(outdir: str) -> dict:
    """Generate the test folder and return the manifest (also written to OUTDIR/manifest.json)."""
    t0 = time.perf_counter()
    outdir = os.path.abspath(outdir)
    _prepare_outdir(outdir)
    # a placeholder manifest marks the folder as ours, so a rerun can clean up after a crash
    _write_manifest(outdir, {"generator": "tests/make_test_files.py", "status": "incomplete"})
    wr = _Writer(outdir)
    optional: dict = {}
    info: dict = {}
    shown = pymupdf.TOOLS.mupdf_display_errors()
    pymupdf.TOOLS.mupdf_display_errors(False)
    try:
        _make_pdfs(wr, info)
        _make_photos(wr, optional, info)
    finally:
        pymupdf.TOOLS.mupdf_display_errors(shown)
    manifest = {
        "generator": "tests/make_test_files.py",
        "status": "complete",
        "version": GENERATOR_VERSION,
        "seed": SEED,
        "outdir": outdir,
        "files": sorted(wr.files),
        "dir_links": wr.dir_links,
        "optional": optional,
        "byte_copies": {k: v for k, v in BYTE_COPIES.items() if k in wr.files},
        "scenes": {name: [f for f in fs if f.split("#")[0] in wr.files] for name, fs in SCENES.items()},
        "passwords": {"pdf/protected.pdf": {"user": PROTECTED_USER_PW, "owner": PROTECTED_OWNER_PW},
                      "pdf/protected_copy.pdf": {"user": PROTECTED_USER_PW, "owner": PROTECTED_OWNER_PW}},
        "long_path": LONG_REL,
        "info": info,
        "generate_seconds": round(time.perf_counter() - t0, 2),
    }
    _write_manifest(outdir, manifest)
    t1 = time.perf_counter()
    manifest["checks"] = check(outdir, manifest)
    manifest["check_seconds"] = round(time.perf_counter() - t1, 2)
    _write_manifest(outdir, manifest)
    return manifest


def _write_manifest(outdir: str, manifest: dict) -> None:
    with open(os.path.join(outdir, MANIFEST_NAME), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")


# --------------------------------------------------------------------------
# Self-checks
# --------------------------------------------------------------------------


def _exif_seconds(dto: str, subsec: str | None) -> float:
    s = float(calendar.timegm(time.strptime(dto, "%Y:%m:%d %H:%M:%S")))
    if subsec:
        s += float("0." + subsec)
    return s


def check(outdir: str, manifest: dict | None = None) -> dict:
    """Measure the generated folder. Returns {"passed", "failures", "assertions", metrics...}."""
    outdir = os.path.abspath(outdir)
    if manifest is None:
        mp = os.path.join(outdir, MANIFEST_NAME)
        manifest = {}
        if os.path.isfile(mp):
            with open(mp, encoding="utf-8") as f:
                manifest = json.load(f)
    created = set(manifest.get("files") or [])

    def P(rel: str) -> str:
        return os.path.join(outdir, *rel.split("/"))

    def exists(rel: str) -> bool:
        return os.path.exists(win_long_path(P(rel)))

    assertions: list[dict] = []
    skipped: list[str] = []

    def expect(name: str, value, ok: bool, limit: str) -> None:
        assertions.append({"name": name, "value": value, "limit": limit, "ok": bool(ok)})

    shown = pymupdf.TOOLS.mupdf_display_errors()
    pymupdf.TOOLS.mupdf_display_errors(False)
    try:
        # ---- hashes, sharpness and exposure of every image -------------
        hashes: dict[str, int] = {}
        sharp: dict[str, float] = {}
        expo: dict[str, dict] = {}
        photo_files = sorted(r for r in (created or _walk_rel(outdir))
                             if r.lower().endswith((".jpg", ".jpeg", ".png", ".heic"))
                             and "corrupt" not in r and r not in BYTE_COPIES
                             and r != "links/hardlink.jpg")
        for rel in photo_files:
            if not exists(rel):
                continue
            img = load_reduced(P(rel))
            hashes[rel] = phash(img)
            sharp[rel] = round(sharpness(img), 2)
            expo[rel] = {k: round(v, 4) for k, v in exposure(img).items()}
        for pdf in ("pdf/scan.pdf", "pdf/scan_recompressed.pdf"):
            for n, img in enumerate(pdf_page_images(P(pdf)), 1):
                hashes[f"{pdf}#p{n}"] = phash(reduce_image(img))

        def dist(a: str, b: str) -> int | None:
            if a not in hashes or b not in hashes:
                return None
            return hamming(hashes[a], hashes[b])

        def expect_close(name, a, b, limit):
            d = dist(a, b)
            if d is None:
                skipped.append(f"{name}: {a if a not in hashes else b} missing")
                return
            expect(name, d, d <= limit, f"<= {limit} bits")

        expect_close("beach vs beach_small", "photos/beach.jpg", "photos/beach_small.jpg",
                     LIMITS["beach_small_bits"])
        expect_close("beach vs beach.heic", "photos/beach.jpg", "photos/beach.heic",
                     LIMITS["beach_heic_bits"])
        expect_close("beach vs beach.png", "photos/beach.jpg", "photos/beach.png", 0)
        expect_close("beach vs beach_exif_edited", "photos/beach.jpg", "photos/beach_exif_edited.jpg", 0)
        expect_close("rotated_flag vs rotated_pixels", "photos/rotated_flag.jpg",
                     "photos/rotated_pixels.jpg", LIMITS["rotated_bits"])
        burst = [f"photos/burst/burst_{i}.jpg" for i in range(1, 6)]
        burst_dist = []
        for a, b in zip(burst, burst[1:]):
            burst_dist.append(dist(a, b))
            expect_close(f"burst {a[-5]} vs {b[-5]}", a, b, LIMITS["burst_consecutive_bits"])
        expect_close("scan_page.jpg vs scan.pdf p1", "photos/scan_page.jpg", "pdf/scan.pdf#p1",
                     LIMITS["scan_page_vs_pdf_bits"])
        for n in (1, 2, 3):
            expect_close(f"scan vs scan_recompressed p{n}", f"pdf/scan.pdf#p{n}",
                         f"pdf/scan_recompressed.pdf#p{n}", LIMITS["scan_recompressed_bits"])

        # every pair of files from different scenes
        scenes = manifest.get("scenes") or SCENES
        members = {s: [f for f in fs if f in hashes] for s, fs in scenes.items()}
        names = list(members)
        min_pair, min_d = None, 65
        scene_min: dict[str, dict] = {}
        for i, s1 in enumerate(names):
            for s2 in names[i + 1:]:
                best = None
                for a in members[s1]:
                    for b in members[s2]:
                        d = hamming(hashes[a], hashes[b])
                        if best is None or d < best[0]:
                            best = (d, a, b)
                if best is None:
                    continue
                scene_min[f"{s1}|{s2}"] = best[0]
                if best[0] < min_d:
                    min_d, min_pair = best[0], f"{best[1]} vs {best[2]}"
        expect("distinct scenes (closest pair)", min_d, min_d >= LIMITS["distinct_scene_min_bits"],
               f">= {LIMITS['distinct_scene_min_bits']} bits ({min_pair})")
        minute_min = min((v for k, v in scene_min.items() if "minute" in k.split("|")), default=None)
        if minute_min is not None:
            expect("scene_minute vs every other scene", minute_min,
                   minute_min >= LIMITS["distinct_scene_min_bits"],
                   f">= {LIMITS['distinct_scene_min_bits']} bits")
        scan_pairs = [scene_min.get(k) for k in ("scan_p1|scan_p2", "scan_p1|scan_p3", "scan_p2|scan_p3")]
        if all(v is not None for v in scan_pairs):
            expect("scan pages vs each other", min(scan_pairs),
                   min(scan_pairs) >= LIMITS["distinct_scene_min_bits"],
                   f">= {LIMITS['distinct_scene_min_bits']} bits")

        # ---- sharpness ---------------------------------------------------
        bs = [sharp.get(b) for b in burst]
        if all(v is not None for v in bs):
            expect("burst_3 is the sharpest frame", bs[2], bs[2] == max(bs), "max of burst")
            expect("burst 2 and 4 are the blurriest", max(bs[1], bs[3]),
                   max(bs[1], bs[3]) < min(bs[0], bs[2], bs[4]), "< frames 1, 3, 5")
        blur = sharp.get("photos/blurry.jpg")
        if blur is not None:
            ratio = LIMITS["sharp_vs_blurry_ratio"]
            soft = {"photos/blurry.jpg", burst[1], burst[3]}
            sharp_set = {r: v for r, v in sharp.items() if r not in soft}
            worst = min(sharp_set, key=sharp_set.get)
            expect("portrait_bokeh / blurry", round(sharp["photos/portrait_bokeh.jpg"] / max(blur, 1e-9), 1),
                   sharp["photos/portrait_bokeh.jpg"] >= ratio * blur, f">= {ratio}")
            expect("least sharp scene / blurry", round(sharp_set[worst] / max(blur, 1e-9), 1),
                   sharp_set[worst] >= ratio * blur, f">= {ratio} ({worst})")

        # ---- exposure ----------------------------------------------------
        if "photos/dark.jpg" in expo:
            v = expo["photos/dark.jpg"]["median"]
            expect("dark median grey", v, v < LIMITS["dark_median_max"], f"< {LIMITS['dark_median_max']}")
        if "photos/overexposed.jpg" in expo:
            v = expo["photos/overexposed.jpg"]["clipped"]
            expect("overexposed clipped share", v, v > LIMITS["overexposed_clipped_min"],
                   f"> {LIMITS['overexposed_clipped_min']}")
            with Image.open(P("photos/overexposed.jpg")) as im:
                full_white = float(np.mean(np.asarray(im.convert("L")) >= 250))
            expect("overexposed share >= 250 (full size)", round(full_white, 4), full_white > 0.35, "> 0.35")
        flagged = [r for r, e in expo.items()
                   if r not in ("photos/dark.jpg", "photos/overexposed.jpg")
                   and (e["median"] < LIMITS["dark_median_max"] or e["clipped"] > LIMITS["overexposed_clipped_min"])]
        expect("no other photo has an exposure flag", len(flagged), not flagged, f"0 {flagged or ''}".strip())

        # ---- text similarity ---------------------------------------------
        jac: dict[str, float] = {}
        rep_tokens = pdf_body_tokens(P("pdf/report.pdf"))
        rep = shingles(rep_tokens)
        for rel in ("pdf/report_resaved.pdf", "pdf/report_edited.pdf", "pdf/report_cover.pdf",
                    "pdf/other_topic.pdf"):
            jac[rel] = round(jaccard(rep, shingles(pdf_body_tokens(P(rel)))), 4)
        expect("report_resaved Jaccard", jac["pdf/report_resaved.pdf"],
               jac["pdf/report_resaved.pdf"] == 1.0, "== 1")
        expect("report_edited Jaccard", jac["pdf/report_edited.pdf"],
               jac["pdf/report_edited.pdf"] > LIMITS["edited_jaccard_min"], f"> {LIMITS['edited_jaccard_min']}")
        expect("report_cover Jaccard", jac["pdf/report_cover.pdf"],
               jac["pdf/report_cover.pdf"] > LIMITS["cover_jaccard_min"], f"> {LIMITS['cover_jaccard_min']}")
        expect("other_topic Jaccard", jac["pdf/other_topic.pdf"],
               jac["pdf/other_topic.pdf"] < LIMITS["other_topic_jaccard_max"],
               f"< {LIMITS['other_topic_jaccard_max']}")
        with pymupdf.open(P("pdf/report.pdf")) as a, pymupdf.open(P("pdf/report_resaved.pdf")) as b:
            same_text = [p.get_text() for p in a] == [p.get_text() for p in b]
            meta_differs = a.metadata.get("title") != b.metadata.get("title") and \
                a.metadata.get("author") != b.metadata.get("author")
        size_a, size_b = os.path.getsize(P("pdf/report.pdf")), os.path.getsize(P("pdf/report_resaved.pdf"))
        expect("report_resaved: same text, new title/author, other size",
               f"{size_a} vs {size_b} bytes", same_text and meta_differs and size_a != size_b, "text equal")
        other_words = len(pdf_body_tokens(P("pdf/other_topic.pdf")))
        expect("report words", len(rep_tokens), 2300 <= len(rep_tokens) <= 2800, "~2,500")
        expect("other_topic words", other_words, 1800 <= other_words <= 2300, "~2,000")
        bad_pages = []
        for rel, first_n, total in (("pdf/report.pdf", 1, 5), ("pdf/report_edited.pdf", 1, 5),
                                    ("pdf/report_resaved.pdf", 1, 5), ("pdf/report_cover.pdf", 2, 6)):
            with pymupdf.open(P(rel)) as d:
                if d.page_count != total:
                    bad_pages.append(f"{rel}: {d.page_count} pages")
                    continue
                if rel == "pdf/report_cover.pdf" and REPORT_HEADER in d[0].get_text():
                    bad_pages.append(f"{rel}: header on the cover")
                for n in range(first_n, total + 1):
                    text = d[n - 1].get_text()
                    if REPORT_HEADER not in text or FOOTER_FMT.format(n=n, total=total) not in text:
                        bad_pages.append(f"{rel} p{n}")
        expect("report headers/footers (Page N of 5/6)", f"{len(bad_pages)} bad pages", not bad_pages,
               "; ".join(bad_pages[:2]) or "all pages")

        # ---- PDFs: scans, protection, corruption ---------------------------
        for pdf in ("pdf/scan.pdf", "pdf/scan_recompressed.pdf"):
            with pymupdf.open(P(pdf)) as d:
                chars = sum(len(p.get_text().strip()) for p in d)
                imgs = [len(p.get_images()) for p in d]
                sizes = {(round(p.rect.width), round(p.rect.height)) for p in d}
            expect(f"{pdf}: image-only", f"{chars} chars, images {imgs}, page {sorted(sizes)}",
                   chars == 0 and imgs == [1, 1, 1] and sizes == {(620, 877)}, "0 chars, 620x877 pt")
        for pdf in ("pdf/protected.pdf", "pdf/protected_copy.pdf"):
            with pymupdf.open(P(pdf)) as d:
                needs = bool(d.needs_pass)
                unlocked = d.authenticate(PROTECTED_USER_PW) > 0
                enc = d.metadata.get("encryption")
            expect(f"{pdf}: password", f"{enc}", needs and unlocked and "AES" in str(enc), "needs_pass, AES-256")
        try:
            with pymupdf.open(P("pdf/corrupt.pdf")) as d:
                corrupt_pdf = f"opened ({d.page_count} pages)"
                corrupt_ok = d.page_count == 0
        except Exception as exc:
            corrupt_pdf, corrupt_ok = f"{type(exc).__name__}", True
        expect("corrupt.pdf fails to open", corrupt_pdf, corrupt_ok, "error")
        try:
            with Image.open(P("photos/corrupt.jpg")) as im:
                im.load()
            corrupt_jpg, ok = "decoded", False
        except Exception as exc:
            corrupt_jpg, ok = type(exc).__name__, True
        expect("corrupt.jpg fails to decode", corrupt_jpg, ok, "error")

        # ---- byte copies, links, long path ---------------------------------
        bad = [c for c, o in BYTE_COPIES.items() if exists(c) and _sha256(P(c)) != _sha256(P(o))]
        present = [c for c in BYTE_COPIES if exists(c)]
        expect("byte copies identical", f"{len(present) - len(bad)}/{len(present)}", not bad, "all")
        if exists("links/hardlink.jpg"):
            a, b = os.stat(P("links/original.jpg")), os.stat(P("links/hardlink.jpg"))
            expect("hardlink shares the file", f"nlink={b.st_nlink}",
                   (a.st_ino, a.st_dev) == (b.st_ino, b.st_dev) and b.st_nlink >= 2, "same inode")
        else:
            skipped.append("hardlink: not created")
        if os.path.lexists(P("links/dir_link")):
            inner = os.path.join(P("links/dir_link"), "inner.jpg")
            expect("dir_link shows real_dir", "ok" if os.path.isfile(inner) else "missing",
                   os.path.isfile(inner), "inner.jpg visible")
        else:
            skipped.append("dir_link: not created")
        if exists(LONG_REL):
            n = len(P(LONG_REL))
            expect("long path length", n, n > LIMITS["long_path_min_chars"],
                   f"> {LIMITS['long_path_min_chars']} chars")
        else:
            skipped.append("long path: not created")

        # ---- EXIF ------------------------------------------------------------
        ex = {rel: read_exif(P(rel)) for rel in sorted(PHOTO_EXIF) if exists(rel)}
        if exists("photos/beach_exif_edited.jpg"):
            ex["photos/beach_exif_edited.jpg"] = read_exif(P("photos/beach_exif_edited.jpg"))
        if exists("photos/beach_small.jpg"):
            ex["photos/beach_small.jpg"] = read_exif(P("photos/beach_small.jpg"))
        b = ex.get("photos/beach.jpg", {})
        e = ex.get("photos/beach_exif_edited.jpg", {})
        s = ex.get("photos/beach_small.jpg", {})
        expect("beach EXIF read back", f"{b.get('make')}/{b.get('model')} {b.get('datetime_original')} gps={b.get('has_gps')}",
               (b.get("make"), b.get("model"), b.get("datetime_original"), b.get("has_gps"))
               == ("TestCam", "Model A", "2023:07:15 14:30:00", True), "TestCam/Model A, GPS")
        expect("beach_exif_edited EXIF", f"{e.get('datetime_original')} gps={e.get('has_gps')}",
               e.get("datetime_original") == BEACH_EDITED_DTO and not e.get("has_gps"), "new date, no GPS")
        expect("beach_small keeps EXIF", f"{s.get('datetime_original')} gps={s.get('has_gps')}",
               s.get("datetime_original") == "2023:07:15 14:30:00" and s.get("has_gps"), "same as beach")
        mism = []
        for rel, (make, model, dto, subsec, orient, gps) in PHOTO_EXIF.items():
            got = ex.get(rel)
            if got is None:
                continue
            want = {"make": make, "model": model, "orientation": orient, "datetime_original": dto,
                    "subsec": subsec, "has_gps": bool(gps)}
            for k, v in want.items():
                if got.get(k) != v:
                    mism.append(f"{rel}:{k}={got.get(k)!r}")
        expect("EXIF tags of every photo", f"{len(mism)} mismatches", not mism, "; ".join(mism[:3]) or "all match")
        if exists("photos/beach.png") and exists("photos/beach_exif_edited.jpg"):
            with Image.open(P("photos/beach.jpg")) as a0:
                ref = np.asarray(a0.convert("RGB"))
            same = []
            for rel in ("photos/beach.png", "photos/beach_exif_edited.jpg"):
                with Image.open(P(rel)) as im:
                    same.append(np.array_equal(np.asarray(im.convert("RGB")), ref))
            expect("beach.png and beach_exif_edited pixels == beach", str(same), all(same), "identical")
        with Image.open(P("photos/rotated_flag.jpg")) as a0, Image.open(P("photos/rotated_pixels.jpg")) as b0:
            sizes = (ImageOps.exif_transpose(a0).size, b0.size)
        expect("rotated_flag upright size", f"{sizes[0]} vs {sizes[1]}", sizes[0] == sizes[1], "equal")

        # capture-time chains: only the burst (and exact-time copies) may chain
        shots = []
        for rel, got in ex.items():
            if got.get("datetime_original"):
                shots.append(((got.get("make") or "", got.get("model") or ""),
                              _exif_seconds(got["datetime_original"], got.get("subsec")), rel))
        shots.sort()
        bad_chain = []
        for (c1, t1, r1), (c2, t2, r2) in zip(shots, shots[1:]):
            if c1 == c2 and abs(t2 - t1) <= LIMITS["burst_gap_seconds"]:
                is_burst = r1 in burst and r2 in burst
                is_copy = t1 == t2 and r1.startswith("photos/beach") and r2.startswith("photos/beach")
                if not (is_burst or is_copy):
                    bad_chain.append(f"{r1}+{r2}")
        expect("no unintended burst chains", len(bad_chain), not bad_chain, "0 " + " ".join(bad_chain))
    finally:
        pymupdf.TOOLS.mupdf_display_errors(shown)

    failures = [a["name"] for a in assertions if not a["ok"]]
    return {
        "passed": not failures,
        "failures": failures,
        "skipped": skipped,
        "assertions": assertions,
        "sharpness": sharp,
        "exposure": expo,
        "phash": {k: f"{v:016x}" for k, v in sorted(hashes.items())},
        "burst_consecutive_bits": burst_dist,
        "scene_min_bits": scene_min,
        "jaccard_vs_report": jac,
    }


def _walk_rel(outdir: str) -> list[str]:
    out = []
    for root, dirs, files in os.walk(outdir):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for f in files:
            out.append(os.path.relpath(os.path.join(root, f), outdir).replace(os.sep, "/"))
    return out


def _cell(value, width: int) -> str:
    text = str(value)
    return text if len(text) <= width else text[: width - 3] + "..."


def format_report(result: dict, manifest: dict | None = None) -> str:
    """The self-check table as plain text."""
    lines = []
    w = max(len(a["name"]) for a in result["assertions"]) + 2
    lines.append(f"{'check':<{w}}{'value':<32}{'limit':<40}result")
    lines.append("-" * (w + 78))
    for a in result["assertions"]:
        lines.append(f"{a['name']:<{w}}{_cell(a['value'], 30):<32}{_cell(a['limit'], 38):<40}"
                     f"{'ok' if a['ok'] else 'FAIL'}")
    lines.append("")
    lines.append(f"{'photo (reduced to <= 1024 px)':<40}{'sharpness':>10}{'median':>8}{'clipped':>9}")
    for rel in sorted(result["sharpness"]):
        e = result["exposure"][rel]
        lines.append(f"{rel:<40}{result['sharpness'][rel]:>10.1f}{e['median']:>8.0f}{e['clipped']:>9.3f}")
    lines.append("")
    lines.append(f"burst consecutive pHash bits: {result['burst_consecutive_bits']}")
    close = sorted(result["scene_min_bits"].items(), key=lambda kv: kv[1])[:3]
    lines.append("closest distinct scenes: " + ", ".join(f"{k} {v}" for k, v in close))
    lines.append("5-shingle Jaccard vs report.pdf: "
                 + ", ".join(f"{k.split('/')[-1]} {v}" for k, v in result["jaccard_vs_report"].items()))
    if manifest and manifest.get("optional"):
        for k, v in manifest["optional"].items():
            state = "created" if v.get("created") else "SKIPPED"
            extra = f" ({v.get('type')})" if v.get("type") else ""
            reason = f": {v['reason']}" if v.get("reason") else ""
            lines.append(f"optional {k}: {state}{extra}{reason}")
    if result["skipped"]:
        lines.append("skipped checks: " + "; ".join(result["skipped"]))
    lines.append("SELF-CHECK " + ("PASSED" if result["passed"] else "FAILED: " + ", ".join(result["failures"])))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build DupeFinder's test folder.")
    ap.add_argument("outdir", help="folder to create (must be empty or a previous output)")
    ap.add_argument("--check", action="store_true",
                    help="print the self-check table and exit 1 if any check fails")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles may not take every character
        sys.stdout.reconfigure(errors="replace")
    manifest = make_test_files(args.outdir)
    result = manifest["checks"]
    if args.check:
        print(format_report(result, manifest))
    else:
        print(f"wrote {len(manifest['files'])} files to {manifest['outdir']} "
              f"in {manifest['generate_seconds'] + manifest['check_seconds']:.1f} s; "
              f"self-check {'passed' if result['passed'] else 'FAILED (run with --check for details)'}")
    return 0 if (result["passed"] or not args.check) else 1


if __name__ == "__main__":
    sys.exit(main())
