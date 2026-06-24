#!/usr/bin/python3 python3
"""
HiLook / Hikvision Multi-Camera Grid Viewer
-------------------------------------------
View several channels of a HiLook/Hikvision NVR (or a single IP camera) at once.

Install:
    pip install PySide6 opencv-python numpy

RTSP URL format (Hikvision / HiLook):
    rtsp://<user>:<pass>@<ip>:<port>/Streaming/Channels/<channel><stream>
        channel : 1..N (one per NVR camera; 1 for a standalone IP camera)
        stream  : 1 -> main stream (full res), 2 -> sub stream (low res / low latency)
    code = channel*100 + stream  ->  cam 1 main = 101, cam 1 sub = 102, cam 3 main = 301

Notes:
  * Each tile runs its own decode thread (RTSP-over-TCP, 1-frame buffer, auto-reconnect).
  * The grid DEFAULTS TO THE SUB STREAM — decoding many main streams at once is very
    CPU-heavy. Use the Main/Sub dropdown to live-switch every tile between the two.
  * Drag a tile onto another to swap their positions; the Channels field order also
    sets the initial tile order (e.g. "2,1,3").
  * Double-click a tile to "feature" it (big, with the others small alongside);
    double-click again to return to an equal grid.
  * Scroll wheel over a tile to digital-zoom (centered on the cursor); drag pans
    when zoomed, right-click resets to fit.
  * Playback: click a tile to select it (or tick "All tiles"), then a preset
    (-30s … -15m) plays recorded footage from the NVR; "Live" returns to realtime.
    Or set a Custom range (From / optional To) to jump to a specific past date/time.
    Requires the NVR to be recording; times use the DVR's local wall clock.
  * Channels field accepts ranges and lists: "1-4", "1,3,5", "1-2,5-6", "2,1,3".
"""

import os
import sys
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import cv2
import numpy as np
from PySide6.QtCore import Qt, QThread, Signal, QMutex, QMutexLocker, QSettings, QMimeData, QDateTime
from PySide6.QtGui import QImage, QPixmap, QAction, QDrag
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QLineEdit, QComboBox,
    QPushButton, QGridLayout, QVBoxLayout, QHBoxLayout, QFileDialog,
    QStatusBar, QSpinBox, QSizePolicy, QCheckBox, QDateTimeEdit,
    QDialog, QToolBar, QFormLayout, QDialogButtonBox,
)

MAX_TILES = 16  # 4x4 of sub streams is already a lot of simultaneous decode
CELL_MIME = "application/x-hilook-cell"  # drag-and-drop payload: a tile's channel number


# --------------------------------------------------------------------------- #
# Camera configuration + URL building
# --------------------------------------------------------------------------- #
@dataclass
class CameraConfig:
    ip: str
    user: str
    password: str
    port: int = 554
    channel: int = 1
    stream: int = 1                          # 1 = main, 2 = sub
    playback_start: datetime | None = None   # DVR-local; when set, build a playback URL
    playback_end: datetime | None = None     # DVR-local; optional end of a playback range

    def rtsp_url(self) -> str:
        user = quote(self.user, safe="")
        pw = quote(self.password, safe="")
        code = f"{self.channel * 100 + self.stream}"
        auth = f"{user}:{pw}@" if user else ""
        if self.playback_start is not None:
            # NVR playback: recorded footage from a start time, played forward (optionally
            # bounded by an end time, in which case the stream stops there). Times are the
            # DVR's LOCAL wall clock — many Hikvision/HiLook DVRs interpret these as
            # device-local and ignore the trailing Z. If your firmware wants true UTC pass
            # UTC datetimes; if it wants the dashed form use "%Y-%m-%dT%H:%M:%SZ".
            base = f"rtsp://{auth}{self.ip}:{self.port}/Streaming/tracks/{code}"
            url = f"{base}?starttime={self.playback_start.strftime('%Y%m%dT%H%M%SZ')}"
            if self.playback_end is not None:
                url += f"&endtime={self.playback_end.strftime('%Y%m%dT%H%M%SZ')}"
            return url
        return f"rtsp://{auth}{self.ip}:{self.port}/Streaming/Channels/{code}"

    def safe_url(self) -> str:
        if not self.user:
            return self.rtsp_url()
        return self.rtsp_url().replace(quote(self.password, safe=""), "****", 1)


