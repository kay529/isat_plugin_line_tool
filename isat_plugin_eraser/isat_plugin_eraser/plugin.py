# -*- coding: utf-8 -*-
"""ISAT eraser plugin -- rub a piece out of an annotation that already exists.

Interaction -- hold to erase, release to commit
-----------------------------------------------
=====================================  ==================================
Click the annotation you want to fix    the eraser locks onto it
Drag                                   the covered part is removed
Release                                commit: the annotation is replaced
                                       by whatever survived, one polygon
                                       per surviving region
``[`` / ``]``                          shrink / grow the eraser by 5 px
``Z``                                  undo: drop the whole erase stroke
``Esc``                                discard the erase in progress
``Esc`` (again, nothing in progress)   leave the tool, restore ISAT keys
=====================================  ==================================

You do **not** pre-select the annotation. Pressing on it is what picks it, so
a correction is one gesture: land on the mistake, drag across it, let go. This
is the same "press, drag, release" contract as a paint program.

Why this is useful
------------------
When the geometry comes from a tool rather than from clicking vertices one by
one, the cheapest way to fix a mistake is to remove the mistake, not to
reconstruct the shape around it. Deleting a vertex moves a corner; deleting a
whole annotation loses everything. Erasing cuts exactly the part you rubbed out
and leaves the rest as it was.

Real cases this covers: a traced road that overshot into the neighbouring
building, a fibre annotation that leaked one pixel past the specimen edge, two
annotations that should have been one, a region that swallowed an adjacent
object.

Design notes
------------
* **Nothing is allocated per image pixel.** The working mask is sized to the
  *bounding box of the annotation being edited*, not to the canvas, and the live
  preview is a stroked ``QGraphicsPathItem`` rather than a full-size RGBA
  overlay. This is the whole reason the plugin stays responsive: an earlier
  version allocated an ``h x w x 4`` buffer and uploaded it as a ``QPixmap`` on
  every mouse press, which on high-resolution imagery meant hundreds of
  megabytes of zeroed memory and a multi-second freeze per click. See the
  performance section of the README.
* **Nothing is appended to ``mainwindow.polygons`` until the stroke is
  released**, and ``MainWindow.save()`` only serializes
  ``mainwindow.polygons``. An unfinished erase can therefore never leak into the
  saved json.
* The erase trail is a private ``QGraphicsPathItem``, removed before the real
  annotation is created, so it is never an annotation.
* The edited annotation keeps its **category, group, colour, note and crowd
  flag** -- the remainder is the same class of thing it was before, not whatever
  happened to be selected in the UI afterwards. Erasing also does not advance
  ISAT's auto-incrementing group counter.
* Per-move work is proportional to the eraser's own footprint, never to the
  image, so dragging stays smooth.
* OpenCV is imported lazily and every use has a fallback, so the plugin still
  loads on an environment where it is missing (slower, but correct).

Nothing in ISAT's own source is touched.

License
-------
MIT. This is an independent plugin for ISAT_with_segment_anything
(https://github.com/yatengLG/ISAT_with_segment_anything), which is developed
by yatengLG and licensed under the Apache License 2.0. No ISAT source file is
copied, modified or redistributed here; the plugin only imports ISAT's public
plugin interface at runtime. ISAT is not bundled with this package. Not
affiliated with or endorsed by the ISAT project.
"""

import math

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

from ISAT.widgets.plugin_base import PluginBase
from ISAT.widgets.polygon import Polygon


# ---------------------------------------------------------------------- #
# Constants
# ---------------------------------------------------------------------- #
# Diameter of the eraser head, in px.
_MIN_ERASE_SIZE = 1
_MAX_ERASE_SIZE = 200
_DEFAULT_ERASE_SIZE = 15
_SIZE_STEP = 5

# Shortest segment the eraser bothers to record, in px. Also the step floor
# used when walking a long jump.
_MIN_SEGMENT = 1.0

# Cap on undo snapshots per stroke, so a pathological drag cannot grow memory
# without bound. Everything is released on commit or abort regardless.
_MAX_UNDO = 512

# Shapely buffer styles for the eraser stroke: round caps and joins, because a
# round head is what the cursor promises.
_CAP_ROUND = 1
_JOIN_ROUND = 1

# Mask -> polygon when finishing an erase.
_APPROX_EPS_RATIO = 0.002
_MIN_CONTOUR_AREA = 4.0

# Width of the channel cut from an enclosed hole out to the boundary. Erasing a
# blob in the middle of a region leaves an annulus, and an ISAT polygon has a
# single ring with no way to express a hole; the channel turns the annulus into
# a C-shape, which a single ring can express exactly. Without it the hole would
# silently fill back in and the erase would look like it did nothing.
_HOLE_CHANNEL_WIDTH = 2.0

# Refuse to build a mask larger than this, as a guard against a polygon with
# absurd coordinates wedging the process. 80 megapixels is far beyond any real
# annotation and still only 80 MB as uint8.
_MAX_MASK_PIXELS = 80_000_000

# ISAT's own single-key shortcuts that collide with this plugin's keys.
# QAction shortcuts are consumed by MainWindow before any view event filter
# can see them, so these are temporarily disabled while the tool is armed and
# restored afterwards. software.yaml is never modified.
_CLASHING_ACTIONS = (
    "actionFinish",      # E -> eraser
    "actionBackspace",   # Z -> undo
)


class _KeyFilter(QtCore.QObject):
    """Real QObject shim so it can be installed as an event filter.

    ``PluginBase`` is a plain ABC, not a QObject, so the plugin itself cannot
    be passed to ``installEventFilter``.
    """

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def eventFilter(self, obj, event):
        try:
            return bool(self.plugin.handle_key_event(event))
        except Exception:
            return False


