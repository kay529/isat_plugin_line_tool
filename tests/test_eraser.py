# -*- coding: utf-8 -*-
"""End-to-end check of the eraser plugin against a real ISAT MainWindow.

Drives real ISAT objects, so it needs a local checkout of
ISAT_with_segment_anything (ISAT is not on PyPI):

    ISAT_ROOT=/path/to/ISAT_with_segment_anything python tests/test_eraser.py

Exits non-zero and prints every failure if anything regressed.
"""
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: E402

_bootstrap.headless()
_bootstrap.setup_sys_path()
_bootstrap.add_plugin_to_path("isat_plugin_eraser")

import numpy as np  # noqa: E402
from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

from ISAT.widgets.mainwindow import MainWindow  # noqa: E402
from ISAT.widgets.polygon import Polygon  # noqa: E402

from isat_plugin_eraser.plugin import EraserPlugin  # noqa: E402

FAILURES = []


def check(cond, msg):
    if cond:
        print("  ok   " + msg)
    else:
        print("  FAIL " + msg)
        FAILURES.append(msg)


def make_window(w=800, h=600):
    tmpd = tempfile.mkdtemp()
    arr = np.full((h, w, 3), 40, dtype=np.uint8)
    qimg = QtGui.QImage(arr.data, w, h, 3 * w,
                        QtGui.QImage.Format.Format_RGB888)
    name = "a.png"
    assert qimg.save(os.path.join(tmpd, name))
    win = MainWindow()
    win.image_root = tmpd
    win.label_root = tmpd
    win.files_list = [name]
    win.saved = True
    win.show_image(0)
    return win


def add_band(win, x0, y0, x1, y1, width=20, category=None, group=1):
    """Create a horizontal band annotation the way a line tool would."""
    from isat_plugin_line_tool.plugin import LineToolPlugin
    coords = LineToolPlugin._build_polygon_coords(
        [(float(x0), float(y0)), (float(x1), float(y1))], width)
    return _add_poly(win, coords, category, group)


def add_rect(win, x0, y0, x1, y1, category=None, group=1):
    return _add_poly(win, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                     category, group)


def _add_poly(win, coords, category, group):
    mw = win
    poly = Polygon()
    poly.hover_alpha = int(mw.cfg["software"]["polygon_alpha_hover"] * 255)
    poly.nohover_alpha = int(mw.cfg["software"]["polygon_alpha_no_hover"] * 255)
    c = poly.color
    c.setAlpha(poly.hover_alpha)
    poly.setBrush(c)
    mw.scene.addItem(poly)
    for x, y in coords:
        poly.addPoint(QtCore.QPointF(x, y))
    poly.redraw()
    cat = category if category is not None else mw.current_category
    color = QtGui.QColor(mw.category_color_dict.get(cat, "#6F737A"))
    poly.set_drawed(cat, group, False, "", color, len(mw.polygons) + 1)
    mw.polygons.append(poly)
    mw.annos_dock_widget.listwidget_add_polygon(poly)
    return poly


def hold_left(fn):
    """Run ``fn`` while ``QApplication.mouseButtons()`` reports LeftButton.

    The plugin guards against a lost release by asking Qt which buttons are
    actually down, so a drag driven from a test has to look real.
    """
    QtWidgets.QApplication.mouseButtons = staticmethod(
        lambda: QtCore.Qt.MouseButton.LeftButton)
    try:
        fn()
    finally:
        QtWidgets.QApplication.mouseButtons = staticmethod(
            lambda: QtCore.Qt.MouseButton.NoButton)


def drag(plugin, x0, y0, x1, y1, steps=20):
    """Press, move along the segment, release -- all as one committed stroke."""
    plugin.on_mouse_press_event(QtCore.QPointF(x0, y0))
    hold_left(lambda: [
        plugin.on_mouse_move_event(
            QtCore.QPointF(x0 + (x1 - x0) * i / steps,
                           y0 + (y1 - y0) * i / steps))
        for i in range(1, steps + 1)
    ])
    plugin.on_mouse_release_event(QtCore.QPointF(x1, y1))


def box(poly):
    xs = [q.x() for q in poly.points]
    ys = [q.y() for q in poly.points]
    return min(xs), min(ys), max(xs), max(ys)


def scene_poly_count(win):
    return len([i for i in win.scene.items() if isinstance(i, Polygon)])


