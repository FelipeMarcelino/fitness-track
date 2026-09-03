"""Opening a file in a shared temporary directory, safely (spec 11.1).

Voice notes land in `/tmp`, which is tmpfs and is shared by everything running
in the container. A path there is guessable — the download names the file and
the retry names it after a database row — so a plain `open` is a hop away from
writing somebody's recording wherever a symlink points, or reading whatever was
substituted for it.

`O_NOFOLLOW` closes that: the open fails rather than following a link at the
final path component. `O_EXCL` covers the other half, refusing a destination
that already exists instead of truncating it. Neither is available through
`Path.open`, which is why these two functions exist rather than a convention in
a comment.

`O_NOFOLLOW` is POSIX and present on Linux, where this runs. Where it is
absent, `getattr` degrades to zero and the exclusive create still holds — which
is what "where supported" means in the task.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO, Final

__all__ = ["NOFOLLOW", "create_private", "open_no_follow", "private_directory"]

# Zero is the identity for `|`, so an absent flag drops out of the mask.
NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
CLOEXEC: Final = getattr(os, "O_CLOEXEC", 0)

# Owner only. The recording is the user's voice, and the container's `/tmp` is
# not private to the process that wrote it.
PRIVATE_MODE: Final = 0o600
PRIVATE_DIR_MODE: Final = 0o700


@contextmanager
def create_private(path: Path) -> Iterator[IO[bytes]]:
    """Create a new file for writing, refusing a link or an existing name.

    Raises `FileExistsError` if the name is taken and `OSError` if the final
    component is a symbolic link.
    """
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | CLOEXEC,
        PRIVATE_MODE,
    )
    with os.fdopen(descriptor, "wb") as sink:
        yield sink


@contextmanager
def open_no_follow(path: Path) -> Iterator[IO[bytes]]:
    """Open an existing file for reading, refusing a symbolic link."""
    descriptor = os.open(path, os.O_RDONLY | NOFOLLOW | CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        yield source


def private_directory(path: Path) -> Path:
    """Create (or accept) a directory in a shared temporary space, safely.

    `mkdir(exist_ok=True)` accepts a *symlink to* a directory, which is the
    whole attack in a shared `/tmp`: another process pre-creates the predictable
    name pointing somewhere else, and everything written afterwards — and
    everything a retention sweep deletes — lands in the target. `lstat` is what
    tells the two apart, so the check is on the link itself rather than on what
    it resolves to.

    Refuses rather than repairs. A name that is taken by something that is not a
    directory of ours is a condition to report, not to clean up.
    """
    with suppress(FileExistsError):
        path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=False)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"{path} is not a directory (it may be a symbolic link)")
    if info.st_uid != os.getuid():
        raise PermissionError(f"{path} belongs to another user")
    return path
