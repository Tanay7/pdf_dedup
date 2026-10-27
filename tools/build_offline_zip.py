"""Build the offline Windows zip: dupefinder.py, every package as a wheel, and install scripts.

    python tools/build_offline_zip.py [--out dist] [--pythons 3.12 3.13 3.14]

Downloading the wheels is the only step that uses the internet, and it
happens here, at build time. On the user's PC, install.bat installs from the
zip's wheels folder with --no-index, and the tool itself never goes online.
"""

import argparse
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["dupefinder.py", "requirements.txt", "install.bat", "run.bat", "README.md"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--pythons", nargs="+", default=["3.12", "3.13", "3.14"])
    ap.add_argument("--platform", default="win_amd64")
    args = ap.parse_args()

    version = re.search(r'__version__ = "([^"]+)"', (ROOT / "dupefinder.py").read_text("utf-8")).group(1)
    name = f"DupeFinder-{version}"
    out = Path(args.out)
    stage = out / name
    shutil.rmtree(stage, ignore_errors=True)
    wheels = stage / "wheels"
    wheels.mkdir(parents=True)

    for py in args.pythons:
        subprocess.check_call([
            sys.executable, "-m", "pip", "download", "--disable-pip-version-check",
            "--only-binary=:all:", "--platform", args.platform, "--python-version", py,
            "--implementation", "cp", "-r", str(ROOT / "requirements.txt"), "-d", str(wheels)])
    for f in FILES:
        shutil.copy2(ROOT / f, stage / f)

    zip_path = out / f"{name}-windows-offline.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, Path(name) / p.relative_to(stage))
    size = zip_path.stat().st_size / 1024 / 1024
    print(f"Built {zip_path} ({size:.0f} MB, {len(list(wheels.iterdir()))} wheels)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
