# ISAT Annotation Tools

Plugins for [**ISAT_with_segment_anything**](https://github.com/yatengLG/ISAT_with_segment_anything)
(image segmentation annotation tool).

This repository holds **two independent plugins**. They install separately,
uninstall separately, and share no code — take only the one you want.

| Plugin | What it does |
| --- | --- |
| **[Line tool](isat_plugin_line_tool/)** | Drag a centerline, release, get a fixed-width polygon |
| **[Eraser](isat_plugin_eraser/)** | Press on an annotation, drag, release — the mistake is gone and the rest is untouched |

Install both if you want to draw and to correct. A mis-drawn line is fixed by
erasing the overshoot rather than deleting vertices one at a time.

---

## Why

### The problem: labelling high-resolution imagery in a browser

Label Studio renders images **inside the local browser**. That works well for
ordinary photographs, but it becomes the bottleneck on high-resolution imagery:

* the image is decoded and held in browser memory, alongside a canvas copy of
  it, so the footprint grows with the pixel count;
* road, remote-sensing and similar measurement work **needs** a high spatial
  resolution — the features being traced are only a few pixels wide, so
  downscaling the image is not an option;
* the practical result is heavy memory use, slow image loading, and a viewer
  that stutters while panning, zooming or drawing.

In other words, the tool gets slow exactly in the workflow that depends on it.
This is a property of client-side browser rendering, not a criticism of any
particular product — it is simply the wrong container for very large rasters.

### The fix: annotate in a native application

ISAT is a **native desktop application** (PyQt5), not a web page, so it is not
bound by a browser tab's memory or canvas limits, and the same high-resolution
files stay workable. These plugins exist because that is the situation we were
in: road and remote-sensing annotation at high resolution, where a
browser-based labeller stalled — so we built the tools we needed for ISAT
instead of fighting the tool.

### And why tools like these specifically

Outlining long thin objects with a plain polygon tool is tedious: either you
click a dozen times per segment trying to trace both edges, or you accept a
ragged, too-thin sliver. With the line tool you trace the object **once** —
down the middle — and the width is handled for you.

And when the result is wrong, ISAT's own answer is to delete the annotation and
draw it again. That loses the effort you already spent and the re-drawn version
is a slightly different shape, so now the data has drifted as well as being
wrong. The eraser removes *only* the part that is wrong and leaves the rest of
the boundary exactly as it was.

That matters most for exactly the features that high-resolution imagery is
needed to resolve: a road a few pixels across, a crack, a vessel, a fibre.

Typical uses: remote-sensing and aerial roads, rivers and pipelines, microscopy
fibres / membranes / vessels, cracks, tubing, plumes, handwriting strokes.

---

## Install

Both plugins live outside ISAT's repo, so they survive ISAT updates and
uninstall cleanly.

```bash
git clone https://github.com/kay529/isat_plugin_line_tool.git
cd isat_plugin_line_tool

# The line tool
pip install -e .

# The eraser (optional, independent)
cd isat_plugin_eraser
pip install -e .
```

`-e` (editable) means later `git pull`s take effect immediately — no reinstall
needed. Each plugin's own README has the full details; the short version is:

```bash
# one plugin
cd isat_plugin_eraser && pip install -e .

# or both
pip install -e . && pip install -e ./isat_plugin_eraser
```

### Finding the right pip

Install into the environment ISAT itself uses, or ISAT will not see the plugins.
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

The line tool only needs `shapely`. The eraser also uses `numpy` and OpenCV
for its mask work; both are imported lazily with pure-Python fallbacks, so a
plugin still loads without them.

---

## Enable them in ISAT

1. Start ISAT.
2. Open **Tools → Plugins**.
3. Tick **ISAT line tool** and/or **ISAT eraser**.
4. Their buttons and spinboxes appear on the toolbar.

Neither plugin needs a configuration file — the last width, eraser size and
settings are remembered in ISAT's own `software.yaml`.

---

## Line tool at a glance

| Action                      | Result                                              |
| --------------------------- | --------------------------------------------------- |
| Hold left button and drag   | Draws the centerline (blue tracer)                  |
| Release                     | Commits it: creates one polygon of the chosen width |
| Click without dragging      | A round dot annotation                              |
| `Shift` while dragging      | Snaps the segment to 45° steps                      |
| `[` / `]`                   | Width −5 px / +5 px                                 |
| `Z`                         | Undo: drop the last point of the line in progress   |
| `Esc`                       | Discard the line in progress                        |
| `K`                         | Arm the tool from the keyboard                      |
| Right click                 | Discard the line in progress                        |

## Eraser at a glance

| Action                      | Result                                                |
| --------------------------- | ----------------------------------------------------- |
| Press on an annotation      | The eraser locks onto it (no pre-selecting needed)    |
| Drag while held             | The covered part is removed (red trail follows)       |
| Release                     | The annotation becomes whatever survived               |
| `[` / `]`                   | Eraser diameter −5 px / +5 px                          |
| `Z`                         | Undo: rewind the last mouse-move                      |
| `Esc`                       | Discard the erase in progress                         |
| `E`                         | Arm the eraser from the keyboard                      |
| Right click                 | Discard the erase in progress                         |

**Nothing is written to disk until you release the button.** An unfinished line
or erase is a private overlay and cannot end up in the saved json.

Full details, worked examples and behaviour notes are in each plugin's README.

---

## Performance

Both plugins are built for high-resolution imagery, and the rule they follow is
simple: **never do work proportional to the image on a mouse-move path.**

The line tool buffers a centerline with shapely, which costs O(vertices). The
eraser keeps its working mask sized to the *bounding box of the annotation being
edited* rather than the canvas, and draws its live preview as a stroked path
rather than a full-size image overlay. A 200 × 40 px annotation allocates an
8 KB mask whether the image is 1 MP or 300 MP.

This is not theoretical. An earlier version of the eraser allocated a
full-canvas RGBA preview on every mouse press; at 300 MP that is over a gigabyte
and roughly half a second of blocked GUI thread *per click*, which is exactly
the "click and nothing happens" behaviour this rewrite exists to fix. Measured
on the current version, a press costs ~12 ms at 300 MP and a drag frame under
3 ms.

---

## Uninstall

```bash
pip uninstall isat-plugin-line-tool     # line tool
pip uninstall isat-plugin-eraser        # eraser
```

Disable them in **Tools → Plugins** first if ISAT is running.

---

## Tests

The `tests/` directory holds checks that drive a real ISAT `MainWindow`, so they
need a local checkout of ISAT_with_segment_anything (ISAT is not on PyPI, so it
cannot be a dependency). Point them at one with `$ISAT_ROOT`; a sibling checkout
is found automatically.

```bash
# Functional checks for the eraser (163 of them)
ISAT_ROOT=/path/to/ISAT_with_segment_anything python tests/test_eraser.py

# Performance, from 2 to 300 megapixels
ISAT_ROOT=/path/to/ISAT_with_segment_anything python tests/bench_eraser.py
```

Both exit non-zero on failure. See the
[eraser README](isat_plugin_eraser/README.md#tests) for details.

Continuous integration runs the cheaper checks (syntax, lint, and that each
plugin still fails to import with a clean `ImportError` when ISAT is absent) on
every push.

---

## Repository layout

```
isat_plugin_line_tool/                 # the line tool plugin
├── setup.py                           # registers the `line_tool` entry point
├── README.md
├── LICENSE
└── isat_plugin_line_tool/
    ├── __init__.py
    └── plugin.py

isat_plugin_eraser/                    # the eraser plugin (independent)
├── setup.py                           # registers the `eraser` entry point
├── README.md
├── LICENSE
└── isat_plugin_eraser/
    ├── __init__.py
    └── plugin.py

tests/                                 # shared local test scripts
├── _bootstrap.py                      # locates ISAT via $ISAT_ROOT
├── test_eraser.py
└── bench_eraser.py

.github/workflows/ci.yml               # syntax + lint + import guard
```

Each plugin directory is a self-contained, independently installable Python
package with its own README, licence and entry point.

---

## Compatibility

Tested against **ISAT 1.5.x** (Python 3.8, PyQt5).

The plugins talk to ISAT only through the documented plugin interface:

* the `isat.plugins` entry-point group,
* the `PluginBase` methods and lifecycle hooks,
* `MainWindow.polygons`, `MainWindow.scene`, `MainWindow.cfg`,
* the annotation dock and category dock widgets.

They do **not** monkey-patch or modify any ISAT file. If a future ISAT release
renames a hook or moves a widget, a plugin will simply stop working and can be
removed with `pip uninstall` — your ISAT installation is untouched.

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

Both plugins are released under the **MIT License** — see
[LICENSE](LICENSE) and [isat_plugin_eraser/LICENSE](isat_plugin_eraser/LICENSE).

They are independent plugins for
[ISAT_with_segment_anything](https://github.com/yatengLG/ISAT_with_segment_anything),
which is developed by **yatengLG** and licensed under the
**Apache License 2.0**.

* No ISAT source file is copied, modified, or redistributed here.
* The plugins merely import ISAT's public plugin interface at runtime
  (`PluginBase`, `Polygon`), which is the intended and documented way to extend
  the application.
* ISAT is **not** bundled with these packages; users install it separately.
* "ISAT" and the names of its authors are used here only to describe
  compatibility. These plugins are **not** affiliated with, endorsed by, or
  maintained by the ISAT project.
