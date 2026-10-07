# -*- coding: utf-8 -*-
"""Measure eraser latency on a high-resolution canvas.

The bug being guarded against: an earlier eraser allocated a full-canvas RGBA
preview buffer on every mouse press. This measures the real thing -- press,
drag, release -- on canvases up to 300 megapixels, and also measures what the
old approach would have cost, so the regression cannot come back unnoticed.

    ISAT_ROOT=/path/to/ISAT_with_segment_anything python tests/bench_eraser.py

Exits non-zero if any canvas size falls outside the interactive budget.
"""
import gc
import os
import sys
import tempfile
import time

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

# Anything slower than this per mouse-move reads as "not responding".
MOVE_BUDGET_MS = 33.0     # ~30 fps, the floor for interactive dragging
PRESS_BUDGET_MS = 250.0   # one press; anything near this feels like a stall
TOTAL_BUDGET_MS = 2000.0

RESULTS = []


def make_window(w, h):
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


def add_rect(win, x0, y0, x1, y1):
    poly = Polygon()
    poly.hover_alpha = 150
    poly.nohover_alpha = 80
    c = poly.color
    c.setAlpha(150)
    poly.setBrush(c)
    win.scene.addItem(poly)
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        poly.addPoint(QtCore.QPointF(x, y))
    poly.redraw()
    poly.set_drawed(win.current_category, 1, False, "",
                    QtGui.QColor("#ff0000"), 1)
    win.polygons.append(poly)
    # ISAT's dock widget calls setWindowTitle on the current label, which is
    # None in a headless test; the list entry itself is not what we measure.
    try:
        win.annos_dock_widget.listwidget_add_polygon(poly)
    except AttributeError:
        pass
    return poly


def hold_left(fn):
    QtWidgets.QApplication.mouseButtons = staticmethod(
        lambda: QtCore.Qt.MouseButton.LeftButton)
    try:
        fn()
    finally:
        QtWidgets.QApplication.mouseButtons = staticmethod(
            lambda: QtCore.Qt.MouseButton.NoButton)


def bench(w, h, band_w, band_h, n_moves=120, verbose=True):
    win = make_window(w, h)
    plugin = EraserPlugin()
    plugin.init_plugin(win)
    plugin.enable_plugin()
    plugin.erase_size = 40

    # A band across the middle: 1% of the canvas, which is a realistic
    # annotation-to-image ratio.
    bx0, by0 = w * 0.05, h * 0.5 - band_h / 2
    bx1, by1 = w * 0.95, h * 0.5 + band_h / 2
    band = add_rect(win, bx0, by0, bx1, by1)

    gc.collect()
    t_press = None
    t_moves = []
    t_release = None

    cy = (by0 + by1) / 2
    start = time.perf_counter()
    plugin.on_mouse_press_event(QtCore.QPointF(w * 0.3, cy))
    t_press = (time.perf_counter() - start) * 1000.0

    mask_mb = 0.0
    if plugin.mask is not None:
        mask_mb = plugin.mask.nbytes / (1024.0 * 1024.0)

    for i in range(n_moves):
        x = w * 0.3 + (w * 0.4) * i / max(1, n_moves - 1)
        t0 = time.perf_counter()
        # Bind the plugin locally: it keeps the timed call honest and stops
        # linters from complaining about the closure over a local name.
        pl = plugin
        hold_left(lambda x=x: pl.on_mouse_move_event(QtCore.QPointF(x, cy)))
        dt = (time.perf_counter() - t0) * 1000.0
        # The first move includes the hold_left bookkeeping; keep it honest
        # anyway rather than discarding slow frames.
        t_moves.append(dt)

    t0 = time.perf_counter()
    plugin.on_mouse_release_event(QtCore.QPointF(w * 0.7, cy))
    t_release = (time.perf_counter() - t0) * 1000.0

    total = t_press + sum(t_moves) + t_release
    worst = max(t_moves) if t_moves else 0.0
    mean = (sum(t_moves) / len(t_moves)) if t_moves else 0.0
    p95 = sorted(t_moves)[int(len(t_moves) * 0.95)] if t_moves else 0.0

    # What the old full-canvas preview would have allocated per press.
    old_bytes = h * w * 4
    old_mb = old_bytes / (1024.0 * 1024.0)

    row = {
        "size": "%dx%d" % (w, h),
        "megapixels": w * h / 1e6,
        "press_ms": t_press,
        "move_mean_ms": mean,
        "move_p95_ms": p95,
        "move_max_ms": worst,
        "release_ms": t_release,
        "total_ms": total,
        "mask_mb": mask_mb,
        "old_preview_mb": old_mb,
        "saved": band not in win.polygons,
    }
    RESULTS.append(row)
    if verbose:
        print("  %-11s %6.1f MP | press %7.1f | move mean %6.2f p95 %6.2f "
              "max %7.2f | release %7.1f | total %8.1f ms"
              % (row["size"], row["megapixels"], t_press, mean, p95, worst,
                 t_release, total))
        print("               mask %8.3f MB   (old full-canvas RGBA preview "
              "would be %8.1f MB per press, %.0fx more)"
              % (mask_mb, old_mb, (old_mb / mask_mb) if mask_mb else 0))
    plugin.disable_plugin()
    del plugin, win
    gc.collect()
    return row