def parse_channels(spec: str) -> list[int]:
    """Parse '2,1,3' -> [2,1,3] and '1-4' -> [1,2,3,4]. Preserves the order given
    (so the Channels field also sets the initial tile order), dedups, keeps >= 1."""
    out: list[int] = []
    seen: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        nums: list[int] = []
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit():
                lo, hi = int(a), int(b)
                step = 1 if lo <= hi else -1
                nums = list(range(lo, hi + step, step))
        elif part.isdigit():
            nums = [int(part)]
        for c in nums:
            if c >= 1 and c not in seen:
                seen.add(c)
                out.append(c)
    return out


# --------------------------------------------------------------------------- #
# Decode worker thread (one per tile)
# --------------------------------------------------------------------------- #
class VideoThread(QThread):
    frame_ready = Signal(QImage)
    status = Signal(str)
    fps_update = Signal(float)

    def __init__(self, config: CameraConfig):
        super().__init__()
        self.config = config
        self._running = True
        self._switch = False          # set when a live stream/channel change is requested
        self._latest_bgr: np.ndarray | None = None
        self._lock = QMutex()         # guards _latest_bgr
        self._cfg_lock = QMutex()     # guards self.config during a live switch

    def run(self) -> None:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"
        backoff = 1.0

        while self._running:
            with QMutexLocker(self._cfg_lock):
                url = self.config.rtsp_url()
                safe = self.config.safe_url()
                self._switch = False

            self.status.emit(f"Connecting to {safe} …")
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass

            if not cap.isOpened():
                self.status.emit(f"open failed — retry in {backoff:.0f}s")
                cap.release()
                self._sleep(backoff)
                backoff = min(backoff * 2, 10.0)
                continue

            self.status.emit("Connected — streaming")
            backoff = 1.0
            frames, t0 = 0, time.monotonic()

            while self._running and not self._switch:
                ok, frame = cap.read()
                if not ok or frame is None:
                    self.status.emit("stalled — reconnecting …")
                    break

                with QMutexLocker(self._lock):
                    self._latest_bgr = frame

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb.shape
                img = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888).copy()
                self.frame_ready.emit(img)

                frames += 1
                dt = time.monotonic() - t0
                if dt >= 1.0:
                    self.fps_update.emit(frames / dt)
                    frames, t0 = 0, time.monotonic()

            cap.release()

            if self._switch:
                self.status.emit("switching …")
                continue
            if self._running:
                self._sleep(backoff)
                backoff = min(backoff * 2, 10.0)

        self.status.emit("stopped")

    def switch(self, config: CameraConfig) -> None:
        """Point the worker at a new stream/channel without restarting the thread."""
        with QMutexLocker(self._cfg_lock):
            self.config = config
        self._switch = True

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while self._running and not self._switch and time.monotonic() < end:
            time.sleep(0.05)

    def snapshot(self) -> np.ndarray | None:
        with QMutexLocker(self._lock):
            return None if self._latest_bgr is None else self._latest_bgr.copy()

    def stop(self) -> None:
        self._running = False
        self.wait(3000)


