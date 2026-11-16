"""Sandbox image tag and build command.

The tag is ``harness-builder-sandbox:<hash>``, where the hash covers the files
that define the image, so a changed Dockerfile or entry script gets a new tag.
"""

import hashlib
from pathlib import Path

IMAGE_DIR = Path(__file__).parent / "image"
IMAGE_REPO = "harness-builder-sandbox"
# Files that define the image. Add new image inputs here.
IMAGE_FILES = ("Dockerfile", "hb_entry.py")
TAG_HASH_LEN = 12


def image_tag(image_dir: Path = IMAGE_DIR) -> str:
    """Return ``harness-builder-sandbox:<content hash>`` for ``image_dir``.

    Line endings are normalised to LF so Windows and POSIX checkouts agree.
    """
    digest = hashlib.sha256()
    for name in sorted(IMAGE_FILES):
        content = (image_dir / name).read_bytes().replace(b"\r\n", b"\n")
        digest.update(name.encode() + b"\0" + content + b"\0")
    return f"{IMAGE_REPO}:{digest.hexdigest()[:TAG_HASH_LEN]}"


def build_command(image_dir: Path = IMAGE_DIR) -> list[str]:
    """Return the ``docker build`` argv that builds the sandbox image."""
    return [
        "docker",
        "build",
        "-t",
        image_tag(image_dir),
        "-f",
        str(image_dir / "Dockerfile"),
        str(image_dir),
    ]
