"""Verify the release workflow's build artifacts from a separate Git checkout."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ARCHIVE = "Riftwire-release.zip"


def require_checkout(checkout, revision):
    git = shutil.which("git.exe" if os.name == "nt" else "git")
    if git is None:
        raise ValueError("Git is required to verify the assigned revision.")

    def read_git(*args):
        return subprocess.run([git, "-c", "core.fsmonitor=false", *args], cwd=checkout,
                              check=True, capture_output=True, encoding="utf-8", errors="surrogateescape").stdout.strip()

    if Path(read_git("rev-parse", "--show-toplevel")).resolve() != checkout:
        raise ValueError("Run the verifier from its approved repository checkout.")
    if read_git("rev-parse", "--verify", "HEAD^{commit}") != revision:
        raise ValueError("The verifier's checkout differs from the assigned Git revision.")
    if read_git("status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("Use a clean checkout of the assigned revision.")


def verify_release(checkout, inputs, revision):
    require_checkout(checkout, revision)
    inputs = inputs.resolve(strict=True)
    artifacts = (inputs / "build" / "dist").resolve(strict=True)
    if not artifacts.is_relative_to(inputs):
        raise ValueError("The build artifacts must be inside the assigned input directory.")
    offered_zip = artifacts / ARCHIVE
    manifest = artifacts / "SHA256SUMS"
    for path in (offered_zip, manifest):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Expected the attached build artifact {path.name}.")

    offered = offered_zip.read_bytes()
    digest = hashlib.sha256(offered).hexdigest()
    if manifest.read_bytes() != f"{digest}  {ARCHIVE}\n".encode("ascii"):
        raise ValueError("The supplied SHA256SUMS does not match the release ZIP.")

    with tempfile.TemporaryDirectory(prefix="riftwire-release-check-") as scratch:
        subprocess.run([sys.executable, "-I", "-B", "scripts/build_release.py", "--out", scratch,
                        "--archive-name", ARCHIVE], cwd=checkout, check=True)
        if (Path(scratch) / ARCHIVE).read_bytes() != offered:
            raise ValueError("The release ZIP differs from the independent rebuild.")

    subprocess.run([sys.executable, "-E", "-s", "-B", "-m", "unittest", "discover", "-s", "tests", "-q"],
                   cwd=checkout, check=True)
    print(f"Native tests passed for {revision}. {ARCHIVE} matches its independent rebuild (SHA-256 {digest}).")


if __name__ == "__main__":
    try:
        assigned = os.environ.get("RIFTWIRE_PROJECT_COMMIT")
        inputs = os.environ.get("RIFTWIRE_PROJECT_INPUTS")
        if not assigned or not inputs:
            raise ValueError("Run with RIFTWIRE_PROJECT_COMMIT and RIFTWIRE_PROJECT_INPUTS from the release workflow.")
        verify_release(Path(__file__).resolve().parents[1], Path(inputs), assigned)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
