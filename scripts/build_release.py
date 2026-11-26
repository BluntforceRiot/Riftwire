"""Package only public release files and write the archive's SHA-256 checksum."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import runpy
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ".gitattributes",
    ".github/verification/native_windows.py",
    ".github/workflows/release.yml",
    ".github/workflows/test.yml",
    ".gitignore",
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "assets/riftwire-banner.webp",
    "docs/CONTINUUM.md",
    "docs/HERMES.md",
    "docs/PROJECTS.md",
    "docs/RELEASE.md",
    "docs/ROADMAP.md",
    "docs/SETUP.md",
    "examples/verify_release.py",
    "examples/workflows/compare-fixes.json",
    "examples/workflows/release.json",
    "riftwire.py",
    "riftwire_continuum.py",
    "riftwire_pairing.py",
    "riftwire_projects.py",
    "riftwire_receiver.py",
    "riftwire_setup.py",
    "scripts/build_release.py",
    "scripts/verify_project_link.py",
    "tests/test_chat_check.py",
    "tests/test_contacts.py",
    "tests/test_continuum_contract.py",
    "tests/test_guided_setup.py",
    "tests/test_hermes.py",
    "tests/test_network.py",
    "tests/test_pairing.py",
    "tests/test_project_mcp.py",
    "tests/test_projects.py",
    "tests/test_receiver.py",
    "tests/test_riftwire.py",
)


def build(folder, archive_name=None):
    version = runpy.run_path(str(ROOT / "riftwire.py"))["VERSION"]
    name = f"Riftwire-{version}"
    folder.mkdir(parents=True, exist_ok=True)
    if archive_name is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.zip", archive_name):
        raise ValueError("Use a plain filename ending in .zip.")
    archive = folder / (archive_name or (name + ".zip"))
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
    parser.add_argument("--archive-name", help="Optional ZIP filename for a workflow with declared artifact names.")
    args = parser.parse_args()
    build(args.out, args.archive_name)
