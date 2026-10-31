"""Duplicate and blur checks on real photographs (tests/data, public domain / CC0)."""

import io
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

import dupefinder as df
from conftest import run

DATA = Path(__file__).parent / "data"
PHOTOS = ["astronaut", "coffee", "chelsea", "rocket"]


def _jpeg(im, q):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=q)
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")


def _motion(im, n):
    a = np.asarray(im, dtype=np.float32)
    out = sum(np.roll(a, k - n // 2, axis=1) for k in range(n)) / n
    return Image.fromarray(out.astype(np.uint8))


def _noise(im, sigma):
    a = np.asarray(im, dtype=np.float32) + np.random.default_rng(1).normal(0, sigma, np.asarray(im).shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def _crop(im):
    w, h = im.size
    if max(w, h) > 1.15 * min(w, h):
        s = min(w, h)
        return im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    return im.crop((w // 10, h // 10, w - w // 10, h - h // 10))


def _warm(im):
    r, g, b = im.split()
    return Image.merge("RGB", (r.point(lambda v: min(255, v + 12)), g, b.point(lambda v: max(0, v - 12))))


def _mark(im):
    im = im.copy()
    ImageDraw.Draw(im).text((10, im.height - 24), "(c) 2024 Holiday", fill=(255, 255, 255))
    return im


COPIES = {   # name -> (make, expected "how" or None)
    "small": (lambda im: _jpeg(im.resize((im.width // 2, im.height // 2)), 70), None),
    "turned": (lambda im: im.transpose(Image.Transpose.ROTATE_90), "rotated"),
    "mirrored": (lambda im: ImageOps.mirror(im), "mirrored"),
    "cropped": (_crop, "cropped"),
    "warm": (_warm, None),
    "grey": (lambda im: ImageOps.grayscale(im).convert("RGB"), None),
    "bright": (lambda im: ImageEnhance.Brightness(im).enhance(1.15), None),
    "marked": (_mark, None),
}


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    root = tmp_path_factory.mktemp("real")
    for p in PHOTOS:
        im = Image.open(DATA / f"{p}.jpg").convert("RGB")
        shutil.copy(DATA / f"{p}.jpg", root / f"{p}.jpg")
        for name, (make, _) in COPIES.items():
            make(im).save(root / f"{p}_{name}.png" if name != "small" else root / f"{p}_{name}.jpg")
        r, g, b = im.split()
        Image.merge("RGB", (b, g, r)).save(root / f"{p}_colourswap.png")
    return run(root, tmp_path_factory.mktemp("cache"), "--types", "image")


@pytest.mark.parametrize("photo", PHOTOS)
@pytest.mark.parametrize("copy", list(COPIES))
def test_real_copies_are_found(real, photo, copy):
    ext = "jpg" if copy == "small" else "png"
    rel = real.relation(f"{photo}_{copy}.{ext}", f"{photo}.jpg")
    assert rel is not None and rel[0] in ("visual", "same_pixels"), rel
    assert rel[1] >= 87.5
    assert rel[2].get("how") == COPIES[copy][1]
    assert real.same_group(f"{photo}.jpg", f"{photo}_{copy}.{ext}")


@pytest.mark.parametrize("photo", PHOTOS)
def test_colour_swapped_copy_never_preselected(real, photo):
    assert real.relation(f"{photo}_colourswap.png", f"{photo}.jpg") is None
    m = real.member(f"{photo}_colourswap.png")
    assert m is None or m.suggest == "keep"


def test_different_real_photos_never_match(real):
    for i, a in enumerate(PHOTOS):
        for b in PHOTOS[i + 1:]:
            assert real.relation(f"{a}.jpg", f"{b}.jpg") is None
            assert not real.same_group(f"{a}.jpg", f"{b}.jpg")


def test_mirrored_and_cropped_copies_not_preselected(real):
    for p in PHOTOS:
        for copy in ("mirrored", "cropped"):
            m = real.member(f"{p}_{copy}.png")
            assert m.suggest == "keep" or m.keeper


BLUR_CASES = {   # name -> (make, should be flagged blurry)
    "sharp": (lambda im: im, False),
    "noisy": (lambda im: _noise(im, 12), False),
    "dark": (lambda im: ImageEnhance.Brightness(im).enhance(0.3), False),
    "compressed": (lambda im: _jpeg(im, 30), False),
    "blurred": (lambda im: im.filter(ImageFilter.GaussianBlur(3)), True),
    "motion": (lambda im: _motion(im, 15), True),
}


@pytest.mark.parametrize("photo", PHOTOS)
@pytest.mark.parametrize("case", list(BLUR_CASES))
def test_real_blur_flags(tmp_path, photo, case):
    make, blurry = BLUR_CASES[case]
    path = tmp_path / f"{photo}_{case}.png"
    make(Image.open(DATA / f"{photo}.jpg").convert("RGB")).save(path)
    score = df.analyse_image(str(path))["sharpness"]
    assert (score < df.DEFAULT_BLUR_CUTOFF) == blurry, score