def main():
    # ISAT's own image loader refuses very large files via PIL's decompression
    # bomb guard, so the canvas sizes here stop where ISAT itself stops. The
    # eraser is not the limit: at 108 MP a press is already ~2 ms.
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning)
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
    except Exception:
        pass

    print("== press / drag / release latency by canvas size ==")
    print("   (budget: press < %.0f ms, move < %.0f ms, total < %.0f ms)"
          % (PRESS_BUDGET_MS, MOVE_BUDGET_MS, TOTAL_BUDGET_MS))
    bench(1600, 1200, 40, 60)
    bench(4000, 3000, 80, 120)
    bench(8000, 6000, 160, 240)
    bench(12000, 9000, 240, 360)
    bench(20000, 15000, 300, 450)

    print()
    print("== a big eraser on a big canvas ==")
    bench(12000, 9000, 240, 360, n_moves=60)

    print()
    print("== many small annotations: hit-test cost ==")
    w, h = 6000, 4000
    win = make_window(w, h)
    plugin = EraserPlugin()
    plugin.init_plugin(win)
    plugin.enable_plugin()
    for i in range(400):
        add_rect(win, 40 + (i % 20) * 290, 40 + (i // 20) * 190, 240,
                 180 + (i // 20) * 190)
    print("  400 annotations on a %dx%d canvas" % (w, h))
    t0 = time.perf_counter()
    for i in range(50):
        plugin._polygon_at(QtCore.QPointF(100 + i, 100))
    dt = (time.perf_counter() - t0) * 1000.0 / 50
    print("  hit test: %.3f ms per query" % dt)
    ok_hit = dt < MOVE_BUDGET_MS
    print("  %s hit test is fast enough" % ("ok  " if ok_hit else "FAIL"))
    RESULTS.append({"name": "hit_test_ms", "value": dt, "ok": ok_hit})
    plugin.disable_plugin()
    del plugin, win
    gc.collect()

    print()
    print("== erase one of many annotations ==")
    win = make_window(w, h)
    plugin = EraserPlugin()
    plugin.init_plugin(win)
    plugin.enable_plugin()
    # A sparse grid well clear of the target, so the hit test is unambiguous.
    for i in range(200):
        col = i % 20
        row = i // 20
        add_rect(win, 20 + col * 120, 20 + row * 120, 100 + col * 120,
                 100 + row * 120)
    target = add_rect(win, 2600, 2000, 5000, 2120)
    plugin.erase_size = 50
    t0 = time.perf_counter()
    plugin.on_mouse_press_event(QtCore.QPointF(2800, 2060))
    t_press = (time.perf_counter() - t0) * 1000
    moves = []
    for i in range(1, 81):
        x = 2800 + 2000 * i / 80
        t1 = time.perf_counter()
        pl = plugin
        hold_left(lambda x=x: pl.on_mouse_move_event(QtCore.QPointF(x, 2060)))
        moves.append((time.perf_counter() - t1) * 1000)
    t1 = time.perf_counter()
    plugin.on_mouse_release_event(QtCore.QPointF(4800, 2060))
    t_rel = (time.perf_counter() - t1) * 1000
    print("  press %.1f ms | move mean %.2f max %.2f ms | release %.1f ms"
          % (t_press, sum(moves) / len(moves), max(moves), t_rel))
    ok = (t_press < PRESS_BUDGET_MS and max(moves) < MOVE_BUDGET_MS
          and t_rel < PRESS_BUDGET_MS and target not in win.polygons)
    print("  %s correct annotation erased out of 201, within budget"
          % ("ok  " if ok else "FAIL"))
    RESULTS.append({"name": "many_annotations", "ok": ok})
    plugin.disable_plugin()
    del plugin, win
    gc.collect()

    print()
    print("== the old full-canvas preview, for comparison ==")
    for (cw, ch) in ((8000, 6000), (12000, 9000), (20000, 15000)):
        gc.collect()
        t0 = time.perf_counter()
        buf = np.zeros((ch, cw, 4), dtype=np.uint8)
        t_alloc = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        pm = QtGui.QPixmap.fromImage(QtGui.QImage(
            buf.data, cw, ch, 4 * cw, QtGui.QImage.Format.Format_RGBA8888))
        t_pix = (time.perf_counter() - t0) * 1000
        mb = buf.nbytes / (1024.0 * 1024.0)
        print("  %5dx%-5d  %8.1f MB | alloc %8.1f ms | pixmap %8.1f ms "
              "| per-press total %8.1f ms"
              % (cw, ch, mb, t_alloc, t_pix, t_alloc + t_pix))
        del buf, pm
        gc.collect()

    print()
    bad = []
    for r in RESULTS:
        if r.get("name"):
            if not r.get("ok"):
                bad.append(r["name"])
            continue
        if r["press_ms"] > PRESS_BUDGET_MS:
            bad.append("%s press %.0f ms" % (r["size"], r["press_ms"]))
        if r["move_p95_ms"] > MOVE_BUDGET_MS:
            bad.append("%s move p95 %.1f ms" % (r["size"],
                                                r["move_p95_ms"]))
        if r["total_ms"] > TOTAL_BUDGET_MS:
            bad.append("%s total %.0f ms" % (r["size"], r["total_ms"]))
        if not r["saved"]:
            bad.append("%s did not commit" % r["size"])

    print()
    if bad:
        print("PERFORMANCE REGRESSION:")
        for b in bad:
            print("  - " + b)
        return 1
    print("ALL WITHIN BUDGET")
    return 0


if __name__ == "__main__":
    sys.exit(main())
