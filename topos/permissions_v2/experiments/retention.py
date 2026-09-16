"""Opt-in private copies of model request and response bodies, for synthetic runs only.

Decision metadata cannot say why the prose arm denied a positive, and reports must
never carry bodies. A caller that flagged its run synthetic may therefore name a
private directory that receives one file per model call. Real owner text must
never be copied out this way, which is why an unflagged run is refused outright
rather than retained quietly. Nothing here is read back by the bridge, a report
or any serving path.
"""
from __future__ import annotations

import json
import os
import secrets
import stat

from ..canonical import PolicyError, digest

VERSION = "topos-synthetic-model-bodies/v1"
MAX_RETAINED_BODY_BYTES = 65536


class SyntheticBodyRetention:
    """One 0700 directory, one 0600 file per call; no symlink anywhere on the path.

    The directory is held open by descriptor, so replacing it with a symlink after
    construction cannot redirect a later write. An existing directory must already
    be private; it is never loosened or tightened on the caller's behalf.
    """

    def __init__(self, directory, *, synthetic_run: bool):
        if synthetic_run is not True:
            raise PolicyError("retention_requires_synthetic_run")
        path = os.fspath(directory)
        if (type(path) is not str or not os.path.isabs(path) or ".." in path.split(os.sep)
                or os.path.realpath(path) != os.path.normpath(path)):
            raise PolicyError("retention_directory_unsafe")
        self._fd = None
        try:
            created = False
            try:
                os.mkdir(path, 0o700)
                created = True
            except FileExistsError:
                pass
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode):
                raise PolicyError("retention_directory_unsafe")
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if created:
                    os.fchmod(fd, 0o700)  # the umask may have removed owner bits
                opened = os.fstat(fd)
                if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_uid != os.getuid()
                        or stat.S_IMODE(opened.st_mode) != 0o700):
                    raise PolicyError("retention_directory_unsafe")
            except BaseException:
                os.close(fd)
                raise
        except OSError:
            raise PolicyError("retention_directory_unsafe") from None
        self._fd = fd
        self._run = secrets.token_hex(4)
        self.files: list[str] = []

    def keep(self, *, stage: str, request, response_body: str | None, reason_code: str) -> str:
        """Write one exchange and return its file name, which carries no content."""
        if self._fd is None:
            raise PolicyError("retention_closed")
        if stage not in ("evidence_use", "output_release"):
            # The stage names the file, so it may never carry a path separator.
            raise PolicyError("retention_stage")
        size = len(response_body.encode("utf8", errors="surrogatepass")) if type(response_body) is str else None
        dumped = request.model_dump()
        record = {"version": VERSION, "stage": stage, "reason_code": reason_code,
                  "request_sha256": digest(dumped), "request": dumped,
                  "response_body": response_body if size is not None and size <= MAX_RETAINED_BODY_BYTES else None,
                  "response_body_bytes": size}
        name = "%s-%05d-%s.json" % (self._run, len(self.files) + 1, stage)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
        except OSError:
            raise PolicyError("retention_write_failed") from None
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(json.dumps(record, ensure_ascii=True, sort_keys=True, indent=2).encode("ascii"))
        except OSError:
            raise PolicyError("retention_write_failed") from None
        finally:
            if fd is not None:
                os.close(fd)
        self.files.append(name)
        return name

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
