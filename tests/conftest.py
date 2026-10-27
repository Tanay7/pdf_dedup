import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dupefinder as df  # noqa: E402
import make_test_files as gen  # noqa: E402


def pytest_configure(config):
    # Windows keeps temp files under AppData, which DupeFinder treats as a
    # system folder (never pre-selected), so test files go somewhere else.
    if os.name == "nt" and not config.option.basetemp:
        config.option.basetemp = str(ROOT / "build" / "pytest-tmp")


def fake_attributes(entry, st):
    """Pretend files named online_only* are OneDrive online-only placeholders."""
    if entry.name.startswith("online_only"):
        return df.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    return getattr(st, "st_file_attributes", 0) or 0


class Scan:
    """A finished scan plus helpers to look files up by their relative path."""

    def __init__(self, root: Path, result, cache):
        self.root = root
        self.result = result
        self.cache = cache
        self.by_rel = {}
        for r in result.recs:
            rel = os.path.relpath(r.path, root).replace(os.sep, "/")
            self.by_rel[rel] = r

    def rec(self, rel):
        return self.by_rel[rel]

    def group(self, rel):
        gid = self.result.member_group.get(self.rec(rel).fid)
        return None if gid is None else self.result.group_by_id[gid]

    def member(self, rel):
        g = self.group(rel)
        fid = self.rec(rel).fid
        return next(m for m in g.members if m.fid == fid) if g else None

    def same_group(self, *rels):
        gids = {self.result.member_group.get(self.rec(r).fid) for r in rels}
        return len(gids) == 1 and None not in gids

    def relation(self, a, b):
        """Direct match of file a against file b, the way the report scores it."""
        ra, rb = self.rec(a), self.rec(b)
        m = self.result.matcher
        if ra.kind == rb.kind == "pdf":
            m.ensure_shingles([ra.fid, rb.fid])
        if ra.kind == rb.kind == "image":
            m.compute("pixsha", [r.fid for r in (ra, rb) if not r.content_sha])
        return self.result.grouper.relation(ra, rb)


def run(root, cache_dir, *extra):
    """Scan `root` in-process the way the command line does."""
    opts = df.parse_args([str(root), "--cache", str(cache_dir), "--workers", "2", *extra])
    cache = df.Cache(opts.cache)
    real = df.file_attributes
    df.file_attributes = fake_attributes
    try:
        result = df.run_scan(opts, cache, show_progress=False)
    finally:
        df.file_attributes = real
    return Scan(Path(root), result, cache)


@pytest.fixture(scope="session")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("testset")
    manifest = gen.make_test_files(str(out))
    return out, manifest


@pytest.fixture(scope="session")
def scan(dataset, tmp_path_factory):
    root, _ = dataset
    return run(root, tmp_path_factory.mktemp("cache"))


@pytest.fixture(autouse=True)
def private_trash(tmp_path, monkeypatch):
    """Linux tests use a throwaway Trash instead of the real one."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
