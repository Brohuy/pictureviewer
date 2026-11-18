"""Core tests that do not require a graphical display."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import picture_viewer as pv  # noqa: E402


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


class NaturalSortTests(unittest.TestCase):
    def test_numbers_are_sorted_naturally(self) -> None:
        names = ["img10.jpg", "IMG2.jpg", "img1.jpg", "img02.jpg"]
        self.assertEqual(
            sorted(names, key=pv.natural_key),
            ["img1.jpg", "IMG2.jpg", "img02.jpg", "img10.jpg"],
        )

    def test_sort_key_groups_directories(self) -> None:
        root = Path("C:/photos")
        paths = [
            root / "zeta" / "a.jpg",
            root / "cover.jpg",
            root / "alpha" / "b.jpg",
            root / "photo2.jpg",
        ]
        ordered = sorted(paths, key=lambda path: pv.image_sort_key(path, root))
        self.assertEqual(
            ordered,
            [
                root / "cover.jpg",
                root / "photo2.jpg",
                root / "alpha" / "b.jpg",
                root / "zeta" / "a.jpg",
            ],
        )


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.files = [
            touch(self.root / "cover.JPG"),
            touch(self.root / "img10.png"),
            touch(self.root / "img2.jpg"),
            touch(self.root / "notes.txt"),
            touch(self.root / "sub" / "a" / "photo1.jpg"),
            touch(self.root / "sub" / "b" / "photo10.bmp"),
            touch(self.root / "sub" / "b" / "photo2.bmp"),
            touch(self.root / "sub" / "deep" / "x.tif"),
            touch(self.root / ".hidden.jpg"),
            touch(self.root / "sub" / ".cache" / "secret.jpg"),
        ]

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_recursive_scan_skips_hidden_entries(self) -> None:
        images, errors = pv.scan_images(self.root)

        self.assertEqual(errors, [])
        self.assertEqual(
            [path.relative_to(self.root).as_posix() for path in images],
            [
                "cover.JPG",
                "img2.jpg",
                "img10.png",
                "sub/a/photo1.jpg",
                "sub/b/photo2.bmp",
                "sub/b/photo10.bmp",
                "sub/deep/x.tif",
            ],
        )

    def test_hidden_entries_can_be_included(self) -> None:
        images, errors = pv.scan_images(self.root, skip_hidden=False)

        self.assertEqual(errors, [])
        self.assertIn(self.root / ".hidden.jpg", images)
        self.assertIn(self.root / "sub" / ".cache" / "secret.jpg", images)

    def test_non_images_are_ignored(self) -> None:
        images, _errors = pv.scan_images(self.root)
        self.assertNotIn(self.root / "notes.txt", images)
        self.assertTrue(all(path.suffix.lower() != ".txt" for path in images))


class FolderGroupsTests(unittest.TestCase):
    def test_groups_are_contiguous(self) -> None:
        root = Path("C:/photos")
        images = [
            root / "cover.jpg",
            root / "a" / "1.jpg",
            root / "a" / "2.jpg",
            root / "b" / "1.jpg",
            root / "c" / "1.jpg",
        ]
        groups = pv.build_folder_groups(images)

        self.assertEqual(
            groups,
            [
                (root, 0, 1),
                (root / "a", 1, 3),
                (root / "b", 3, 4),
                (root / "c", 4, 5),
            ],
        )

    def test_empty_list_has_no_groups(self) -> None:
        self.assertEqual(pv.build_folder_groups([]), [])


class FormattingTests(unittest.TestCase):
    def test_file_sizes(self) -> None:
        self.assertEqual(pv.format_file_size(500), "500 B")
        self.assertEqual(pv.format_file_size(2048), "2 KB")
        self.assertEqual(pv.format_file_size(3 * 1024 * 1024), "3.0 MB")

    def test_zoom_percentages(self) -> None:
        self.assertEqual(pv.format_zoom(1.0), "100%")
        self.assertEqual(pv.format_zoom(0.5), "50%")
        self.assertEqual(pv.format_zoom(0.1), "10%")
        self.assertEqual(pv.format_zoom(0.1234), "12.3%")


if __name__ == "__main__":
    unittest.main()
