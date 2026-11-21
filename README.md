# OpenShot AutoDub

AI dubbing plugin for [OpenShot Video Editor](https://github.com/OpenShot/openshot-qt).

Add a video, and AutoDub:

1. **Transcribes** the whole video up front with Whisper (faster-whisper). The text is split into
   sentences, each with a speaker (diarization) and a start/end timecode.
2. Shows an **editable list of sentences**: play any sentence in the OpenShot preview (▶), fix the
   text, timing or speaker, or delete noise lines (✕).
3. **Translates** each sentence (Argos Translate, offline). Translations are editable too.
4. **Dubs** each sentence with **CosyVoice3** voice cloning (▷ to listen). The speaker's own
   original line is used as the voice prompt, and a phrase that does not fit its time slot is
   regenerated faster.
5. **Places** all dubbed phrases on the OpenShot timeline (one track per speaker) and mixes the
   original audio. You can play the dub over the original or replace it, set original and dub
   volume, and duck the original only while a phrase plays. Then you export an MP4 as usual.

CosyVoice runs **inside the plugin**, in its own Python process: no ComfyUI server is required.
It reuses the `cosyvoice/` + `matcha/` code from
[ComfyUI_FL-CosyVoice3](https://github.com/filliptm/ComfyUI_FL-CosyVoice3) and the models it
downloads, so an existing ComfyUI setup can be reused as is. A running ComfyUI server is also
supported as an alternative engine.

Before the first use the plugin **checks your machine** (Python environments, packages, CUDA,
Whisper / CosyVoice models, language packs). It offers to download or install what is missing,
or shows manual instructions.

> Status: Windows-first (tested setup: Windows 11, NVIDIA GPU). The plugin itself is
> cross-platform Python/Qt; the helper scripts in `tools/windows` are Windows-only.

---

## Installation (Windows)

### 0. Requirements

| Component | Why |
|---|---|
| [MSYS2](https://www.msys2.org/) | Builds and runs OpenShot from source (libopenshot + PyQt6) |
| Windows Python 3.10–3.12 x64 ([python.org](https://www.python.org/downloads/windows/) or `winget install Python.Python.3.12`) | Used to create the plugin's worker environments |
| NVIDIA GPU + recent driver (recommended) | Whisper and CosyVoice on CUDA; CPU works but is slow |
| ~10 GB free disk | torch (CUDA), Whisper medium (~1.5 GB), CosyVoice3 (~4 GB) |
| git | to clone the repositories |

Recommended folder layout (the scripts assume `openshot-qt` is next to this folder):

```
C:\src\openshot-qt          <- the fork with the plugin loader (see below), or upstream openshot-qt
C:\src\openshot-autodub     <- this repository
```

Either clone the fork, which already contains the plugin loader:

```bat
git clone -b feature/plugin-loader https://github.com/shibisty/openshot-qt.git
```

or clone upstream `https://github.com/OpenShot/openshot-qt.git` (branch `develop`). In that case
`install.py` adds the loader for you (step 2).

### 1. Build libopenshot and run OpenShot from source

OpenShot does not have a plugin system, so AutoDub needs OpenShot running **from source**.
On Windows its native library (`libopenshot`) must be built with MSYS2.

1. Install MSYS2 and open the **MSYS2 UCRT64** shell (`C:\msys64\ucrt64.exe`).
   Do not use MINGW64: MSYS2 deprecated it in 2026 and it no longer ships PyQt.
2. Update MSYS2. Run this twice if the window closes after the first update:
   ```bash
   pacman -Syu
   ```
3. Build `libopenshot-audio` + `libopenshot` (branch `develop`, about 20–40 min the first time):
   ```bash
   bash /c/src/openshot-autodub/tools/windows/build-libopenshot-msys2.sh
   ```
   The script installs the toolchain, Qt6, PyQt6, FFmpeg and SWIG. It builds both libraries,
   installs them into `C:\msys64\ucrt64` and finishes with a line like
   `openshot 1.x | Qt 6.x | PyQt 6.x`. Run it again any time to update to the latest `develop`.

### 2. Install the plugin into openshot-qt

From any Python (stdlib only):

```bat
cd C:\src\openshot-autodub
python install.py
rem or:  python install.py --openshot-dir D:\path\to\openshot-qt
```

The installer changes three small things in openshot-qt and registers the plugin:

* copies `openshot_patch/plugins.py` to `openshot-qt/src/classes/plugins.py` (generic plugin loader);
* adds a 6-line hook to `src/windows/main_window.py` that calls the loader;
* adds one compatibility line to `src/qt_api.py`, because with PyQt6 ≥ 6.12
  `QEvent.LayoutRequest` and the like are only reachable as `QEvent.Type.*`;
* registers this folder in `%USERPROFILE%\.openshot_qt\plugins.json`.

With the fork, the loader is already committed: `install.py` reports `already` for these files
and only registers the plugin. Uninstall with `python install.py --uninstall`. Re-running
`install.py` is safe.

### 3. Start OpenShot

```bat
C:\src\openshot-autodub\tools\windows\run-openshot.cmd
```

Use `run-openshot.cmd --trace` to capture Python tracebacks into `openshot-qt\openshot-crash.txt`
if OpenShot closes unexpectedly. If openshot-qt is not next to this folder, set
`OPENSHOT_QT_DIR` first.

### 4. First run: environment check

Open **View → AutoDub View** (`Alt+Shift+4`). The panel immediately checks the environment and
shows the result at the top. Click **Настройка и проверка…** (Setup & check) to see every item:

| Check | Fix offered by the plugin | Manual alternative |
|---|---|---|
| Recognition Python | **Create environment**: `envs\asr` venv + `requirements-asr.txt` | `py -3.12 -m venv envs\asr` → `envs\asr\Scripts\python -m pip install -r requirements-asr.txt` |
| Recognition packages (faster-whisper, ctranslate2, …) | `pip install -r requirements-asr.txt` | same |
| CUDA for Whisper | `pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"` | NVIDIA driver + those wheels |
| Whisper model | **Download now** (otherwise downloaded on first transcription) | – |
| Argos package (e.g. en→ru) | **Download now** (otherwise installed on first translation) | – |
| CosyVoice Python (torch) | **Create environment**: `envs\tts` venv + torch CUDA + `requirements-tts.txt` (~5 GB) | point it at an existing ComfyUI venv, e.g. `C:\ComfyUI\venv\Scripts\python.exe` |
| CosyVoice code | **Download from GitHub** to `vendor\ComfyUI_FL-CosyVoice3` | point it at `ComfyUI\custom_nodes\ComfyUI_FL-CosyVoice3` |
| CosyVoice model | **Download** `FunAudioLLM/Fun-CosyVoice3-0.5B-2512` (~4 GB) into the models folder | point it at `ComfyUI\models\cosyvoice` |
| ComfyUI server (only if that engine is selected) | – | start ComfyUI with the FL CosyVoice3 nodes |

All paths and model choices are edited in the same dialog. They are stored in
`%USERPROFILE%\.openshot_qt\autodub.json`. An existing ComfyUI installation in `C:\ComfyUI` (venv,
node and models) is detected automatically.

The panel also re-checks before each action: **Распознать** (transcribe) needs the recognition
items, **Перевести** (translate) needs Argos, and **Озвучить** (dub) needs the CosyVoice items.
If something blocking is missing, the setup dialog opens with that group highlighted.

---

## Usage

1. Save the OpenShot project (dubbing data is stored next to it), import a video, open
   **View → AutoDub View**.
2. Select the video, keep source language **Авто** (auto) and speakers **авто** (auto) →
   **Распознать** (transcribe).
3. Check the sentences:
   * **▶** plays a sentence in the preview;
   * double-click edits time (`0:01.250 – 0:03.900`), speaker or text;
   * **✕** deletes a line;
   * the right-click menu offers: set start/end from the playhead, rename a speaker everywhere,
     use a line as the speaker's voice sample, merge with the next line.
4. Choose the target language → **Перевести всё** (translate all), or **⇄** per line. Edit
   translations as needed.
5. **Озвучить всё** (dub all), or **♪** per line. **▷** listens to a generated phrase.
6. Set the mix options → **Разместить дубляж на таймлайне** (place on timeline). One Ctrl+Z
   undoes the whole placement, and placing again replaces it.
7. Review → **Экспорт MP4…** (export).

Target languages are limited to what CosyVoice3 speaks: ru, en, es, de, fr, it, ja, ko, zh.

### Where data lives

| What | Where |
|---|---|
| Plugin settings | `%USERPROFILE%\.openshot_qt\autodub.json` |
| Per-video session (sentences, edits, translations) | `<project folder>\<project>_dubbing\<video>\session.json` |
| Extracted audio, voice samples, generated phrases | same folder: `audio16k.wav`, `refs\`, `dubs\` |
| Unsaved projects | `%USERPROFILE%\.openshot_qt\dubbing\` |
| Logs | `%USERPROFILE%\.openshot_qt\openshot-qt.log` (`[dub_worker]`, `[cosyvoice_tts]` lines) |

---

## Plugin loader API

The loader is generic: it is not tied to AutoDub, and any number of plugins can use it. It lives in
`openshot-qt/src/classes/plugins.py` (committed in the fork, or copied there by `install.py` from
`openshot_patch/plugins.py`).

### Discovery

Plugin folders are collected, in this order, with duplicates removed, from:

1. `~/.openshot_qt/plugins.json` (`%USERPROFILE%\.openshot_qt\plugins.json` on Windows):
   ```json
   { "paths": ["C:\\src\\openshot-autodub", "D:\\my-other-plugin"] }
   ```
2. the `OPENSHOT_PLUGIN_PATH` environment variable: folders separated by `os.pathsep`
   (`;` on Windows, `:` elsewhere).

### Manifest: `openshot_plugin.json`

Every plugin folder must contain this file:

```json
{
  "name": "AutoDub",
  "module": "openshot_autodub",
  "entry": "register",
  "version": "0.1.0",
  "description": "free text"
}
```

| Key | Required | Meaning |
|---|---|---|
| `module` | yes | Python module or package to import, relative to the plugin folder |
| `entry` | no (default `register`) | name of a callable in that module |
| `name` | no (default `module`) | key under which the plugin is stored in `classes.plugins.LOADED` |
| `version`, `description` | no | informational |

### Lifecycle

1. `MainWindow.__init__` calls `classes.plugins.load_plugins(self)`. By then the built-in docks,
   the menus (`menuView`, `menuWindow`, …), the timeline (`window.timeline`) and the preview thread
   (`window.preview_thread`) exist. The saved window/dock state has **not** been restored yet.
2. For each folder the loader puts the folder at the front of `sys.path`, imports `module` and
   calls `entry(main_window)`.
3. The return value is kept in `classes.plugins.LOADED[name]`. Keep a reference to your objects
   there or parent them to the main window.
4. Any exception during import or `entry()` is logged
   (`Plugins: failed to load …` in `openshot-qt.log`) and the plugin is skipped. OpenShot keeps
   starting.

There is no unload or hot-reload: plugins live until OpenShot quits. Use
`QApplication.instance().aboutToQuit` to stop background processes.

### Writing a plugin

```python
# my_plugin/__init__.py
def register(window):
    from qt_api import Qt, QDockWidget, QLabel, QAction
    dock = QDockWidget("My Plugin", window)
    dock.setObjectName("dockMyPlugin")   # unique and stable -> OpenShot saves/restores its position
    dock.setWidget(QLabel("Hello"))
    dock.hide()
    window.addDockWidget(Qt.RightDockWidgetArea, dock)  # shows up in View -> Docks automatically
    action = QAction("My Plugin", window)
    action.triggered.connect(lambda: (dock.show(), dock.raise_()))
    window.menuView.addAction(action)
    return dock
```

Guidelines:

* **Import Qt only via `qt_api`** (`from qt_api import QWidget, pyqtSignal, …`). OpenShot runs on
  PyQt6, PySide6 or PyQt5, and `qt_api` hides the differences, including the flat-enum aliases
  such as `Qt.UserRole` and `QEvent.Resize`.
* Give every dock a **stable `objectName`** and create it inside `entry()`. The dock state is
  restored right after the plugins load, so your dock keeps its position between sessions.
* Build heavy widgets **lazily**, for example on `dock.visibilityChanged(True)`, so start-up stays
  fast.
* Run heavy work (ML models, downloads) **outside the GUI thread**: in a `QThread`, or better a
  separate `QProcess` with its own Python environment, as AutoDub does.
* Use the public OpenShot objects that the built-in docks use:

| What | How |
|---|---|
| App, settings, project data | `from classes.app import get_app`; `get_app().get_settings()`; `get_app().project` |
| Files / clips / tracks | `from classes.query import File, Clip, Track` (`.filter()`, `.get()`, `.save()`, `.delete()`) |
| Import media | `window.files_model.add_files(path, quiet=True)` |
| Add a clip to the timeline | `window.timeline.addClip(file_id, QPointF(seconds, track), track)` |
| Undo grouping | set `get_app().updates.transaction_id = <uuid>` around your changes, then `None` |
| Listen to project changes | `get_app().updates.add_listener(obj)`; `obj.changed(action)` is then called |
| Preview control | `window.SeekSignal.emit(frame, True)`, `window.PlaySignal.emit()`, `window.PauseSignal.emit()`, `window.LoadFileSignal.emit(path or "")`; position: `window.preview_thread.position_changed(int)` |
| Logging | `from classes.logger import log` |

* Keep plugin settings in your own file (e.g. `~/.openshot_qt/<plugin>.json`).
  `get_app().get_settings().set()` only accepts keys declared in OpenShot's `_default.settings`.

---

## Repository layout

```
install.py                  patch/unpatch openshot-qt, register the plugin
openshot_plugin.json        plugin manifest (module + entry point)
openshot_patch/plugins.py   generic loader copied into openshot-qt
openshot_autodub/           the plugin (Qt panel, session model, timeline placement, checks)
  plugin.py                 dock + View menu entry
  panel.py                  the AutoDub panel
  doctor.py, setup_dialog.py  environment checks, downloads/installs, setup UI
  local_tts.py              keeps the CosyVoice worker process alive and feeds it phrases
  comfy_dubber.py           alternative engine: ComfyUI HTTP API
  session.py, timeline_placer.py, worker_process.py, config.py
workers/
  dub_worker.py             faster-whisper transcription, speaker clustering (CAM++), Argos translation
  cosyvoice_tts.py          CosyVoice3 synthesis server (JSON over stdin/stdout)
requirements-asr.txt        packages for the recognition/translation environment
requirements-tts.txt        packages for the CosyVoice environment (install torch first)
tools/windows/              build libopenshot with MSYS2, launch OpenShot, crash tracing
```

The workers run in separate Python environments, so heavy ML dependencies never get imported
into OpenShot itself. They talk to the plugin over stdout lines prefixed with `@@` (JSON).

## Troubleshooting

* **`No suitable Qt binding found … QtStateMachine`**: run
  `pacman -S mingw-w64-ucrt-x86_64-qt6-scxml` in the UCRT64 shell.
* **`ImportError: DLL load failed` for `openshot`**: start OpenShot via `run-openshot.cmd`. It
  puts `C:\msys64\ucrt64\bin` first on PATH, so other Qt or FFmpeg copies are not picked up.
* **Build errors about `int64_t` / `<cstdint>`**: you are using an older copy of the build script.
  The current one passes `-include cstdint`, which is needed with GCC 15.
* **FFmpeg API errors while building libopenshot**: install `mingw-w64-ucrt-x86_64-ffmpeg7.1`,
  export `FFMPEGDIR` to its prefix, delete `~/openshot-dev/libopenshot/build` and rebuild.
* **Dubbing is very slow**: check the "CUDA" rows in the setup dialog. Close ComfyUI while
  dubbing, so the model is not loaded twice on the GPU, or enable fp16.

## License

GPL-3.0-or-later, the same as OpenShot. CosyVoice code and models are downloaded from their
upstream projects and keep their own licenses.
