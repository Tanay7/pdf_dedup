"""Moving files, the local server, offline guarantees and the command line."""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import dupefinder as df
from conftest import ROOT, run

SUBSET = ["photos/beach.jpg", "photos/backup/beach copy.jpg", "photos/beach_small.jpg",
          "pdf/report.pdf", "pdf/archive/report renamed.pdf", "photos/blurry.jpg"]


@pytest.fixture
def served(dataset, tmp_path, monkeypatch):
    root, _ = dataset
    work = tmp_path / "work"
    for rel in SUBSET:
        dest = work / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, dest)
    # Review folders go to the test folder, not the real drive root.
    monkeypatch.setattr(df, "volume_root", lambda p: str(work))
    s = run(work, tmp_path / "cache")
    server = df.ReportServer(s.result, s.cache)
    server.start()
    yield s, server
    server.stop()


NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(server, path, body=None, token=True, headers=None):
    url = f"http://127.0.0.1:{server.port}{path}"
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
        if token:
            h.setdefault("X-Token", server.token)
    elif token:
        url += ("&" if "?" in url else "?") + "token=" + server.token
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if body is not None else "GET")
    try:
        with NO_PROXY.open(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def move(server, fids):
    code, body = call(server, "/api/move", {"ids": fids})
    assert code == 200
    return {r["fid"]: r for r in json.loads(body)["results"]}


def fids_of(s, *rels):
    return [s.rec(r).fid for r in rels]


# -- server access rules ------------------------------------------------------------

def test_report_needs_token(served):
    s, server = served
    assert call(server, "/", token=False)[0] == 403
    code, body = call(server, "/")
    assert code == 200 and b"window.DF_DATA" in body
    assert call(server, "/api/move", {"ids": []}, token=False)[0] == 403


def test_other_origins_and_hosts_refused(served):
    s, server = served
    assert call(server, "/api/move", {"ids": []}, headers={"Origin": "https://evil.example"})[0] == 403
    assert call(server, "/", headers={"Host": "evil.example"})[0] == 403
    assert call(server, "/", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403


def test_thumbnail_and_view(served):
    s, server = served
    fid = s.rec("photos/beach.jpg").fid
    code, body = call(server, f"/thumb/{fid}")
    assert code == 200 and body[:2] == b"\xff\xd8"
    assert call(server, f"/view/{fid}")[0] == 200
    assert call(server, f"/crop/{fid}/sharp")[0] == 200
    assert call(server, "/thumb/999999")[0] == 404


# -- moving files ------------------------------------------------------------------------

def test_trash_every_copy_refused(served):
    s, server = served
    g = s.group("photos/beach.jpg")
    res = move(server, [m.fid for m in g.members])
    assert all(not r["ok"] for r in res.values())
    assert all("no copy" in r["reason"] for r in res.values())
    for m in g.members:
        assert os.path.exists(s.result.recs[m.fid].path)


def test_move_to_recycle_bin_and_log(served, monkeypatch):
    s, server = served
    trashed = []
    if sys.platform != "linux":   # don't fill the CI machine's real Recycle Bin
        def fake_trash(p):
            trashed.append(p)
            os.remove(p)
        monkeypatch.setattr(df, "trash_file", fake_trash)
    fid = s.rec("photos/backup/beach copy.jpg").fid
    res = move(server, [fid])[fid]
    assert res["ok"] and res["action"] == "recycle"
    assert not os.path.exists(s.rec("photos/backup/beach copy.jpg").path)
    if sys.platform == "linux":
        trash = Path(os.environ["XDG_DATA_HOME"]) / "Trash" / "files"
        assert any(p.name.startswith("beach copy") for p in trash.iterdir())
    acts = s.cache.actions()
    assert acts[-1]["action"] == "recycle" and acts[-1]["path"].endswith("beach copy.jpg")
    assert os.path.exists(os.path.join(s.cache.dir, "actions.csv"))
    assert move(server, [fid])[fid]["reason"] == "Already moved"


def test_file_edited_after_scan_refused(served):
    s, server = served
    path = s.rec("photos/beach_small.jpg").path
    with open(path, "ab") as f:
        f.write(b"\0")
    fid = s.rec("photos/beach_small.jpg").fid
    res = move(server, [fid])[fid]
    assert not res["ok"] and "changed" in res["reason"]
    assert os.path.exists(path)


def test_hard_linked_file_refused(served, tmp_path):
    s, server = served
    path = s.rec("pdf/archive/report renamed.pdf").path
    os.link(path, tmp_path / "extra-link.pdf")
    fid = s.rec("pdf/archive/report renamed.pdf").fid
    res = move(server, [fid])[fid]
    assert not res["ok"] and "Hard-linked" in res["reason"]


def test_usb_drive_goes_to_review_folder_and_undo(served, monkeypatch):
    s, server = served
    monkeypatch.setattr(df, "drive_kind", lambda p: "removable")
    rel = "pdf/archive/report renamed.pdf"
    path = s.rec(rel).path
    fid = s.rec(rel).fid
    res = move(server, [fid])[fid]
    assert res["ok"] and res["action"] == "review"
    assert df.REVIEW_DIR in res["dest"] and os.path.exists(res["dest"])
    assert not os.path.exists(path)
    assert df.undo_review(s.cache) == 0
    assert os.path.exists(path) and not os.path.exists(res["dest"])
    assert s.cache.actions()[-1]["action"] == "restore"


def test_unknown_ids_refused(served):
    s, server = served
    res = move(server, [123456, "x"])
    assert all(not r["ok"] for r in res.values())


def test_quit_stops_server(served):
    s, server = served
    assert call(server, "/api/quit", {})[0] == 200
    assert server.stopped.wait(5)


# -- offline guarantee -------------------------------------------------------------------

def test_report_page_loads_nothing_from_the_web():
    html = df.REPORT_TEMPLATE
    assert not re.search(r"""(src|href)\s*=\s*["']?\s*(https?:)?//""", html, re.I)
    assert not re.search(r"url\(\s*['\"]?\s*(https?:)?//", html, re.I)
    assert "@import" not in html
    for url in re.findall(r"https?://[^\s\"'<>)]+", html):
        assert url.startswith("http://www.w3.org/"), url


def test_network_guard_blocks_internet_but_not_loopback(served):
    s, server = served
    df.install_network_guard()
    with pytest.raises(OSError):
        socket.create_connection(("93.184.216.34", 80), timeout=2)
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(PermissionError):
        raw.connect(("93.184.216.34", 80))
    raw.close()
    with pytest.raises(OSError):
        socket.getaddrinfo("example.com", 443)
    assert call(server, "/")[0] == 200


# -- worker pool --------------------------------------------------------------------------

@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO to simulate a stuck file")
def test_stuck_file_times_out_and_scan_continues(dataset, tmp_path):
    root, _ = dataset
    fifo = tmp_path / "stuck.jpg"
    os.mkfifo(fifo)
    pool = df.WorkerPool(1, {"max_pages": 30, "tesseract": None}, timeout=2)
    try:
        tasks = [(0, "analyse", {"path": str(fifo), "kind": "image", "analyse": True}),
                 (1, "analyse", {"path": str(root / "photos/beach.jpg"), "kind": "image",
                                 "analyse": True})]
        out = {k: (ok, res) for k, ok, res in pool.run(tasks)}
    finally:
        pool.close()
    assert out[0][0] is False and "Timed out" in out[0][1]
    assert out[1][0] is True and out[1][1]["phash"]


# -- command line ---------------------------------------------------------------------------

def test_report_only_writes_html_and_csv(dataset, tmp_path):
    root, _ = dataset
    cache = tmp_path / "cache"
    code = df.main([str(root / "photos"), "--report-only", "--no-browser", "--cache", str(cache),
                    "--workers", "2", "--types", "image"])
    assert code == 0
    html = list((cache / "reports").glob("*.html"))
    csvs = list((cache / "reports").glob("*.csv"))
    assert html and csvs
    text = html[0].read_text(encoding="utf-8")
    assert "Content-Security-Policy" in text and '"mode":"static"' in text
    assert "beach" in csvs[0].read_text(encoding="utf-8-sig")


def test_cli_help_and_bad_args():
    script = str(ROOT / "dupefinder.py")
    out = subprocess.run([sys.executable, script, "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "--report-only" in out.stdout
    bad = subprocess.run([sys.executable, script, "--types", "video", "."], capture_output=True, text=True)
    assert bad.returncode == 2


def test_extended_path_prefix():
    assert df.to_extended_path("C:\\a\\b.jpg", windows=True) == "\\\\?\\C:\\a\\b.jpg"
    assert df.to_extended_path("\\\\srv\\share\\x.pdf", windows=True) == "\\\\?\\UNC\\srv\\share\\x.pdf"
    assert df.to_extended_path("\\\\?\\C:\\x", windows=True) == "\\\\?\\C:\\x"
    assert df.to_extended_path("/home/x", windows=False) == "/home/x"


def test_copy_markers():
    assert df.has_copy_marker("C:/a/report (1).pdf")
    assert df.has_copy_marker("C:/a/report - Copy.pdf")
    assert df.has_copy_marker("C:/a/Copy of report.pdf")
    assert not df.has_copy_marker("C:/a/report.pdf")
    assert not df.has_copy_marker("C:/a/copyright notice.pdf")


@pytest.mark.skipif(os.name != "nt", reason="Windows folder rules")
def test_windows_system_folders():
    assert df.system_folder_reason("C:\\Windows", "Windows", "C:\\")
    assert df.system_folder_reason("C:\\Program Files (x86)", "Program Files (x86)", "C:\\")
    assert df.system_folder_reason("C:\\Users\\Tan\\AppData", "AppData", "C:\\Users\\Tan")
    assert not df.system_folder_reason("D:\\Photos\\Windows", "Windows", "D:\\Photos")
    assert df.drive_kind("C:\\") == "fixed"
