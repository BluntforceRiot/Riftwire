"""Package only public release files and write the archive's SHA-256 checksum."""
import argparse
import hashlib
import os
from pathlib import Path
import runpy
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ".gitattributes",
    ".github/workflows/test.yml",
    ".gitignore",
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "assets/riftwire-banner.webp",
    "docs/HERMES.md",
    "docs/SETUP.md",
    "riftwire.py",
    "scripts/build_release.py",
    "tests/test_chat_check.py",
    "tests/test_hermes.py",
    "tests/test_riftwire.py",
)


def build(folder):
    version = runpy.run_path(str(ROOT / "riftwire.py"))["VERSION"]
    name = f"Riftwire-{version}"
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / (name + ".zip")
    with tempfile.TemporaryDirectory(dir=folder) as temporary:
        staged = Path(temporary) / archive.name
        # Stored entries avoid any dependency on the builder's compression library.
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_STORED) as bundle:
            for relative in FILES:
                entry = zipfile.ZipInfo(f"{name}/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = (0o100755 if relative == "riftwire.py" else 0o100644) << 16
                content = (ROOT / relative).read_bytes()
                if not relative.endswith(".webp"):
                    content = content.replace(b"\r\n", b"\n")
                bundle.writestr(entry, content)
        digest = hashlib.sha256(staged.read_bytes()).hexdigest()
        manifest = Path(temporary) / "SHA256SUMS"
        manifest.write_bytes(f"{digest}  {archive.name}\n".encode("ascii"))
        os.replace(staged, archive)
        os.replace(manifest, folder / manifest.name)
    print(f"{digest}  {archive.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    build(parser.parse_args().out)
