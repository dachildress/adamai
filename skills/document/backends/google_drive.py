"""
Google Drive backend - STUB FOR v2.

To implement:
  1. OAuth flow (offline access + drive.file scope minimum)
  2. Token cache in user-config dir
  3. Drive API v3 file create/upload
  4. Map metadata.session_id etc. into Drive file properties
  5. Return drive_url and file_id alongside the universal fields

For v1, this raises NotImplementedError if anyone tries to use it.
"""

from typing import Any, Dict

from .base import StorageBackend


class GoogleDriveBackend(StorageBackend):
    """v2: Google Drive backend. Not implemented in v1."""

    name = "google_drive"

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "GoogleDriveBackend is reserved for ADAM v2. Use "
            "'local_filesystem' for v1. To implement: OAuth + Drive API v3 "
            "wrapper that uploads bytes and returns drive_url + file_id "
            "alongside the universal SkillResult fields."
        )

    def save(
        self,
        content:   bytes,
        filename:  str,
        mime_type: str,
        metadata:  Dict[str, Any],
    ) -> Dict[str, Any]:
        raise NotImplementedError("GoogleDriveBackend not implemented in v1")
