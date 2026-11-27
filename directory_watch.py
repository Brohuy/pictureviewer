"""Recursive Windows change notifications with a portable polling fallback."""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path


class DirectoryWatch:
    def __init__(self, directory: Path) -> None:
        self._handles: list[int] = []
        if os.name != "nt":
            return
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        first = self._api.FindFirstChangeNotificationW
        first.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD]
        first.restype = wintypes.HANDLE
        self._api.FindNextChangeNotification.argtypes = [wintypes.HANDLE]
        self._api.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
        wait = self._api.WaitForMultipleObjects
        wait.argtypes = [
            wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD
        ]
        wait.restype = wintypes.DWORD
        self._api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._api.WaitForSingleObject.restype = wintypes.DWORD
        # Names affect the sidebar. Size and last-write changes only affect
        # the contents of the currently viewed file.
        for mask in (0x01 | 0x02, 0x08 | 0x10):
            handle = first(str(directory), True, mask)
            if handle == wintypes.HANDLE(-1).value:
                self.close()
                break
            self._handles.append(handle)

    def close(self) -> None:
        for handle in self._handles:
            self._api.FindCloseChangeNotification(handle)
        self._handles.clear()

    def wait(
        self, stop: threading.Event, retry_at: float | None = None
    ) -> tuple[bool, bool]:
        """Return (names changed, contents may have changed) for one batch."""
        if self._handles:
            handles = (wintypes.HANDLE * len(self._handles))(*self._handles)
            wait = self._api.WaitForMultipleObjects
            while not stop.is_set():
                if retry_at is not None and time.monotonic() >= retry_at:
                    return True, True
                index = wait(2, handles, False, 250)
                if index == 258:  # WAIT_TIMEOUT
                    continue
                if index not in (0, 1):
                    self.close()
                    break
                # Leave signals set briefly to merge an editor's temp-file,
                # rename and write events into one refresh.
                if stop.wait(0.2):
                    return False, False
                changed = []
                for handle in self._handles:
                    status = self._api.WaitForSingleObject(handle, 0)
                    if status not in (0, 258) or (
                        status == 0 and not self._api.FindNextChangeNotification(handle)
                    ):
                        self.close()
                        return True, True
                    changed.append(status == 0)
                return changed[0], any(changed)
        if stop.wait(5):
            return False, False
        return True, True
