"""The design's test plan, run on the generated test folder (tests/make_test_files.py)."""

import os
import time

import pytest

import dupefinder as df
from conftest import run


def kind_of(rel):
    return rel[0] if rel else None


# -- exact and same-content copies ----------------------------------------------

def test_renamed_copies_are_identical(scan):
    assert kind_of(scan.relation("pdf/archive/report renamed.pdf", "pdf/report.pdf")) == "identical"
    assert kind_of(scan.relation("photos/backup/beach copy.jpg", "photos/beach.jpg")) == "identical"
    assert scan.same_group("pdf/report.pdf", "pdf/archive/report renamed.pdf")
    assert scan.same_group("photos/beach.jpg", "photos/backup/beach copy.jpg")


def test_resaved_pdf_has_same_text(scan):
    rel = scan.relation("pdf/report_resaved.pdf", "pdf/report.pdf")
    assert rel[0] == "same_text" and rel[1] == 100
    assert scan.same_group("pdf/report.pdf", "pdf/report_resaved.pdf")


def test_edited_pdf_is_similar_above_90(scan):
    kind, score, _ = scan.relation("pdf/report_edited.pdf", "pdf/report.pdf")
    assert kind == "similar_text" and 90 < score < 100
    assert scan.same_group("pdf/report.pdf", "pdf/report_edited.pdf")


def test_extra_cover_page_is_similar_above_85(scan):
    kind, score, _ = scan.relation("pdf/report_cover.pdf", "pdf/report.pdf")
    assert kind == "similar_text" and 85 < score < 100
    assert scan.same_group("pdf/report.pdf", "pdf/report_cover.pdf")


def test_different_pdf_on_same_topic_is_not_grouped(scan):
    assert scan.group("pdf/other_topic.pdf") is None
    assert scan.relation("pdf/other_topic.pdf", "pdf/report.pdf") is None


def test_image_only_pdf_and_recompressed_copy_match_visually(scan):
    kind, score, _ = scan.relation("pdf/scan_recompressed.pdf", "pdf/scan.pdf")
    assert kind == "visual_pdf" and score >= 85
    assert scan.same_group("pdf/scan.pdf", "pdf/scan_recompressed.pdf")


def test_resized_recompressed_photo_matches_90_or_more(scan):
    kind, score, _ = scan.relation("photos/beach_small.jpg", "photos/beach.jpg")
    assert kind == "visual" and score >= 90
    assert scan.same_group("photos/beach.jpg", "photos/beach_small.jpg")


def test_exif_edited_photo_has_same_pixels(scan):
    rel = scan.relation("photos/beach_exif_edited.jpg", "photos/beach.jpg")
    assert rel[0] == "same_pixels" and rel[1] == 100
    assert scan.same_group("photos/beach.jpg", "photos/beach_exif_edited.jpg")


def test_png_converted_from_jpg_has_same_pixels(scan):
    assert kind_of(scan.relation("photos/beach.png", "photos/beach.jpg")) == "same_pixels"
    assert scan.same_group("photos/beach.jpg", "photos/beach.png")


def test_heic_copy_matches(scan, dataset):
    if "photos/beach.heic" not in scan.by_rel:
        pytest.skip("HEIC encoding not available on this machine")
    assert kind_of(scan.relation("photos/beach.heic", "photos/beach.jpg")) in ("visual", "same_pixels")
    assert scan.same_group("photos/beach.jpg", "photos/beach.heic")


def test_rotation_flag_vs_rotated_pixels(scan):
    assert kind_of(scan.relation("photos/rotated_flag.jpg", "photos/rotated_pixels.jpg")) in (
        "visual", "same_pixels")
    assert scan.same_group("photos/rotated_flag.jpg", "photos/rotated_pixels.jpg")
    r = scan.rec("photos/rotated_flag.jpg")
    p = scan.rec("photos/rotated_pixels.jpg")
    assert (r.meta["w"], r.meta["h"]) == (p.meta["w"], p.meta["h"])


def test_duplicate_group_suggestions(scan):
    g = scan.group("photos/beach.jpg")
    keep = [m for m in g.members if m.suggest == "keep"]
    assert len(keep) == 1 and keep[0].keeper
    assert g.space(scan.result.recs) > 0


# -- bursts and quality ------------------------------------------------------------

BURST = [f"photos/burst/burst_{i}.jpg" for i in range(1, 6)]


