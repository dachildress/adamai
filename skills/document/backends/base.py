"""
StorageBackend abstract interface.

The document skill renders bytes; backends save them. This separation lets
us add cloud backends (Google Drive, OneDrive, SharePoint) without
changing the renderer or skill handler.

Every backend implements:
    save(content: bytes, filename: str, mime_type: str, metadata: dict) -> dict

The returned dict carries any backend-specific data alongside the universal
fields (path, sha256, size_bytes, backend). The skill handler merges these
into the SkillResult body.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict


class StorageBackend(ABC):
    """Abstract interface every document storage backend implements."""

    name: str = "abstract"

    @abstractmethod
    def save(
        self,
        content:   bytes,
        filename:  str,
        mime_type: str,
        metadata:  Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Persist the given byte content under the given filename.

        Args:
            content:   The rendered file bytes.
            filename:  The intended filename (already sanitized by caller).
            mime_type: Standard MIME type, e.g. 'text/markdown',
                       'application/vnd.openxmlformats-officedocument.wordprocessingml.document'.
            metadata:  Free-form audit metadata (session_id, invocation_id,
                       artifact_id, etc.) that the backend may attach as
                       file properties or ignore.

        Returns a dict with at least these keys:
          - path       : where the file ended up (string)
          - sha256     : hash of the saved bytes (string)
          - size_bytes : size of the saved file (int)
          - backend    : self.name (string)

        Backends may add their own keys (e.g., 'drive_url' for Google Drive).
        """
        raise NotImplementedError
