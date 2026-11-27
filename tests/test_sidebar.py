"""Regression tests for lazy sidebar population and live directory updates."""

import sys
import time
import tkinter as tk
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import picture_viewer as pv


class SidebarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as error:
            raise unittest.SkipTest(f"Tk display unavailable: {error}")
        cls.root.withdraw()
        cls.app = pv.PictureViewerApp(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.app.close()

    def setUp(self):
        self.app._clear_sidebar("", "")
        self.base = Path.cwd() / "virtual-album"
        self.app.root_path = self.base
        self.app.index = -1

    def populate(self, files, directories):
        self.app.images = sorted(files, key=lambda path: pv.image_sort_key(path, self.base))
        self.app.folder_groups = pv.build_folder_groups(self.app.images)
        self.app.folder_directories = [self.base, *directories]
        self.app._populate_sidebar()

    def test_closed_branches_and_navigation_to_nested_image(self):
        album = self.base / "album"
        deep = album / "deep"
        files = [deep / f"{i}.jpg" for i in range(500)]
        self.populate(files, [album, deep])
        self.assertNotIn(deep, self.app._folder_items_by_path)
        self.assertFalse(self.app._image_items_by_path)

        self.app.index = 499
        self.app._sync_sidebar_selection()
        item = self.app._folder_items_by_path[deep]
        self.assertEqual(self.app.folder_tree.selection(), (item,))
        self.assertEqual(self.app._folder_first_index[item], 0)
        self.assertFalse(self.app._image_items_by_path)

        self.app.folder_tree.focus(item)
        self.app.folder_tree.event_generate("<<TreeviewOpen>>")
        self.app.folder_tree.item(item, open=True)
        self.app._sync_sidebar_selection()
        selected = self.app._image_items_by_path[files[499]]
        self.assertEqual(self.app.folder_tree.selection(), (selected,))
        self.assertLess(len(self.app._image_items_by_path), len(files))
        self.finish_batches()
        self.assertEqual(
            [self.app.folder_tree.item(row, "text") for row in self.app.folder_tree.get_children(item)],
            [path.name for path in files],
        )

    def test_refresh_preserves_rows_expansion_and_indices(self):
        album = self.base / "album"
        keep, deleted = album / "2.jpg", album / "3.jpg"
        self.populate([keep, deleted], [album])
        item = self.app._folder_items_by_path[album]
        self.app._expand_sidebar_folder(album)
        self.app.folder_tree.item(item, open=True)
        picture = self.app._image_items_by_path[keep]
        self.app.index = 1
        self.populate([self.base / "cover.jpg", keep], [album])
        self.assertEqual(self.app.folder_tree.selection(), (picture,))
        self.assertTrue(self.app.folder_tree.item(item, "open"))
        self.assertEqual(self.app._image_item_index[picture], 1)
        self.assertNotIn(deleted, self.app._image_items_by_path)

        self.app.index = -1
        self.populate([], [])
        self.assertFalse(self.app.folder_tree.exists(item))
        self.assertNotIn(album, self.app._sidebar_rows)
        self.assertFalse(self.app._image_items_by_path)

    def test_replacing_album_during_pending_batch(self):
        files = [self.base / f"{i}.png" for i in range(400)]
        self.populate(files, [])
        self.assertTrue(self.app._sidebar_jobs)
        self.populate([files[-1]], [])
        self.finish_batches()
        self.assertEqual(set(self.app._image_items_by_path), {files[-1]})

    def finish_batches(self):
        deadline = time.monotonic() + 3
        while self.app._sidebar_jobs and time.monotonic() < deadline:
            self.root.update()
        self.assertFalse(self.app._sidebar_jobs)


if __name__ == "__main__":
    unittest.main()