def test_burst_grouped_and_sharpest_kept(scan):
    g = scan.group(BURST[0])
    assert g is not None and g.type == "burst" and g.burst_shots == 5
    assert {scan.result.recs[m.fid].path for m in g.members} == {scan.rec(b).path for b in BURST}
    assert scan.member("photos/burst/burst_3.jpg").keeper
    for b in ("photos/burst/burst_2.jpg", "photos/burst/burst_4.jpg"):
        m = scan.member(b)
        assert m.suggest == "trash" and m.rel_sharpness < 60
    assert scan.member("photos/burst/burst_3.jpg").rel_sharpness == 100


def test_burst_members_listed_in_time_order(scan):
    g = scan.group(BURST[0])
    times = [scan.result.recs[m.fid].taken_at for m in g.members]
    assert times == sorted(times)


def test_portrait_with_blurred_background_not_flagged(scan):
    m = scan.member("photos/portrait_bokeh.jpg")
    assert m is None or "blurry" not in m.flags
    r = scan.rec("photos/portrait_bokeh.jpg")
    assert r.sharpness >= scan.result.opts.blur_cutoff


def test_single_blurry_photo_listed_not_preselected(scan):
    g = scan.group("photos/blurry.jpg")
    assert g is not None and g.type == "poor"
    m = scan.member("photos/blurry.jpg")
    assert "blurry" in m.flags and m.suggest == "keep"


def test_exposure_flags(scan):
    assert "dark" in scan.member("photos/dark.jpg").flags
    assert "clipped" in scan.member("photos/overexposed.jpg").flags
    for rel in ("photos/dark.jpg", "photos/overexposed.jpg"):
        assert scan.member(rel).suggest == "keep"


def test_photos_a_minute_apart_are_not_a_burst(scan):
    for rel in ("photos/scene_minute_a.jpg", "photos/scene_minute_b.jpg"):
        g = scan.group(rel)
        assert g is None or g.type != "burst"
        assert scan.rec(rel).fid not in scan.result.matcher.burst_chain


def test_copies_with_same_capture_time_are_not_a_burst(scan):
    # beach.jpg, its byte copy and its resized copy share one capture time.
    assert scan.group("photos/beach.jpg").type != "burst"


# -- cross-type, unrelated files ---------------------------------------------------

def test_photo_matching_a_scanned_page_is_related(scan):
    kind, _, extra = scan.relation("photos/scan_page.jpg", "pdf/scan.pdf")
    assert kind == "related" and extra["page"] == 1
    g = scan.group("photos/scan_page.jpg")
    assert g is not None and g.type == "related"
    assert scan.same_group("photos/scan_page.jpg", "pdf/scan.pdf")
    m = scan.member("photos/scan_page.jpg")
    assert m.suggest == "keep" and m.label_kind in ("related", "keeper")


def test_unrelated_photos_are_not_grouped(scan):
    for rel in ("photos/unrelated_1.jpg", "photos/unrelated_2.png", "photos/unrelated_3.jpg"):
        g = scan.group(rel)
        assert g is None or g.type == "poor"


def test_every_trash_suggestion_scores_against_keeper(scan):
    for g in scan.result.groups:
        if g.type == "poor":
            assert all(m.suggest == "keep" for m in g.members)
            continue
        assert sum(m.keeper for m in g.members) == 1
        assert any(m.suggest == "keep" for m in g.members)
        for m in g.members:
            if m.suggest == "trash":
                assert m.label_kind in df.PRESELECT_KINDS
                if m.label_kind not in ("burst",):
                    assert m.score is not None


# -- whole-drive safeguards ----------------------------------------------------------

def test_hard_link_and_dir_link_not_duplicates(scan, dataset):
    _, manifest = dataset
    present = [r for r in ("links/original.jpg", "links/hardlink.jpg") if r in scan.by_rel]
    assert len(present) >= 1
    if len(present) == 1:      # hard link created: one file, two paths, counted once
        assert scan.result.stats.same_file >= 1
    for rel in present:
        g = scan.group(rel)
        assert g is None or g.type == "poor"
    inner = [r for r in scan.by_rel if r.endswith("inner.jpg")]
    assert inner == ["links/real_dir/inner.jpg"]


def test_online_only_file_skipped_and_counted(scan):
    assert "onedrive/online_only.pdf" not in scan.by_rel
    assert scan.result.stats.online_only >= 1


