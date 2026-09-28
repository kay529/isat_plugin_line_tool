# ISAT Line Tool Plugin

A plugin for [**ISAT_with_segment_anything**](https://github.com/yatengLG/ISAT_with_segment_anything)
(image segmentation annotation tool).

**Drag a centerline, let go, get a fixed-width polygon.**

Hold the left button and drag along the spine of the thing you are labelling —
a fibre, a road, a tube, a membrane — then release. The plugin expands that
centerline into a polygon of the width you chose and drops it in as a normal
ISAT annotation (category, group, colour, list entry, dirty flag: all normal).

Same idea as ImageJ's *Segmented Line* with a width, or Label Studio's brush:
**one gesture, one annotation.** No "click to start, click again to stop"
dance.

---

## Why

Outlining long thin objects with ISAT's built-in polygon tool is tedious: either
you click a dozen times per segment trying to trace both edges, or you accept a
ragged, too-thin sliver. With this tool you trace the object **once** — down the
middle — and the width is handled for you. Change the width with a spinbox or
two keys, and it stays where you left it.

Typical uses: microscopy fibres / membranes / vessels, aerial and remote-sensing
roads and rivers, cracks, tubing, plumes, handwriting strokes.

---

## Install

The plugin lives outside ISAT's repo, so it survives ISAT updates and uninstalls
cleanly.

```bash
# 1. Get the code
git clone https://github.com/kay529/isat_plugin_line_tool.git
cd isat_plugin_line_tool

# 2. Install it into the SAME python environment ISAT runs in
pip install -e .
```

That is the whole install. `-e` (editable) means later `git pull`s take effect
immediately — no reinstall needed.

### Finding the right pip

Install into the environment ISAT itself uses, or ISAT will not see the plugin.
If you are unsure which python that is:

```bash
# Option A: ISAT installed as a regular package
python -c "import ISAT, os; print(os.path.dirname(ISAT.__file__))"

# Option B: the ISAT conda environment (adjust the name to yours)
conda activate isat_env
pip install -e .
```

On Windows with a portable / embedded Anaconda layout, call that environment's
python explicitly:

```bash
D:\path\to\ISAT\Anaconda\envs\isat_env\python.exe -m pip install -e .
```

### Requirements

* Python >= 3.8
* [`shapely`](https://pypi.org/project/shapely/) — installed automatically
* PyQt5 — already present, since ISAT depends on it

Only `shapely` is added on top of what ISAT already ships.

---

## Enable it in ISAT

1. Start ISAT.
2. Open **Tools → Plugins**.
3. Find **ISAT line tool** in the plugin list and tick it.
4. A **Line** button and a width spinbox appear on the toolbar.

The plugin needs no configuration file — the last width you used is remembered
in ISAT's own `software.yaml`.

---

## How to use

| Action                      | Result                                              |
| --------------------------- | --------------------------------------------------- |
| Hold left button and drag   | Draws the centerline (blue tracer)                  |
| Release                     | Commits it: creates one polygon of the chosen width |
| Click without dragging      | A round dot annotation                              |
| `Shift` while dragging      | Snaps the segment to 45° steps                      |
| `[` / `]`                   | Width −5 px / +5 px                                 |
| `Z`                         | Undo: drop the last point of the line in progress   |
| `Esc`                       | Discard the line in progress                        |
| `Esc` (nothing in progress) | Leave the tool, restore ISAT's own shortcuts        |
| `K`                         | Arm the tool from the keyboard                      |
| Right click                 | Discard the line in progress                        |

**Width** is the total thickness of the finished polygon, in pixels — not a
radius. The circular cursor shows exactly what you will get.

**Nothing is written to disk until you release the button.** An unfinished line
is a private overlay item and cannot end up in the saved json.

### Worked example

1. Set the width to `12 px` with the spinbox (or press `]` a few times).
2. Press and hold the left button at one end of the fibre.
3. Drag along its middle. Hold `Shift` for the straight stretches.
4. Release.
5. A `12 px` wide polygon appears, selected and listed in the annotation dock —
   same as if you had hand-drawn it.

---

## Notes on behaviour

* **One gesture, one annotation.** If you want three separate pieces, draw
  three times. Each release commits independently.
* **The tracer is not an annotation.** The blue line you drag is a temporary
  overlay that is removed the instant the real polygon is created.
* **`Z` undoes a point, not an annotation.** To remove a committed annotation,
  use ISAT's own delete (select it, press `Delete`).
* **Any drawing direction** works; the polygon is buffered from the centerline,
  so direction does not matter.
* A single click produces a small octagonal dot, so you can dot-annotate
  without dragging.
* **Flat caps.** The polygon ends where you stopped drawing, rather than
  ballooning into a rounded blob.

---

## Uninstall

```bash
pip uninstall isat-plugin-line-tool
```

Disable it in **Tools → Plugins** first if ISAT is running.

---

## Implementation notes

Three things about ISAT are worth knowing if you want to adapt this plugin.

**1. Dragging must be handled in `on_mouse_move_event`.**
ISAT only fires `on_mouse_pressed_and_mouse_move_event` while the scene's
internal `pressed` flag is set, and that flag is raised exclusively in the
CREATE / REPAINT annotation modes. This plugin deliberately leaves ISAT in its
normal VIEW mode, so it hooks `on_mouse_move_event` instead — the callback ISAT
does trigger on every move.

**2. QAction shortcuts beat view event filters.**
`QAction` shortcuts are window-level and are consumed by the main window before
any widget event filter sees the key. ISAT binds `Z` to its own undo action, so
while the tool is armed the plugin temporarily disables that action and restores
it **to its exact previous state** afterwards (`_arm()` / `_disarm()`).
ISAT's `software.yaml` is never modified.

**3. Press/release state must be tracked, not inferred.**
`on_mouse_release_event` snapshots `was_left = self._left_pressed` *before*
clearing it, so a stroke that was already thrown away by `Esc` or a right click
is not mistakenly committed.

### Two gotchas about `init_plugin` and `enable_plugin`

These cost real debugging time, so they are documented here.

**`mainwindow.cfg` does not exist yet when `init_plugin()` runs.**
ISAT constructs its plugin manager dialog from inside `init_ui()`, while the
config object is only assigned later. So `init_plugin()` must never let a `cfg`
lookup raise — an uncaught exception makes ISAT print
`failed to load plugin` and silently drop the plugin from the list, with no
visible error in the UI. Every `cfg` read in this plugin is wrapped in
`try/except`, and the width simply falls back to its default.

**`enable_plugin()` can be called more than once, and the manager reloads.**
The plugin manager dialog is constructed more than once during start-up, and
each construction re-runs plugin discovery, so more than one instance of the
same class can end up in the manager's list. Reloading also calls
`disable_plugin()` on every previous instance. Therefore:

* `_install_ui()` begins by calling `_remove_ui()`, making installation
  idempotent.
* `_remove_ui()` locates its own widgets **by `objectName`** instead of trusting
  a cached toolbar pointer — otherwise each reload leaves an orphaned spinbox
  behind.

Widget names used: `actionLineTool`, `spinBox_line_tool_width`,
`label_line_tool_width`.

Nothing in ISAT's own source is patched — the plugin only uses the public
`PluginBase` hooks. See [Compatibility](#compatibility).

---

## Compatibility

Tested against **ISAT 1.5.x** (Python 3.8, PyQt5).

The plugin talks to ISAT only through the documented plugin interface:

* the `isat.plugins` entry-point group,
* the `PluginBase` methods and lifecycle hooks,
* `MainWindow.polygons`, `MainWindow.scene`, `MainWindow.cfg`,
* the annotation dock and category dock widgets.

It does **not** monkey-patch or modify any ISAT file. If a future ISAT release
renames a hook or moves a widget, the plugin will simply stop working and can be
removed with `pip uninstall` — your ISAT installation is untouched.

---

## Files

```
isat_plugin_line_tool/
├── setup.py                        # registers the `line_tool` entry point
├── README.md
├── LICENSE
└── isat_plugin_line_tool/
    ├── __init__.py                 # exports Plugin
    └── plugin.py                   # all the logic
```

`entry_points` group: `isat.plugins`, name: `line_tool`.

---

## Contributing

Issues and pull requests are welcome. If you hit a problem, please include:

* your ISAT version,
* your Python version and OS,
* anything printed on the console when ISAT starts (the plugin loader logs
  there).

---

## License and attribution

This plugin is released under the **MIT License** — see [LICENSE](LICENSE).

It is an independent plugin for
[ISAT_with_segment_anything](https://github.com/yatengLG/ISAT_with_segment_anything),
which is developed by **yatengLG** and licensed under the
**Apache License 2.0**.

* No ISAT source file is copied, modified, or redistributed here.
* The plugin merely imports ISAT's public plugin interface at runtime
  (`PluginBase`, `Polygon`), which is the intended and documented way to extend
  the application.
* ISAT is **not** bundled with this package; users install it separately.
* "ISAT" and the names of its authors are used here only to describe
  compatibility. This plugin is **not** affiliated with, endorsed by, or
  maintained by the ISAT project.