# --------------------------------------------------------------------------- #
# A single grid tile: a video label + a compact status line + its own thread
# --------------------------------------------------------------------------- #
class CameraCell(QWidget):
    doubleClicked = Signal(object)        # emits self (toggle "featured")
    requestMove = Signal(int, object)     # (source channel, target cell) for drag-reorder
    selected = Signal(object)             # emits self on click (sets playback target)

    def __init__(self, channel: int):
        super().__init__()
        self.channel = channel
        self.thread: VideoThread | None = None
        self._pixmap: QPixmap | None = None
        self._press_pos = None
        self._pan_last = None
        self._zoom = 1.0
        self._pan_cx = 0.5   # normalized center of the visible region (0..1 of source)
        self._pan_cy = 0.5
        self._playback_start: datetime | None = None
        self._playback_end: datetime | None = None
        self._playback_label = ""
        self.setAcceptDrops(True)

        self.status = QLabel(f"Ch {channel} — idle")
        self.status.setStyleSheet("color:#bbb; font-size:11px; padding:1px 4px; background:#161616;")
        # Don't let a long status string force the column wider, and let clicks fall through.
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.status.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self.video = QLabel(f"Ch {channel}")
        self.video.setAlignment(Qt.AlignCenter)
        self.video.setMinimumSize(160, 120)
        self.video.setStyleSheet("background:#0b0b0b; border:2px solid #2a2a2a; color:#777;")
        self.video.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.status)
        lay.addWidget(self.video, stretch=1)

    def set_selected(self, on: bool) -> None:
        color = "#4da3ff" if on else "#2a2a2a"
        self.video.setStyleSheet(f"background:#0b0b0b; border:2px solid {color}; color:#777;")

    def start(self, config: CameraConfig) -> None:
        self.stop()
        self.thread = VideoThread(config)
        self.thread.frame_ready.connect(self._on_frame)
        self.thread.status.connect(self._on_status)
        self.thread.fps_update.connect(self._on_fps)
        self.thread.start()

    def switch(self, config: CameraConfig) -> None:
        if self.thread is not None:
            self.thread.switch(config)

    def stop(self) -> None:
        if self.thread is not None:
            self.thread.stop()
            self.thread = None
        self._pixmap = None
        self._zoom = 1.0
        self._pan_cx = self._pan_cy = 0.5
        self._playback_start = None
        self._playback_end = None
        self._playback_label = ""
        self.video.setText(f"Ch {self.channel}")

    def snapshot(self) -> np.ndarray | None:
        return self.thread.snapshot() if self.thread else None

    def _on_frame(self, img: QImage) -> None:
        self._pixmap = QPixmap.fromImage(img)
        self._repaint()

    def _repaint(self) -> None:
        if self._pixmap is None:
            return
        pm = self._pixmap
        if self._zoom <= 1.0:
            view = pm
        else:
            W, H = pm.width(), pm.height()
            vw, vh = W / self._zoom, H / self._zoom
            left = min(max(self._pan_cx * W - vw / 2, 0), W - vw)
            top = min(max(self._pan_cy * H - vh / 2, 0), H - vh)
            view = pm.copy(int(round(left)), int(round(top)), int(round(vw)), int(round(vh)))
        self.video.setPixmap(
            view.scaled(self.video.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._repaint()

    def _on_status(self, msg: str) -> None:
        if msg.startswith("Connecting"):
            msg = "connecting …"
        self.status.setText(f"Ch {self.channel} — {msg}")

    def _on_fps(self, fps: float) -> None:
        extras = []
        if self._playback_label:
            extras.append(f"⏮ {self._playback_label}")
        if self._zoom > 1.0:
            extras.append(f"{self._zoom:.1f}x")
        tail = (" • " + " • ".join(extras)) if extras else ""
        self.status.setText(f"Ch {self.channel} — {fps:.0f} fps{tail}")

    # --- drag-and-drop reordering ---
    # --- mouse: pan when zoomed, reorder at 1x, right-click resets to fit ---
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
            self._pan_last = event.position().toPoint()
            self.selected.emit(self)
        elif event.button() == Qt.RightButton:
            self._zoom = 1.0
            self._pan_cx = self._pan_cy = 0.5
            self._repaint()
            self.status.setText(f"Ch {self.channel} — fit")
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if not (event.buttons() & Qt.LeftButton) or self._press_pos is None:
            return

        # Zoomed in: left-drag pans the image (grab-and-drag).
        if self._zoom > 1.0:
            cur = event.position().toPoint()
            if self._pan_last is not None:
                dx = cur.x() - self._pan_last.x()
                dy = cur.y() - self._pan_last.y()
                lw = max(self.video.width(), 1)
                lh = max(self.video.height(), 1)
                self._pan_cx -= (dx / lw) / self._zoom
                self._pan_cy -= (dy / lh) / self._zoom
                self._clamp_pan()
                self._repaint()
            self._pan_last = cur
            return

        # At 1x: left-drag starts a tile reorder.
        moved = (event.position().toPoint() - self._press_pos).manhattanLength()
        if moved < QApplication.startDragDistance():
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(CELL_MIME, str(self.channel).encode())
        drag.setMimeData(mime)
        if self._pixmap is not None:  # use the live frame as the drag cursor
            drag.setPixmap(self._pixmap.scaled(160, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        drag.exec(Qt.MoveAction)

    # --- wheel: digital zoom, centered on the cursor ---
    def wheelEvent(self, event) -> None:
        if self._pixmap is None:
            return
        lw, lh = self.video.width(), self.video.height()
        if lw <= 0 or lh <= 0:
            return
        # cursor position within the video label (status line sits above it)
        pos = event.position().toPoint() - self.video.geometry().topLeft()
        fx = min(max(pos.x() / lw, 0.0), 1.0)
        fy = min(max(pos.y() / lh, 0.0), 1.0)

        W, H = self._pixmap.width(), self._pixmap.height()
        old_z = self._zoom
        vw, vh = W / old_z, H / old_z
        left = min(max(self._pan_cx * W - vw / 2, 0), W - vw)
        top = min(max(self._pan_cy * H - vh / 2, 0), H - vh)
        ix, iy = left + fx * vw, top + fy * vh  # image point under the cursor

        step = 1.25 if event.angleDelta().y() > 0 else 1.0 / 1.25
        self._zoom = min(max(old_z * step, 1.0), 8.0)

        if self._zoom <= 1.0:
            self._pan_cx = self._pan_cy = 0.5
        else:
            nvw, nvh = W / self._zoom, H / self._zoom
            self._pan_cx = (ix - fx * nvw + nvw / 2) / W   # keep cursor point fixed
            self._pan_cy = (iy - fy * nvh + nvh / 2) / H
            self._clamp_pan()

        self._repaint()
        self.status.setText(f"Ch {self.channel} — {self._zoom:.1f}x")
        event.accept()

    def _clamp_pan(self) -> None:
        if self._zoom <= 1.0:
            self._pan_cx = self._pan_cy = 0.5
            return
        m = 0.5 / self._zoom
        self._pan_cx = min(max(self._pan_cx, m), 1.0 - m)
        self._pan_cy = min(max(self._pan_cy, m), 1.0 - m)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(CELL_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat(CELL_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        data = event.mimeData().data(CELL_MIME)
        if not data:
            return
        try:
            src_channel = int(bytes(data).decode())
        except ValueError:
            return
        self.requestMove.emit(src_channel, self)
        event.acceptProposedAction()

    # --- double-click toggles the featured (big) tile ---
    def mouseDoubleClickEvent(self, event) -> None:
        self.doubleClicked.emit(self)
        super().mouseDoubleClickEvent(event)


# --------------------------------------------------------------------------- #
# Dialogs — set-once / occasional controls live here to keep the window compact
# --------------------------------------------------------------------------- #
class ConnectionDialog(QDialog):
    """Hosts the set-once connection fields. The widgets are created by MainWindow
    and reparented here, so the rest of the code keeps reading them directly."""
    def __init__(self, ip_edit, port_spin, user_edit, pass_edit, channels_edit, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connection settings")
        self.setModal(True)
        form = QFormLayout()
        form.addRow("IP", ip_edit)
        form.addRow("Port", port_spin)
        form.addRow("User", user_edit)
        form.addRow("Password", pass_edit)
        form.addRow("Channels", channels_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Connect")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)


class RangeDialog(QDialog):
    """Pick a custom playback start (and optional end). Defaults to ~1 h ago."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Custom playback range")
        self.setModal(True)
        self.from_dt = QDateTimeEdit(QDateTime.currentDateTime().addSecs(-3600))
        self.from_dt.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.from_dt.setCalendarPopup(True)
        self.to_dt = QDateTimeEdit(QDateTime.currentDateTime())
        self.to_dt.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.to_dt.setCalendarPopup(True)
        self.to_dt.setEnabled(False)
        self.use_end = QCheckBox("Stop at end time")
        self.use_end.toggled.connect(self.to_dt.setEnabled)
        form = QFormLayout()
        form.addRow("From", self.from_dt)
        form.addRow("To", self.to_dt)
        form.addRow("", self.use_end)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Play")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)

    def values(self):
        start = self.from_dt.dateTime().toPython()
        end = self.to_dt.dateTime().toPython() if self.use_end.isChecked() else None
        return start, end


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HiLook Grid Viewer")
        self.resize(1200, 800)
        self.settings = QSettings("HiLookViewer", "GridViewer")
        self.cells: list[CameraCell] = []
        self._featured: CameraCell | None = None
        self._selected: CameraCell | None = None

        # Connection fields live inside ConnectionDialog; the rest of the code keeps
        # reading these widgets directly, so persistence/connect logic is unchanged.
        self.ip_edit = QLineEdit("192.168.1.64")
        self.port_spin = QSpinBox(); self.port_spin.setRange(1, 65535); self.port_spin.setValue(554)
        self.user_edit = QLineEdit("admin")
        self.pass_edit = QLineEdit(); self.pass_edit.setEchoMode(QLineEdit.Password)
        self.channels_edit = QLineEdit("1-4"); self.channels_edit.setPlaceholderText("e.g. 1-4 or 1,3,5")
        self.conn_dialog = ConnectionDialog(
            self.ip_edit, self.port_spin, self.user_edit, self.pass_edit, self.channels_edit, self
        )

        # Live controls that stay on the toolbar.
        self.stream_combo = QComboBox(); self.stream_combo.addItems(["Main", "Sub"])
        self.stream_combo.currentIndexChanged.connect(self._on_stream_changed)
        self.connect_btn = QPushButton("Connect All")
        self.connect_btn.clicked.connect(self.toggle_all)
        self.snap_btn = QPushButton("Snapshot")
        self.snap_btn.clicked.connect(self.snapshot_all)
        self.snap_btn.setEnabled(False)
        self.all_tiles_chk = QCheckBox("All tiles")

        self._build_menu()
        self._build_toolbar()

        # Central widget is just the video grid — controls live on the toolbar/menu.
        self.grid_container = QWidget()
        self.grid = QGridLayout(self.grid_container)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(2)
        self.setCentralWidget(self.grid_container)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready — Connect All (edit details in Settings…)")

        self.load_settings()

    # --- window chrome (compact toolbar + menu; set-once fields are in dialogs) ---
    def _build_menu(self) -> None:
        mb = self.menuBar()
        cam = mb.addMenu("&Camera")
        a = cam.addAction("Connection settings…"); a.setShortcut("Ctrl+E")
        a.triggered.connect(self._open_connection)
        a = cam.addAction("Snapshot…"); a.setShortcut("Ctrl+S")
        a.triggered.connect(self.snapshot_all)
        cam.addSeparator()
        a = cam.addAction("Quit"); a.setShortcut("Ctrl+Q"); a.triggered.connect(self.close)

        view = mb.addMenu("&View")
        self.act_fs = view.addAction("Fullscreen"); self.act_fs.setCheckable(True)
        self.act_fs.setShortcut("F11"); self.act_fs.triggered.connect(self.toggle_fullscreen)
        self.act_hide = view.addAction("Hide controls"); self.act_hide.setCheckable(True)
        self.act_hide.setShortcut("Ctrl+H"); self.act_hide.toggled.connect(self._toggle_controls)
        self.addAction(self.act_fs)  # keep F11 working while the menu bar is hidden

    def _build_toolbar(self) -> None:
        tb = QToolBar("Controls"); tb.setMovable(False)
        self.addToolBar(tb)
        self.toolbar = tb
        tb.addWidget(self.connect_btn)
        settings_btn = QPushButton("Settings…"); settings_btn.clicked.connect(self._open_connection)
        tb.addWidget(settings_btn)
        tb.addSeparator()
        tb.addWidget(QLabel(" Stream ")); tb.addWidget(self.stream_combo)
        tb.addSeparator()
        tb.addWidget(self.snap_btn)
        tb.addSeparator()
        tb.addWidget(QLabel(" Playback "))
        live_btn = QPushButton("● Live"); live_btn.clicked.connect(self._go_live)
        tb.addWidget(live_btn)
        for secs in (30, 60, 180, 300, 600, 900):
            b = QPushButton("-" + self._fmt_offset(secs))
            b.clicked.connect(lambda checked=False, s=secs: self._rewind(s))
            tb.addWidget(b)
        tb.addWidget(self.all_tiles_chk)
        tb.addSeparator()
        range_btn = QPushButton("Range…"); range_btn.clicked.connect(self._open_range)
        tb.addWidget(range_btn)

    def _toggle_controls(self, hidden: bool) -> None:
        self.toolbar.setVisible(not hidden)

    def _open_connection(self) -> None:
        if self.conn_dialog.exec() == QDialog.Accepted:
            self.save_settings()
            if self.cells:
                self.disconnect_all()
            self.connect_all()

    # --- helpers ---
    def _base_config(self, channel: int, stream: int,
                     playback_start: datetime | None = None,
                     playback_end: datetime | None = None) -> CameraConfig:
        return CameraConfig(
            ip=self.ip_edit.text().strip(),
            user=self.user_edit.text().strip(),
            password=self.pass_edit.text(),
            port=self.port_spin.value(),
            channel=channel,
            stream=stream,
            playback_start=playback_start,
            playback_end=playback_end,
        )

    def _config_for(self, cell: "CameraCell") -> CameraConfig:
        return self._base_config(cell.channel, self._stream(),
                                 cell._playback_start, cell._playback_end)

    def _stream(self) -> int:
        return 1 if self.stream_combo.currentIndex() == 0 else 2

    # --- connect / disconnect ---
    def toggle_all(self) -> None:
        if self.cells:
            self.disconnect_all()
        else:
            self.connect_all()

    def connect_all(self) -> None:
        chans = parse_channels(self.channels_edit.text())
        if not chans:
            self.statusBar().showMessage("Enter channels, e.g. 1-4 or 1,3,5")
            return
        if len(chans) > MAX_TILES:
            self.statusBar().showMessage(f"Showing first {MAX_TILES} of {len(chans)} channels")
            chans = chans[:MAX_TILES]

        self.save_settings()
        self._featured = None
        self._selected = None

        self.cells = []
        for ch in chans:
            cell = CameraCell(ch)
            cell.doubleClicked.connect(self._toggle_featured)
            cell.requestMove.connect(self._move_cell)
            cell.selected.connect(self._select_cell)
            self.cells.append(cell)
        self._relayout()

        for cell in self.cells:
            cell.start(self._config_for(cell))

        self.connect_btn.setText("Disconnect All")
        self.snap_btn.setEnabled(True)
        self.statusBar().showMessage(
            f"Streaming {len(self.cells)} channel(s) — drag to rearrange, double-click to feature one"
        )

    def disconnect_all(self) -> None:
        for cell in self.cells:
            cell.stop()
        for cell in self.cells:
            self.grid.removeWidget(cell)
            cell.setParent(None)
            cell.deleteLater()
        self.cells = []
        self._featured = None
        self._selected = None
        self.connect_btn.setText("Connect All")
        self.snap_btn.setEnabled(False)
        self.statusBar().showMessage("Disconnected")

    # --- layout ---
    def _relayout(self) -> None:
        g = self.grid
        for cell in self.cells:
            g.removeWidget(cell)
        for i in range(MAX_TILES):
            g.setRowStretch(i, 0)
            g.setColumnStretch(i, 0)

        if not self.cells:
            return
        for cell in self.cells:
            cell.setVisible(True)

        # Featured layout: one big tile on the left, the rest stacked small on the
        # right (all still live). Falls back to an equal grid when nothing featured.
        if self._featured is not None and self._featured in self.cells and len(self.cells) > 1:
            smalls = [c for c in self.cells if c is not self._featured]
            rows = len(smalls)
            for r, small in enumerate(smalls):
                g.addWidget(small, r, 0)                 # smalls stacked on the left
            g.addWidget(self._featured, 0, 1, rows, 1)   # big tile on the right
            g.setColumnStretch(0, 1)
            g.setColumnStretch(1, 3)   # big tile ~75% of the width
            for r in range(rows):
                g.setRowStretch(r, 1)
            return

        # Equal near-square grid.
        n = len(self.cells)
        cols = max(1, math.ceil(math.sqrt(n)))
        rows = math.ceil(n / cols)
        for i, cell in enumerate(self.cells):
            r, c = divmod(i, cols)
            g.addWidget(cell, r, c)
        for c in range(cols):
            g.setColumnStretch(c, 1)
        for r in range(rows):
            g.setRowStretch(r, 1)

    def _toggle_featured(self, cell: CameraCell) -> None:
        self._featured = None if self._featured is cell else cell
        self._relayout()

    def _move_cell(self, src_channel: int, target_cell: CameraCell) -> None:
        # Swap the dragged tile with the one it was dropped on.
        src = next((c for c in self.cells if c.channel == src_channel), None)
        if src is None or src is target_cell:
            return
        i, j = self.cells.index(src), self.cells.index(target_cell)
        self.cells[i], self.cells[j] = self.cells[j], self.cells[i]
        self._relayout()

    # --- live stream switching (all tiles) ---
    def _on_stream_changed(self, *args) -> None:
        if not self.cells:
            return
        stream = self._stream()
        for cell in self.cells:
            cell.switch(self._config_for(cell))   # _config_for keeps each tile's playback state
        self.save_settings()
        self.statusBar().showMessage(f"Switched all tiles to {'Main' if stream == 1 else 'Sub'} stream")

    # --- tile selection + playback scope ---
    def _select_cell(self, cell: CameraCell) -> None:
        if self._selected is cell:
            return
        if self._selected is not None:
            self._selected.set_selected(False)
        self._selected = cell
        cell.set_selected(True)

    def _playback_targets(self) -> list[CameraCell]:
        if self.all_tiles_chk.isChecked():
            return list(self.cells)
        return [self._selected] if self._selected is not None else []

    @staticmethod
    def _fmt_offset(seconds: int) -> str:
        return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m"

    def _rewind(self, seconds: int) -> None:
        targets = self._playback_targets()
        if not targets:
            self.statusBar().showMessage("Click a tile to select it, or tick 'All tiles'")
            return
        start = datetime.now() - timedelta(seconds=seconds)   # DVR-local wall clock
        label = self._fmt_offset(seconds)
        for cell in targets:
            cell._playback_start = start
            cell._playback_end = None
            cell._playback_label = label
            cell.switch(self._config_for(cell))
        self.statusBar().showMessage(f"Playback -{label} on {len(targets)} tile(s)")

    def _go_live(self) -> None:
        targets = self._playback_targets()
        if not targets:
            self.statusBar().showMessage("Click a tile to select it, or tick 'All tiles'")
            return
        for cell in targets:
            cell._playback_start = None
            cell._playback_end = None
            cell._playback_label = ""
            cell.switch(self._config_for(cell))
        self.statusBar().showMessage(f"Live on {len(targets)} tile(s)")

    def _open_range(self) -> None:
        targets = self._playback_targets()
        if not targets:
            self.statusBar().showMessage("Click a tile to select it, or tick 'All tiles'")
            return
        dlg = RangeDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        start, end = dlg.values()
        if end is not None and end <= start:
            self.statusBar().showMessage("End time must be after start time")
            return
        label = start.strftime("%m-%d %H:%M")
        for cell in targets:
            cell._playback_start = start
            cell._playback_end = end
            cell._playback_label = label
            cell.switch(self._config_for(cell))
        span = "all tiles" if self.all_tiles_chk.isChecked() else f"Ch {targets[0].channel}"
        self.statusBar().showMessage(f"Playback from {start:%Y-%m-%d %H:%M:%S} on {span}")

    # --- snapshot ---
    def snapshot_all(self) -> None:
        grabbed = [(c.channel, c.snapshot()) for c in self.cells]
        grabbed = [(ch, f) for ch, f in grabbed if f is not None]
        if not grabbed:
            self.statusBar().showMessage("No frames yet")
            return
        folder = QFileDialog.getExistingDirectory(self, "Save snapshots to folder")
        if not folder:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        for ch, frame in grabbed:
            cv2.imwrite(os.path.join(folder, f"snapshot_{ts}_ch{ch}.png"), frame)
        self.statusBar().showMessage(f"Saved {len(grabbed)} snapshot(s) to {folder}")

    # --- settings persistence (plaintext; ~/.config/HiLookViewer/GridViewer.conf) ---
    def load_settings(self) -> None:
        s = self.settings
        self.ip_edit.setText(s.value("ip", "192.168.1.64", type=str))
        self.port_spin.setValue(s.value("port", 554, type=int))
        self.user_edit.setText(s.value("user", "admin", type=str))
        self.pass_edit.setText(s.value("password", "", type=str))
        self.channels_edit.setText(s.value("channels", "1-4", type=str))
        self.stream_combo.setCurrentIndex(s.value("stream_index", 1, type=int))  # default Sub

    def save_settings(self) -> None:
        s = self.settings
        s.setValue("ip", self.ip_edit.text().strip())
        s.setValue("port", self.port_spin.value())
        s.setValue("user", self.user_edit.text().strip())
        s.setValue("password", self.pass_edit.text())
        s.setValue("channels", self.channels_edit.text().strip())
        s.setValue("stream_index", self.stream_combo.currentIndex())

    def toggle_fullscreen(self, *args) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.menuBar().setVisible(True)
            self.toolbar.setVisible(not self.act_hide.isChecked())
            self.act_fs.setChecked(False)
        else:
            self.showFullScreen()
            self.menuBar().setVisible(False)
            self.toolbar.setVisible(False)
            self.act_fs.setChecked(True)

    def closeEvent(self, event) -> None:
        self.save_settings()
        self.disconnect_all()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
