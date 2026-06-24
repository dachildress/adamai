"""
OneDrive backend - STUB FOR v2.

To implement:
  1. Microsoft Graph OAuth flow
  2. Token cache and refresh
  3. Graph API drive item upload
  4. Map metadata into file properties / SharePoint columns
  5. Return sharepoint_url and item_id alongside the universal fields

For v1, this raises NotImplementedError if anyone tries to use it.
"""

from typing import Any, Dict

from .base import StorageBackend


class OneDriveBackend(StorageBackend):
    """v2: OneDrive backend. Not implemented in v1."""

    name = "onedrive"

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "OneDriveBackend is reserved for ADAM v2. Use "
            "'local_filesystem' for v1. To implement: Microsoft Graph "
            "OAuth + drive item upload wrapper."
        )

    def save(
        self,
        content:   bytes,
        filename:  str,
        mime_type: str,
        metadata:  Dict[str, Any],
    ) -> Dict[str, Any]:
        raise NotImplementedError("OneDriveBackend not implemented in v1")