def test_protected_and_corrupt_files_logged(scan):
    s = scan.result.stats
    assert any(p.endswith("protected.pdf") for p in s.protected)
    errs = {os.path.basename(p) for p, _ in s.errors}
    assert {"corrupt.pdf", "corrupt.jpg"} <= errs
    # Protected PDFs still get the file fingerprint check.
    assert kind_of(scan.relation("pdf/protected_copy.pdf", "pdf/protected.pdf")) == "identical"
    assert scan.same_group("pdf/protected.pdf", "pdf/protected_copy.pdf")


def test_system_folders_skipped_by_default(scan):
    assert not any("node_modules" in r for r in scan.by_rel)
    assert scan.result.stats.system_skipped >= 1


def test_include_system_never_preselects(dataset, tmp_path):
    root, _ = dataset
    s = run(root, tmp_path / "cache", "--include-system", "--types", "image")
    m = s.member("projects/node_modules/pkg/beach_dup.jpg")
    assert m is not None and "system" in m.flags and m.suggest == "keep"
    assert not m.keeper


def test_long_path_copy_found(scan):
    deep = [r for r in scan.by_rel if r.endswith("deep_copy.jpg")]
    assert len(deep) == 1 and len(scan.rec(deep[0]).path) > 260
    assert scan.same_group("photos/long_twin.jpg", deep[0])
    assert kind_of(scan.relation(deep[0], "photos/long_twin.jpg")) == "identical"


def test_rescan_with_no_changes_uses_cache(dataset, tmp_path):
    root, _ = dataset
    t0 = time.monotonic()
    first = run(root, tmp_path / "c", "--types", "image")
    t1 = time.monotonic()
    second = run(root, tmp_path / "c", "--types", "image")
    t2 = time.monotonic()
    assert first.result.stats.analysed > 0
    assert second.result.stats.analysed == 0
    assert second.result.stats.from_cache == len(second.result.recs)
    assert t2 - t1 < (t1 - t0)
    sig = lambda s: sorted(sorted(s.result.recs[m.fid].path for m in g.members)
                           for g in s.result.groups)
    assert sig(first) == sig(second)


def test_rescan_flag_recomputes(dataset, tmp_path):
    root, _ = dataset
    run(root, tmp_path / "c", "--types", "pdf")
    again = run(root, tmp_path / "c", "--types", "pdf", "--rescan")
    assert again.result.stats.analysed == len(again.result.recs)


# -- more photo formats ---------------------------------------------------------------------

def test_common_photo_formats_and_raw_preview(dataset, tmp_path):
    from PIL import Image
    root, _ = dataset
    work = tmp_path / "fmt"
    work.mkdir()
    src = Image.open(root / "photos/beach.jpg")
    exif = src.getexif()
    (work / "beach.jpg").write_bytes((root / "photos/beach.jpg").read_bytes())
    src.save(work / "beach.webp", quality=85)
    src.save(work / "beach.bmp")
    src.save(work / "beach.tiff")
    src.convert("P", palette=Image.Palette.ADAPTIVE).save(work / "beach.gif")
    try:
        src.save(work / "beach.avif", quality=70)
    except Exception:
        pass
    # A DNG-like RAW: TIFF container with the camera's EXIF, plus a full-size JPEG preview.
    thumb = src.resize((256, 192))
    thumb.save(work / "beach.dng", format="TIFF", exif=exif)
    with open(work / "beach.dng", "ab") as f:
        f.write((root / "photos/beach.jpg").read_bytes())
    s = run(work, tmp_path / "c", "--types", "image")
    assert not s.result.stats.errors
    names = [p for p in s.by_rel if p != "beach.jpg"]
    assert {"beach.webp", "beach.bmp", "beach.tiff", "beach.gif", "beach.dng"} <= set(names)
    assert s.same_group("beach.jpg", *names)
    assert kind_of(s.relation("beach.bmp", "beach.jpg")) == "same_pixels"
    assert kind_of(s.relation("beach.tiff", "beach.jpg")) == "same_pixels"
    raw = s.rec("beach.dng")
    assert (raw.meta["w"], raw.meta["h"]) == (2000, 1500) and raw.taken_at is not None
    assert s.member("beach.dng").suggest == "keep" or s.member("beach.dng").keeper
    if not s.member("beach.dng").keeper:
        assert "RAW" in s.member("beach.dng").detail
