"""File monitoring regressions using real temporary folders."""

import os
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import picture_viewer as pv


class WatchTests(unittest.TestCase):
    def test_portable_fallback_rechecks_names_and_contents_until_stopped(self):
        directory = Path("album")
        with patch.object(pv.os, "name", "posix"):
            watch = pv.DirectoryWatch(directory)
        stop = Mock()
        stop.wait.side_effect = [False, True]
        self.assertEqual(watch.wait(stop), (True, True))
        self.assertEqual(watch.wait(stop), (False, False))
        watch.close()

    def test_transient_access_error_preserves_only_unreadable_subtree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            album = root / "album"
            previous = ([album / "old.jpg", root / "gone.jpg"], [root, album])

            def partial_walk(directory, *, onerror, **kwargs):
                yield str(directory), ["album"], ["new.jpg"]
                onerror(PermissionError(13, "Access denied", str(album)))

            with patch.object(pv.os, "walk", partial_walk):
                images, folders, errors = pv.scan_tree(root, previous=previous)
            self.assertEqual(images, [root / "new.jpg", album / "old.jpg"])
            self.assertEqual(folders, [root, album])
            self.assertTrue(errors)

    def test_same_file_version_is_not_reloaded_twice(self):
        viewer = pv.PictureViewerApp.__new__(pv.PictureViewerApp)
        viewer._scan_token = 1
        viewer._load_token = 9
        viewer._watch_path = Path("album/1.jpg")
        viewer.source = object()
        viewer.metadata = {"path": viewer._watch_path}
        viewer._loader = Mock()
        viewer._requested_version = (1, 2, 3)
        for token, path, version in [
            (1, viewer._watch_path, (1, 2, 3)),
            (0, viewer._watch_path, (2, 2, 3)),
            (1, Path("other.jpg"), (2, 2, 3)),
            (1, viewer._watch_path, (2, 2, 3)),
            (1, viewer._watch_path, (2, 2, 3)),
        ]:
            viewer._handle_file_version(token, path, version)
        viewer._loader.request.assert_called_once_with(10, viewer._watch_path)
        self.assertEqual(viewer._preserve_view_token, 10)

    @unittest.skipUnless(os.name == "nt", "Windows directory notifications")
    def test_native_overwrite_does_not_rescan_and_rename_updates_spelling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = root / "test.jpg"
            path.write_bytes(b"first")
            viewer = pv.PictureViewerApp.__new__(pv.PictureViewerApp)
            viewer._events = queue.Queue()
            viewer._watch_path = path
            stop = threading.Event()
            worker = threading.Thread(target=viewer._scan_worker, args=(1, root, stop))
            with patch.object(pv, "scan_tree", wraps=pv.scan_tree) as scan:
                worker.start()
                try:
                    self.assertEqual(viewer._events.get(timeout=5)[0], "scan_ready")
                    path.write_bytes(b"changed")
                    event = viewer._events.get(timeout=5)
                    self.assertEqual(event, ("file_version", 1, path, pv.file_version(path.stat())))
                    self.assertEqual(scan.call_count, 1)
                    path.rename(root / "TEST.jpg")
                    event = viewer._events.get(timeout=5)
                    self.assertEqual(event[0], "watch_changed")
                    self.assertEqual([p.name for p in event[3]], ["TEST.jpg"])
                finally:
                    stop.set()
                    worker.join(5)
                self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
