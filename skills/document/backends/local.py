"""
LocalFilesystemBackend - saves artifacts to disk under the session's
artifacts directory.

Path: logs/adam_<stamp>.artifacts/<filename>.<ext>

The stamp comes from the session context passed in metadata. If two skill
calls produce the same filename within a session, the second gets '-2'
appended (and so on). The artifact is never silently overwritten.
"""

import hashlib
from pathlib import Path
from typing import Any, Dict

from .base import StorageBackend


class LocalFilesystemBackend(StorageBackend):
    """Saves to a per-session artifacts directory under logs/."""

    name = "local_filesystem"

    def __init__(self, artifacts_root: Path) -> None:
        """
        Args:
            artifacts_root: The base directory for THIS session's artifacts,
                            typically logs/adam_<stamp>.artifacts/. The
                            backend ensures the directory exists.
        """
        self.artifacts_root = artifacts_root
        self.artifacts_root.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        content:   bytes,
        filename:  str,
        mime_type: str,
        metadata:  Dict[str, Any],
    ) -> Dict[str, Any]:
        # Resolve collisions: filename, then filename-2, filename-3, etc.
        target = self.artifacts_root / filename
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            n = 2
            while True:
                candidate = self.artifacts_root / f"{stem}-{n}{suffix}"
                if not candidate.exists():
                    target = candidate
                    break
                n += 1

        target.write_bytes(content)

        sha = hashlib.sha256(content).hexdigest()
        return {
            "path":       str(target),
            "sha256":     sha,
            "size_bytes": len(content),
            "backend":    self.name,
            "mime_type":  mime_type,
        }
