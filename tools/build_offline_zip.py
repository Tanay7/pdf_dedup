"""Build the offline Windows zip.

    python tools/build_offline_zip.py --portable PYTHON_DIR [--out dist]
        Portable zip (the one users download): copies a complete 64-bit Windows
        Python into the zip and installs every package into it, so the user
        needs no Python and no install step. Run on Windows; PYTHON_DIR is a
        python.org install (CI uses the one actions/setup-python provides).

    python tools/build_offline_zip.py [--pythons 3.12 3.13 3.14] [--out dist]
        Wheels zip: dupefinder.py, every package as a wheel and install.bat,
        for people who want to use their own Python 3.12-3.14.

Downloading packages is the only step that uses the internet, and it happens
here, at build time. On the user's PC the tool never goes online.
"""

import argparse
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Parts of a python.org install that DupeFinder never uses (tests, docs, headers).
PORTABLE_SKIP = {"Doc", "include", "libs", "Tools", "test", "tests", "idle_test",
                 "__pycache__", "Scripts"}


def download_wheels(dest: Path, pythons: list[str], platform: str):
    for py in pythons:
        subprocess.check_call([
            sys.executable, "-m", "pip", "download", "--disable-pip-version-check",
            "--only-binary=:all:", "--platform", platform, "--python-version", py,
            "--implementation", "cp", "-r", str(ROOT / "requirements.txt"), "-d", str(dest)])


def build_portable(stage: Path, python_dir: Path, platform: str):
    exe = python_dir / "python.exe"
    version = subprocess.check_output(
        [str(exe), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], text=True).strip()
    wheels = stage.parent / "wheels-portable"
    shutil.rmtree(wheels, ignore_errors=True)
    download_wheels(wheels, [version], platform)
    target = stage / "python"
    shutil.copytree(python_dir, target, ignore=lambda d, names: [n for n in names if n in PORTABLE_SKIP])
    subprocess.check_call([str(target / "python.exe"), "-m", "pip", "install", "--no-index",
                           "--disable-pip-version-check", "--no-warn-script-location",
                           "--find-links", str(wheels), "-r", str(ROOT / "requirements.txt")])
    # Prove the copy is self-contained before shipping it.
    subprocess.check_call([str(target / "python.exe"), "-c",
                           "import numpy, PIL, pillow_heif, pymupdf, send2trash, tkinter"])
    for f in ["dupefinder.py", "run.bat", "README.md"]:
        shutil.copy2(ROOT / f, stage / f)
    return f"portable Python {version}"


def build_wheels(stage: Path, pythons: list[str], platform: str):
    download_wheels(stage / "wheels", pythons, platform)
    for f in ["dupefinder.py", "requirements.txt", "install.bat", "run.bat", "README.md"]:
        shutil.copy2(ROOT / f, stage / f)
    return f"{len(list((stage / 'wheels').iterdir()))} wheels"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the offline Windows zip.")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--portable", metavar="PYTHON_DIR",
                    help="bundle this Windows Python with the packages installed")
    ap.add_argument("--pythons", nargs="+", default=["3.12", "3.13", "3.14"])
    ap.add_argument("--platform", default="win_amd64")
    args = ap.parse_args()

    version = re.search(r'__version__ = "([^"]+)"', (ROOT / "dupefinder.py").read_text("utf-8")).group(1)
    name = f"DupeFinder-{version}"
    out = Path(args.out)
    stage = out / name
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    if args.portable:
        what = build_portable(stage, Path(args.portable), args.platform)
        zip_path = out / f"{name}-windows.zip"
    else:
        what = build_wheels(stage, args.pythons, args.platform)
        zip_path = out / f"{name}-windows-wheels.zip"

    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, Path(name) / p.relative_to(stage))
    print(f"Built {zip_path} ({zip_path.stat().st_size / 1024 / 1024:.0f} MB, {what})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
