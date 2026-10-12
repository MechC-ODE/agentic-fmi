"""Fetch the pinned Modelica Reference-FMUs release into the local cache.

Downloads ``Reference-FMUs.zip`` for ``VERSION`` and checks its size and SHA-256 against the
pinned values. It then rejects the archive if any member path is absolute or has a ``..``
component, before anything is extracted. The archive is extracted into
``<repo>/.cache/reference-fmus/<VERSION>/`` and a ``.complete`` marker is written there. A
later run that finds the marker prints the path and exits without downloading. The archive
goes to a temporary file beside the destination and is removed after extraction or on any
failure. A failed run also removes any directories it created. A pre-existing destination
without the ``.complete`` marker may keep partial files, which the next run overwrites.

Usage::

    python -I scripts/fetch-reference-fmus.py              # default destination
    python -I scripts/fetch-reference-fmus.py --dest DIR   # custom destination

Progress goes to stderr; the destination path is the last line on stdout. Collaborators:
none inside the project. The script uses only the standard library so it runs before
``uv sync``; FMU consumers read the FMUs from the printed path.
"""

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PureWindowsPath

VERSION = "0.0.41"
URL = "https://github.com/modelica/Reference-FMUs/releases/download/v0.0.41/Reference-FMUs.zip"
EXPECTED_SIZE = 3390239
# Upstream does not publish this hash; it was computed from the v0.0.41 download.
EXPECTED_SHA256 = "62babca76b9c23a51c3096be4bb5930ff8b4388659056be3c0c4ef7a3aeb5403"
MARKER_NAME = ".complete"
TIMEOUT_SECONDS = 60
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = REPO_ROOT / ".cache" / "reference-fmus" / VERSION


class FetchError(RuntimeError):
    """A download, verification or extraction step failed; the message says what to do."""


def log(message: str) -> None:
    """Write a progress or error message to stderr, keeping stdout for the path."""
    print(message, file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description=f"Fetch Modelica Reference-FMUs v{VERSION} into the local cache."
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        help=f"directory to extract into (default: {DEFAULT_DEST})",
    )
    return parser


def is_complete(dest: Path) -> bool:
    """Return True when an earlier run finished extracting into ``dest``."""
    return (dest / MARKER_NAME).is_file()


def missing_dirs(path: Path) -> list[Path]:
    """Return ``path`` and its ancestors that do not exist yet, deepest first."""
    return [candidate for candidate in [path, *path.parents] if not candidate.exists()]


def remove_created(created: list[Path]) -> None:
    """Delete the outermost directory this run created, which takes the rest with it."""
    if created:
        shutil.rmtree(created[-1], ignore_errors=True)


def make_temp_archive(directory: Path) -> Path:
    """Create an empty temporary file in ``directory`` for the download."""
    fd, name = tempfile.mkstemp(
        dir=directory, prefix=f"reference-fmus-{VERSION}-", suffix=".zip.part"
    )
    os.close(fd)
    return Path(name)


def download(url: str, target: Path) -> None:
    """Stream ``url`` into ``target``, replacing its contents."""
    with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response, target.open("wb") as out:
        shutil.copyfileobj(response, out)


def sha256_of(path: Path) -> str:
    """Return the SHA-256 hex digest of the file at ``path``."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def archive_problem(path: Path) -> str | None:
    """Return how ``path`` differs from the pinned size and SHA-256, or None if it matches."""
    size = path.stat().st_size
    if size != EXPECTED_SIZE:
        return f"size is {size} bytes, expected {EXPECTED_SIZE}"
    digest = sha256_of(path)
    if digest != EXPECTED_SHA256:
        return f"SHA-256 is {digest}, expected {EXPECTED_SHA256}"
    return None


def verify_archive(archive: Path) -> None:
    """Raise FetchError if ``archive`` differs from the pinned size or SHA-256."""
    problem = archive_problem(archive)
    if problem is not None:
        raise FetchError(
            f"downloaded archive does not match the pin: {problem}. The temporary "
            f"download was deleted and nothing was extracted. Check the release at {URL}, "
            "or update EXPECTED_SIZE and EXPECTED_SHA256 in scripts/fetch-reference-fmus.py."
        )


def unsafe_member(name: str) -> bool:
    """Return True when a zip member name is absolute or has a ``..`` component."""
    normalized = name.replace("\\", "/")
    return (
        normalized.startswith("/")
        or PureWindowsPath(name).drive != ""
        or ".." in normalized.split("/")
    )


def check_members(archive: Path) -> None:
    """Raise FetchError if any member is absolute or has a ``..`` component, extracting nothing."""
    with zipfile.ZipFile(archive) as zf:
        unsafe = [name for name in zf.namelist() if unsafe_member(name)]
    if unsafe:
        raise FetchError(
            f"archive rejected before extraction: {len(unsafe)} member path(s) are absolute "
            f"or contain '..', first: {unsafe[0]!r}. Nothing was extracted."
        )


def extract(archive: Path, dest: Path) -> None:
    """Extract every member of ``archive`` into ``dest``; run ``check_members`` first."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dest)


def mark_complete(dest: Path) -> None:
    """Write the ``.complete`` marker that lets later runs skip the download."""
    (dest / MARKER_NAME).write_text(
        f"version={VERSION}\nsha256={EXPECTED_SHA256}\n", encoding="utf-8"
    )


def install(dest: Path) -> None:
    """Stage the archive beside ``dest``, check it, extract it and write the marker."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    archive = make_temp_archive(dest.parent)
    try:
        log(f"Downloading {URL} ({EXPECTED_SIZE} bytes)")
        download(URL, archive)
        verify_archive(archive)
        check_members(archive)
        log("Size and SHA-256 verified; member paths checked; extracting")
        extract(archive, dest)
    finally:
        archive.unlink(missing_ok=True)
    mark_complete(dest)


def fetch(dest: Path) -> None:
    """Download, verify and extract the pinned release into ``dest``.

    Directories this call creates are removed again when any step fails.
    """
    created = missing_dirs(dest)
    try:
        install(dest)
    except BaseException:
        remove_created(created)
        raise


def main(argv: list[str] | None = None) -> int:
    """Fetch the Reference FMUs unless already cached, print the destination, return exit code."""
    args = build_parser().parse_args(argv)
    dest: Path = args.dest.resolve()
    if is_complete(dest):
        log(f"Reference FMUs {VERSION} already present; skipping download")
    else:
        try:
            fetch(dest)
        except (OSError, zipfile.BadZipFile, FetchError) as exc:
            log(f"error: {exc}")
            return 1
    print(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
