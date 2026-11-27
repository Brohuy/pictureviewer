"""Pixel and render scheduling regressions; no desktop session required."""

import queue
import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import picture_viewer as pv  # noqa: E402


class TransparencyTests(unittest.TestCase):
    def test_png_transparent_color_survives_loading(self):
        events = queue.Queue()
        loader = pv.ImageLoader(events)
        self.addCleanup(loader.close)
        with tempfile.TemporaryDirectory() as folder:
            for mode, clear, solid in [("RGB", (1, 2, 3), (4, 5, 6)), ("L", 7, 8)]:
                with self.subTest(mode=mode):
                    path = Path(folder) / f"{mode}.png"
                    image = Image.new(mode, (2, 1), clear)
                    image.putpixel((1, 0), solid)
                    image.save(path, transparency=clear)
                    loader.request(1, path)
                    event = events.get(timeout=5)
                    self.assertEqual(event[:3], ("image_ready", 1, path))
                    self.assertEqual(event[3].getchannel("A").tobytes(), bytes([0, 255]))


class RenderTests(unittest.TestCase):
    def test_viewport_uses_selected_source_pixels(self):
        image = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        image.paste((60, 30, 90, 255), (4, 4, 12, 12))
        for size in [(8, 8), (4, 4), (16, 16)]:
            with self.subTest(size=size):
                viewport = pv.render_region(image, (4, 4, 12, 12), size)
                self.assertEqual(viewport.size, size)
                self.assertEqual(viewport.getextrema(), ((60, 60), (30, 30), (90, 90), (255, 255)))

    def make_viewer(self):
        viewer = pv.PictureViewerApp.__new__(pv.PictureViewerApp)
        viewer.root = Mock()
        viewer.canvas = Mock()
        viewer.canvas.winfo_width.return_value = 40
        viewer.canvas.winfo_height.return_value = 40
        viewer._closed = False
        viewer.source = Image.new("RGB", (80, 80))
        viewer.zoom = 0.5
        viewer.offset_x = viewer.offset_y = 0
        viewer.fit_mode = True
        viewer._fit_photo = None
        viewer._fit_pixels = None
        viewer._wheel_job = None
        viewer._render_pool = Mock()
        viewer._render_version = 1
        viewer._render_job = None
        viewer._display_photo = Mock()
        return viewer

    def test_drag_moves_existing_picture_while_new_pixels_are_pending(self):
        viewer = self.make_viewer()
        viewer.zoom = 1.0
        viewer.fit_mode = False
        viewer._dragging = True
        viewer._canvas_image = 1
        viewer._drag_origin = (20, 20)
        viewer._drag_offset = (0, 0)
        viewer._on_drag_motion(Mock(x=15, y=10))
        viewer.canvas.move.assert_called_once_with(1, -5, -10)
        viewer._on_drag_motion(Mock(x=15, y=10))
        self.assertEqual(viewer.root.after.call_count, 1)
        self.assertEqual(viewer._render_version, 2)

    @patch.object(pv.ImageTk, "PhotoImage")
    def test_continuous_wheel_displays_anchored_preview_then_refines(self, photo_image):
        viewer = self.make_viewer()
        viewer.zoom_label = Mock()
        preview = Image.new("RGB", (40, 40))
        preview.putpixel((10, 20), (150, 30, 40))
        viewer._fit_pixels = preview
        pending = []

        def submit(function, *args):
            future = Future()
            future.set_result(function(*args))
            pending.append(function)
            return future

        viewer._render_pool.submit.side_effect = submit
        viewer._zoom_to(1, 10, 20, wheel=True)
        viewer._render()
        first_args = viewer.root.after.call_args.args[2:]
        viewer._zoom_to(2, 10, 20, wheel=True)
        viewer._finish_render(*first_args)
        self.assertEqual(pending, [pv.render_zoom_preview, pv.render_zoom_preview])
        self.assertEqual(photo_image.call_args.args[0].getpixel((10, 20)), (150, 30, 40))
        self.assertEqual(viewer._display_photo.call_count, 1)

        second_args = viewer.root.after.call_args.args[2:]
        viewer._finish_wheel_zoom()
        viewer._finish_render(*second_args)
        self.assertIs(pending[-1], pv.render_region)
        self.assertEqual(viewer._display_photo.call_count, 1)
        viewer._finish_render(*viewer.root.after.call_args.args[2:])
        self.assertEqual(viewer._display_photo.call_count, 2)
        self.assertEqual(photo_image.call_args.args[0].getpixel((10, 20)), (0, 0, 0))

    @patch.object(pv.ImageTk, "PhotoImage")
    def test_newest_view_wins_and_fit_preview_is_reused(self, photo_image):
        viewer = self.make_viewer()
        first, latest = Future(), Future()
        viewer._render_pool.submit.side_effect = [first, latest]
        viewer._render()
        viewer.zoom = 0.25
        viewer._schedule_render()
        viewer.zoom = 0.125
        viewer._schedule_render()
        self.assertEqual(viewer._render_pool.submit.call_count, 1)
        self.assertEqual(viewer.root.after.call_count, 1)

        first.set_result(Image.new("RGB", (40, 40)))
        viewer._finish_render(first, 1, 0, 0, True)
        photo_image.assert_not_called()
        self.assertEqual(viewer._render_pool.submit.call_args.args[-1], (10, 10))

        latest.set_result(Image.new("RGB", (10, 10)))
        photo = photo_image.return_value
        photo.width.return_value = photo.height.return_value = 10
        viewer._finish_render(latest, viewer._render_version, 0, 0, True)
        viewer._render()
        self.assertEqual(viewer._render_pool.submit.call_count, 2)
        self.assertEqual(viewer._display_photo.call_count, 2)
        self.assertIs(viewer._fit_photo, photo)

        viewer._canvas_image = None
        viewer._clear_canvas_image()
        self.assertIsNone(viewer._fit_photo)


if __name__ == "__main__":
    unittest.main()
