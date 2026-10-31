"""
Filesystem-safe, no-overwrite file movement.

Linux:
    Uses renameat2(RENAME_NOREPLACE).

Other platforms:
    Uses hard-link + unlink when supported.

Never silently overwrites an existing destination.
Unsupported filesystem operations fail closed.
"""

import ctypes
import errno
import os
import sys
from pathlib import Path


AT_FDCWD = -100
RENAME_NOREPLACE = 1


class UnsupportedMoveError(OSError):
    """Filesystem cannot guarantee a safe no-overwrite move."""


def move_no_replace(source, destination):
    source = Path(source)
    destination = Path(destination)

    if source.is_symlink() or not source.is_file():
        raise ValueError("Source must be a regular file")

    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)

    if source.stat().st_dev != destination.parent.stat().st_dev:
        raise UnsupportedMoveError(
            "Source and destination must share a filesystem"
        )

    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)

        renameat2 = getattr(libc, "renameat2", None)

        if renameat2 is None:
            raise UnsupportedMoveError(
                "Linux renameat2 is unavailable"
            )

        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]

        renameat2.restype = ctypes.c_int

        result = renameat2(
            AT_FDCWD,
            os.fsencode(source),
            AT_FDCWD,
            os.fsencode(destination),
            RENAME_NOREPLACE,
        )

        if result != 0:
            error = ctypes.get_errno()

            if error == errno.EEXIST:
                raise FileExistsError(destination)

            if error in (
                errno.ENOSYS,
                errno.EINVAL,
                errno.EOPNOTSUPP,
                errno.ENOTSUP,
            ):
                raise UnsupportedMoveError(
                    "Filesystem does not support "
                    "atomic no-overwrite rename"
                )

            raise OSError(
                error,
                os.strerror(error),
                str(source),
            )

        return destination

    # Portable fallback, primarily for local development.
    # The link operation must create the destination exclusively.
    try:
        os.link(source, destination, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in (
            errno.EPERM,
            errno.EOPNOTSUPP,
            errno.ENOTSUP,
            errno.EXDEV,
        ):
            raise UnsupportedMoveError(
                "Filesystem does not support safe hard-link movement"
            ) from exc

        raise

    # If unlink fails, both names remain attached to the same file.
    # Recovery must reconcile this condition without deleting data.
    source.unlink()

    return destination