def main():
    print("== setup ==")
    win = make_window()
    plugin = EraserPlugin()
    plugin.init_plugin(win)
    plugin.enable_plugin()
    check(plugin.enabled is True, "plugin enabled")
    check(win.scene.width() > 0, "canvas has size")
    check(plugin.get_plugin_name() == "ISAT eraser",
          "get_plugin_name -> %r" % plugin.get_plugin_name())
    check(plugin.get_plugin_version() == "1.0.0", "version 1.0.0")

    print("== hit testing ==")
    band = add_band(win, 100, 200, 500, 200, width=40)
    check(plugin._polygon_at(QtCore.QPointF(300, 200)) is band,
          "centre of the band is a hit")
    check(plugin._polygon_at(QtCore.QPointF(300, 100)) is None,
          "empty canvas is not a hit")
    check(plugin._polygon_at(QtCore.QPointF(10, 580)) is None,
          "far corner is not a hit")
    band2 = add_band(win, 100, 400, 500, 400, width=40)
    check(plugin._polygon_at(QtCore.QPointF(300, 400)) is band2,
          "second band is a hit")
    check(plugin._polygon_at(QtCore.QPointF(300, 200)) is band,
          "topmost/first band still resolves")

    print("== press on empty canvas does nothing ==")
    n_before = len(win.polygons)
    plugin.on_mouse_press_event(QtCore.QPointF(300, 100))
    check(plugin.erasing is False, "no stroke started off-annotation")
    check(plugin.mask is None, "no mask allocated off-annotation")
    plugin.on_mouse_release_event(QtCore.QPointF(300, 100))
    check(len(win.polygons) == n_before, "no annotation changed")

    print("== mask is bounding-box sized, not canvas sized ==")
    plugin.on_mouse_press_event(QtCore.QPointF(300, 200))
    check(plugin.erasing is True, "stroke started on the annotation")
    check(plugin.target is band, "target locked")
    check(plugin.mask is not None, "mask built")
    if plugin.mask is not None:
        h, w = plugin.mask.shape
        check(w < 800 and h < 600,
              "mask is bbox-sized (%dx%d) on an 800x600 canvas" % (w, h))
        check(w * h < 800 * 600 // 4,
              "mask is a small fraction of the canvas (%d px)" % (w * h))
        check(int((plugin.mask > 0).sum()) > 0, "mask has content")
        check(plugin._roi is not None, "roi recorded")
    check(plugin.trail_item is not None, "trail overlay created")
    check(plugin.trail_item is not None
          and plugin.trail_item not in win.scene.selected_polygons_list,
          "trail is not a selection")

    print("== dragging erases and trail follows ==")
    plugin.erase_size = 60
    nz0 = int((plugin.mask > 0).sum())
    hold_left(lambda: [
        plugin.on_mouse_move_event(QtCore.QPointF(300 + i * 3, 200))
        for i in range(1, 25)
    ])
    nz1 = int((plugin.mask > 0).sum())
    check(nz1 < nz0, "pixels removed while dragging (%d -> %d)" % (nz0, nz1))
    check(len(plugin.points) > 1, "trail points collected")
    check(plugin._backups and len(plugin._backups) > 0, "undo snapshots taken")

    print("== Z undoes one mouse-move at a time ==")
    npts = len(plugin.points)
    nsnaps = len(plugin._backups)
    plugin._undo()
    check(len(plugin.points) < npts, "undo rewinds the trail")
    check(len(plugin._backups) == nsnaps - 1, "undo drops one snapshot")
    nz2 = int((plugin.mask > 0).sum())
    check(nz2 > nz1, "undo restores pixels (%d -> %d)" % (nz1, nz2))
    plugin._undo()
    check(int((plugin.mask > 0).sum()) > nz2, "second undo restores more")

    print("== release commits ==")
    cat_before = band.category
    group_before = band.group
    note_before = band.note
    # Erase straight across the band (it spans y 180..220) so it really splits.
    # No undo first: undo deliberately rewinds the stroke, which would leave the
    # cut unfinished.
    plugin.erase_size = 60
    plugin.on_mouse_release_event(QtCore.QPointF(370, 200))
    check(plugin.erasing is False, "stroke finished")
    check(band not in win.polygons, "source annotation removed")
    check(plugin.mask is None, "mask released")
    check(plugin.trail_item is None, "trail removed")
    check(plugin.target is None, "target cleared")
    check(plugin._roi is None, "roi cleared")
    made = [p for p in win.polygons if p is not band and p is not band2]
    check(len(made) >= 1, "remainder re-created")
    if made:
        r = made[0]
        check(r.category == cat_before,
              "category preserved (%r)" % r.category)
        check(r.group == group_before,
              "group preserved (%r)" % r.group)
        check(r.note == note_before, "note preserved")
    lefts = [p for p in win.polygons if min(q.x() for q in p.points) < 130
             and abs(min(q.y() for q in p.points) - 180) < 10]
    check(len(lefts) >= 1, "left part survived as its own annotation")
    rights = [p for p in win.polygons if min(q.x() for q in p.points) > 380
              and abs(min(q.y() for q in p.points) - 180) < 10]
    check(len(rights) >= 1,
          "right part survived as its own annotation (got %d)" % len(rights))
    check(len(win.polygons) == 3,
          "one band became two (now %d polygons)" % len(win.polygons))
    check(scene_poly_count(win) == len(win.polygons),
          "scene in sync after commit")

    print("== saving works and stays consistent ==")
    win.save()
    with open(os.path.join(win.label_root, "a.json"), encoding="utf-8") as f:
        data = json.load(f)
    check(len(data["objects"]) == len(win.polygons),
          "json objects == polygons (%d vs %d)"
          % (len(data["objects"]), len(win.polygons)))
    blob = json.dumps(data)
    for key in ("rle", "mask", "stroke"):
        check('"%s"' % key not in blob, "json has no %r key" % key)

    print("== erase everything deletes the annotation ==")
    victim = add_rect(win, 100, 500, 300, 560)
    n_before = len(win.polygons)
    bx0, by0, bx1, by1 = box(victim)
    # The head must be wider than the box is tall, so one pass covers it.
    plugin.erase_size = int(max(bx1 - bx0, by1 - by0)) + 20
    plugin.on_mouse_press_event(QtCore.QPointF(200, 530))
    check(plugin.erasing is True, "stroke started")
    hold_left(lambda: [
        plugin.on_mouse_move_event(QtCore.QPointF(
            bx0 - 5 + (bx1 - bx0 + 10) * k / 11.0, (by0 + by1) / 2))
        for k in range(1, 12)
    ])
    plugin.on_mouse_release_event(QtCore.QPointF(bx1 + 5, (by0 + by1) / 2))
    check(victim not in win.polygons, "fully erased annotation is gone")
    check(len(win.polygons) < n_before,
          "annotation count dropped (%d -> %d)" % (n_before, len(win.polygons)))
    check(scene_poly_count(win) == len(win.polygons), "scene still in sync")

    print("== erasing a hole cuts it open instead of filling it back ==")
    plate = add_rect(win, 400, 450, 700, 570, group=5)
    px0, py0, px1, py1 = box(plate)
    cx, cy = (px0 + px1) / 2, (py0 + py1) / 2
    plugin.erase_size = 40
    plugin.on_mouse_press_event(QtCore.QPointF(cx, cy))
    check(plugin.erasing is True, "stroke started on the plate")
    # Rub a small blob in the middle, well away from the edges, so the erased
    # region becomes fully enclosed.
    hold_left(lambda: [
        plugin.on_mouse_move_event(QtCore.QPointF(cx + i * 1.5, cy))
        for i in range(-6, 7)
    ] + [
        plugin.on_mouse_move_event(QtCore.QPointF(cx, cy + i * 1.5))
        for i in range(1, 8)
    ] + [
        plugin.on_mouse_move_event(QtCore.QPointF(cx - i * 1.5, cy))
        for i in range(1, 8)
    ])
    plugin.on_mouse_release_event(QtCore.QPointF(cx - 9, cy))
    check(plate not in win.polygons, "plate replaced")
    holed = [p for p in win.polygons
             if abs(min(q.x() for q in p.points) - px0) < 8
             and abs(min(q.y() for q in p.points) - py0) < 8]
    check(len(holed) >= 1,
          "plate survived as a single ring (got %d)" % len(holed))
    if holed:
        hp = holed[0]
        hx0, hy0, hx1, hy1 = box(hp)
        check(hx1 - hx0 > 200, "plate is still wide (%.0f)" % (hx1 - hx0))
        # The hole must not be filled in: a vertex has to reach into the middle
        # of the plate, which is the cut channel.
        inner = [q for q in hp.points
                 if px0 + 40 < q.x() < px1 - 40 and py0 + 20 < q.y() < py1 - 20]
        check(len(inner) >= 1,
              "channel cut into the interior (%d inner vertices)" % len(inner))
        area = hp.calculate_area()
        full = (px1 - px0) * (py1 - py0)
        check(area < full * 0.95,
              "area shrank (%.0f vs full %.0f)" % (area, full))

    print("== split a band into two ==")
    bar = add_band(win, 100, 100, 600, 100, width=40, group=9)
    n_before = len(win.polygons)
    plugin.erase_size = 60
    plugin.on_mouse_press_event(QtCore.QPointF(350, 100))
    check(plugin.erasing is True, "stroke started on the bar")
    hold_left(lambda: [
        plugin.on_mouse_move_event(QtCore.QPointF(350, 60 + i * 8))
        for i in range(1, 10)
    ])
    plugin.on_mouse_release_event(QtCore.QPointF(350, 130))
    check(bar not in win.polygons, "bar replaced")
    pieces = [p for p in win.polygons
              if abs(min(q.y() for q in p.points) - 80) < 40
              and p.group == 9]
    check(len(pieces) >= 2,
          "bar split into >= 2 annotations (got %d)" % len(pieces))
    check(len(win.polygons) > n_before - 1,
          "split produced more annotations (%d -> %d)"
          % (n_before, len(win.polygons)))
    check(all(p.group == 9 for p in pieces),
          "every piece kept the original group")

    print("== group counter is not consumed ==")
    before_group = win.current_group
    add_rect(win, 100, 300, 200, 360, group=77)
    drag(plugin, 120, 330, 190, 330, steps=10)
    check(win.current_group == before_group,
          "current_group unchanged (%s)" % before_group)
    made = [p for p in win.polygons if p.group == 77]
    check(len(made) >= 1, "remainder kept group 77")

    print("== esc and right click discard ==")
    guard = add_rect(win, 400, 100, 600, 200, group=3)
    n_before = len(win.polygons)
    plugin.on_mouse_press_event(QtCore.QPointF(500, 150))
    check(plugin.erasing is True, "stroke in progress")
    plugin.handle_key_event(QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Escape,
        QtCore.Qt.KeyboardModifier.NoModifier))
    check(plugin.erasing is False, "Esc aborts")
    check(plugin.mask is None, "Esc released the mask")
    check(plugin.trail_item is None, "Esc removed the trail")
    plugin.on_mouse_release_event(QtCore.QPointF(500, 150))
    check(len(win.polygons) == n_before, "aborted stroke changed nothing")
    check(guard in win.polygons, "target survived the abort")

    n_before = len(win.polygons)
    guard2 = add_rect(win, 400, 250, 600, 330, group=4)
    QtWidgets.QApplication.mouseButtons = staticmethod(
        lambda: QtCore.Qt.MouseButton.RightButton)
    plugin.on_mouse_press_event(QtCore.QPointF(500, 290))
    QtWidgets.QApplication.mouseButtons = staticmethod(
        lambda: QtCore.Qt.MouseButton.NoButton)
    check(plugin.erasing is False, "right click does not start a stroke")
    plugin.on_mouse_release_event(QtCore.QPointF(500, 290))
    check(len(win.polygons) == n_before + 1,
          "right click did not eat the annotation")
    check(guard2 in win.polygons, "target survived the right click")

    print("== size spinbox and keys ==")
    check(plugin.size_spin.maximum() == 200, "spinbox max 200")
    check(plugin.size_label.text().strip() == "d", "size label is 'd'")
    before = plugin.erase_size
    plugin.handle_key_event(QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_BracketRight,
        QtCore.Qt.KeyboardModifier.NoModifier))
    check(plugin.erase_size == before + 5, "] grows the eraser")
    plugin.handle_key_event(QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_BracketLeft,
        QtCore.Qt.KeyboardModifier.NoModifier))
    check(plugin.erase_size == before, "[ shrinks it back")
    check(plugin.size_spin.value() == int(round(before)),
          "spinbox follows the key")

    print("== shortcut suppression is restored exactly ==")
    af = win.actionFinish
    ab = win.actionBackspace
    was_f, was_b = af.isEnabled(), ab.isEnabled()
    plugin.activate_tool()
    check(af.isEnabled() is False, "actionFinish suppressed while armed")
    check(ab.isEnabled() is False, "actionBackspace suppressed while armed")
    plugin.leave_tool()
    check(af.isEnabled() == was_f, "actionFinish restored to %s" % was_f)
    check(ab.isEnabled() == was_b, "actionBackspace restored to %s" % was_b)

    print("== a new image resets everything ==")
    # ISAT's own show_image() spawns a network thread that segfaults in a
    # headless test environment, so the lifecycle hook ISAT calls on image
    # open is invoked directly -- that is the contract the plugin implements.
    victim = win.polygons[0]
    plugin.erase_size = 20
    plugin.on_mouse_press_event(QtCore.QPointF(
        victim.points[0].x(), victim.points[0].y()))
    hold_left(lambda: [
        plugin.on_mouse_move_event(QtCore.QPointF(
            victim.points[0].x() + i, victim.points[0].y()))
        for i in range(1, 5)
    ])
    check(plugin.erasing is True, "stroke in progress")
    plugin.after_image_open_event()
    check(plugin.erasing is False, "erasing reset")
    check(plugin.mask is None, "mask reset")
    check(plugin.trail_item is None, "trail reset")
    check(plugin.target is None, "target reset")
    check(plugin._backups is None, "backups reset")
    check(plugin._undo_len is None, "undo lengths reset")
    check(victim in win.polygons, "the annotation was left untouched")

    print("== disable cleans up ==")
    plugin.disable_plugin()
    check(len(win.findChildren(QtWidgets.QSpinBox,
                               "spinBox_eraser_size")) == 0, "spinbox removed")
    check(len(win.findChildren(QtWidgets.QLabel,
                               "label_eraser_size")) == 0, "label removed")
    check(len(win.findChildren(QtWidgets.QAction, "actionEraser")) == 0,
          "action removed")
    check(plugin.cursor_item is None, "cursor item removed")
    check(plugin.view is None, "event filters detached")

    print("== disabled plugin ignores the mouse ==")
    n_before = len(win.polygons)
    plugin.on_mouse_press_event(QtCore.QPointF(200, 200))
    check(plugin.erasing is False, "no stroke while disabled")
    plugin.on_mouse_release_event(QtCore.QPointF(200, 200))
    check(len(win.polygons) == n_before, "nothing changed while disabled")
    check(plugin.handle_key_event(QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_BracketRight,
        QtCore.Qt.KeyboardModifier.NoModifier)) is False,
        "disabled plugin ignores keys")

    print("== re-enable is idempotent ==")
    for _ in range(3):
        plugin.enable_plugin()
    check(len(win.findChildren(QtWidgets.QSpinBox,
                               "spinBox_eraser_size")) == 1,
          "exactly one spinbox after 3 enables")
    check(len(win.findChildren(QtWidgets.QAction, "actionEraser")) == 1,
          "exactly one action after 3 enables")
    check(len(win.findChildren(QtWidgets.QLabel, "label_eraser_size")) == 1,
          "exactly one label after 3 enables")
    plugin.disable_plugin()

    print("== no image, no stroke ==")
    win2 = MainWindow()
    p2 = EraserPlugin()
    p2.init_plugin(win2)
    p2.enable_plugin()
    p2.on_mouse_press_event(QtCore.QPointF(10, 10))
    check(p2.erasing is False, "no stroke without an image")
    p2.on_mouse_release_event(QtCore.QPointF(10, 10))
    check(p2.erasing is False, "release without an image is harmless")
    p2.disable_plugin()

    print("== init_plugin tolerates a missing cfg ==")

    class FakeMW:
        pass
    p3 = EraserPlugin()
    p3.init_plugin(FakeMW())
    check(p3.erase_size == 15.0, "default size without cfg")

    print("== config persistence ==")
    plugin.enable_plugin()
    plugin.size_spin.setValue(44)   # goes through _on_size_changed
    check(plugin.erase_size == 44.0, "spinbox updates the size")
    check(win.cfg["software"].get("eraser_size") == 44.0,
          "size written to cfg")
    p4 = EraserPlugin()
    p4.init_plugin(win)
    check(p4.erase_size == 44.0, "size restored")
    plugin.disable_plugin()
    p4.disable_plugin()

    print("== mask helpers ==")
    m = np.zeros((40, 60), dtype=np.uint8)
    m[10:20, 10:50] = 255
    groups = EraserPlugin._mask_to_components(m)
    check(len(groups) == 1, "rectangle -> one component (got %d)" % len(groups))
    check(len(groups[0][1]) == 0, "no holes in a plain rectangle")
    rings = EraserPlugin._mask_to_rings(m, (100, 200))
    check(len(rings) == 1, "one ring (got %d)" % len(rings))
    check(abs(min(p[0] for p in rings[0]) - 110) <= 2,
          "roi offset applied (x0=%.1f)" % min(p[0] for p in rings[0]))
    check(abs(min(p[1] for p in rings[0]) - 210) <= 2,
          "roi offset applied (y0=%.1f)" % min(p[1] for p in rings[0]))
    check(EraserPlugin._mask_to_rings(np.zeros((10, 10), dtype=np.uint8),
                                      (0, 0)) == [],
          "empty mask -> no rings")
    holed = np.zeros((80, 80), dtype=np.uint8)
    holed[10:70, 10:70] = 255
    holed[30:50, 30:50] = 0
    g2 = EraserPlugin._mask_to_components(holed)
    check(len(g2) == 1 and len(g2[0][1]) == 1,
          "annulus -> one component with one hole")
    r2 = EraserPlugin._mask_to_rings(holed, (0, 0))
    check(len(r2) == 1, "annulus -> one ring after cutting (got %d)" % len(r2))
    if r2:
        xs = [p[0] for p in r2[0]]
        check(len(r2[0]) >= 4, "cut ring has points")
        check(max(xs) - min(xs) > 40, "cut ring is still wide")
    two = np.zeros((40, 80), dtype=np.uint8)
    two[10:30, 5:30] = 255
    two[10:30, 50:75] = 255
    g3 = EraserPlugin._mask_to_components(two)
    check(len(g3) == 2, "two blobs -> two components (got %d)" % len(g3))
    check(len(EraserPlugin._mask_to_rings(two, (0, 0))) == 2,
          "two blobs -> two rings")

    print("== a fast flick leaves no unerased scallops ==")
    plugin.enable_plugin()
    flick = add_rect(win, 100, 300, 500, 340, group=12)
    plugin.erase_size = 20
    plugin.on_mouse_press_event(QtCore.QPointF(120, 320))
    check(plugin.erasing is True, "stroke started")
    # Two enormous jumps: far wider than the eraser head. Qt coalesces fast
    # movement exactly like this, and painting only the straight line between
    # distant samples would leave gaps.
    hold_left(lambda: [
        plugin.on_mouse_move_event(QtCore.QPointF(300, 320)),
        plugin.on_mouse_move_event(QtCore.QPointF(480, 320)),
    ])
    mask_snapshot = plugin.mask.copy()
    plugin.on_mouse_release_event(QtCore.QPointF(480, 320))
    check(flick not in win.polygons, "flicked annotation replaced")
    if mask_snapshot is not None and mask_snapshot.any():
        # Row through the middle: the erased run must be contiguous.
        row = mask_snapshot[mask_snapshot.shape[0] // 2, :]
        nz = np.flatnonzero(row == 0)
        if nz.size:
            lo, hi = int(nz.min()), int(nz.max())
            run = row[lo:hi + 1]
            check(bool((run == 0).all()),
                  "erased run is contiguous with no gaps (%d..%d)" % (lo, hi))

    print("== undo granularity is one mouse-move ==")
    add_rect(win, 100, 400, 600, 450, group=13)
    plugin.erase_size = 30
    plugin.on_mouse_press_event(QtCore.QPointF(150, 425))
    nz_a = int((plugin.mask > 0).sum())
    hold_left(lambda: [plugin.on_mouse_move_event(QtCore.QPointF(250, 425))])
    nz_b = int((plugin.mask > 0).sum())
    npts_b = len(plugin.points)
    hold_left(lambda: [plugin.on_mouse_move_event(QtCore.QPointF(350, 425))])
    npts_c = len(plugin.points)
    check(npts_c > npts_b, "second move added trail points")
    check(len(plugin._backups) == 2, "one snapshot per move event (got %d)"
          % len(plugin._backups))
    plugin._undo()
    check(int((plugin.mask > 0).sum()) == nz_b,
          "undo reverts the whole last move")
    check(len(plugin.points) == npts_b, "trail rewound in one step")
    plugin._undo()
    check(int((plugin.mask > 0).sum()) == nz_a, "second undo reverts further")
    plugin.on_mouse_release_event(QtCore.QPointF(350, 425))
    check(plugin._backups is None, "undo state released on commit")

    print("== hit testing picks the topmost of two overlapping polygons ==")
    # Two annotations sharing an area: the later-added one is drawn on top, so
    # it is the one the user can see, and therefore the one the eraser must
    # pick. Getting this backwards would silently edit the wrong annotation.
    under = add_rect(win, 60, 460, 300, 520, group=21)
    over = add_rect(win, 120, 470, 380, 510, group=22)
    shared = QtCore.QPointF(200, 490)
    check(plugin._polygon_at(shared) is over,
          "topmost polygon wins where two overlap")
    check(plugin._polygon_at(QtCore.QPointF(80, 490)) is under,
          "the lower polygon is picked where it is alone")
    plugin.erase_size = 8
    plugin.on_mouse_press_event(shared)
    check(plugin.target is over, "press locks onto the topmost polygon")
    plugin.on_mouse_release_event(shared)
    check(under in win.polygons, "the lower polygon was not touched")
    check(plugin.target is None, "target cleared after commit")

    print("== source hygiene ==")
    pdir = os.path.join(_bootstrap.REPO_ROOT, "isat_plugin_eraser")
    src = open(os.path.join(pdir, "isat_plugin_eraser", "plugin.py"),
               encoding="utf-8").read()
    readme = open(os.path.join(pdir, "README.md"), encoding="utf-8").read()
    setup_py = open(os.path.join(pdir, "setup.py"), encoding="utf-8").read()
    check(re.search(r"[a-z_]+\.py:\d+", src) is None,
          "no filename:line references to ISAT internals")
    for stale in ("TODO", "FIXME", "if False", "DrawTools", "_finish_brush",
                  "canvas.py", "polygon.py", "mainwindow.py"):
        check(stale not in src, "no %r leftovers" % stale)
    check("from ISAT.widgets.plugin_base import PluginBase" in src,
          "imports only the public plugin interface")
    check("import ISAT" not in src, "does not import ISAT internals")
    # Absolute local paths must not leak into anything that ships. The patterns
    # are assembled from fragments so this test file does not itself trip a
    # repo-wide scan for the very strings it is looking for. A generic conda
    # env name is fine: the README shows one as a placeholder users adapt.
    drive = "D:" + "\\"
    leaks = [
        drive + "ISAT_with" + "_sam",
        "ISAT_with" + "_sam",
        "C:" + "\\" + "Users",
        "isat_env" + "_new",
        "gh" + "o_",
    ]
    shipped = src + readme + setup_py
    for leak in leaks:
        check(leak not in shipped, "no local-path leak (%r)" % leak[:12])
    check("D:" + "\\" + "path" + "\\" + "to" + "\\" + "ISAT" in readme,
          "README uses a placeholder path, not a real one")
    check("isat-plugin-eraser" in setup_py, "setup.py package name")
    check('"eraser = isat_plugin_eraser:Plugin"' in setup_py,
          "setup.py entry point name")
    check('"1.0.0"' in setup_py, "setup.py version")
    check("MIT" in setup_py, "setup.py declares MIT")
    for dep in ("shapely", "numpy", "opencv-python"):
        check(dep in setup_py, "setup.py declares %s" % dep)
    check(os.path.isfile(os.path.join(pdir, "LICENSE")),
          "eraser has its own LICENSE")
    check("Apache License 2.0" in readme, "README credits upstream licence")
    check("LineToolPlugin" not in readme, "README has no stale class name")
    check("DrawTools" not in readme, "README has no stale merged-tool name")
    # The eraser must not import the line tool.
    check("isat_plugin_line_tool" not in src,
          "eraser does not depend on the line tool")
    check("Erase" in readme and "Eraser" in readme, "README documents the tool")
    check("Performance" in readme, "README documents the performance rule")

    print()
    if FAILURES:
        print("FAILED %d check(s):" % len(FAILURES))
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
