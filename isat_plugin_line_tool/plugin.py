# -*- coding: utf-8 -*-
"""ISAT line tool plugin -- drag a centerline, get a fixed-width polygon.

Interaction -- hold to draw, release to commit
----------------------------------------------
Exactly like a paint program (and like Label Studio's brush):

=====================================  ==================================
Press the left button                  start the line at that point
Drag                                   keep extending the centerline
Release                                commit: buffer the centerline by
                                       width/2 and create ONE polygon
``Shift`` (while dragging)             snap the segment to 45 degree steps
``[`` / ``]``                          shrink / grow the width by 5 px
``Z``                                  undo: drop the last point
``Esc``                                discard the line in progress
``Esc`` (again, nothing in progress)   leave the tool, restore ISAT keys
=====================================  ==================================

One gesture = one annotation. There is no "click to start, click again to
stop" dance: you draw for as long as you hold the button, and letting go
writes the annotation. Holding, lifting and drawing again gives you a second
annotation.

A single click (press and release without moving) creates a small
round-ish dot, so you can dot-annotate without dragging.

Why this is useful
------------------
Stacked objects in microscopy / remote sensing (roads, fibres, membranes,
tubing, plumes) are painful to outline with the normal polygon tool: you
either click a dozen times per segment or you end up with a thin sliver.
Here you draw the object's spine once and the plugin expands it to the width
you want -- the same idea as ImageJ's ``Segmented Line`` with a width.

Design notes
------------
* Nothing is appended to ``mainwindow.polygons`` until the stroke is
  released, and ``MainWindow.save()`` only serializes
  ``mainwindow.polygons``. An unfinished line can therefore never leak into
  the saved json.
* The centerline is drawn as a private ``QGraphicsPathItem`` and removed
  before the real polygon is created, so the tracer is never an annotation.
* Finishing goes through the same steps ISAT itself uses, so the result is a
  first-class annotation (category, group, colour, list entry, dirty flag).

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

from PyQt5 import QtCore, QtGui, QtWidgets

from ISAT.widgets.plugin_base import PluginBase
from ISAT.widgets.polygon import Polygon


# ---------------------------------------------------------------------- #
# Constants
# ---------------------------------------------------------------------- #
# Width of the generated polygon, in px.
_MIN_LINE_WIDTH = 1
_MAX_LINE_WIDTH = 200
_DEFAULT_LINE_WIDTH = 8
_SIZE_STEP = 5

# Minimum travel (px) between sampled points while dragging. Keeps the
# centerline from collecting thousands of nearly-identical points.
_SAMPLE_DISTANCE = 3.0

# shapely buffer styles: flat cap so the polygon stops where you stopped
# drawing, round joins so corners are not chopped off.
_CAP_FLAT = 1
_JOIN_ROUND = 1

# ISAT's own single-key shortcuts that collide with this plugin's keys.
# QAction shortcuts are consumed by MainWindow before any view event filter
# can see them, so these are temporarily disabled while the tool is armed and
# restored afterwards. software.yaml is never modified.
_CLASHING_ACTIONS = (
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
    """Redraws the circular width cursor as the mouse moves."""

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


class LineToolPlugin(PluginBase):
    """One toolbar button: drag a centerline, get a fixed-width polygon."""

    def __init__(self):
        super().__init__()
        self.mainwindow = None

        self.line_width = float(_DEFAULT_LINE_WIDTH)
        """Width of the generated polygon, in px."""

        # --- stroke state ---------------------------------------------------
        self.drawing = False
        """True while the left button is held down and a line is in progress."""

        self.points = None
        """Centerline points collected so far (list of QPointF)."""

        self.cursor_pos = None
        self._left_pressed = False
        self._right_pressed = False

        # --- scene items / UI -----------------------------------------------
        self.tracer_item = None
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
            saved = mainwindow.cfg["software"].get("line_tool_width")
            if saved is not None:
                self.line_width = float(saved)
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
        return "ISAT line tool"

    def get_plugin_author(self) -> str:
        return "ISAT line tool"

    def get_plugin_version(self) -> str:
        return "1.0.0"

    def get_plugin_description(self) -> str:
        return (
            "Drag a centerline and release to turn it into a fixed-width "
            "polygon annotation -- one gesture, one annotation, just like a "
            "paint program. Shift snaps to 45 degrees, [ / ] change the "
            "width, Z undoes a point, Esc discards. The result is a normal "
            "ISAT polygon (category, group, colour, list entry)."
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
            self.tool_action.setObjectName("actionLineTool")
            self.tool_action.setText("Line")
            self.tool_action.setToolTip(
                "Line tool: hold the left button and drag to draw a line, "
                "release to create the polygon (K)"
            )
            self.tool_action.setStatusTip(
                "Hold the left button and drag to draw. Release to create a "
                "polygon of the current width. Shift snaps to 45 degrees."
            )
            self.tool_action.setShortcut(QtGui.QKeySequence("K"))
            self.tool_action.setCheckable(False)
            self.tool_action.triggered.connect(self.activate_tool)
            toolbar.addAction(self.tool_action)

            self.size_spin = QtWidgets.QSpinBox(self.mainwindow)
            self.size_spin.setObjectName("spinBox_line_tool_width")
            self.size_spin.setRange(_MIN_LINE_WIDTH, _MAX_LINE_WIDTH)
            self.size_spin.setSingleStep(_SIZE_STEP)
            self.size_spin.setSuffix(" px")
            self.size_spin.setMaximumWidth(80)
            self.size_spin.setToolTip(
                "Line width of the generated polygon ( [ / ] to change )"
            )
            self.size_spin.valueChanged.connect(self._on_size_changed)
            toolbar.addWidget(self.size_spin)

            self.size_label = QtWidgets.QLabel(" w")
            self.size_label.setObjectName("label_line_tool_width")
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
            print("LineTool: failed to install UI:", e)

    def _toolbar_actions(self):
        """Actions currently living on the toolbar we injected into."""
        if self.toolbar is None:
            return []
        try:
            return list(self.toolbar.actions())
        except Exception:
            return []

    def _forget_ui(self):
        """Drop references to widgets, without touching the toolbar."""
        self.tool_action = None
        self.size_spin = None
        self.size_label = None
        self.toolbar = None

    def _widgets_by_name(self, name):
        """Find our injected widgets by objectName.

        ISAT's plugin manager builds ``PluginManagerDialog`` twice during
        start-up, so two instances of this plugin exist and each installs its
        own button and spinbox. Tracking the widgets by name lets teardown
        clean up its own regardless of which toolbar object is current.
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
            try:
                action.deleteLater()
            except Exception:
                pass
        self.tool_action = None

        # Pull any spinbox / label with our objectName off whatever toolbar
        # still holds it, then destroy it.
        for name in ("spinBox_line_tool_width", "label_line_tool_width"):
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
        if self.size_spin is None:
            return
        blocked = self.size_spin.blockSignals(True)
        self.size_spin.setValue(int(round(self.line_width)))
        self.size_spin.blockSignals(blocked)

    def _on_size_changed(self, value):
        self.line_width = float(value)
        try:
            self.mainwindow.cfg["software"]["line_tool_width"] = float(value)
        except Exception:
            pass
        self._update_cursor_item()

    def activate_tool(self):
        """Toolbar button: arm the tool and tell the user what to do."""
        if not self.enabled:
            return
        self._arm()
        self._install_cursor_item()
        self.mainwindow.statusbar.showMessage(
            "Line tool: hold the left button and drag, release to create the "
            "polygon. Shift snaps to 45 degrees.",
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
            if self.drawing:
                self._abort()
                self._sync_ui()
                self.mainwindow.statusbar.showMessage(
                    "Line tool: discarded. Esc again leaves the tool.", 3000
                )
            elif self._armed:
                self.leave_tool()
                self.mainwindow.statusbar.showMessage(
                    "Line tool off. ISAT shortcuts restored.", 3000
                )
            return True

        return False

    def _nudge_size(self, delta):
        new = int(round(self.line_width)) + delta
        new = max(_MIN_LINE_WIDTH, min(_MAX_LINE_WIDTH, new))
        if self.size_spin is not None:
            self.size_spin.setValue(new)  # fires _on_size_changed
        else:
            self.line_width = float(new)
            self._update_cursor_item()
        self.mainwindow.statusbar.showMessage(
            "Line width: {} px".format(new), 1500
        )

    def _undo(self):
        """Z: drop the last point of a line that is in progress."""
        if not self.drawing:
            return False
        if self.points:
            self.points.pop()
            self._redraw_tracer()
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
        return max(0.5, self.line_width / 2.0)

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
            # finish, because a line is committed on release.
            self._right_pressed = True
            self._abort()
            return

        self._right_pressed = False
        self._left_pressed = True

        # Press always begins a fresh line. The previous one was already
        # committed on its own release, so there is nothing to finish here.
        if self.drawing:
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
        if not self.drawing:
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
            self._redraw_tracer()
            return

        self._extend_line(scene_pos)

    def on_mouse_pressed_and_mouse_move_event(self, scene_pos):
        """Kept for completeness; not reached in VIEW mode (see above)."""
        self.on_mouse_move_event(scene_pos)

    def on_mouse_release_event(self, scene_pos):
        """Releasing the left button commits the line -- that is the whole
        point of the tool: draw while held, let go when done."""
        if not self.enabled:
            return
        was_left = self._left_pressed
        self._left_pressed = False
        if self._right_pressed:
            self._right_pressed = False
            return
        if was_left and self.drawing:
            self.finish(scene_pos)

    def _start(self, scene_pos):
        scene = getattr(self.mainwindow, "scene", None)
        if scene is None or scene.image_item is None:
            return
        if self._canvas_size() is None:
            return
        self.drawing = True
        self.points = [QtCore.QPointF(scene_pos)]
        self.tracer_item = QtWidgets.QGraphicsPathItem()
        pen = QtGui.QPen(QtGui.QColor("#00A0FF"), 2)
        self.tracer_item.setPen(pen)
        self.tracer_item.setZValue(1e5)
        scene.addItem(self.tracer_item)
        self._redraw_tracer()

        self._arm()
        self._install_cursor_item()
        self.mainwindow.statusbar.showMessage(
            "Line tool: drawing... release the button to commit.", 2000
        )

    def _abort(self):
        """Throw away anything in progress without creating an annotation."""
        self.drawing = False
        self._left_pressed = False
        self._right_pressed = False

        if self.tracer_item is not None:
            try:
                self.mainwindow.scene.removeItem(self.tracer_item)
            except Exception:
                pass
            self.tracer_item = None
        self.points = None
        self.cursor_pos = None

        self._clear_cursor_item()
        if self.mainwindow is not None:
            self._set_os_cursor(QtCore.Qt.CursorShape.ArrowCursor)

    def finish(self, scene_pos=None):
        """Commit the centerline into a polygon annotation."""
        if not self.drawing:
            return
        self._finish_line()

    # ------------------------------------------------------------------ #
    # Line geometry
    # ------------------------------------------------------------------ #
    def _redraw_tracer(self):
        if self.tracer_item is None or not self.points:
            return
        path = QtGui.QPainterPath()
        path.moveTo(self.points[0])
        for p in self.points[1:]:
            path.lineTo(p)
        if self.cursor_pos is not None and self._left_pressed:
            path.lineTo(self.cursor_pos)
        self.tracer_item.setPath(path)

    def _extend_line(self, scene_pos):
        p = QtCore.QPointF(scene_pos)
        if self.shift_held() and self.points:
            p = self._constrain_to_angle(self.points[-1], p)
        if self.points:
            last = self.points[-1]
            if math.hypot(last.x() - p.x(), last.y() - p.y()) < _SAMPLE_DISTANCE:
                self._redraw_tracer()
                return
        else:
            self.points = []
        self.points.append(p)
        self._redraw_tracer()

    def _finish_line(self):
        pts = [(p.x(), p.y()) for p in (self.points or [])]
        self._cleanup_line()
        if not pts:
            return
        coords = self._build_polygon_coords(pts, self.line_width)
        if not coords:
            self.mainwindow.statusbar.showMessage(
                "Line tool: the line is too short to make a polygon.", 3000
            )
            return
        self._create_polygon(coords)
        self.mainwindow.statusbar.showMessage(
            "Line tool: created 1 annotation (width {} px).".format(
                int(round(self.line_width))
            ),
            3000,
        )

    def _cleanup_line(self):
        self.drawing = False
        self._left_pressed = False
        self._right_pressed = False
        if self.tracer_item is not None:
            try:
                self.mainwindow.scene.removeItem(self.tracer_item)
            except Exception:
                pass
            self.tracer_item = None
        self.points = None
        self.cursor_pos = None
        self._set_os_cursor(QtCore.Qt.CursorShape.ArrowCursor)

    @staticmethod
    def _build_polygon_coords(points, width):
        """Buffer a centerline by width/2 into polygon coordinates."""
        from shapely.geometry import LineString

        half = max(0.5, float(width) / 2.0)

        if len(points) == 1:
            # Single point -> a small octagon (a dot annotation).
            cx, cy = points[0]
            return [
                (
                    cx + half * math.cos(math.radians(a)),
                    cy + half * math.sin(math.radians(a)),
                )
                for a in range(0, 360, 45)
            ]

        buffered = LineString(points).buffer(
            half, cap_style=_CAP_FLAT, join_style=_JOIN_ROUND
        )
        if buffered.is_empty:
            return []
        if buffered.geom_type != "Polygon":
            polys = [
                g for g in getattr(buffered, "geoms", [])
                if g.geom_type == "Polygon"
            ]
            if not polys:
                return []
            buffered = max(polys, key=lambda g: g.area)

        coords = LineToolPlugin._simplify(
            list(buffered.exterior.coords), tolerance=max(0.5, half * 0.25)
        )
        return [(float(x), float(y)) for x, y in coords]

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

    def shift_held(self):
        try:
            return bool(
                QtWidgets.QApplication.keyboardModifiers()
                & QtCore.Qt.KeyboardModifier.ShiftModifier
            )
        except Exception:
            return False

    @staticmethod
    def _constrain_to_angle(last_point, current_point):
        """Snap the segment to the nearest 45 degrees, keeping its length."""
        dx = current_point.x() - last_point.x()
        dy = current_point.y() - last_point.y()
        distance = math.hypot(dx, dy)
        if distance < 1:
            return current_point
        angle_deg = math.degrees(math.atan2(dy, dx))
        if angle_deg > 180:
            angle_deg -= 360
        constrained = round(angle_deg / 45.0) * 45.0
        rad = math.radians(constrained)
        return QtCore.QPointF(
            last_point.x() + distance * math.cos(rad),
            last_point.y() + distance * math.sin(rad),
        )

    # ------------------------------------------------------------------ #
    # Annotation creation (mirrors ISAT's own polygon finish flow)
    # ------------------------------------------------------------------ #
    def _create_polygon(self, coords):
        scene = self.mainwindow.scene
        mw = self.mainwindow

        polygon = Polygon()
        polygon.hover_alpha = int(
            mw.cfg["software"]["polygon_alpha_hover"] * 255
        )
        polygon.nohover_alpha = int(
            mw.cfg["software"]["polygon_alpha_no_hover"] * 255
        )
        color = polygon.color
        color.setAlpha(polygon.hover_alpha)
        polygon.setBrush(color)
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

        category = mw.current_category
        group = mw.current_group
        polygon.set_drawed(
            category,
            group,
            False,
            "",
            QtGui.QColor(mw.category_color_dict.get(category, "#6F737A")),
            len(mw.polygons) + 1,
        )

        if getattr(mw, "group_select_mode", "auto") == "auto":
            mw.current_group += 1
            try:
                mw.categories_dock_widget.lineEdit_currentGroup.setText(
                    str(mw.current_group)
                )
            except Exception:
                pass

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
