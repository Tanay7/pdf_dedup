"""Large scans must finish without crashing or running out of memory."""

import shutil
import time

import numpy as np
from PIL import Image

import dupefinder as df
from conftest import run


def test_matching_60k_photos_with_near_identical_flood(tmp_path):
    rng = np.random.default_rng(7)
    n = 60_000
    hashes = [int(h) for h in rng.integers(0, 2**63, n, dtype=np.uint64)]
    base = hashes[0]
    for k in range(1, 3001):              # 3,000 near-identical images (e.g. screenshots)
        hashes[k] = base ^ (1 << int(rng.integers(0, 64)))
    for k in range(5000, 5500):           # 500 resized-copy pairs
        hashes[k + 500] = hashes[k] ^ 1
    recs = []
    for i, h in enumerate(hashes):
        r = df.FileRec(fid=i, path=f"/x/{i}.jpg", kind="image", size=1000 + i, mtime_ns=i)
        r.phash, r.analysed, r.sharpness = h, True, 100.0
        r.meta = {"w": 100, "h": 100, "flat": False}
        r.content_sha = f"px{i}"           # no pixel fingerprints needed
        recs.append(r)
    opts = df.parse_args([str(tmp_path), "--types", "image", "--workers", "2"])
    t = time.time()
    m = df.Matcher(recs, opts, lambda op, fids: {})
    links = m.match_all()
    groups = df.Grouper(recs, m, opts).build(links)
    assert time.time() - t < 120
    per_file = np.bincount([ln.a for ln in links], minlength=n)
    assert per_file.max() <= df.MAX_MATCHES_PER_FILE + 1
    sizes = sorted((len(g.members) for g in groups if g.type != "poor"), reverse=True)
    assert sizes[0] == 3001                # the flood still forms one group
    assert sizes.count(2) >= 500           # every resized-copy pair found


def test_scan_of_thousands_of_real_files(tmp_path):
    root = tmp_path / "many"
    rng = np.random.default_rng(3)
    for i in range(2000):
        sub = root / f"folder{i % 40:02d}"
        sub.mkdir(parents=True, exist_ok=True)
        img = Image.fromarray(rng.integers(0, 255, (48, 64, 3), dtype=np.uint8))
        img.save(sub / f"img{i:04d}.{'png' if i % 5 == 0 else 'jpg'}")
    for i in range(0, 2000, 100):          # 20 exact copies
        src = next(root.glob(f"folder*/img{i:04d}.*"))
        shutil.copy(src, src.with_name("copy_" + src.name))
    s = run(root, tmp_path / "cache", "--types", "image")
    st = s.result.stats
    assert len(s.result.recs) == 2020 and not st.errors
    identical = [g for g in s.result.groups if g.type == "identical"]
    assert len(identical) >= 20


def test_report_for_20k_groups_builds_fast_and_parses(tmp_path):
    import json
    import re
    data = {"mode": "server", "token": "t", "generated": "", "summary": {}, "skipped": {},
            "groups": [{"id": g, "type": "similar", "kind": "image", "title": "2 similar files",
                        "space": 1, "min_score": 90, "burst_shots": None,
                        "members": [{"fid": 2 * g + k, "name": f"<x>{g}.jpg", "path": f"D:\\a\\{g}.jpg",
                                     "keeper": k == 0} for k in range(2)]}
                       for g in range(20000)]}
    t = time.time()
    html = df.render_report(data)
    assert time.time() - t < 10
    block = re.search(r'<script id="df-data" type="application/json">(.*?)</script>', html, re.S).group(1)
    assert "<x>" not in block                     # names can't break out of the data block
    assert len(json.loads(block)["groups"]) == 20000
    assert 'JSON.parse(document.getElementById("df-data").textContent)' in html
