# HiLook Grid Viewer

A lightweight desktop viewer for HiLook / Hikvision NVRs (and standalone IP cameras).
Shows several channels at once in a grid, with live stream switching, digital zoom,
drag-to-rearrange, a featured (spotlight) layout, snapshots, and NVR instant playback —
all over plain RTSP, with no vendor plugins or cloud account.

Built with PySide6 (Qt) for the UI and OpenCV/FFmpeg for decoding. Each tile runs its
own decode thread, so one flaky camera never takes down the rest.

---

## Features

- **Multi-camera grid** — view any set of channels at once (auto-arranged into a near-square grid).
- **Main / Sub stream toggle** — switch every tile between full-res (main) and low-res (sub) live, without reconnecting.
- **Featured layout** — double-click a tile to blow it up big with the others stacked small alongside.
- **Drag to rearrange** — drag one tile onto another to swap their positions.
- **Per-tile digital zoom & pan** — scroll to zoom (centered on the cursor), drag to pan, right-click to reset.
- **NVR instant playback** — jump back 30 s / 1 / 3 / 5 / 10 / 15 min on one tile or all of them, or pick a **custom date/time range**.
- **Snapshots** — save the current frame of every live tile to a folder.
- **Encrypted credentials** — the password is stored encrypted at rest (see [Where settings live](#where-settings-are-stored)).
- **Compact UI** — connection details live in a dialog; everything else is on a single toolbar. Fullscreen and a controls-hide toggle let you collapse to pure video.

---

## Requirements

- **Python 3.10 or newer** (the code uses `X | None` type syntax from PEP 604).
- A desktop session to display the window: X11 or Wayland on Linux, or a VNC session with a desktop. (Developed and tested on **Ubuntu 22.04**; it should also run on macOS and Windows with minor path differences.)
- A **HiLook / Hikvision DVR/NVR or IP camera** with **RTSP enabled** (default port `554`).
- For playback: the **DVR must be recording** to disk, and its **clock must be correct** (see [Troubleshooting](#troubleshooting)).

> FFmpeg is bundled inside the `opencv-python` wheel, so you do **not** need to install FFmpeg separately for the app to decode RTSP. (The `ffprobe`/`ffmpeg` command-line tools are only useful for debugging — see Troubleshooting.)

---

## Installation

```bash
# (optional but recommended) create a virtual environment
python3 -m venv venv
source venv/bin/activate            # on Windows: venv\Scripts\activate

# install the dependencies
pip install PySide6 opencv-python numpy cryptography
```

| Package        | Why it's needed                                              |
|----------------|--------------------------------------------------------------|
| `PySide6`      | The Qt GUI framework (window, widgets, toolbar, dialogs).    |
| `opencv-python`| Decodes the RTSP streams (bundles FFmpeg).                   |
| `numpy`        | Frame buffers (required by OpenCV).                          |
| `cryptography` | Encrypts the saved password at rest.                         |

If you skip `cryptography`, the app still runs but won't save the password (it shows a
reminder in the status bar) — install it to get encrypted credential storage.

---

## Running

```bash
python3 hilook_grid_viewer.py
```

(Rename the script to whatever you like; the filename doesn't matter.)

---

## Usage

### 1. Connect

1. Click **Settings…** on the toolbar (or **Camera → Connection settings…**, `Ctrl+E`).
2. Fill in your DVR's **IP**, **Port** (usually `554`), **User**, **Password**, and **Channels**.
3. Click **Connect**. (Settings are saved, so next time you can just hit **Connect All** on the toolbar.)

**Channels syntax** — the order you type also sets the initial tile order:

| You type     | You get                |
|--------------|------------------------|
| `1-4`        | channels 1, 2, 3, 4    |
| `1,3,5`      | channels 1, 3, 5       |
| `2,1,3`      | channels 2, 1, 3 (in that order) |
| `1-2,5-6`    | channels 1, 2, 5, 6    |

Up to **16 tiles** are shown at once.

**Stream** — leave on **Sub** for the grid (low-res, cheap to decode). Switch to **Main**
for full resolution; the dropdown switches every tile live.

### 2. Grid interactions

| Action | Result |
|--------|--------|
| **Click** a tile | Selects it (blue border) — the target for the playback buttons |
| **Double-click** a tile | Features it (big, others small); double-click again to restore the grid |
| **Drag** one tile onto another | Swaps their positions (at 1× zoom) |
| **Scroll wheel** over a tile | Digital zoom in/out, centered on the cursor (1×–8×) |
| **Drag** a zoomed tile | Pans the zoomed image |
| **Right-click** a tile | Resets that tile's zoom to fit |

### 3. Playback (recorded footage from the DVR)

1. **Click a tile** to select it, **or** tick **All tiles** to act on the whole grid.
2. Click a preset — **-30s, -1m, -3m, -5m, -10m, -15m** — to play recorded footage from that long ago, playing forward. The tile's status shows e.g. `⏮ -5m`.
3. Click **● Live** to return to the live stream.

For a specific moment in the past, click **Range…**, set a **From** date/time (and
optionally a **To** to stop at), and hit **Play**.

> Playback streams **forward** from the start time — it's a jump-to, not a scrubber.
> It requires the DVR to be recording, and uses the **DVR's local wall-clock time**.

### 4. Snapshots

Click **Snapshot** (or `Ctrl+S`) and choose a folder. The current frame of every live
tile is saved as `snapshot_<timestamp>_ch<N>.png`.

### Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+E` | Open connection settings |
| `Ctrl+S` | Snapshot |
| `F11`    | Fullscreen (hides menu + toolbar for pure video) |
| `Ctrl+H` | Hide / show the toolbar |
| `Ctrl+Q` | Quit |

---

## How it works

The viewer builds standard Hikvision RTSP URLs and decodes them with OpenCV's FFmpeg
backend over TCP (more reliable than UDP on Wi-Fi), with a 1-frame buffer to keep
latency low and automatic reconnect-with-backoff.

**Live stream**

```
rtsp://<user>:<pass>@<ip>:<port>/Streaming/Channels/<code>
```

**Playback**

```
rtsp://<user>:<pass>@<ip>:<port>/Streaming/tracks/<code>?starttime=<YYYYMMDDThhmmssZ>[&endtime=...]
```

where `code = channel * 100 + stream` — so channel 1 main = `101`, channel 1 sub = `102`,
channel 3 main = `301`, etc.

---

## Where settings are stored

Settings persist via Qt's `QSettings`. On Linux that's:

```
~/.config/HiLookViewer/GridViewer.conf
```

The file holds the IP, port, user, channels, and stream choice in plain text, plus the
password as an **encrypted token** (`password_enc`) — never in the clear.

The password is encrypted with [Fernet](https://cryptography.io/en/latest/fernet/) under
a key derived from this machine's ID + your username. This deliberately:

- keeps the password out of the config file as readable text, and
- makes the stored token **non-portable** — copy the config to another machine or user
  and it won't decrypt (the password field just comes up blank; re-enter it once).

**Security note:** because the key is machine-derived, this protects against *casual*
exposure (synced/backed-up dotfiles, screenshots, a glance at the file). It does **not**
protect against someone who can already run code as you on this machine. A master
password prompted at launch would be the stronger, service-free option.

This app intentionally does **not** use the OS keyring / Secret Service, because that
path talks to a D-Bus daemon that is slow or locked under VNC and was blocking startup.
Local encryption is pure CPU (~milliseconds) and can't hang.

---

## Troubleshooting

**A tile shows `open failed — retry in Ns`**
The stream couldn't be opened.
- *Live:* check the IP / port / user / password, confirm RTSP is enabled on the DVR, and that the box is reachable on the network.
- *Playback:* the requested time has no recording, or the time is wrong (see below).

Test a URL directly to isolate the cause (FFmpeg avoids an old auth quirk that affects VLC):

```bash
ffprobe -rtsp_transport tcp -i "rtsp://USER:PASS@DVRIP:554/Streaming/tracks/101?starttime=20260624T072700Z" 2>&1 | tail -20
```

If that prints stream info (`Video: h264 …`), the path and credentials are fine.

**Playback fails or shows the wrong time**
Playback uses the **DVR's local wall-clock time**, derived from your PC's clock.
- Make sure the **DVR's own clock and date are correct** (enable NTP on the DVR).
- Make sure your **PC's timezone matches the DVR's** so "5 minutes ago" lines up. If your PC runs in UTC but the DVR is on local time, the presets will ask for footage in the future. Fix with `sudo timedatectl set-timezone America/Costa_Rica` (or your zone), or use the **Custom range** with an explicit time read off the DVR's on-screen clock.
- Some firmware want UTC or a dashed timestamp instead of the compact local one — there's a commented alternative line in `CameraConfig.rtsp_url()` if needed.

**High CPU usage / dropped frames**
Decoding several **main** streams at once is heavy. Keep the grid on **Sub** and only
switch a tile to **Main** when you need to inspect detail.

**Portrait cameras look stretched**
Tiles fill their cell (aspect ratio ignored). To letterbox instead, change the single
`Qt.IgnoreAspectRatio` to `Qt.KeepAspectRatio` in `CameraCell._repaint()`.

**Password isn't remembered**
Either `cryptography` isn't installed (`pip install cryptography`), or the config was
created on a different machine/user (the encrypted token won't decrypt — re-enter once).

---

## Limitations

- Playback is jump-to presets / a range, not a free-scrub timeline.
- Up to 16 tiles at once.
- Digital zoom only (it magnifies the decoded frame); there's no PTZ/optical control.
- The saved-password encryption is machine-bound (see the security note above).