class _CursorTracker(QtCore.QObject):
    """Redraws the circular size cursor as the mouse moves."""

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def eventFilter(self, obj, event):
        try:
            if event.type() in (
                QtCore.QEvent.Type.MouseMove,
                QtCore.QEvent.Type.Enter,
            ):
                self.plugin._update_cursor_item()
        except Exception:
            pass
        return False


class EraserPlugin(PluginBase):
    """One toolbar button: rub a piece out of an existing annotation."""

    def __init__(self):
        super().__init__()
        self.mainwindow = None

        self.erase_size = float(_DEFAULT_ERASE_SIZE)
        """Diameter of the eraser head, in px."""

        # --- stroke state ---------------------------------------------------
        self.erasing = False
        """True while the left button is held down and an erase is in progress."""

        self.points = None
        """Erase trail so far (list of QPointF, scene coordinates)."""

        self.target = None
        """The Polygon being carved; replaced on commit."""

        self.cursor_pos = None
        self._left_pressed = False
        self._right_pressed = False

        # --- working mask (bounding-box sized, never canvas sized) ----------
        self.mask = None
        """Binary uint8 mask of the target's bounding box; 255 inside."""

        self._roi = None
        """``(x0, y0)`` scene coordinate of the mask's top-left pixel."""

        self._backups = None
        """Snapshots of the mask, one per mouse-move event, for undo."""

        self._undo_len = None
        """Trail length before each snapshot, so undo can rewind in one step."""

        # --- scene items / UI -----------------------------------------------
        self.trail_item = None
        self.cursor_item = None
        self.tool_action = None
        self.size_spin = None
        self.size_label = None
        self.toolbar = None
        self.view = None
        self.key_filter = None
        self.cursor_filter = None

        self._armed = False
        self._suppressed_state = {}

    # ------------------------------------------------------------------ #
    # PluginBase interface
    # ------------------------------------------------------------------ #
    def init_plugin(self, mainwindow):
        """Called by ISAT's plugin manager.

        Note: ISAT's plugin manager dialog is constructed from inside
        ``MainWindow.init_ui()``, while ``mainwindow.cfg`` is only assigned
        later, in ``reload_cfg()``. So ``cfg`` is normally absent here --
        reading it must never raise, or ISAT's loader reports
        "failed to load plugin" and drops the plugin entirely.
        """
        self.mainwindow = mainwindow
        try:
            software = mainwindow.cfg.get("software", {})
        except Exception:
            software = {}
        try:
            saved = software.get("eraser_size")
            if saved is not None:
                self.erase_size = float(saved)
        except Exception:
            pass

    def enable_plugin(self):
        self.enabled = True
        self._install_ui()

    def disable_plugin(self):
        self.enabled = False
        self._abort()
        self._disarm()
        self._remove_ui()

    def get_plugin_name(self) -> str:
        # Without this, ISAT's plugin list shows the raw class name.
        return "ISAT eraser"

    def get_plugin_author(self) -> str:
        return "ISAT eraser contributors"

    def get_plugin_version(self) -> str:
        return "1.0.0"

    def get_plugin_description(self) -> str:
        return (
            "Erase a piece out of an annotation that already exists, like a "
            "brush in Label Studio. Press on the annotation, drag over the part "
            "you want gone, release: it is replaced by whatever survived, one "
            "annotation per surviving region, keeping its category and group. "
            "[ / ] change the eraser size, Z undoes the stroke, Esc discards."
        )

    def application_start_event(self):
        if self.enabled:
            self._install_ui()

    def after_image_open_event(self):
        # A new image means a different canvas: drop everything in progress.
        self._abort()
        self._sync_ui()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _install_ui(self):
        # Idempotent and self-healing. ISAT's plugin manager calls
        # load_plugins() more than once during start-up and each pass calls
        # enable_plugin() on the same instance; installing twice would
        # duplicate every widget and leak event filters that _remove_ui()
        # could no longer find. Going through _remove_ui() first also cleans
        # up stale widgets left behind by an earlier reload.
        self._remove_ui()
        try:
            toolbar = None
            for tb in self.mainwindow.findChildren(QtWidgets.QToolBar):
                toolbar = tb
                break
            if toolbar is None:
                return
            self.toolbar = toolbar

            self.tool_action = QtWidgets.QAction(self.mainwindow)
            self.tool_action.setObjectName("actionEraser")
            self.tool_action.setText("Erase")
            self.tool_action.setToolTip(
                "Eraser: press on an annotation, drag over the part to remove, "
                "release to apply (E)"
            )
            self.tool_action.setStatusTip(
                "Press on the annotation you want to fix, drag across the part "
                "to erase, release to apply. [ / ] change the eraser size."
            )
            self.tool_action.setShortcut(QtGui.QKeySequence("E"))
            self.tool_action.setCheckable(False)
            self.tool_action.triggered.connect(self.activate_tool)
            toolbar.addAction(self.tool_action)

            self.size_spin = QtWidgets.QSpinBox(self.mainwindow)
            self.size_spin.setObjectName("spinBox_eraser_size")
            self.size_spin.setRange(_MIN_ERASE_SIZE, _MAX_ERASE_SIZE)
            self.size_spin.setSingleStep(_SIZE_STEP)
            self.size_spin.setSuffix(" px")
            self.size_spin.setMaximumWidth(80)
            self.size_spin.setToolTip(
                "Eraser diameter in px ( [ / ] to change )"
            )
            self.size_spin.valueChanged.connect(self._on_size_changed)
            toolbar.addWidget(self.size_spin)

            self.size_label = QtWidgets.QLabel(" d")
            self.size_label.setObjectName("label_eraser_size")
            toolbar.addWidget(self.size_label)

            # Keys and cursor tracking.
            view = getattr(self.mainwindow, "view", None)
            if view is not None:
                self.key_filter = _KeyFilter(self)
                view.installEventFilter(self.key_filter)
                view.setMouseTracking(True)
                vp = view.viewport()
                if vp is not None:
                    self.cursor_filter = _CursorTracker(self)
                    vp.installEventFilter(self.cursor_filter)
                    vp.setMouseTracking(True)
                self.view = view

            self._sync_ui()
        except Exception as e:  # pragma: no cover - defensive
            print("Eraser: failed to install UI:", e)

    def _widgets_by_name(self, name):
        """Find our injected widgets by objectName.

        ISAT's plugin manager builds its dialog twice during start-up, so two
        instances of this plugin exist and each installs its own widgets.
        Tracking them by name lets teardown clean up its own regardless of
        which toolbar object is current.
        """
        try:
            return list(self.mainwindow.findChildren(QtWidgets.QWidget, name))
        except Exception:
            return []

    def _remove_ui(self):
        try:
            if self.key_filter is not None and self.view is not None:
                self.view.removeEventFilter(self.key_filter)
        except Exception:
            pass
        try:
            if self.cursor_filter is not None and self.view is not None:
                vp = self.view.viewport()
                if vp is not None:
                    vp.removeEventFilter(self.cursor_filter)
        except Exception:
            pass
        self.key_filter = None
        self.cursor_filter = None
        self.view = None

        # Always work against the toolbar that is live right now -- after a
        # plugin reload the cached one may be stale.
        toolbar = self.toolbar
        try:
            found = self.mainwindow.findChildren(QtWidgets.QToolBar)
            if found:
                toolbar = found[0]
        except Exception:
            pass

        action = self.tool_action
        if action is not None:
            for tb in (self.toolbar, toolbar):
                if tb is None:
                    continue
                try:
                    tb.removeAction(action)
                except Exception:
                    pass
            # Unparent as well as schedule for deletion. deleteLater() only
            # takes effect once the event loop runs, so without this the action
            # would stay a child of the main window -- and therefore still
            # findable, and still holding its shortcut -- for an unbounded
            # time after the plugin is switched off.
            try:
                action.setParent(None)
            except Exception:
                pass
            try:
                action.deleteLater()
            except Exception:
                pass
        self.tool_action = None

        # Pull any spinbox / label with our objectName off whatever toolbar
        # still holds it, then destroy it.
        for name in ("spinBox_eraser_size", "label_eraser_size"):
            for widget in self._widgets_by_name(name):
                try:
                    parent = widget.parent()
                    if isinstance(parent, QtWidgets.QToolBar):
                        parent.removeWidget(widget)
                except Exception:
                    pass
                try:
                    widget.hide()
                    widget.setParent(None)
                    widget.deleteLater()
                except Exception:
                    pass
        self.size_spin = None
        self.size_label = None
        self.toolbar = None

    def _sync_ui(self):
        if self.size_spin is not None:
            blocked = self.size_spin.blockSignals(True)
            self.size_spin.setValue(int(round(self.erase_size)))
            self.size_spin.blockSignals(blocked)

    def _on_size_changed(self, value):
        self.erase_size = float(value)
        try:
            self.mainwindow.cfg["software"]["eraser_size"] = float(value)
        except Exception:
            pass
        self._update_cursor_item()

    def activate_tool(self):
        """Toolbar button or ``E``: arm the eraser and say what to do."""
        if not self.enabled:
            return
        self._arm()
        self._install_cursor_item()
        self.mainwindow.statusbar.showMessage(
            "Eraser: press on the annotation to fix, drag over the part to "
            "remove, release to apply.",
            4000,
        )

    # ------------------------------------------------------------------ #
    # Shortcut takeover
    # ------------------------------------------------------------------ #
    def _arm(self):
        """Disable the ISAT actions whose keys this plugin needs."""
        if self._armed:
            return
        self._armed = True
        self._suppressed_state = {}
        for name in _CLASHING_ACTIONS:
            action = getattr(self.mainwindow, name, None)
            if action is None:
                continue
            try:
                self._suppressed_state[name] = action.isEnabled()
                action.setEnabled(False)
            except Exception:
                pass

    def _disarm(self):
        """Restore exactly the enabled/disabled state seen on arming."""
        if not self._armed:
            return
        for name, was_enabled in self._suppressed_state.items():
            action = getattr(self.mainwindow, name, None)
            if action is None:
                continue
            try:
                action.setEnabled(was_enabled)
            except Exception:
                pass
        self._suppressed_state = {}
        self._armed = False

    def leave_tool(self):
        """Hand ISAT's shortcuts back and put the cursor back to normal."""
        self._disarm()
        self._clear_cursor_item()
        self._set_os_cursor(QtCore.Qt.CursorShape.ArrowCursor)

    # ------------------------------------------------------------------ #
    # Keyboard
    # ------------------------------------------------------------------ #
    def handle_key_event(self, event):
        if not self.enabled:
            return False
        if event.type() != QtCore.QEvent.Type.KeyPress:
            return False
        key = event.key()

        if key == QtCore.Qt.Key.Key_BracketLeft:
            self._nudge_size(-_SIZE_STEP)
            return True
        if key == QtCore.Qt.Key.Key_BracketRight:
            self._nudge_size(+_SIZE_STEP)
            return True

        if key == QtCore.Qt.Key.Key_Z:
            self._undo()
            return True

        if key == QtCore.Qt.Key.Key_Escape:
            if self.erasing:
                self._abort()
                self.mainwindow.statusbar.showMessage(
                    "Eraser: discarded. Esc again leaves the tool.", 3000
                )
            elif self._armed:
                self.leave_tool()
                self.mainwindow.statusbar.showMessage(
                    "Eraser off. ISAT shortcuts restored.", 3000
                )
            return True

        return False

    def _nudge_size(self, delta):
        new = int(round(self.erase_size)) + delta
        new = max(_MIN_ERASE_SIZE, min(_MAX_ERASE_SIZE, new))
        if self.size_spin is not None:
            self.size_spin.setValue(new)  # fires _on_size_changed
        else:
            self.erase_size = float(new)
            self._update_cursor_item()
        self.mainwindow.statusbar.showMessage(
            "Eraser size: {} px".format(new), 1500
        )

    def _undo(self):
        """Z: drop the last bit of the eraser path, one mouse-move at a time.

        Undo granularity is one mouse-move event, not one interpolated
        sub-sample: a single fast flick can be split into many sub-steps, and
        stepping back through them one at a time would need a keypress per
        pixel. Everything painted by the last move event is restored at once.
        """
        if not self.erasing or not self._backups:
            return False
        self.mask = self._backups.pop()
        del self.points[self._undo_len.pop():]
        self._redraw_trail()
        return True

    # ------------------------------------------------------------------ #
    # Cursor
    # ------------------------------------------------------------------ #
    def _set_os_cursor(self, shape):
        try:
            scene = self.mainwindow.scene
            if scene.image_item is not None:
                scene.image_item.setCursor(QtGui.QCursor(shape))
        except Exception:
            pass

    def _install_cursor_item(self):
        scene = getattr(self.mainwindow, "scene", None)
        if scene is None or scene.image_item is None:
            return
        if self.cursor_item is None:
            self.cursor_item = QtWidgets.QGraphicsEllipseItem()
            pen = QtGui.QPen(QtGui.QColor(255, 255, 255), 0)
            pen.setCosmetic(True)
            self.cursor_item.setPen(pen)
            self.cursor_item.setBrush(QtGui.QBrush())
            self.cursor_item.setZValue(1e6)
            scene.addItem(self.cursor_item)
        self._update_cursor_item()

    def _clear_cursor_item(self):
        if self.cursor_item is None:
            return
        try:
            self.mainwindow.scene.removeItem(self.cursor_item)
        except Exception:
            pass
        self.cursor_item = None

    def _cursor_radius(self):
        return max(0.5, self.erase_size / 2.0)

    def _update_cursor_item(self):
        if not self.enabled or self.cursor_item is None:
            return
        view = self.view
        if view is None:
            return
        try:
            local = view.viewport().mapFromGlobal(QtGui.QCursor.pos())
            scene_pos = view.mapToScene(local)
        except Exception:
            return
        r = self._cursor_radius()
        self.cursor_item.setRect(
            QtCore.QRectF(scene_pos.x() - r, scene_pos.y() - r, r * 2, r * 2)
        )

    # ------------------------------------------------------------------ #
    # Canvas
    # ------------------------------------------------------------------ #
    def _canvas_size(self):
        scene = getattr(self.mainwindow, "scene", None)
        if scene is None or scene.image_item is None:
            return None
        if not getattr(self.mainwindow, "can_be_annotated", False):
            return None
        w = int(round(scene.width()))
        h = int(round(scene.height()))
        if w <= 0 or h <= 0:
            return None
        return w, h

    # ------------------------------------------------------------------ #
    # Hit testing
    # ------------------------------------------------------------------ #
    def _polygon_at(self, scene_pos):
        """The topmost annotation under ``scene_pos``, or ``None``.

        ISAT draws annotations in list order, so the last match is the one the
        user sees on top. Walking the list backwards and returning on the first
        hit therefore picks the annotation that is actually visible, even where
        two of them overlap. Testing every polygon against every vertex is cheap
        at realistic annotation counts, and early exit keeps it cheap for large
        ones.
        """
        polygons = getattr(self.mainwindow, "polygons", None)
        if not polygons:
            return None
        p = QtCore.QPointF(scene_pos)
        for poly in reversed(polygons):
            try:
                if len(poly.points) < 3:
                    continue
                # QGraphicsPolygonItem.contains() wants item coordinates.
                if poly.contains(poly.mapFromScene(p)):
                    return poly
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------ #
    # Working mask, sized to the target's bounding box
    # ------------------------------------------------------------------ #
    def _build_mask(self, polygon):
        """Rasterize ``polygon`` into a bounding-box sized mask.

        Returns ``True`` on success. The mask covers only the polygon's own
        bounding box, which is all that can ever be erased -- the eraser only
        ever clears pixels that are already inside the annotation -- so no
        canvas-sized allocation is needed at any point.
        """
        if self._canvas_size() is None:
            return False
        try:
            pts = [polygon.mapToScene(QtCore.QPointF(q)) for q in polygon.points]
        except Exception:
            pts = [QtCore.QPointF(q) for q in polygon.points]
        if len(pts) < 3:
            return False

        xs = [q.x() for q in pts]
        ys = [q.y() for q in pts]
        cw, ch = self._canvas_size()
        x0 = max(0, int(math.floor(min(xs))))
        y0 = max(0, int(math.floor(min(ys))))
        x1 = min(cw - 1, int(math.ceil(max(xs))))
        y1 = min(ch - 1, int(math.ceil(max(ys))))
        w = x1 - x0 + 1
        h = y1 - y0 + 1
        if w <= 1 or h <= 1:
            return False
        if w * h > _MAX_MASK_PIXELS:
            self.mainwindow.statusbar.showMessage(
                "Eraser: that annotation is too large to edit here.", 4000
            )
            return False

        mask = np.zeros((h, w), dtype=np.uint8)
        local = [(q.x() - x0, q.y() - y0) for q in pts]

        # OpenCV scan-line fill: O(polygon area), independent of canvas size.
        filled = False
        try:
            import cv2

            arr = np.array(
                [[int(round(a)), int(round(b))] for a, b in local],
                dtype=np.int32,
            )
            cv2.fillPoly(mask, [arr], 255)
            filled = True
        except Exception:
            filled = False

        if not filled:
            # Fallback: per-scanline even-odd crossing test, restricted to the
            # bounding box. Still O(box), and no whole-canvas grids. Handles
            # any vertex order, which is all ISAT ever hands us.
            n = len(local)
            for row in range(h):
                yc = row + 0.5
                crossings = []
                for i in range(n):
                    ax, ay = local[i]
                    bx, by = local[(i + 1) % n]
                    if (ay <= yc < by) or (by <= yc < ay):
                        t = (yc - ay) / (by - ay)
                        crossings.append(ax + t * (bx - ax))
                crossings.sort()
                for k in range(0, len(crossings) - 1, 2):
                    a = int(math.ceil(crossings[k] - 0.5))
                    b = int(math.floor(crossings[k + 1] - 0.5))
                    a = max(a, 0)
                    b = min(b, w - 1)
                    if b >= a:
                        mask[row, a:b + 1] = 255
            filled = True

        if not mask.any():
            return False
        self.mask = mask
        self._roi = (x0, y0)
        self._backups = []
        self._undo_len = []
        return True

    def _paint_segment(self, p0, p1):
        """Clear a round-capped eraser segment from the mask.

        Both points are scene coordinates. The work is bounded by the eraser's
        own footprint, so this is the same cost for a 1 px dot and a 200 px
        head.
        """
        if self.mask is None or self._roi is None:
            return False
        h, w = self.mask.shape
        x0r, y0r = self._roi
        radius = max(0.5, self.erase_size / 2.0)

        # Scene coords -> mask coords, then the affected box in mask space.
        ax, ay = p0.x() - x0r, p0.y() - y0r
        bx, by = p1.x() - x0r, p1.y() - y0r
        min_x = max(0, int(math.floor(min(ax, bx) - radius)) - 1)
        max_x = min(w - 1, int(math.ceil(max(ax, bx) + radius)) + 1)
        min_y = max(0, int(math.floor(min(ay, by) - radius)) - 1)
        max_y = min(h - 1, int(math.ceil(max(ay, by) + radius)) + 1)
        if min_x > max_x or min_y > max_y:
            return False

        ys = np.arange(min_y, max_y + 1, dtype=np.float32)
        xs = np.arange(min_x, max_x + 1, dtype=np.float32)
        grid_x, grid_y = np.meshgrid(xs, ys)

        dx, dy = bx - ax, by - ay
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq < 1e-9:
            dist = np.hypot(grid_x - ax, grid_y - ay)
        else:
            t = ((grid_x - ax) * dx + (grid_y - ay) * dy) / seg_len_sq
            t = np.clip(t, 0.0, 1.0)
            dist = np.hypot(grid_x - (ax + t * dx), grid_y - (ay + t * dy))

        region = self.mask[min_y:max_y + 1, min_x:max_x + 1]
        region[dist <= radius] = 0
        return True

    # ------------------------------------------------------------------ #
    # Stroke lifecycle -- press, drag, release
    # ------------------------------------------------------------------ #
    def on_mouse_press_event(self, scene_pos):
        if not self.enabled:
            return
        try:
            buttons = QtWidgets.QApplication.mouseButtons()
        except Exception:
            buttons = QtCore.Qt.MouseButton.NoButton

        if buttons & QtCore.Qt.MouseButton.RightButton:
            # Right click abandons whatever is in progress. There is nothing to
            # finish, because a stroke is committed on release.
            self._right_pressed = True
            self._abort()
            return

        self._right_pressed = False
        self._left_pressed = True

        # Press always begins a fresh stroke. The previous one was already
        # committed on its own release, so there is nothing to finish here.
        if self.erasing:
            self._abort()
        self._start(scene_pos)

    def on_mouse_move_event(self, scene_pos):
        """Drives all dragging.

        ISAT only fires ``on_mouse_pressed_and_mouse_move_event`` while the
        scene's internal ``pressed`` flag is True, and that flag is set
        exclusively in the CREATE / REPAINT annotation modes. This plugin
        keeps ISAT in its normal VIEW mode, so dragging must be handled here,
        on the callback ISAT triggers on every move regardless of mode.
        """
        if not self.enabled:
            return
        self.cursor_pos = QtCore.QPointF(scene_pos)
        if not self.erasing:
            return

        if self._left_pressed:
            # Guard against a lost release leaving the flag stuck on.
            try:
                buttons = QtWidgets.QApplication.mouseButtons()
                if not (buttons & QtCore.Qt.MouseButton.LeftButton):
                    self._left_pressed = False
            except Exception:
                pass

        if not self._left_pressed:
            self._redraw_trail()
            return

        self._extend_erase(scene_pos)

    def on_mouse_pressed_and_mouse_move_event(self, scene_pos):
        """Kept for completeness; not reached in VIEW mode (see above)."""
        self.on_mouse_move_event(scene_pos)

    def on_mouse_release_event(self, scene_pos):
        """Releasing the left button commits the erase."""
        if not self.enabled:
            return
        was_left = self._left_pressed
        self._left_pressed = False
        if self._right_pressed:
            self._right_pressed = False
            return
        if was_left and self.erasing:
            self.finish(scene_pos)

    def _start(self, scene_pos):
        scene = getattr(self.mainwindow, "scene", None)
        if scene is None or scene.image_item is None:
            return
        if self._canvas_size() is None:
            return

        target = self._polygon_at(scene_pos)
        if target is None:
            self.mainwindow.statusbar.showMessage(
                "Eraser: press directly on an annotation to erase it.", 3000
            )
            return

        if not self._build_mask(target):
            return

        self.target = target
        self.erasing = True
        self.points = [QtCore.QPointF(scene_pos)]
        self._backups = []
        self._undo_len = []

        self.trail_item = QtWidgets.QGraphicsPathItem()
        pen = QtGui.QPen(QtGui.QColor(255, 64, 64, 180),
                         max(1.0, self.erase_size))
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        self.trail_item.setPen(pen)
        self.trail_item.setZValue(1e6)
        scene.addItem(self.trail_item)
        self._redraw_trail()

        self._arm()
        self._install_cursor_item()
        self.mainwindow.statusbar.showMessage(
            "Eraser: erasing... release the button to apply.", 2000
        )

    def _abort(self):
        """Throw away anything in progress without touching any annotation."""
        self.erasing = False
        self.points = None
        self.target = None
        self.cursor_pos = None
        self._left_pressed = False
        self._right_pressed = False
        self._clear_trail()
        self.mask = None
        self._roi = None
        self._backups = None
        self._undo_len = None
        self._clear_cursor_item()
        if self.mainwindow is not None:
            self._set_os_cursor(QtCore.Qt.CursorShape.ArrowCursor)

    def finish(self, scene_pos=None):
        """Replace the edited annotation with whatever survived the erase."""
        if not self.erasing:
            return
        self._finish_erase()

    # ------------------------------------------------------------------ #
    # Erase geometry
    # ------------------------------------------------------------------ #
    def _redraw_trail(self):
        if self.trail_item is None or not self.points:
            return
        path = QtGui.QPainterPath()
        path.moveTo(self.points[0])
        for p in self.points[1:]:
            path.lineTo(p)
        if self.cursor_pos is not None and self._left_pressed:
            path.lineTo(self.cursor_pos)
        self.trail_item.setPath(path)

    def _clear_trail(self):
        if self.trail_item is not None:
            try:
                self.mainwindow.scene.removeItem(self.trail_item)
            except Exception:
                pass
        self.trail_item = None

    def _extend_erase(self, scene_pos):
        p = QtCore.QPointF(scene_pos)
        if not self.points:
            self.points = [p]
            self._redraw_trail()
            return
        last = self.points[-1]
        gap = math.hypot(last.x() - p.x(), last.y() - p.y())
        if gap < _MIN_SEGMENT:
            self._redraw_trail()
            return

        # Walk the segment in steps no longer than the eraser diameter. Qt
        # coalesces fast mouse movement into sparse move events, so a single
        # jump can be longer than the eraser is wide; painting only the
        # straight line between two distant samples would then leave a row of
        # unerased scallops along the stroke.
        step = max(_MIN_SEGMENT, self._cursor_radius())
        steps = int(math.ceil(gap / step))
        # One snapshot per mouse-move event, taken before anything is painted.
        if self._backups is not None and len(self._backups) < _MAX_UNDO:
            self._backups.append(self.mask.copy())
            self._undo_len.append(len(self.points))
        for i in range(1, steps + 1):
            q = QtCore.QPointF(
                last.x() + (p.x() - last.x()) * i / steps,
                last.y() + (p.y() - last.y()) * i / steps,
            )
            self._paint_segment(self.points[-1], q)
            self.points.append(q)
        self._redraw_trail()

    def _finish_erase(self):
        mask = self.mask
        target = self.target
        roi = self._roi

        self.erasing = False
        self.points = None
        self.target = None
        self.cursor_pos = None
        self._left_pressed = False
        self._right_pressed = False
        self._clear_trail()
        self.mask = None
        self._roi = None
        self._backups = None
        self._undo_len = None
        self._set_os_cursor(QtCore.Qt.CursorShape.ArrowCursor)

        if mask is None or target is None:
            return

        # The original annotation goes away either way: if something is left it
        # is re-created from the remaining mask, otherwise it stays deleted.
        removed = self._remove_polygon(target)

        if mask is None or not mask.any() or not removed:
            if not removed:
                # Nothing was changed, so put the annotation back as it was.
                self._restore_polygon(target)
                self.mainwindow.statusbar.showMessage(
                    "Eraser: nothing changed.", 3000
                )
            else:
                self.mainwindow.statusbar.showMessage(
                    "Eraser: annotation removed.", 3000
                )
            return

        rings = self._mask_to_rings(mask, roi)
        if not rings:
            self.mainwindow.statusbar.showMessage(
                "Eraser: annotation removed (nothing measurable left).", 3000
            )
            return

        for coords in rings:
            self._create_polygon(coords, source=target)
        self.mainwindow.statusbar.showMessage(
            "Eraser: annotation updated -> {} region(s).".format(len(rings)),
            3000,
        )

    # ------------------------------------------------------------------ #
    # Annotation surgery
    # ------------------------------------------------------------------ #
    def _remove_polygon(self, polygon):
        """Delete a polygon the same way ISAT's own delete does.

        The order matters: drop it from the selection list, from
        ``mainwindow.polygons``, from the annotation dock, then ``delete()`` it
        (which also removes its vertex items) and take it off the scene.
        Skipping the dock leaves a ghost entry in the annotation list, and
        skipping ``delete()`` strands vertex items on the canvas.

        Returns ``True`` when the polygon was really removed.
        """
        scene = getattr(self.mainwindow, "scene", None)
        removed = False
        if scene is not None:
            try:
                if polygon in scene.selected_polygons_list:
                    scene.selected_polygons_list.remove(polygon)
            except Exception:
                pass
        try:
            if polygon in self.mainwindow.polygons:
                self.mainwindow.polygons.remove(polygon)
                removed = True
        except Exception:
            pass
        try:
            self.mainwindow.annos_dock_widget.listwidget_remove_polygon(polygon)
        except Exception:
            pass
        try:
            polygon.delete()
        except Exception:
            pass
        if scene is not None:
            try:
                scene.removeItem(polygon)
            except Exception:
                pass
        try:
            self.mainwindow.set_saved_state(False)
        except Exception:
            pass
        return removed

    def _restore_polygon(self, polygon):
        """Put a polygon we decided not to touch back on the canvas."""
        try:
            if polygon not in self.mainwindow.polygons:
                self.mainwindow.polygons.append(polygon)
            self.mainwindow.scene.addItem(polygon)
            self.mainwindow.annos_dock_widget.listwidget_add_polygon(polygon)
        except Exception:
            pass

    def _create_polygon(self, coords, source=None):
        """Create a polygon, inheriting the edited annotation's identity.

        When ``source`` is given, the new polygon keeps the source's category,
        group, colour, note and crowd flag, and the layer it was drawn on.
        Erasing must not silently re-classify an annotation, and it must not
        advance ISAT's auto-incrementing group counter either.
        """
        scene = self.mainwindow.scene
        mw = self.mainwindow

        polygon = Polygon()
        polygon.hover_alpha = int(
            mw.cfg["software"]["polygon_alpha_hover"] * 255
        )
        polygon.nohover_alpha = int(
            mw.cfg["software"]["polygon_alpha_no_hover"] * 255
        )
        # The item must be on the scene before the first addPoint: ISAT's
        # addPoint() reaches through self.scene() to reach mainwindow, and
        # scene() is None while the item is parentless.
        scene.addItem(polygon)

        max_x = max(0.1, scene.width() - 1)
        max_y = max(0.1, scene.height() - 1)
        for x, y in coords:
            polygon.addPoint(
                QtCore.QPointF(
                    min(max(0.1, x), max_x), min(max(0.1, y), max_y)
                )
            )
        polygon.redraw()

        if source is not None:
            category = source.category
            group = source.group
            color = QtGui.QColor(source.color)
            color.setAlpha(255)
            note = source.note
            iscrowd = source.iscrowd
            try:
                layer = int(source.zValue())
            except Exception:
                layer = len(mw.polygons) + 1
        else:
            category = mw.current_category
            group = mw.current_group
            color = QtGui.QColor(
                mw.category_color_dict.get(category, "#6F737A")
            )
            note = ""
            iscrowd = False
            layer = len(mw.polygons) + 1
            if getattr(mw, "group_select_mode", "auto") == "auto":
                mw.current_group += 1
                try:
                    mw.categories_dock_widget.lineEdit_currentGroup.setText(
                        str(mw.current_group)
                    )
                except Exception:
                    pass

        polygon.set_drawed(category, group, iscrowd, note, color, layer)
        # set_drawed leaves the brush at nohover_alpha; start from the polygon
        # colour so the remainder looks like the annotation it came from.
        brush_color = polygon.color
        brush_color.setAlpha(polygon.hover_alpha)
        polygon.setBrush(QtGui.QBrush(brush_color))

        mw.polygons.append(polygon)
        try:
            mw.annos_dock_widget.listwidget_add_polygon(polygon)
        except Exception:
            pass
        try:
            mw.set_saved_state(False)
        except Exception:
            pass
        try:
            mw.plugin_manager_dialog.trigger_after_annotation_created()
        except Exception:
            pass
        return polygon

    # ------------------------------------------------------------------ #
    # Mask -> polygon rings
    # ------------------------------------------------------------------ #
    @staticmethod
    def _mask_to_rings(mask, roi):
        """Extract simplified scene-coordinate rings from a binary mask.

        ``roi`` is the mask's ``(x0, y0)`` scene offset, added back to every
        extracted coordinate. Holes are dealt with by cutting a channel to the
        boundary, because an ISAT polygon is a single ring.
        """
        x_off, y_off = roi if roi is not None else (0, 0)
        groups = EraserPlugin._mask_to_components(mask)
        rings = []
        for exterior, holes in groups:
            ring = EraserPlugin._close_holes(exterior, holes)
            if ring is not None:
                rings.append(
                    [(x + x_off, y + y_off) for x, y in ring]
                )
        return rings

    @staticmethod
    def _mask_to_components(mask):
        """Split a mask into ``(exterior, [holes])`` groups of scene-local pts.

        OpenCV is the primary path. The fallback unions horizontal runs with
        shapely, which yields geometries without holes -- acceptable, because
        it is only reached when OpenCV is unavailable.
        """
        try:
            import cv2
        except Exception:
            return EraserPlugin._mask_to_components_fallback(mask)

        binary = (mask > 0).astype(np.uint8) * 255
        contours, hierarchy = cv2.findContours(
            binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE
        )
        if not contours or hierarchy is None:
            return []

        groups = []
        # hierarchy is [next, previous, first_child, parent] per contour.
        for i, contour in enumerate(contours):
            if hierarchy[0][i][3] != -1:
                continue  # this contour is somebody's hole
            if abs(cv2.contourArea(contour)) < _MIN_CONTOUR_AREA:
                continue
            exterior = EraserPlugin._approx(contour, cv2)
            if exterior is None:
                continue
            holes = []
            child = hierarchy[0][i][2]
            while child != -1:
                if abs(cv2.contourArea(contours[child])) >= _MIN_CONTOUR_AREA:
                    hole = EraserPlugin._approx(contours[child], cv2)
                    if hole is not None:
                        holes.append(hole)
                child = hierarchy[0][child][0]
            groups.append((exterior, holes))
        return groups

    @staticmethod
    def _approx(contour, cv2):
        perimeter = cv2.arcLength(contour, True)
        eps = max(0.5, perimeter * _APPROX_EPS_RATIO)
        pts = cv2.approxPolyDP(contour, eps, True).reshape(-1, 2)
        if len(pts) < 3:
            return None
        return [(float(x), float(y)) for x, y in pts]

    @staticmethod
    def _mask_to_components_fallback(mask):
        """Used only when OpenCV is missing: union row runs with shapely."""
        try:
            from shapely.geometry import box
            from shapely.ops import unary_union
        except Exception:
            return []
        h, _ = mask.shape
        boxes = []
        for y in range(h):
            row = mask[y] > 0
            if not row.any():
                continue
            idx = np.flatnonzero(
                np.diff(np.concatenate(([0], row.view(np.int8), [0])))
            )
            for start, end in zip(idx[::2], idx[1::2]):
                if end - start >= 1:
                    boxes.append(
                        box(float(start), float(y), float(end), float(y + 1))
                    )
        if not boxes:
            return []
        merged = unary_union(boxes)
        groups = []
        for geom in getattr(merged, "geoms", [merged]):
            if geom.geom_type != "Polygon":
                continue
            coords = [(float(x), float(y))
                      for x, y in geom.exterior.coords[:-1]]
            if len(coords) >= 3:
                groups.append((coords, []))
        return groups

    @staticmethod
    def _close_holes(exterior, holes):
        """Turn a polygon with holes into one ring ISAT can store.

        ISAT polygons are a single vertex list with no hole support, so a hole
        would have to be filled back in -- making an erase in the middle of a
        region look like it did nothing. Instead cut a narrow channel from each
        hole to the outside, which turns an annulus into a C-shape that a single
        ring represents exactly.

        Returns the ring, or ``None`` if the result is degenerate.
        """
        if not holes:
            return exterior
        try:
            from shapely.geometry import LineString, Polygon as ShpPolygon
        except Exception:
            # No shapely: keep the exterior and accept the filled-back hole.
            return exterior
        try:
            poly = ShpPolygon(exterior)
            if not poly.is_valid:
                poly = poly.buffer(0)
            half = max(0.5, _HOLE_CHANNEL_WIDTH / 2.0)
            for hole in holes:
                hp = ShpPolygon(hole)
                if not hp.is_valid:
                    hp = hp.buffer(0)
                if hp.is_empty:
                    continue
                inner = hp.representative_point()
                # Shortest way out: aim at the nearest point on the exterior.
                nearest = _nearest_on_ring(exterior, (inner.x, inner.y))
                if nearest is None:
                    continue
                channel = LineString(
                    [(inner.x, inner.y), nearest]
                ).buffer(half, cap_style=_CAP_ROUND, join_style=_JOIN_ROUND)
                poly = poly.difference(channel)
            if poly.is_empty:
                return None
            if poly.geom_type == "MultiPolygon":
                # The channels split the region; take the largest piece and let
                # the others go, rather than emitting a shape ISAT cannot hold.
                poly = max(poly.geoms, key=lambda g: g.area)
            if poly.geom_type != "Polygon":
                return None
            coords = EraserPlugin._simplify(
                list(poly.exterior.coords), tolerance=0.75
            )
            if len(coords) < 3:
                return None
            return coords[:-1] if coords[0] == coords[-1] else coords
        except Exception:
            return exterior

    @staticmethod
    def _simplify(coords, tolerance):
        """Douglas-Peucker simplification, preserving the ring closure."""
        pts = list(coords)
        if len(pts) < 4:
            return pts
        closed = pts[0] == pts[-1]
        ring = pts[:-1] if closed else pts
        keep = [False] * len(ring)
        keep[0] = True

        def dp(lo, hi):
            if hi <= lo + 1:
                return
            (x1, y1), (x2, y2) = ring[lo], ring[hi]
            dx, dy = x2 - x1, y2 - y1
            seg = math.hypot(dx, dy)
            best_i, best_d = -1, -1.0
            for i in range(lo + 1, hi):
                px, py = ring[i]
                if seg < 1e-12:
                    d = math.hypot(px - x1, py - y1)
                else:
                    d = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / seg
                if d > best_d:
                    best_d, best_i = d, i
            if best_d > tolerance and best_i > 0:
                keep[best_i] = True
                dp(lo, best_i)
                dp(best_i, hi)

        dp(0, len(ring) - 1)
        out = [p for p, k in zip(ring, keep) if k]
        if len(out) < 3:
            return pts
        if closed:
            out.append(out[0])
        return out


def _nearest_on_ring(ring, point):
    """The point on ``ring`` closest to ``point``, as ``(x, y)``.

    Used to aim the hole-cutting channel at the shortest route out.
    """
    px, py = point
    best = None
    best_d = None
    n = len(ring)
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        seg_sq = dx * dx + dy * dy
        if seg_sq < 1e-12:
            t = 0.0
        else:
            t = ((px - ax) * dx + (py - ay) * dy) / seg_sq
            t = max(0.0, min(1.0, t))
        qx, qy = ax + t * dx, ay + t * dy
        d = (qx - px) ** 2 + (qy - py) ** 2
        if best_d is None or d < best_d:
            best_d = d
            best = (qx, qy)
    return best
