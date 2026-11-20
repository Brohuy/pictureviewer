#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Picture Viewer — a fast, minimal folder-based image viewer.

Run:
    python picture_viewer.py [FOLDER_OR_IMAGE]

Requires Python 3.10+ and Pillow.
"""

from __future__ import annotations

import argparse
import ctypes
import math
import os
import queue
import re
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable, Sequence

try:
    from PIL import Image, ImageDraw, ImageOps, ImageTk
except ImportError:  # pragma: no cover - only hit on a broken installation
    sys.stderr.write(
        "缺少 Pillow。请先运行：python -m pip install -r requirements.txt\n"
    )
    raise SystemExit(1)

try:
    import pillow_heif  # optional HEIC/HEIF support
except ImportError:  # pragma: no cover - optional dependency
    pass
else:  # pragma: no cover - depends on the local environment
    try:
        pillow_heif.register_heif_opener()
    except Exception:
        # A broken optional plugin should never prevent the viewer from starting.
        pass


APP_NAME = "Picture Viewer"

BG = "#0e1117"
SURFACE = "#171c26"
SURFACE_HOVER = "#202736"
CANVAS_BG = "#0a0c11"
SIDEBAR_BG = "#10141c"
SIDEBAR_SELECT = "#22314f"
BORDER = "#252c3a"
TEXT = "#e9edf5"
MUTED = "#8e98ab"
MUTED_DIM = "#566073"
ACCENT = "#7aa2f7"
ACCENT_HOVER = "#9bb9ff"
ACCENT_TEXT = "#0b1120"
SIDEBAR_WIDTH = 248
SIDEBAR_MIN_WIDTH = 180
SIDEBAR_MAX_WIDTH = 640
SIDEBAR_ROW_HEIGHT = 22

MIN_ZOOM = 0.001
MAX_ZOOM = 32.0
WHEEL_FACTOR = 1.15
FIT_PADDING = 32
POLL_MS = 30
WATCH_INTERVAL = 1.5

_SPLIT_DIGITS = re.compile(r"(\d+)")


# ---------------------------------------------------------------------------
# File discovery and ordering
# ---------------------------------------------------------------------------


def supported_extensions() -> frozenset[str]:
    """Return every file extension Pillow can currently open."""
    extensions = {
        ext.lower()
        for ext, image_format in Image.registered_extensions().items()
        if image_format in Image.OPEN
    }
    extensions.update({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"})
    return frozenset(extensions)


SUPPORTED_EXTENSIONS = supported_extensions()


def natural_key(value: str) -> tuple:
    """Sort key where img2 comes before img10, case-insensitively."""
    parts = []
    for part in _SPLIT_DIGITS.split(value):
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part.casefold()))
    return tuple(parts)


def image_sort_key(path: Path, root: Path) -> tuple:
    """Group images by directory, natural-sorting both folders and filenames."""
    relative = path.relative_to(root)
    parent = relative.parent
    if str(parent) == ".":
        parent_key = (0,)
    else:
        # The raw path is a tie-breaker so one directory's files always stay
        # contiguous, even when natural keys collide on case-sensitive systems.
        parent_key = (
            1,
            tuple(natural_key(part) for part in parent.parts),
            parent.as_posix(),
        )
    return parent_key, natural_key(relative.name)


def scan_tree(
    root: Path, *, skip_hidden: bool = True
) -> tuple[list[Path], list[Path], list[str]]:
    """Recursively collect supported images and all visible directories.

    Hidden directories can contain cache or thumbnail files, so they are
    skipped by default. Unreadable directories are reported instead of
    interrupting the scan. Symbolic links are not followed.
    """
    root = Path(root).resolve()
    images: list[Path] = []
    directories: list[Path] = []
    errors: list[str] = []

    def on_error(error: OSError) -> None:
        errors.append(str(error))

    for dirpath, dirnames, filenames in os.walk(
        root, topdown=True, onerror=on_error, followlinks=False
    ):
        if skip_hidden:
            dirnames[:] = [
                name
                for name in dirnames
                if not name.startswith(".")
                and name not in {"$RECYCLE.BIN", "System Volume Information"}
            ]
        dirnames.sort(key=natural_key)
        filenames.sort(key=natural_key)
        directories.append(Path(dirpath))

        for filename in filenames:
            if skip_hidden and filename.startswith("."):
                continue
            path = Path(dirpath) / filename
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                images.append(path)

    images.sort(key=lambda item: image_sort_key(item, root))
    directories.sort(
        key=lambda item: tuple(
            natural_key(part) for part in item.relative_to(root).parts
        )
    )
    return images, directories, errors


def scan_images(root: Path, *, skip_hidden: bool = True) -> tuple[list[Path], list[str]]:
    """Recursively collect supported images below *root*."""
    images, _directories, errors = scan_tree(root, skip_hidden=skip_hidden)
    return images, errors


def build_folder_groups(images: Sequence[Path]) -> list[tuple[Path, int, int]]:
    """Return contiguous (folder, start_index, end_index_exclusive) groups."""
    groups: list[list] = []
    for index, image in enumerate(images):
        parent = image.parent
        if not groups or groups[-1][0] != parent:
            groups.append([parent, index, index + 1])
        else:
            groups[-1][2] = index + 1
    return [(folder, start, end) for folder, start, end in groups]


def format_file_size(byte_count: int) -> str:
    if byte_count >= 1024 * 1024:
        return f"{byte_count / (1024 * 1024):.1f} MB"
    if byte_count >= 1024:
        return f"{byte_count / 1024:.0f} KB"
    return f"{byte_count} B"


def format_zoom(zoom: float) -> str:
    percent = zoom * 100
    if percent >= 100:
        return f"{percent:.0f}%"
    if percent >= 10:
        value = f"{percent:.1f}"
    elif percent >= 1:
        value = f"{percent:.2f}"
    else:
        value = f"{percent:.3f}"
    return value.rstrip("0").rstrip(".") + "%"


def friendly_image_error(error: Exception) -> str:
    if isinstance(error, FileNotFoundError):
        return "文件不存在，可能已被移动或删除。"
    if isinstance(error, PermissionError):
        return "没有权限读取这个文件。"
    if isinstance(error, Image.DecompressionBombError):
        return "图片尺寸过大，出于安全考虑已拒绝载入。"
    if isinstance(error, Image.UnidentifiedImageError):
        return "无法识别图片格式，或文件已经损坏。"
    message = str(error).strip()
    return f"{message}" if message else type(error).__name__


# ---------------------------------------------------------------------------
# Background image loading
# ---------------------------------------------------------------------------


class ImageLoader:
    """Single background worker that always processes the latest request."""

    def __init__(self, events: queue.Queue) -> None:
        self._events = events
        self._condition = threading.Condition()
        self._request: tuple[int, Path] | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="picture-loader", daemon=True
        )
        self._thread.start()

    def request(self, token: int, path: Path) -> None:
        with self._condition:
            if self._closed:
                return
            self._request = (token, path)
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._request is None and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                token, path = self._request
                self._request = None

            try:
                with Image.open(path) as opened:
                    image_format = opened.format or path.suffix.lstrip(".").upper()
                    had_alpha = (
                        opened.mode in {"RGBA", "LA", "PA"}
                        or "A" in opened.getbands()
                        or (opened.mode == "P" and "transparency" in opened.info)
                    )
                    image = ImageOps.exif_transpose(opened)
                    image = image.convert("RGBA" if had_alpha else "RGB")
                    image.load()
                    width, height = image.size

                try:
                    byte_count = path.stat().st_size
                except OSError:
                    byte_count = 0

                metadata = {
                    "width": width,
                    "height": height,
                    "format": image_format,
                    "bytes": byte_count,
                }
                self._events.put(("image_ready", token, path, image, metadata))
            except Exception as error:  # noqa: BLE001 - shown to the user
                self._events.put(
                    ("image_failed", token, path, friendly_image_error(error))
                )


# ---------------------------------------------------------------------------
# Small UI helpers
# ---------------------------------------------------------------------------


class Tooltip:
    """Delayed tooltip for toolbar buttons."""

    def __init__(self, widget: tk.Widget, text: str, delay: int = 550) -> None:
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id: str | None = None
        self._window: tk.Toplevel | None = None

        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def _schedule(self, _event: tk.Event | None = None) -> None:
        self.cancel()
        self._after_id = self.widget.after(self.delay, self.show)

    def cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def show(self) -> None:
        self._after_id = None
        if self._window is not None:
            return
        try:
            x = self.widget.winfo_pointerx() + 12
            y = self.widget.winfo_pointery() + 22
        except tk.TclError:
            return

        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.attributes("-topmost", True)
        frame = tk.Frame(window, bg=BORDER, padx=1, pady=1)
        frame.pack()
        label = tk.Label(
            frame,
            text=self.text,
            bg=SURFACE,
            fg=TEXT,
            font=self.widget.winfo_toplevel().ui_font,
            padx=9,
            pady=6,
        )
        label.pack()

        window.update_idletasks()
        width = window.winfo_reqwidth()
        screen_width = window.winfo_screenwidth()
        if x + width > screen_width - 8:
            x = screen_width - width - 8
        window.wm_geometry(f"+{x}+{y}")
        self._window = window

    def hide(self, _event: tk.Event | None = None) -> None:
        self.cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except tk.TclError:
                pass
            self._window = None


def enable_high_dpi() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Brohuy.PictureViewer.1"
        )
    except Exception:
        pass


def choose_ui_font(root: tk.Misc) -> str:
    available = set(tkfont.families(root))
    for family in (
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "PingFang SC",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "WenQuanYi Micro Hei",
        "Segoe UI",
    ):
        if family in available:
            return family
    return tkfont.nametofont("TkDefaultFont").actual("family")


def choose_mono_font(root: tk.Misc) -> str:
    available = set(tkfont.families(root))
    for family in ("Cascadia Mono", "Consolas", "SF Mono", "DejaVu Sans Mono"):
        if family in available:
            return family
    return tkfont.nametofont("TkFixedFont").actual("family")


def make_app_icon(size: int = 64) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = max(6, size // 5)

    draw.rounded_rectangle(
        (1, 1, size - 2, size - 2),
        radius=radius,
        fill=(21, 27, 37, 255),
        outline=(122, 162, 247, 255),
        width=max(1, size // 32),
    )
    # Sun
    sun_r = max(3, size // 11)
    draw.ellipse(
        (
            size - 17 - sun_r,
            14 - sun_r,
            size - 17 + sun_r,
            14 + sun_r,
        ),
        fill=(235, 187, 111, 255),
    )
    # Mountains
    draw.polygon(
        [
            (int(size * 0.14), int(size * 0.72)),
            (int(size * 0.38), int(size * 0.38)),
            (int(size * 0.56), int(size * 0.61)),
            (int(size * 0.67), int(size * 0.48)),
            (int(size * 0.86), int(size * 0.72)),
        ],
        fill=(206, 217, 235, 255),
    )
    draw.rectangle(
        (
            int(size * 0.14),
            int(size * 0.71),
            int(size * 0.86),
            int(size * 0.78),
        ),
        fill=(122, 162, 247, 255),
    )
    return image


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class PictureViewerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._closed = False

        self.ui_font = choose_ui_font(root)
        self.mono_font = choose_mono_font(root)
        root.ui_font = (self.ui_font, 9)  # used by tooltips

        self.root_path: Path | None = None
        self.images: list[Path] = []
        self.folder_groups: list[tuple[Path, int, int]] = []
        self.folder_directories: list[Path] = []
        self.index = -1

        self.source: Image.Image | None = None
        self.metadata: dict | None = None
        self.zoom = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.fit_mode = True

        self._scan_token = 0
        self._load_token = 0
        self._pending_initial_path: Path | None = None
        self._events: queue.Queue = queue.Queue()
        self._loader = ImageLoader(self._events)
        self._last_dir = Path.home()
        self._watch_stop_event: threading.Event | None = None
        self._watch_thread: threading.Thread | None = None

        self._sidebar_dragging = False
        self._sidebar_drag_origin = 0
        self._sidebar_drag_width = SIDEBAR_WIDTH
        self._sticky_folder_item: str | None = None
        self._sticky_folder_path: Path | None = None
        self._pinned_row_update_job: str | None = None
        self._context_path: Path | None = None
        self._toast_after: str | None = None

        self._canvas_size = (0, 0)
        self._render_job: str | None = None
        self._fit_retry_job: str | None = None
        self._configure_job: str | None = None
        self._dragging = False
        self._drag_origin = (0, 0)
        self._drag_offset = (0.0, 0.0)
        self._tooltips: list[Tooltip] = []

        self._photo: ImageTk.PhotoImage | None = None
        self._canvas_image: int | None = None
        self._window_icon: ImageTk.PhotoImage | None = None
        self._brand_icon: ImageTk.PhotoImage | None = None

        self._configure_window()
        self._build_ui()
        self._bind_events()
        self._show_welcome()
        self.root.after(POLL_MS, self._poll_events)
        self.root.after(20, self._apply_windows_dark_title)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # -- window and theme -------------------------------------------------

    def _configure_window(self) -> None:
        self.root.title(APP_NAME)
        self.root.configure(bg=BG)
        self.root.minsize(900, 560)

        width, height = 1280, 820
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(width, screen_width - 80)
        height = min(height, screen_height - 100)
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2 - 12)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        icon = make_app_icon(64)
        self._window_icon = ImageTk.PhotoImage(icon)
        self.root.iconphoto(True, self._window_icon)
        self._brand_icon = ImageTk.PhotoImage(
            icon.resize((28, 28), Image.Resampling.LANCZOS)
        )

        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(2, weight=1)

    def _apply_windows_dark_title(self) -> None:
        if os.name != "nt":
            return
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                hwnd = self.root.winfo_id()
            value = ctypes.c_int(1)
            for attribute in (20, 19):
                result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
                )
                if result == 0:
                    break
        except Exception:
            pass

    # -- widget construction ---------------------------------------------

    def _font(self, size: int = 10, weight: str = "normal") -> tuple[str, int, str]:
        return (self.ui_font, size, weight)

    def _mono(self, size: int = 10, weight: str = "normal") -> tuple[str, int, str]:
        return (self.mono_font, size, weight)

    def _make_button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        kind: str = "ghost",
        font: tuple | None = None,
        padx: int = 11,
        pady: int = 5,
    ) -> tk.Button:
        styles = {
            "ghost": (BG, SURFACE, MUTED, TEXT),
            "canvasghost": (CANVAS_BG, SURFACE, MUTED, TEXT),
            "surface": (SURFACE, SURFACE_HOVER, TEXT, TEXT),
            "accent": (ACCENT, ACCENT_HOVER, ACCENT_TEXT, ACCENT_TEXT),
        }
        base_bg, hover_bg, fg, hover_fg = styles[kind]
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=base_bg,
            fg=fg,
            activebackground=hover_bg,
            activeforeground=hover_fg,
            disabledforeground=MUTED_DIM,
            relief="flat",
            overrelief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=padx,
            pady=pady,
            font=font or self._font(10),
            cursor="hand2",
            takefocus=0,
        )
        button._base_bg = base_bg  # type: ignore[attr-defined]
        button._hover_bg = hover_bg  # type: ignore[attr-defined]
        button._base_fg = fg  # type: ignore[attr-defined]
        button._hover_fg = hover_fg  # type: ignore[attr-defined]

        def on_enter(_event: tk.Event) -> None:
            if str(button.cget("state")) != "disabled":
                button.configure(bg=hover_bg, fg=hover_fg)

        def on_leave(_event: tk.Event) -> None:
            if str(button.cget("state")) != "disabled":
                button.configure(bg=base_bg, fg=fg)

        button.bind("<Enter>", on_enter, add="+")
        button.bind("<Leave>", on_leave, add="+")
        return button

    def _add_tooltip(self, widget: tk.Widget, text: str) -> None:
        self._tooltips.append(Tooltip(widget, text))

    def _vertical_separator(self, parent: tk.Misc) -> tk.Frame:
        separator = tk.Frame(parent, bg=BORDER, width=1, height=20)
        return separator

    def _build_ui(self) -> None:
        self.header = tk.Frame(self.root, bg=BG, height=60)
        self.header.grid(row=0, column=0, sticky="ew")
        self.header.pack_propagate(False)

        brand_area = tk.Frame(self.header, bg=BG)
        brand_area.pack(side="left", padx=(18, 0))

        if self._brand_icon is not None:
            tk.Label(
                brand_area, image=self._brand_icon, bg=BG, bd=0
            ).pack(side="left")
        tk.Label(
            brand_area,
            text=APP_NAME,
            bg=BG,
            fg=TEXT,
            font=self._font(11, "bold"),
        ).pack(side="left", padx=(9, 0))

        controls = tk.Frame(self.header, bg=BG)
        controls.pack(side="right", padx=(0, 18))

        self.fit_button = self._make_button(
            controls, "适应", self.fit_to_window, font=self._font(10)
        )
        self.fit_button.pack(side="left")
        self._add_tooltip(self.fit_button, "适应窗口 · F")

        self.actual_button = self._make_button(
            controls, "1:1", self.set_actual_size, font=self._mono(10)
        )
        self.actual_button.pack(side="left", padx=(2, 0))
        self._add_tooltip(self.actual_button, "原始大小 · 1")

        separator = self._vertical_separator(controls)
        separator.pack(side="left", padx=11, pady=8)

        self.zoom_out_button = self._make_button(
            controls,
            "−",
            lambda: self.zoom_by(1 / 1.25),
            font=self._font(13),
            padx=9,
            pady=3,
        )
        self.zoom_out_button.pack(side="left")
        self._add_tooltip(self.zoom_out_button, "缩小 · − / 滚轮")

        self.zoom_label = tk.Label(
            controls,
            text="—",
            bg=BG,
            fg=MUTED,
            font=self._mono(10),
            width=7,
            anchor="center",
        )
        self.zoom_label.pack(side="left", padx=2)

        self.zoom_in_button = self._make_button(
            controls,
            "+",
            lambda: self.zoom_by(1.25),
            font=self._font(12),
            padx=9,
            pady=3,
        )
        self.zoom_in_button.pack(side="left")
        self._add_tooltip(self.zoom_in_button, "放大 · + / 滚轮")

        nav = tk.Frame(self.header, bg=BG)
        nav.place(relx=0.5, rely=0.5, anchor="center")

        self.prev_button = self._make_button(
            nav,
            "←",
            lambda: self.navigate_relative(-1),
            kind="surface",
            font=self._font(11, "bold"),
            padx=12,
            pady=4,
        )
        self.prev_button.pack(side="left")
        self._add_tooltip(self.prev_button, "上一张 · ← / ↑")

        self.index_label = tk.Label(
            nav,
            text="— / —",
            bg=SURFACE,
            fg=MUTED,
            font=self._mono(10),
            width=11,
            padx=4,
            pady=6,
            anchor="center",
        )
        self.index_label.pack(side="left", padx=6)

        self.next_button = self._make_button(
            nav,
            "→",
            lambda: self.navigate_relative(1),
            kind="surface",
            font=self._font(11, "bold"),
            padx=12,
            pady=4,
        )
        self.next_button.pack(side="left")
        self._add_tooltip(self.next_button, "下一张 · → / ↓")

        header_rule = tk.Frame(self.root, bg=BORDER, height=1)
        header_rule.grid(row=1, column=0, sticky="ew")

        self.content = tk.Frame(self.root, bg=CANVAS_BG)
        self.content.grid(row=2, column=0, sticky="nsew")
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(2, weight=1)

        self._build_sidebar(self.content)

        self.sidebar_splitter = tk.Frame(
            self.content,
            bg=BORDER,
            width=6,
            cursor="sb_h_double_arrow",
        )
        self.sidebar_splitter.grid(row=0, column=1, sticky="ns")
        self.sidebar_splitter.bind("<Enter>", self._on_splitter_enter)
        self.sidebar_splitter.bind("<Leave>", self._on_splitter_leave)
        self.sidebar_splitter.bind("<ButtonPress-1>", self._on_splitter_press)
        self.sidebar_splitter.bind("<B1-Motion>", self._on_splitter_motion)
        self.sidebar_splitter.bind("<ButtonRelease-1>", self._on_splitter_release)

        self.canvas = tk.Canvas(
            self.content,
            bg=CANVAS_BG,
            highlightthickness=0,
            borderwidth=0,
            cursor="arrow",
        )
        self.canvas.grid(row=0, column=2, sticky="nsew")

        self._build_overlay()
        self.loading_pill = tk.Label(
            self.canvas,
            text="",
            bg=SURFACE,
            fg=MUTED,
            font=self._font(9),
            padx=14,
            pady=6,
        )
        self.toast_label = tk.Label(
            self.canvas,
            text="",
            bg=SURFACE,
            fg=TEXT,
            font=self._font(9),
            padx=14,
            pady=6,
        )

        self.context_menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=SURFACE,
            fg=TEXT,
            activebackground=ACCENT,
            activeforeground=ACCENT_TEXT,
            borderwidth=0,
            activeborderwidth=0,
            font=self._font(9),
        )
        self.context_menu.add_command(
            label="复制路径", command=self._copy_context_path
        )

        footer_rule = tk.Frame(self.root, bg=BORDER, height=1)
        footer_rule.grid(row=3, column=0, sticky="ew")

        self.footer = tk.Frame(self.root, bg=BG, height=38)
        self.footer.grid(row=4, column=0, sticky="ew")
        self.footer.pack_propagate(False)

        self.meta_label = tk.Label(
            self.footer,
            text="",
            bg=BG,
            fg=MUTED,
            font=self._mono(9),
            anchor="e",
        )
        self.meta_label.pack(side="right", padx=(12, 18))

        path_area = tk.Frame(self.footer, bg=BG)
        path_area.pack(side="left", fill="x", expand=True, padx=(18, 0))

        self.file_label = tk.Label(
            path_area,
            text="未打开文件夹",
            bg=BG,
            fg=TEXT,
            font=self._font(9, "bold"),
            anchor="w",
        )
        self.file_label.pack(side="left")

        self.folder_label = tk.Label(
            path_area,
            text="",
            bg=BG,
            fg=MUTED,
            font=self._font(9),
            anchor="w",
        )
        self.folder_label.pack(side="left", padx=(8, 0))

        self._set_toolbar_state()

    def _build_sidebar(self, parent: tk.Misc) -> None:
        self.sidebar = tk.Frame(parent, bg=SIDEBAR_BG, width=SIDEBAR_WIDTH)
        self.sidebar.grid(row=0, column=0, sticky="ns")
        self.sidebar.pack_propagate(False)

        title = tk.Label(
            self.sidebar,
            text="目录",
            bg=SIDEBAR_BG,
            fg=TEXT,
            font=self._font(10, "bold"),
            anchor="w",
        )
        title.pack(fill="x", padx=16, pady=(16, 2))

        self.sidebar_summary = tk.Label(
            self.sidebar,
            text="",
            bg=SIDEBAR_BG,
            fg=MUTED,
            font=self._font(8),
            anchor="w",
        )
        self.sidebar_summary.pack(fill="x", padx=16, pady=(0, 12))

        self.sidebar_open_button = self._make_button(
            self.sidebar,
            "＋ 打开文件夹",
            self.open_folder_dialog,
            kind="surface",
            font=self._font(9, "bold"),
            padx=10,
            pady=7,
        )
        self.sidebar_open_button.pack(fill="x", padx=12, pady=(0, 12))
        self._add_tooltip(self.sidebar_open_button, "打开文件夹 · Ctrl+O")

        rule = tk.Frame(self.sidebar, bg=BORDER, height=1)
        rule.pack(fill="x", padx=12)

        self.list_area = tk.Frame(self.sidebar, bg=SIDEBAR_BG)
        self.list_area.pack(fill="both", expand=True, padx=(10, 4), pady=(10, 12))
        self.list_area.grid_rowconfigure(0, weight=1)
        self.list_area.grid_columnconfigure(0, weight=1)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Sidebar.Treeview",
            background=SIDEBAR_BG,
            fieldbackground=SIDEBAR_BG,
            foreground=MUTED,
            borderwidth=0,
            relief="flat",
            font=self._font(9),
            rowheight=SIDEBAR_ROW_HEIGHT,
            indent=15,
        )
        style.map(
            "Sidebar.Treeview",
            background=[("selected", SIDEBAR_SELECT)],
            foreground=[("selected", TEXT)],
        )
        style.layout(
            "Sidebar.Treeview",
            [("Treeview.treearea", {"sticky": "nswe"})],
        )

        self._folder_yview = (0.0, 1.0)
        self._folder_items_by_path: dict[Path, str] = {}
        self._folder_first_index: dict[str, int] = {}
        self._folder_item_paths: dict[str, Path] = {}
        self._image_item_index: dict[str, int] = {}
        self._image_items_by_path: dict[Path, str] = {}

        self.folder_tree = ttk.Treeview(
            self.list_area,
            show="tree",
            selectmode="browse",
            style="Sidebar.Treeview",
            cursor="hand2",
            takefocus=0,
        )
        self.folder_tree.column("#0", width=190, minwidth=100, stretch=True)
        self.folder_tree.grid(row=0, column=0, sticky="nsew")
        self.folder_tree.bind("<Configure>", self._on_folder_tree_configure, add="+")

        self.sidebar_scrollbar = tk.Canvas(
            self.list_area,
            width=5,
            bg=SIDEBAR_BG,
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
        )
        self.sidebar_scrollbar.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        self.folder_tree.configure(yscrollcommand=self._on_folder_scroll)
        self.sidebar_scrollbar.bind(
            "<Configure>", lambda _event: self._draw_sidebar_scrollbar()
        )
        self.sidebar_scrollbar.bind(
            "<Button-1>", self._on_sidebar_scrollbar_click
        )
        self.sidebar_scrollbar.bind(
            "<B1-Motion>", self._on_sidebar_scrollbar_click
        )

        self.sidebar_empty = tk.Label(
            self.list_area,
            text="打开文件夹后\n在这里切换目录",
            bg=SIDEBAR_BG,
            fg=MUTED_DIM,
            font=self._font(9),
            justify="center",
            wraplength=180,
        )
        self.sidebar_empty.place(relx=0.5, rely=0.38, anchor="center")

        self.folder_tree.bind("<ButtonRelease-1>", self._on_sidebar_click)
        self.folder_tree.bind("<Button-3>", self._on_sidebar_context_menu)
        self.folder_tree.bind("<Button-2>", self._on_sidebar_context_menu)
        self.folder_tree.bind("<MouseWheel>", self._on_sidebar_wheel)
        self.folder_tree.bind(
            "<Button-4>", lambda event: self._on_sidebar_wheel(event, 1)
        )
        self.folder_tree.bind(
            "<Button-5>", lambda event: self._on_sidebar_wheel(event, -1)
        )

        self.pinned_row = tk.Canvas(
            self.list_area,
            bg=SIDEBAR_BG,
            highlightthickness=0,
            borderwidth=0,
            height=SIDEBAR_ROW_HEIGHT,
            cursor="hand2",
        )
        self.pinned_row.bind("<Button-1>", self._on_pinned_row_click)
        self.pinned_row.bind("<Button-3>", self._on_pinned_row_context_menu)
        self.pinned_row.bind("<Button-2>", self._on_pinned_row_context_menu)

    def _clear_sidebar(self, summary: str, empty_message: str) -> None:
        self._hide_sticky_folder()
        children = self.folder_tree.get_children("")
        if children:
            self.folder_tree.delete(*children)
        selection = self.folder_tree.selection()
        if selection:
            self.folder_tree.selection_remove(*selection)
        self._folder_items_by_path.clear()
        self._folder_first_index.clear()
        self._folder_item_paths.clear()
        self._image_item_index.clear()
        self._image_items_by_path.clear()
        self.folder_tree.yview_moveto(0)
        self._folder_yview = (0.0, 1.0)
        self._draw_sidebar_scrollbar()
        self.sidebar_summary.configure(text=summary)
        if empty_message:
            self.sidebar_empty.configure(text=empty_message)
            self.sidebar_empty.place(relx=0.5, rely=0.38, anchor="center")
        else:
            self.sidebar_empty.place_forget()

    def _populate_sidebar(self, open_paths: set[Path] | None = None) -> None:
        self._hide_sticky_folder()
        children = self.folder_tree.get_children("")
        if children:
            self.folder_tree.delete(*children)
        self._folder_items_by_path.clear()
        self._folder_first_index.clear()
        self._folder_item_paths.clear()
        self._image_item_index.clear()
        self._image_items_by_path.clear()

        if self.root_path is None:
            self.sidebar_summary.configure(text="没有可显示的目录")
            self.sidebar_empty.configure(text="打开文件夹后\n在这里切换目录")
            self.sidebar_empty.place(relx=0.5, rely=0.38, anchor="center")
            self._folder_yview = (0.0, 1.0)
            self._draw_sidebar_scrollbar()
            return

        root = self.root_path
        nodes: dict[Path, dict] = {}

        def ensure_node(path: Path) -> dict:
            if path not in nodes:
                nodes[path] = {
                    "path": path,
                    "children": set(),
                    "start": None,
                    "end": None,
                }
            return nodes[path]

        ensure_node(root)
        directory_paths: set[Path] = {root}
        for path in self.folder_directories:
            try:
                path.relative_to(root)
            except ValueError:
                continue
            directory_paths.add(path)
        for path in directory_paths:
            ensure_node(path)
            current = path
            while current != root:
                parent = current.parent
                ensure_node(parent)["children"].add(current)
                current = parent

        for folder, start, end in self.folder_groups:
            try:
                folder.relative_to(root)
            except ValueError:
                continue
            node = ensure_node(folder)
            node["start"] = start
            node["end"] = end
            current = folder
            while current != root:
                parent = current.parent
                ensure_node(parent)["children"].add(current)
                current = parent

        def prepare(path: Path) -> tuple[int | None, int]:
            node = nodes[path]
            direct = (
                node["end"] - node["start"]
                if node["start"] is not None
                else 0
            )
            first = node["start"]
            ordered = sorted(
                node["children"],
                key=lambda item: (natural_key(item.name), item.name),
            )
            for child in ordered:
                child_first, child_total = prepare(child)
                if child_first is not None and (
                    first is None or child_first < first
                ):
                    first = child_first
                direct += child_total
            node["ordered"] = ordered
            node["first"] = first
            node["total"] = direct
            return first, direct

        prepare(root)

        counter = 0

        def insert_node(path: Path, parent_id: str = "") -> None:
            nonlocal counter
            node = nodes[path]
            label = root.name if path == root else path.name
            if not label:
                label = str(path)
            item_id = f"folder-{counter}"
            counter += 1
            item_text = f"{label}    ·  {node['total']}"
            self.folder_tree.insert(
                parent_id,
                "end",
                iid=item_id,
                text=item_text,
                open=(path == root),
            )
            self._folder_items_by_path[path] = item_id
            self._folder_first_index[item_id] = (
                int(node["first"]) if node["first"] is not None else -1
            )
            self._folder_item_paths[item_id] = path
            for child in node["ordered"]:
                insert_node(child, item_id)
            if node["start"] is not None:
                for image_index in range(node["start"], node["end"]):
                    image_path = self.images[image_index]
                    image_item = f"image-{counter}"
                    counter += 1
                    self.folder_tree.insert(
                        item_id,
                        "end",
                        iid=image_item,
                        text=image_path.name,
                    )
                    self._image_item_index[image_item] = image_index
                    self._image_items_by_path[image_path] = image_item

        insert_node(root)
        if open_paths is not None:
            for path, item_id in self._folder_items_by_path.items():
                try:
                    self.folder_tree.item(item_id, open=path in open_paths)
                except tk.TclError:
                    pass
        self.sidebar_empty.place_forget()
        self.sidebar_summary.configure(
            text=(
                f"{len(nodes)} 个目录"
                f"  ·  {len(self.images)} 张图片"
            )
        )
        self.folder_tree.yview_moveto(0)
        self._folder_yview = (0.0, 1.0)
        self._draw_sidebar_scrollbar()
        self._sync_sidebar_selection()

    def _sync_sidebar_selection(self) -> None:
        if not self.images or self.index < 0:
            selection = self.folder_tree.selection()
            if selection:
                self.folder_tree.selection_remove(*selection)
            return
        current_image = self.images[self.index]
        folder_item = self._folder_items_by_path.get(current_image.parent)
        if folder_item is None:
            return

        parent = self.folder_tree.parent(folder_item)
        while parent:
            self.folder_tree.item(parent, open=True)
            parent = self.folder_tree.parent(parent)

        item_id = folder_item
        image_item = self._image_items_by_path.get(current_image)
        if image_item is not None and bool(
            self.folder_tree.item(folder_item, "open")
        ):
            item_id = image_item

        selection = self.folder_tree.selection()
        if selection:
            self.folder_tree.selection_remove(*selection)
        self.folder_tree.selection_set(item_id)
        self.folder_tree.see(item_id)

    def _on_sidebar_click(self, event: tk.Event) -> str:
        item_id = self.folder_tree.identify_row(event.y)
        if not item_id:
            return "break"
        if (
            item_id not in self._folder_first_index
            and item_id not in self._image_item_index
        ):
            return "break"

        element = str(self.folder_tree.identify_element(event.x, event.y))
        if "indicator" in element:
            opened = bool(self.folder_tree.item(item_id, "open"))
            if opened:
                self._sync_sticky_to_current_image()
                self._schedule_tree_item_top(item_id)
            else:
                self._hide_sticky_if_collapsed(item_id)
            self.canvas.focus_set()
            return "break"

        image_index = self._image_item_index.get(item_id)
        if image_index is not None:
            if image_index != self.index:
                self.navigate_to(image_index)
            else:
                self._sync_sidebar_selection()
            self.canvas.focus_set()
            return "break"

        had_children = bool(self.folder_tree.get_children(item_id))
        was_open = bool(self.folder_tree.item(item_id, "open"))
        if had_children:
            if not was_open:
                self.folder_tree.item(item_id, open=True)
            self._sync_sticky_to_current_image()

        start = self._folder_first_index[item_id]
        selection = self.folder_tree.selection()
        if selection:
            self.folder_tree.selection_remove(*selection)
        self.folder_tree.selection_set(item_id)
        if start >= 0:
            if start != self.index:
                self.navigate_to(start)
            else:
                self._sync_sidebar_selection()
        if had_children and not was_open:
            self._schedule_tree_item_top(item_id)
        self.canvas.focus_set()
        return "break"

    def _sync_sticky_to_current_image(self) -> None:
        if self.source is None or not self.images or self.index < 0:
            self._hide_sticky_folder()
            return
        current_folder = self.images[self.index].parent
        item_id = self._folder_items_by_path.get(current_folder)
        if item_id is None:
            self._hide_sticky_folder()
            return
        self._sticky_folder_item = item_id
        self._sticky_folder_path = current_folder
        self._schedule_pinned_row_update()

    def _hide_sticky_folder(self) -> None:
        self._sticky_folder_item = None
        self._sticky_folder_path = None
        self._hide_pinned_row()

    def _hide_sticky_if_collapsed(self, item_id: str) -> None:
        if self._sticky_folder_path is None:
            return
        collapsed_path = self._folder_item_paths.get(item_id)
        if collapsed_path is None:
            return
        try:
            self._sticky_folder_path.relative_to(collapsed_path)
        except ValueError:
            return
        self._hide_sticky_folder()

    def _schedule_pinned_row_update(self) -> None:
        if self._pinned_row_update_job is not None or self._closed:
            return
        try:
            self._pinned_row_update_job = self.root.after_idle(
                self._update_pinned_row_visibility
            )
        except tk.TclError:
            self._pinned_row_update_job = None

    def _update_pinned_row_visibility(self) -> None:
        self._pinned_row_update_job = None
        item_id = self._sticky_folder_item
        if item_id is None or not self.folder_tree.exists(item_id):
            self._hide_pinned_row()
            return
        try:
            bbox = self.folder_tree.bbox(item_id)
        except tk.TclError:
            return
        if bbox and abs(bbox[1]) <= 1:
            self._hide_pinned_row()
        else:
            self._show_pinned_row(item_id)

    def _show_pinned_row(self, item_id: str) -> None:
        path = self._folder_item_paths.get(item_id)
        if path is None or self.root_path is None:
            return
        try:
            relative = path.relative_to(self.root_path)
        except ValueError:
            depth = 0
        else:
            depth = 0 if str(relative) == "." else len(relative.parts)

        text = self.folder_tree.item(item_id, "text")
        is_open = bool(self.folder_tree.item(item_id, "open"))
        selected = item_id in self.folder_tree.selection()
        background = SIDEBAR_SELECT if selected else SIDEBAR_BG
        foreground = TEXT if selected else MUTED
        width = max(1, self.folder_tree.winfo_width())

        canvas = self.pinned_row
        canvas.configure(width=width, bg=background)
        canvas.place(
            x=0,
            y=0,
            width=width,
            height=SIDEBAR_ROW_HEIGHT,
        )
        try:
            canvas.tk.call("raise", canvas._w)
        except tk.TclError:
            pass
        canvas.delete("all")
        left = 4 + depth * 15
        if is_open:
            triangle = (
                left,
                8,
                left + 8,
                8,
                left + 4,
                13,
            )
        else:
            triangle = (
                left,
                6,
                left,
                14,
                left + 6,
                10,
            )
        canvas.create_polygon(
            *triangle,
            fill=foreground,
            outline="",
        )
        canvas.create_text(
            left + 15,
            SIDEBAR_ROW_HEIGHT // 2,
            text=text,
            anchor="w",
            fill=foreground,
            font=self._font(9),
        )

    def _hide_pinned_row(self) -> None:
        pinned = getattr(self, "pinned_row", None)
        if pinned is not None:
            pinned.place_forget()

    def _on_pinned_row_click(self, _event: tk.Event | None = None) -> str:
        item_id = self._sticky_folder_item
        was_open = bool(
            item_id is not None
            and self.folder_tree.exists(item_id)
            and self.folder_tree.item(item_id, "open")
        )
        self._hide_sticky_folder()
        if was_open and item_id is not None:
            self.folder_tree.item(item_id, open=False)
            self.folder_tree.see(item_id)
        self.canvas.focus_set()
        return "break"

    def _on_sidebar_context_menu(self, event: tk.Event) -> str:
        item_id = self.folder_tree.identify_row(event.y)
        if not item_id:
            return "break"
        image_index = self._image_item_index.get(item_id)
        if image_index is not None and 0 <= image_index < len(self.images):
            path = self.images[image_index]
        else:
            path = self._folder_item_paths.get(item_id)
        if path is None:
            return "break"
        self._show_path_menu(path, event.x_root, event.y_root)
        return "break"

    def _on_pinned_row_context_menu(self, event: tk.Event) -> str:
        path = self._sticky_folder_path
        if path is not None:
            self._show_path_menu(path, event.x_root, event.y_root)
        return "break"

    def _show_path_menu(self, path: Path, x_root: int, y_root: int) -> None:
        self._context_path = path
        try:
            self.context_menu.tk_popup(int(x_root), int(y_root))
        finally:
            self.context_menu.grab_release()

    def _copy_context_path(self) -> None:
        if self._context_path is not None:
            self._copy_path(self._context_path)

    def _copy_path(self, path: Path) -> None:
        try:
            absolute = Path(path).resolve()
        except OSError:
            absolute = Path(path)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(absolute))
            self.root.update_idletasks()
        except tk.TclError:
            self._show_toast("复制失败")
            return
        self._show_toast("路径已复制")

    def _show_toast(self, text: str) -> None:
        if self._toast_after is not None:
            try:
                self.root.after_cancel(self._toast_after)
            except tk.TclError:
                pass
            self._toast_after = None
        self.toast_label.configure(text=text)
        self.toast_label.place(relx=0.5, rely=1.0, y=-22, anchor="s")
        self.toast_label.lift()
        self._toast_after = self.root.after(1600, self._hide_toast)

    def _hide_toast(self) -> None:
        self._toast_after = None
        self.toast_label.place_forget()

    def _schedule_tree_item_top(self, item_id: str) -> None:
        try:
            self.root.after_idle(
                lambda: self._scroll_tree_item_to_top(item_id)
            )
        except tk.TclError:
            pass

    def _scroll_tree_item_to_top(self, item_id: str) -> None:
        try:
            if not self.folder_tree.exists(item_id):
                return
            for _attempt in range(2):
                self.folder_tree.update_idletasks()
                bbox = self.folder_tree.bbox(item_id)
                if not bbox:
                    return
                offset = bbox[1]
                if offset <= 1:
                    return
                first, last = self.folder_tree.yview()
                span = last - first
                height = self.folder_tree.winfo_height()
                if span <= 0.0 or height <= 1:
                    return
                shift = (offset / height) * span
                target = min(max(0.0, first + shift), max(0.0, 1.0 - span))
                if math.isclose(target, first, abs_tol=1e-9):
                    return
                self.folder_tree.yview_moveto(target)
            self._schedule_pinned_row_update()
        except tk.TclError:
            pass

    def _on_sidebar_wheel(self, event: tk.Event, direction: int | None = None) -> str:
        if not self._folder_first_index:
            return "break"
        if direction is None:
            delta = int(getattr(event, "delta", 0))
            if delta == 0:
                return "break"
            steps = -1 if delta > 0 else 1
            steps *= max(1, abs(delta) // 120)
        else:
            steps = -direction
        self.folder_tree.yview_scroll(steps, "units")
        return "break"

    def _on_folder_tree_configure(self, event: tk.Event) -> None:
        width = max(80, event.width - 2)
        try:
            current = int(float(self.folder_tree.column("#0", "width")))
        except (tk.TclError, TypeError, ValueError):
            current = -1
        if current != width:
            self.folder_tree.column("#0", width=width, stretch=True)
        pinned = getattr(self, "pinned_row", None)
        if pinned is not None and pinned.winfo_ismapped():
            pinned.place_configure(width=max(1, event.width))
        self._schedule_pinned_row_update()

    def _on_splitter_enter(self, _event: tk.Event) -> None:
        if not self._sidebar_dragging:
            self.sidebar_splitter.configure(bg=SURFACE_HOVER)

    def _on_splitter_leave(self, _event: tk.Event) -> None:
        if not self._sidebar_dragging:
            self.sidebar_splitter.configure(bg=BORDER)

    def _sidebar_width_limit(self) -> int:
        content_width = self.content.winfo_width()
        return max(
            SIDEBAR_MIN_WIDTH,
            min(SIDEBAR_MAX_WIDTH, content_width - 360),
        )

    def _on_splitter_press(self, event: tk.Event) -> None:
        self._sidebar_dragging = True
        self._sidebar_drag_origin = event.x_root
        self._sidebar_drag_width = self.sidebar.winfo_width()
        self.sidebar_splitter.configure(bg=ACCENT)

    def _on_splitter_motion(self, event: tk.Event) -> None:
        if not self._sidebar_dragging:
            return
        width = (
            self._sidebar_drag_width
            + int(event.x_root)
            - int(self._sidebar_drag_origin)
        )
        width = max(SIDEBAR_MIN_WIDTH, min(self._sidebar_width_limit(), width))
        self.sidebar.configure(width=width)

    def _on_splitter_release(self, _event: tk.Event) -> None:
        if not self._sidebar_dragging:
            return
        self._sidebar_dragging = False
        self.sidebar_splitter.configure(bg=SURFACE_HOVER)

    def _on_folder_scroll(self, first: str, last: str) -> None:
        try:
            self._folder_yview = (float(first), float(last))
        except ValueError:
            return
        self._draw_sidebar_scrollbar()
        self._schedule_pinned_row_update()

    def _draw_sidebar_scrollbar(self) -> None:
        canvas = getattr(self, "sidebar_scrollbar", None)
        if canvas is None:
            return
        try:
            canvas.delete("all")
        except tk.TclError:
            return

        first, last = self._folder_yview
        if first <= 0.0 and last >= 1.0:
            return

        height = canvas.winfo_height()
        width = canvas.winfo_width()
        if height <= 2 or width <= 2:
            return

        top = first * height
        bottom = last * height
        minimum = 24
        if bottom - top < minimum:
            midpoint = (top + bottom) / 2
            top = max(0.0, midpoint - minimum / 2)
            bottom = min(float(height), top + minimum)

        canvas.create_rectangle(
            1, top, max(2, width - 1), bottom, fill=MUTED_DIM, outline=""
        )

    def _on_sidebar_scrollbar_click(self, event: tk.Event) -> str:
        first, last = self._folder_yview
        visible = last - first
        if visible <= 0.0 or visible >= 1.0:
            return "break"
        height = max(1, self.sidebar_scrollbar.winfo_height())
        top = event.y / height - visible / 2
        top = max(0.0, min(1.0 - visible, top))
        self.folder_tree.yview_moveto(top)
        return "break"

    def _build_overlay(self) -> None:
        self.overlay = tk.Frame(self.canvas, bg=CANVAS_BG)

        self.overlay_icon = tk.Canvas(
            self.overlay,
            width=108,
            height=82,
            bg=CANVAS_BG,
            highlightthickness=0,
            bd=0,
        )
        self.overlay_icon.pack(pady=(0, 6))
        self._draw_overlay_icon()

        self.overlay_title = tk.Label(
            self.overlay,
            text="",
            bg=CANVAS_BG,
            fg=TEXT,
            font=self._font(22, "bold"),
        )
        self.overlay_title.pack()

        self.overlay_body = tk.Label(
            self.overlay,
            text="",
            bg=CANVAS_BG,
            fg=MUTED,
            font=self._font(10),
            justify="center",
            wraplength=590,
            pady=10,
        )
        self.overlay_body.pack()

        self.overlay_buttons = tk.Frame(self.overlay, bg=CANVAS_BG)
        self.overlay_buttons.pack(pady=(6, 0))

        self.overlay_primary = self._make_button(
            self.overlay_buttons,
            "",
            lambda: None,
            kind="accent",
            font=self._font(10, "bold"),
            padx=18,
            pady=8,
        )
        self.overlay_secondary = self._make_button(
            self.overlay_buttons,
            "",
            lambda: None,
            kind="canvasghost",
            font=self._font(10),
            padx=14,
            pady=8,
        )

    def _draw_overlay_icon(self) -> None:
        canvas = self.overlay_icon
        canvas.delete("all")
        canvas.create_rectangle(
            18, 16, 90, 66, outline="#35405a", width=2
        )
        canvas.create_line(
            20, 58, 41, 35, 55, 51, 65, 40, 88, 64,
            fill="#4b5d82", width=3,
        )
        canvas.create_oval(64, 24, 77, 37, outline=ACCENT, width=2)
        canvas.create_line(14, 74, 94, 74, fill="#222a38", width=2)

    def _bind_events(self) -> None:
        self.root.bind_all("<Left>", lambda event: self._on_arrow(event, -1))
        self.root.bind_all("<Up>", lambda event: self._on_arrow(event, -1))
        self.root.bind_all("<Right>", lambda event: self._on_arrow(event, 1))
        self.root.bind_all("<Down>", lambda event: self._on_arrow(event, 1))
        self.root.bind_all(
            "<Control-Left>", lambda event: self._jump_folder_key(-1)
        )
        self.root.bind_all(
            "<Control-Right>", lambda event: self._jump_folder_key(1)
        )
        self.root.bind_all("<Control-o>", lambda event: self.open_folder_dialog())
        self.root.bind_all("<Control-O>", lambda event: self.open_folder_dialog())
        self.root.bind_all("<Home>", lambda event: self.navigate_to(0))
        self.root.bind_all("<End>", lambda event: self.navigate_to(len(self.images) - 1))
        self.root.bind_all("<Prior>", lambda event: self.navigate_relative(-1))
        self.root.bind_all("<Next>", lambda event: self.navigate_relative(1))
        self.root.bind_all("<space>", lambda event: self.navigate_relative(1))
        self.root.bind_all("<Shift-space>", lambda event: self.navigate_relative(-1))
        self.root.bind_all("<Key-f>", lambda event: self.fit_to_window())
        self.root.bind_all("<Key-F>", lambda event: self.fit_to_window())
        self.root.bind_all("<Key-0>", lambda event: self.fit_to_window())
        self.root.bind_all("<Key-1>", lambda event: self.set_actual_size())
        self.root.bind_all("<plus>", lambda event: self.zoom_by(1.25))
        self.root.bind_all("<equal>", lambda event: self.zoom_by(1.25))
        self.root.bind_all("<KP_Add>", lambda event: self.zoom_by(1.25))
        self.root.bind_all("<minus>", lambda event: self.zoom_by(1 / 1.25))
        self.root.bind_all("<KP_Subtract>", lambda event: self.zoom_by(1 / 1.25))

        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Button-4>", lambda event: self._on_linux_wheel(event, 1))
        self.canvas.bind("<Button-5>", lambda event: self._on_linux_wheel(event, -1))
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)

    # -- welcome and centered states -------------------------------------

    def _show_overlay(
        self,
        title: str,
        body: str,
        *,
        primary: tuple[str, Callable[[], None]] | None = None,
        secondary: tuple[str, Callable[[], None]] | None = None,
    ) -> None:
        self._hide_loading_pill()
        self.overlay_title.configure(text=title)
        self.overlay_body.configure(text=body)

        self.overlay_primary.pack_forget()
        self.overlay_secondary.pack_forget()

        if primary is not None:
            label, callback = primary
            self.overlay_primary.configure(text=label, command=callback)
            self.overlay_primary.pack(side="left")
        if secondary is not None:
            label, callback = secondary
            self.overlay_secondary.configure(text=label, command=callback)
            self.overlay_secondary.pack(side="left", padx=(10, 0))

        self.overlay.place(relx=0.5, rely=0.5, anchor="center")

    def _hide_overlay(self) -> None:
        self.overlay.place_forget()

    def _show_loading_pill(self, text: str) -> None:
        self.loading_pill.configure(text=text)
        self.loading_pill.place(relx=0.5, y=16, anchor="n")

    def _hide_loading_pill(self) -> None:
        self.loading_pill.place_forget()

    def _show_welcome(self, message: str | None = None) -> None:
        self._stop_watcher()
        self._scan_token += 1
        self._load_token += 1
        self.root_path = None
        self.images = []
        self.folder_groups = []
        self.folder_directories = []
        self.index = -1
        self.source = None
        self.metadata = None
        self._pending_initial_path = None
        self._clear_canvas_image()
        self._hide_loading_pill()
        self._update_labels(
            file_text="未打开文件夹",
            folder_text="",
            meta_text="",
        )
        self.index_label.configure(text="— / —")
        self.zoom_label.configure(text="—")
        self._set_toolbar_state()
        self._clear_sidebar("尚未打开文件夹", "打开文件夹后\n在这里切换目录")
        self.root.title(APP_NAME)
        self.canvas.configure(cursor="arrow")

        intro = (
            "打开一个文件夹，即可浏览其中及所有子文件夹里的图片。\n\n"
            "← ↑ ↓ → 切换图片     鼠标滚轮 缩放     拖动鼠标 平移\n"
            "Ctrl + ← / → 跨文件夹     F 适应窗口     1 原始大小\n\n"
            "支持 JPG、PNG、WebP、GIF、TIFF、BMP 等常见格式"
        )
        if message:
            intro = f"{message}\n\n{intro}"
        self._show_overlay(
            "浏览整个文件夹",
            intro,
            primary=("打开文件夹", self.open_folder_dialog),
        )
        try:
            self.canvas.focus_set()
        except tk.TclError:
            pass

    # -- opening and scanning --------------------------------------------

    def open_folder_dialog(self) -> None:
        initial = self.root_path or self._last_dir
        if not Path(initial).is_dir():
            initial = Path.home()
        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择图片文件夹",
            initialdir=str(initial),
            mustexist=True,
        )
        if selected:
            self.open_directory(Path(selected))

    def open_path(self, path: Path) -> None:
        path = Path(path).expanduser()
        try:
            path = path.resolve()
        except OSError:
            pass

        if path.is_file():
            self.open_directory(path.parent, initial_path=path)
        elif path.is_dir():
            self.open_directory(path)
        else:
            self._show_welcome(f"路径不存在：{path}")

    def open_directory(
        self, directory: Path, *, initial_path: Path | None = None
    ) -> None:
        directory = Path(directory).expanduser()
        try:
            directory = directory.resolve()
        except OSError:
            pass
        if not directory.is_dir():
            self._show_welcome(f"无法打开文件夹：{directory}")
            return

        self._stop_watcher()
        self._last_dir = directory
        self._scan_token += 1
        scan_token = self._scan_token
        self._load_token += 1

        self.root_path = directory
        self.images = []
        self.folder_groups = []
        self.folder_directories = []
        self.index = -1
        self.source = None
        self.metadata = None
        self.zoom = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.fit_mode = True
        self._pending_initial_path = initial_path

        self._clear_canvas_image()
        self._hide_loading_pill()
        self._set_toolbar_state()
        self._clear_sidebar("正在扫描…", "正在查找图片…")
        self.canvas.configure(cursor="arrow")
        self.root.title(APP_NAME)
        self.index_label.configure(text="— / —")
        self.zoom_label.configure(text="—")
        self._update_labels(
            file_text="正在扫描…",
            folder_text=str(directory),
            meta_text="",
        )
        self._show_overlay(
            "正在扫描文件夹",
            f"{directory}\n\n正在查找其中的图片…",
        )

        threading.Thread(
            target=self._scan_worker,
            args=(scan_token, directory),
            name="picture-scanner",
            daemon=True,
        ).start()

    def _scan_worker(self, token: int, directory: Path) -> None:
        try:
            images, directories, errors = scan_tree(directory)
            self._events.put(
                ("scan_ready", token, directory, images, directories, errors)
            )
        except Exception as error:  # noqa: BLE001 - shown to the user
            self._events.put(
                ("scan_failed", token, directory, str(error) or type(error).__name__)
            )

    def _start_watcher(
        self,
        token: int,
        directory: Path,
        images: list[Path],
        directories: list[Path],
    ) -> None:
        self._stop_watcher()
        stop_event = threading.Event()
        self._watch_stop_event = stop_event
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            args=(token, directory, images, directories, stop_event),
            name="picture-watcher",
            daemon=True,
        )
        self._watch_thread.start()

    def _stop_watcher(self) -> None:
        if self._watch_stop_event is not None:
            self._watch_stop_event.set()
            self._watch_stop_event = None
        self._watch_thread = None

    def _watch_loop(
        self,
        token: int,
        directory: Path,
        images: list[Path],
        directories: list[Path],
        stop_event: threading.Event,
    ) -> None:
        baseline = (tuple(images), tuple(directories))
        while not stop_event.wait(WATCH_INTERVAL):
            try:
                current_images, current_directories, _errors = scan_tree(directory)
            except Exception:
                continue
            signature = (tuple(current_images), tuple(current_directories))
            if signature == baseline:
                continue
            baseline = signature
            if stop_event.is_set():
                return
            self._events.put(
                (
                    "watch_changed",
                    token,
                    directory,
                    current_images,
                    current_directories,
                )
            )

    def _handle_watch_changed(
        self,
        token: int,
        directory: Path,
        images: list[Path],
        directories: list[Path],
    ) -> None:
        if self._closed or token != self._scan_token:
            return
        if self.root_path is None or directory != self.root_path:
            return

        old_index = self.index
        old_images = self.images
        current_path = (
            old_images[old_index]
            if 0 <= old_index < len(old_images)
            else None
        )
        open_paths = {
            path
            for path, item_id in self._folder_items_by_path.items()
            if self.folder_tree.exists(item_id)
            and bool(self.folder_tree.item(item_id, "open"))
        }
        old_view = self.folder_tree.yview()

        new_index = -1
        if current_path is not None:
            current_key = os.path.normcase(str(current_path))
            for position, image in enumerate(images):
                if os.path.normcase(str(image)) == current_key:
                    new_index = position
                    break

        self.images = images
        self.folder_groups = build_folder_groups(images)
        self.folder_directories = directories

        if new_index >= 0 and self.source is not None:
            self.index = new_index
            current_path = images[new_index]
            self._populate_sidebar(open_paths)
            self._update_labels(
                file_text=current_path.name,
                folder_text=self._folder_display(current_path),
            )
            self.index_label.configure(
                text=f"{self.index + 1} / {len(self.images)}"
            )
            self._set_toolbar_state()
            self.root.title(f"{current_path.name} — {APP_NAME}")
            self._sync_sticky_to_current_image()
            self.folder_tree.yview_moveto(old_view[0])
            self._schedule_pinned_row_update()
            return

        self._load_token += 1
        self._hide_loading_pill()
        self._hide_sticky_folder()
        self.source = None
        self.metadata = None
        self._clear_canvas_image()
        self.index = -1
        self._populate_sidebar(open_paths)

        if not images:
            self._set_toolbar_state()
            self.zoom_label.configure(text="—")
            self._update_labels(
                file_text="没有找到图片",
                folder_text=str(directory),
                meta_text="",
            )
            self.index_label.configure(text="0 / 0")
            self.root.title(APP_NAME)
            self.canvas.configure(cursor="arrow")
            self._show_overlay(
                "这里没有图片",
                f"{directory}\n\n"
                "没有找到支持的图片文件。请换一个文件夹，"
                "或确认其中包含 JPG、PNG、WebP 等格式的图片。",
                primary=("换一个文件夹", self.open_folder_dialog),
            )
            return

        if new_index >= 0:
            target_index = new_index
        elif old_index >= 0:
            target_index = min(old_index, len(images) - 1)
        else:
            target_index = 0
        self.navigate_to(target_index)

    def _handle_scan_ready(
        self,
        token: int,
        directory: Path,
        images: list[Path],
        directories: list[Path],
        errors: list[str],
    ) -> None:
        if self._closed or token != self._scan_token:
            return

        self.images = images
        self.folder_groups = build_folder_groups(images)
        self.folder_directories = directories
        self._populate_sidebar()
        self._start_watcher(token, directory, images, directories)
        initial = self._pending_initial_path
        self._pending_initial_path = None

        if not images:
            self.index = -1
            self._set_toolbar_state()
            detail = ""
            if errors:
                detail = f"\n\n有 {len(errors)} 个位置无法访问，已自动跳过。"
            self._update_labels(
                file_text="没有找到图片",
                folder_text=str(directory),
                meta_text="",
            )
            self.index_label.configure(text="0 / 0")
            self._show_overlay(
                "这里没有图片",
                f"{directory}\n\n"
                "没有找到支持的图片文件。请换一个文件夹，"
                "或确认其中包含 JPG、PNG、WebP 等格式的图片。" + detail,
                primary=("换一个文件夹", self.open_folder_dialog),
            )
            return

        start_index = 0
        if initial is not None:
            try:
                target = initial.resolve()
            except OSError:
                target = initial
            for position, image in enumerate(images):
                if os.path.normcase(str(image)) == os.path.normcase(str(target)):
                    start_index = position
                    break

        if errors:
            self.root.title(APP_NAME)
        self.navigate_to(start_index)

    def _handle_scan_failed(
        self, token: int, directory: Path, message: str
    ) -> None:
        if self._closed or token != self._scan_token:
            return
        self._stop_watcher()
        self.images = []
        self.folder_groups = []
        self.folder_directories = []
        self.index = -1
        self._set_toolbar_state()
        self._clear_sidebar("扫描失败", "无法读取此文件夹")
        self._update_labels(
            file_text="扫描失败",
            folder_text=str(directory),
            meta_text="",
        )
        self._show_overlay(
            "无法扫描这个文件夹",
            f"{directory}\n\n{message}",
            primary=("换一个文件夹", self.open_folder_dialog),
        )

    # -- navigation -------------------------------------------------------

    def navigate_relative(self, delta: int) -> str:
        if not self.images:
            return "break"
        if self.index < 0:
            new_index = 0
        else:
            new_index = (self.index + delta) % len(self.images)
        if new_index != self.index:
            self.navigate_to(new_index)
        return "break"

    def navigate_to(self, index: int) -> str:
        if not self.images:
            return "break"
        index = max(0, min(index, len(self.images) - 1))
        if index == self.index:
            return "break"

        self.index = index
        self._load_token += 1
        token = self._load_token
        path = self.images[index]

        self._update_labels(
            file_text=path.name,
            folder_text=self._folder_display(path),
            meta_text="正在载入…",
        )
        self.index_label.configure(text=f"{index + 1} / {len(self.images)}")
        self.zoom_label.configure(text="…")
        self.root.title(f"{path.name} — {APP_NAME}")
        self._set_toolbar_state()
        self._sync_sidebar_selection()

        self._hide_overlay()
        if self.source is not None:
            self._show_loading_pill(f"正在载入  {path.name}")
        else:
            self._show_overlay(
                "正在载入图片",
                f"{path.name}\n\n{self._folder_display(path)}",
            )

        self._loader.request(token, path)
        return "break"

    def jump_folder(self, direction: int) -> str:
        if len(self.folder_groups) <= 1:
            return "break"
        current_group = 0
        for position, (_folder, start, end) in enumerate(self.folder_groups):
            if start <= self.index < end:
                current_group = position
                break
        destination = (current_group + direction) % len(self.folder_groups)
        _folder, start, _end = self.folder_groups[destination]
        if start != self.index:
            self.navigate_to(start)
        return "break"

    def _jump_folder_key(self, direction: int) -> str:
        self.jump_folder(direction)
        return "break"

    def _on_arrow(self, event: tk.Event, step: int) -> str:
        ctrl_pressed = bool(event.state & 0x0004)
        if ctrl_pressed:
            return self._jump_folder_key(step)
        return self.navigate_relative(step)

    def _folder_display(self, path: Path) -> str:
        if self.root_path is None:
            return str(path.parent)
        root_name = self.root_path.name or str(self.root_path)
        try:
            relative_parent = path.parent.relative_to(self.root_path)
        except ValueError:
            return str(path.parent)
        if str(relative_parent) == ".":
            return root_name
        return f"{root_name} / {relative_parent.as_posix()}"

    # -- asynchronous loading events -------------------------------------

    def _poll_events(self) -> None:
        if self._closed:
            return
        try:
            while True:
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break

                kind = event[0]
                if kind == "scan_ready":
                    self._handle_scan_ready(
                        event[1], event[2], event[3], event[4], event[5]
                    )
                elif kind == "scan_failed":
                    self._handle_scan_failed(event[1], event[2], event[3])
                elif kind == "image_ready":
                    self._handle_image_ready(
                        event[1], event[2], event[3], event[4]
                    )
                elif kind == "image_failed":
                    self._handle_image_failed(event[1], event[2], event[3])
                elif kind == "watch_changed":
                    self._handle_watch_changed(
                        event[1], event[2], event[3], event[4]
                    )
        finally:
            if not self._closed:
                self.root.after(POLL_MS, self._poll_events)

    def _handle_image_ready(
        self, token: int, path: Path, image: Image.Image, metadata: dict
    ) -> None:
        if self._closed or token != self._load_token:
            return

        self.source = image
        self.metadata = metadata
        self.fit_mode = True
        self._hide_loading_pill()
        self._hide_overlay()
        self.zoom_label.configure(text="…")
        self.meta_label.configure(
            text=(
                f"{metadata['format']}  ·  {metadata['width']} × {metadata['height']}  ·  "
                f"{format_file_size(metadata['bytes'])}"
            )
        )
        self.canvas.configure(cursor="fleur")
        try:
            self.canvas.focus_set()
        except tk.TclError:
            pass
        self._set_toolbar_state()
        self._fit_to_window_now()
        self._sync_sticky_to_current_image()

    def _handle_image_failed(self, token: int, path: Path, message: str) -> None:
        if self._closed or token != self._load_token:
            return

        self.source = None
        self.metadata = None
        self._clear_canvas_image()
        self._hide_loading_pill()
        self._hide_sticky_folder()
        self.zoom_label.configure(text="—")
        self.meta_label.configure(text="无法载入")
        self.canvas.configure(cursor="arrow")
        self._set_toolbar_state()
        self._update_labels(
            file_text=path.name,
            folder_text=self._folder_display(path),
            meta_text="无法载入",
        )
        self.root.title(f"{path.name} — {APP_NAME}")
        self._show_overlay(
            "这张图片打不开",
            f"{path.name}\n\n{message}\n\n可以继续用方向键浏览其他图片。",
            primary=("重试", lambda: self._retry_current()),
            secondary=("打开其他文件夹", self.open_folder_dialog),
        )

    def _retry_current(self) -> None:
        if not self.images or self.index < 0:
            return
        path = self.images[self.index]
        self.source = None
        self.metadata = None
        self._clear_canvas_image()

        self._load_token += 1
        token = self._load_token
        self._hide_overlay()
        self._show_overlay(
            "正在载入图片",
            f"{path.name}\n\n{self._folder_display(path)}",
        )
        self._update_labels(
            file_text=path.name,
            folder_text=self._folder_display(path),
            meta_text="正在载入…",
        )
        self.zoom_label.configure(text="…")
        self.canvas.configure(cursor="arrow")
        self._set_toolbar_state()
        self._loader.request(token, path)

    # -- toolbar state and labels ----------------------------------------

    def _set_button_enabled(self, button: tk.Button, enabled: bool) -> None:
        button.configure(
            state="normal" if enabled else "disabled",
            bg=button._base_bg,  # type: ignore[attr-defined]
            fg=button._base_fg,  # type: ignore[attr-defined]
        )

    def _set_toolbar_state(self) -> None:
        has_image = self.source is not None
        can_navigate = len(self.images) > 1
        has_any = len(self.images) > 0

        for button in (self.prev_button, self.next_button):
            self._set_button_enabled(button, can_navigate)
        for button in (
            self.fit_button,
            self.actual_button,
            self.zoom_in_button,
            self.zoom_out_button,
        ):
            self._set_button_enabled(button, has_image)

        if not has_image and not has_any:
            self.index_label.configure(text="— / —")
            self.zoom_label.configure(text="—")
        elif not has_image:
            self.zoom_label.configure(text="—")

    def _update_labels(
        self, *, file_text: str, folder_text: str, meta_text: str | None = None
    ) -> None:
        self.file_label.configure(text=file_text)
        self.folder_label.configure(text=f"·  {folder_text}" if folder_text else "")
        if meta_text is not None:
            self.meta_label.configure(text=meta_text)

    # -- zoom, fit, and pan ----------------------------------------------

    def _fit_scale(self) -> float:
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        if self.source is None:
            return 1.0
        padding = min(FIT_PADDING, max(8, min(width, height) * 0.05))
        available_width = max(1, width - padding * 2)
        available_height = max(1, height - padding * 2)
        image_width, image_height = self.source.size
        scale = min(available_width / image_width, available_height / image_height, 1.0)
        return max(MIN_ZOOM, min(MAX_ZOOM, scale))

    def fit_to_window(self) -> str:
        if self.source is None:
            return "break"
        self.fit_mode = True
        self._fit_to_window_now()
        return "break"

    def _fit_to_window_now(self) -> None:
        if self.source is None:
            return
        self._fit_retry_job = None
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 2 or height <= 2:
            self._fit_retry_job = self.root.after(40, self._fit_to_window_now)
            return

        self.fit_mode = True
        self.zoom = self._fit_scale()
        image_width, image_height = self.source.size
        self.offset_x = (width - image_width * self.zoom) / 2
        self.offset_y = (height - image_height * self.zoom) / 2
        self._update_zoom_label()
        self._schedule_render()

    def set_actual_size(self) -> str:
        if self.source is None:
            return "break"
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        if not math.isclose(self.zoom, 1.0, rel_tol=1e-9):
            self._zoom_to(1.0, width / 2, height / 2)
        self.fit_mode = False
        self.zoom = 1.0
        self._clamp_offsets()
        self._update_zoom_label()
        self._schedule_render()
        return "break"

    def _zoom_to(self, new_zoom: float, anchor_x: float, anchor_y: float) -> None:
        if self.source is None:
            return
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, new_zoom))
        if math.isclose(new_zoom, self.zoom, rel_tol=1e-9):
            return

        image_x = (anchor_x - self.offset_x) / self.zoom
        image_y = (anchor_y - self.offset_y) / self.zoom
        self.zoom = new_zoom
        self.fit_mode = False
        self.offset_x = anchor_x - image_x * new_zoom
        self.offset_y = anchor_y - image_y * new_zoom
        self._clamp_offsets()
        self._update_zoom_label()
        self._schedule_render()

    def zoom_by(self, factor: float) -> None:
        if self.source is None:
            return
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        self._zoom_to(self.zoom * factor, width / 2, height / 2)

    def _update_zoom_label(self) -> None:
        if self.source is None:
            self.zoom_label.configure(text="—")
        else:
            self.zoom_label.configure(text=format_zoom(self.zoom))

    def _on_mousewheel(self, event: tk.Event) -> str:
        pointer_x = self.folder_tree.winfo_pointerx()
        pointer_y = self.folder_tree.winfo_pointery()
        list_x = self.folder_tree.winfo_rootx()
        list_y = self.folder_tree.winfo_rooty()
        if (
            list_x <= pointer_x < list_x + self.folder_tree.winfo_width()
            and list_y <= pointer_y < list_y + self.folder_tree.winfo_height()
        ):
            return self._on_sidebar_wheel(event)

        if self.source is None:
            return "break"
        delta = int(event.delta)
        if delta == 0:
            return "break"

        # Wheel events can be delivered to the focused widget on Windows, so
        # use the actual pointer position and only zoom over the image area.
        x = pointer_x - self.canvas.winfo_rootx()
        y = pointer_y - self.canvas.winfo_rooty()
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if not (0 <= x < width and 0 <= y < height):
            return "break"

        steps = delta if sys.platform == "darwin" else delta / 120.0
        factor = WHEEL_FACTOR**steps
        self._zoom_to(self.zoom * factor, x, y)
        return "break"

    def _on_linux_wheel(self, event: tk.Event, direction: int) -> str:
        if self.source is None:
            return "break"
        factor = WHEEL_FACTOR**direction
        self._zoom_to(self.zoom * factor, event.x, event.y)
        return "break"

    def _on_double_click(self, event: tk.Event) -> str:
        if self.source is None:
            return "break"
        self._dragging = False
        if self.fit_mode:
            self._zoom_to(1.0, event.x, event.y)
        else:
            self.fit_to_window()
        return "break"

    def _clamp_offsets(self) -> None:
        if self.source is None:
            return
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        display_width = self.source.width * self.zoom
        display_height = self.source.height * self.zoom

        if display_width <= width:
            self.offset_x = (width - display_width) / 2
        else:
            self.offset_x = min(0.0, max(width - display_width, self.offset_x))

        if display_height <= height:
            self.offset_y = (height - display_height) / 2
        else:
            self.offset_y = min(0.0, max(height - display_height, self.offset_y))

    def _on_drag_start(self, event: tk.Event) -> None:
        if self.source is None:
            return
        self.canvas.focus_set()
        self._dragging = True
        self._drag_origin = (event.x, event.y)
        self._drag_offset = (self.offset_x, self.offset_y)
        self.canvas.configure(cursor="fleur")

    def _on_drag_motion(self, event: tk.Event) -> None:
        if not self._dragging or self.source is None:
            return
        delta_x = event.x - self._drag_origin[0]
        delta_y = event.y - self._drag_origin[1]
        self.offset_x = self._drag_offset[0] + delta_x
        self.offset_y = self._drag_offset[1] + delta_y
        self._clamp_offsets()
        self._schedule_render()

    def _on_drag_end(self, _event: tk.Event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self.canvas.configure(cursor="fleur" if self.source is not None else "arrow")
        if self.source is not None:
            self._schedule_render()

    def _on_canvas_configure(self, event: tk.Event) -> None:
        new_size = (event.width, event.height)
        old_width, old_height = self._canvas_size
        self._canvas_size = new_size
        if self.source is None or event.width <= 2 or event.height <= 2:
            return

        if self.fit_mode:
            if self._configure_job is not None:
                try:
                    self.root.after_cancel(self._configure_job)
                except tk.TclError:
                    pass
            self._configure_job = self.root.after(40, self._refit_after_resize)
        else:
            if old_width > 2 and old_height > 2:
                self.offset_x += (event.width - old_width) / 2
                self.offset_y += (event.height - old_height) / 2
                self._clamp_offsets()
            self._schedule_render()

    def _refit_after_resize(self) -> None:
        self._configure_job = None
        if self.source is not None and self.fit_mode:
            self._fit_to_window_now()

    # -- rendering --------------------------------------------------------

    def _schedule_render(self) -> None:
        if self._render_job is None and not self._closed:
            self._render_job = self.root.after(8, self._render)

    def _render(self) -> None:
        self._render_job = None
        if self._closed or self.source is None:
            return

        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()
        if canvas_width <= 2 or canvas_height <= 2:
            return

        image_width, image_height = self.source.size
        scale = self.zoom
        if scale <= 0:
            return

        source_left = max(0, math.floor(-self.offset_x / scale))
        source_top = max(0, math.floor(-self.offset_y / scale))
        source_right = min(
            image_width, math.ceil((canvas_width - self.offset_x) / scale)
        )
        source_bottom = min(
            image_height, math.ceil((canvas_height - self.offset_y) / scale)
        )

        if source_right <= source_left or source_bottom <= source_top:
            self._clear_canvas_image()
            return

        target_width = max(1, round((source_right - source_left) * scale))
        target_height = max(1, round((source_bottom - source_top) * scale))

        region = self.source.crop(
            (source_left, source_top, source_right, source_bottom)
        )
        if region.size != (target_width, target_height):
            resampling = (
                Image.Resampling.LANCZOS
                if scale < 1.0
                else Image.Resampling.BICUBIC
            )
            region = region.resize(
                (target_width, target_height),
                resampling,
                reducing_gap=2.0 if scale < 1.0 else None,
            )

        self._photo = ImageTk.PhotoImage(region)
        draw_x = self.offset_x + source_left * scale
        draw_y = self.offset_y + source_top * scale

        if self._canvas_image is None:
            self._canvas_image = self.canvas.create_image(
                draw_x,
                draw_y,
                anchor="nw",
                image=self._photo,
            )
        else:
            self.canvas.coords(self._canvas_image, draw_x, draw_y)
            self.canvas.itemconfigure(self._canvas_image, image=self._photo)

    def _clear_canvas_image(self) -> None:
        if self._canvas_image is not None:
            try:
                self.canvas.delete(self._canvas_image)
            except tk.TclError:
                pass
            self._canvas_image = None
        self._photo = None

    # -- shutdown ---------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_watcher()
        self._loader.close()
        self.root.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="picture_viewer.py",
        description="一个轻量的文件夹图片查看器：方向键切换，滚轮缩放。",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="要打开的文件夹或图片路径（不填则显示欢迎页）",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    enable_high_dpi()
    root = tk.Tk()
    app = PictureViewerApp(root)

    if args.path:
        root.after(60, lambda: app.open_path(Path(args.path)))

    root.mainloop()


if __name__ == "__main__":
    main()
