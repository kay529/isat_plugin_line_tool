# ISAT Eraser Plugin

A plugin for [**ISAT_with_segment_anything**](https://github.com/yatengLG/ISAT_with_segment_anything)
(image segmentation annotation tool).

**Press on an annotation, drag over the mistake, release. The rest of the
annotation is left alone.**

It is the paint-program eraser for ISAT: same "hold to draw, release to
commit" contract as a brush in Label Studio, one gesture, one result.

---

## Why

### The problem: fixing geometry without redoing it

Every annotation tool eventually gets geometry wrong. A traced road overshoots
into the building next to it. A fibre annotation leaks one pixel past the
specimen edge. Two regions that should have been one annotation get drawn as
two. A region swallows the object it was supposed to sit beside.

ISAT's own answer is to delete the annotation and draw it again. That is fine
for a small mistake on a simple shape, and terrible for a long traced feature:
you redo the whole thing, and the re-done version is a slightly different
shape, so now the data has drifted as well as being wrong.

The alternative is surgical. If you can remove *only* the part that is wrong,
the rest of the boundary you already spent effort on stays exactly as it was.

### What this plugin does

The eraser subtracts from an existing annotation:

* **You press on the annotation.** No pre-selecting — landing on it is what
  picks it, so a fix is a single gesture.
* **You drag across the part you want gone.** A live trail shows exactly what
  the eraser is covering.
* **You release.** The annotation is replaced by whatever survived. If your
  stroke cut it in two, you get two annotations, because that is the honest
  result.

The remainder keeps the original's **category, group, colour, note and crowd
flag**. Erasing a road does not turn it into a building, and it does not
consume a new group number in ISAT's auto-incrementing group counter.

Typical uses: trimming a region that overlapped its neighbour, splitting one
annotation that should be two, removing a thin overshoot at one end of a traced
line, shaving an annotation back to the specimen boundary.

---

## Install

The plugin lives outside ISAT's repo, so it survives ISAT updates and
uninstalls cleanly.

```bash
# 1. Get the code (from this repo's root)
cd isat_plugin_line_tool/isat_plugin_eraser

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
* [`numpy`](https://pypi.org/project/numpy/) — installed automatically
* [`opencv-python`](https://pypi.org/project/opencv-python/) — installed
  automatically; ISAT already ships OpenCV, so in practice this is already
  satisfied
* PyQt5 — already present, since ISAT depends on it

OpenCV does the raster fill and the contour tracing. It is imported lazily and
both uses have pure-Python fallbacks, so the plugin still loads without it — but
erasing will be noticeably slower.

---

## Enable it in ISAT

1. Start ISAT.
2. Open **Tools → Plugins**.
3. Find **ISAT eraser** in the list and tick it.
4. An **Erase** button and a diameter spinbox appear on the toolbar.

The plugin needs no configuration file — the last diameter you used is
remembered in ISAT's own `software.yaml`.

---

## How to use

| Action                      | Result                                                |
| --------------------------- | ----------------------------------------------------- |
| Press on an annotation      | The eraser locks onto it                              |
| Drag while held             | The covered part is removed (red trail shows the head) |
| Release                     | Commits: the annotation becomes whatever survived       |
| `[` / `]`                   | Eraser diameter −5 px / +5 px                           |
| `Z`                         | Undo: drop the last segment of the stroke               |
| `Esc`                       | Discard the erase in progress                           |
| `Esc` (nothing in progress) | Leave the tool, restore ISAT's own shortcuts            |
| `E`                         | Arm the eraser from the keyboard                       |
| Right click                 | Discard the erase in progress                           |

**Diameter** is the width of the eraser head, in pixels — not a radius. The
circular cursor shows exactly what will be removed.

**Nothing is written to disk until you release the button.** An unfinished erase
is a private overlay and cannot end up in the saved json.

### Worked example

You traced a road and it ran 40 px into the building on the right.

1. Press `E` (or click the **Erase** button) and set the diameter to `60 px`.
2. Press the left button on the road annotation, directly on the overshoot.
3. Drag left across the part that went into the building. The red trail follows
   the eraser; the circular cursor shows the diameter.
4. Release. The road annotation now ends at the building wall, with the same
   category and group it had before.

### Worked example: splitting one annotation into two

1. Set the diameter to something wider than the neck you want to cut, e.g.
   `80 px`.
2. Press on the annotation and drag straight across it.
3. Release. You now have two annotations, because that is what the region
   actually is. Re-assign categories in the annotation list as usual.

---

## Notes on behaviour

* **You do not pre-select.** Pressing on an annotation is what picks it. If you
  press on empty canvas, nothing happens and the status bar says so, rather
  than appearing broken.
* **The topmost annotation wins.** ISAT draws annotations in list order, so if
  two overlap, the eraser edits the one you can actually see.
* **The remainder keeps its identity.** Category, group, colour, note and crowd
  flag all carry over, and the auto-incrementing group counter is not advanced.
* **Erase can split an annotation.** Cut a band in two and you get two
  annotations. That is intentional — it matches what a brush does, and it is
  usually the correct reading of what you just drew.
* **Erase can delete an annotation.** Rub the whole thing away and it is gone,
  exactly as if you had selected it and pressed `Delete`.
* **Holes are cut open, not filled in.** If your stroke encloses an island in
  the middle of a region, ISAT polygons cannot store a hole (a polygon is a
  single ring), so the plugin cuts a narrow channel from the hole to the
  boundary. The result is a C-shape rather than an annulus, and the erased area
  stays erased instead of silently filling back in.
* **`Z` undoes one segment at a time**, so a long drag can be walked back
  incrementally. There are no vertices to move, so that is the finest grain
  available.
* **The erase trail is not an annotation.** The red stroke is a temporary
  overlay, removed the instant the real polygon is created.
* **A too-large annotation is refused** rather than attempted: past a
  deliberately generous limit the plugin says so instead of trying to allocate
  an enormous buffer.

---

## Uninstall

```bash
pip uninstall isat-plugin-eraser
```

Disable it in **Tools → Plugins** first if ISAT is running.

---

## Performance

This is the part that matters on real data, and the reason this plugin is
written the way it is.

**The rule: never do work proportional to the image on a mouse-move path.**

A first version of this plugin worked, but felt broken — click, wait, nothing.
The cause was allocation, not geometry. On every mouse press it allocated a
full-canvas RGBA preview buffer (`height × width × 4` bytes) and handed it to
`QPixmap.fromImage`. On a 20 000 × 15 000 image that is **1.1 GB** of zeroed
memory and a very large pixmap upload, once per click, on the GUI thread. ISAT
was not slow — it was blocked.

What this version does instead:

* **The working mask is sized to the target's bounding box**, not the canvas.
  The eraser only ever clears pixels that are already inside the annotation, so
  the polygon's own box is all that can ever be affected. A 200 × 40 px
  annotation allocates an 8 KB mask regardless of whether the image is 1 MP or
  300 MP.
* **Rasterizing uses a scan-line fill** (`cv2.fillPoly`), which costs
  O(polygon area) rather than O(canvas).
* **The live preview is a stroked `QGraphicsPathItem`**, so redrawing the trail
  costs the same for a 1 px dot and a 200 px head. No pixmap, no upload.
* **Every per-move operation is bounded by the eraser's own footprint** — the
  distance-to-segment test runs over just the affected rectangle.
* **Undo snapshots are capped** at 512 per stroke, so a pathological drag cannot
  grow memory without bound, and everything is released on commit or abort.
* **No pixmap is uploaded on the move path at all.** The image data is not
  touched while you drag.

If you extend this plugin, keep that budget. Anything that scales with
`width × height` on a mouse-move or mouse-press path will eventually freeze
ISAT, and the higher the resolution you work at, the sooner.

---

## Implementation notes

Four things about ISAT are worth knowing if you want to adapt this plugin.

**1. Dragging must be handled in `on_mouse_move_event`.**
ISAT only fires `on_mouse_pressed_and_mouse_move_event` while the scene's
internal `pressed` flag is set, and that flag is raised exclusively in the
CREATE / REPAINT annotation modes. This plugin deliberately leaves ISAT in its
normal VIEW mode, so it hooks `on_mouse_move_event` instead — the callback ISAT
does trigger on every move.

**2. QAction shortcuts beat view event filters.**
`QAction` shortcuts are window-level and are consumed by the main window before
any widget event filter sees the key. ISAT binds `Z` to its own undo action and
`E` to its finish action, so while the eraser is armed the plugin temporarily
disables those actions and restores them **to their exact previous state**
afterwards (`_arm()` / `_disarm()`). ISAT's `software.yaml` is never modified.

**3. Press/release state must be tracked, not inferred.**
`on_mouse_release_event` snapshots `was_left = self._left_pressed` *before*
clearing it, so a stroke that was already thrown away by `Esc` or a right click
is not mistakenly committed.

**4. Hit testing needs item coordinates.**
`QGraphicsPolygonItem.contains()` expects a point in the item's own coordinate
system, so a scene position has to go through `mapFromScene()` first. Passing
the scene position straight in works only while the item sits at the origin.

### Two gotchas about `init_plugin` and `enable_plugin`

These cost real debugging time, so they are documented here.

**`mainwindow.cfg` does not exist yet when `init_plugin()` runs.**
ISAT constructs its plugin manager dialog from inside `init_ui()`, while the
config object is only assigned later. So `init_plugin()` must never let a `cfg`
lookup raise — an uncaught exception makes ISAT print
`failed to load plugin` and silently drop the plugin from the list, with no
visible error in the UI. Every `cfg` read in this plugin is wrapped in
`try/except`, and the size simply falls back to its default.

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

Widget names used: `actionEraser`, `spinBox_eraser_size`, `label_eraser_size`.

`get_plugin_name()` is overridden so ISAT's plugin list shows a readable
`ISAT eraser` rather than the raw class name.

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

This plugin is independent of the
[line tool plugin](../isat_plugin_line_tool/README.md) in the same repository:
they install and uninstall separately and share no code. Install both if you
want to draw and to correct.

---

## Tests

Two scripts drive a real ISAT `MainWindow`, so they need a local checkout of
ISAT_with_segment_anything (ISAT is not on PyPI, so it cannot be a dependency).
Point them at one with `$ISAT_ROOT`; a sibling checkout is found automatically.

```bash
# Functional: 163 checks over hit testing, editing, commit, undo, teardown
ISAT_ROOT=/path/to/ISAT_with_segment_anything python tests/test_eraser.py

# Performance: press/drag/release latency from 2 to 300 megapixels
ISAT_ROOT=/path/to/ISAT_with_segment_anything python tests/bench_eraser.py
```

Both exit non-zero on failure. The benchmark is the important one: it fails if
any canvas size falls outside the interactive budget, so the full-canvas
allocation that caused the original freeze cannot come back unnoticed. It also
prints what the old full-canvas preview *would* have cost at each size, for
comparison.

Continuous integration runs the cheaper checks (syntax, lint, and that the
plugin still fails to import with a clean `ImportError` when ISAT is absent) on
every push.

---

## Files

```
isat_plugin_eraser/
├── setup.py                        # registers the `eraser` entry point
├── README.md
├── LICENSE
└── isat_plugin_eraser/
    ├── __init__.py                 # exports Plugin
    └── plugin.py                   # all the logic
```

`entry_points` group: `isat.plugins`, name: `eraser`.

The `tests/` directory at the repository root is shared by both plugins.

---

## Contributing

Issues and pull requests are welcome. If you hit a problem, please include:

* your ISAT version,
* your Python version and OS,
* the image dimensions you were working at (performance problems are almost
  always resolution-dependent),
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
