"""
Storage backend registry.

The handler imports get_backend() to resolve a backend by name. v1 only
knows about LocalFilesystemBackend; future cloud backends register the
same way:

    BACKENDS["google_drive"] = GoogleDriveBackend
    BACKENDS["onedrive"]     = OneDriveBackend
    BACKENDS["sharepoint"]   = SharePointBackend
"""

from pathlib import Path
from typing import Any, Dict, Optional

from .base import StorageBackend
from .local import LocalFilesystemBackend


BACKENDS: Dict[str, type] = {
    "local_filesystem": LocalFilesystemBackend,
}


def get_backend(name: str, artifacts_root: Path) -> Optional[StorageBackend]:
    """Instantiate the named backend, or return None if not registered."""
    cls = BACKENDS.get(name)
    if cls is None:
        return None
    # LocalFilesystemBackend needs the artifacts_root; cloud backends
    # would take credentials from env or a config dict. For v1, only
    # local needs construction args, so we special-case here. If the
    # constructors diverge further in v2, switch to a factory function
    # per backend.
    return cls(artifacts_root)
