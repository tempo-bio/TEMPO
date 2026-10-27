"""Bounded plot teardown checks for late Qt callbacks and replacement views."""
import gc
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pyqtgraph as pg
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication, QWidget, QPushButton, QVBoxLayout
from tempo_plot import LifecycleAxisItem, PlotItem, PlotWidget, ColorBarItem, add_plot, add_item

APP = QApplication.instance() or QApplication([])


def teardown_probe():
    # Capture callback exceptions only in this diagnostic child, then fail the
    # process. Production keeps its normal exception handling.
    errors = []
    original = sys.excepthook
    def report(kind, value, traceback):
        errors.append(str(value)); original(kind, value, traceback)
    sys.excepthook = report
    for iteration in range(20):
        host = QWidget(); row = QVBoxLayout(host)
        graph = pg.GraphicsLayoutWidget(); row.addWidget(graph)
        plot = add_plot(graph, title='Teardown fixture')
        bar = ColorBarItem(values=(0, 3), colorMap=pg.colormap.get('viridis'))
        graph.addItem(bar)
        signal = PlotWidget(); row.addWidget(signal)
        for item in (plot, signal.getPlotItem()):
            item.plot([0, 1, 2], [0, 3, 1])
            item.setLabel('bottom', 'Time', units='ms')
        axes = [entry['item'] for item in (plot, bar, signal.getPlotItem()) for entry in item.axes.values()]
        host.resize(900, 650); host.show(); APP.processEvents()
        host.resize(640, 480); APP.processEvents()
        host.close(); host.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        assert all(sip.isdeleted(axis) for axis in axes)
        for axis in axes:
            axis.resizeEvent()  # deterministic reproduction of a late callback
        del host, row, graph, plot, bar, signal, axes, axis, item
        gc.collect()
        next_view = QPushButton('Next non-plot view'); next_view.show(); APP.processEvents()
        next_view.close(); next_view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
    if errors:
        raise AssertionError('Qt callback failures: ' + '; '.join(errors))
    print('PASS: 20 plot teardown and replacement-view cycles; no Qt callback failures.', flush=True)


class PlotLifecycleTests(unittest.TestCase):
    def test_scene_disposal_destroys_detached_plot_controls_before_application(self):
        from tempo_plot import own_plot_resources
        from tempo_tasks import dispose_closed_views
        for iteration in range(20):
            host = QWidget(); layout = QVBoxLayout(host)
            graph = pg.GraphicsLayoutWidget(); layout.addWidget(graph)
            plot = add_plot(graph)
            bar = ColorBarItem(values=(0, 1), colorMap=pg.colormap.get('viridis'))
            graph.addItem(bar)
            signal = PlotWidget(); layout.addWidget(signal)
            resources = own_plot_resources(host)
            self.assertGreaterEqual(len(resources), 9)
            self.assertTrue(all(not sip.ispyowned(resource) for resource in resources))
            host.show(); APP.processEvents(); host.close()
            dispose_closed_views(host)
            self.assertTrue(all(sip.isdeleted(resource) for resource in resources))

    def test_installation_probe_exercises_actual_plots(self):
        from tempo_release import verify_plot_lifecycle
        verify_plot_lifecycle(APP)

    def test_guard_axes_preserve_plot_defaults_explicit_axes_and_colorbar_frame(self):
        graph = pg.GraphicsLayoutWidget(); self.addCleanup(graph.deleteLater)
        plot = add_plot(graph)
        for side in ('top', 'right'):
            self.assertFalse(plot.getAxis(side).isVisible())
        for side in ('left', 'bottom'):
            self.assertTrue(plot.getAxis(side).isVisible())
        explicit = add_plot(graph, axisItems={'top': LifecycleAxisItem('top')})
        self.assertTrue(explicit.getAxis('top').isVisible())
        self.assertFalse(explicit.getAxis('right').isVisible())
        bar = ColorBarItem(values=(1, 5), colorMap=pg.colormap.get('viridis'))
        graph.addItem(bar)
        for side in ('left', 'right', 'top', 'bottom'):
            self.assertTrue(bar.getAxis(side).isVisible())
            self.assertEqual(bar.getAxis(side).style['showValues'],side == 'right')
        self.assertEqual(bar.axis.range,[1,5])

    def test_add_item_places_item_in_layout_grid(self):
        graph = pg.GraphicsLayoutWidget(); self.addCleanup(graph.deleteLater)
        bar = ColorBarItem(values=(0, 1), colorMap=pg.colormap.get('viridis'))
        placed = add_item(graph, bar, row=0, col=1, rowspan=2)
        self.assertIs(placed, bar)
        self.assertIs(graph.ci.getItem(0, 1), bar)

    def test_live_axes_resize_normally_in_every_orientation(self):
        plot = PlotItem(); graph = pg.GraphicsLayoutWidget(); graph.addItem(plot)
        self.addCleanup(graph.deleteLater)
        for side in ('left', 'right', 'top', 'bottom'):
            axis = plot.getAxis(side)
            self.assertIsInstance(axis, LifecycleAxisItem)
            axis.setLabel('Fixture', units='px')
            axis.resizeEvent()
            self.assertFalse(sip.isdeleted(axis))

    def test_deleted_axis_and_deleted_label_ignore_late_resize(self):
        for delete_label in (False, True):
            axis = LifecycleAxisItem('right')
            sip.delete(axis.label if delete_label else axis)
            axis.resizeEvent()
            if not sip.isdeleted(axis): sip.delete(axis)

    def test_deletion_during_resize_is_safe_and_live_errors_are_not_hidden(self):
        axis = LifecycleAxisItem('right')
        def finish_deletion(event):
            sip.delete(axis)
            axis.size()  # the same deleted-wrapper operation seen in Windows logs
        with patch.object(pg.AxisItem, 'resizeEvent', side_effect=finish_deletion):
            axis.resizeEvent()
        live_axis = LifecycleAxisItem('right')
        try:
            with patch.object(pg.AxisItem, 'resizeEvent', side_effect=RuntimeError('live resize error')):
                with self.assertRaisesRegex(RuntimeError, 'live resize error'):
                    live_axis.resizeEvent()
        finally: sip.delete(live_axis)

    def test_colorbar_and_analysis_axes_all_use_lifecycle_guard(self):
        with patch('platform.machine', return_value='x86_64'):
            import tempo_gui as gui
        from tests.presentation_fixtures import write_dataset
        import tempfile
        view = gui.AnalyzerTab()
        try:
            with tempfile.TemporaryDirectory() as directory:
                view.load_csv(str(write_dataset(directory)))
                plots = [view.plot_item, view.color_bar,
                         view.summary_plot.getPlotItem(), view.mini_plot.getPlotItem()]
                for plot in plots:
                    for entry in plot.axes.values():
                        self.assertIsInstance(entry['item'], LifecycleAxisItem)
        finally:
            view.close(); view.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            APP.processEvents()

    def test_teardown_and_following_widget_in_bounded_child(self):
        result = subprocess.run([sys.executable, '-m', 'tests.test_plot_lifecycle', '--probe'],
            cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('20 plot teardown and replacement-view cycles', result.stdout)


if __name__ == '__main__':
    if '--probe' in sys.argv:
        teardown_probe()
    else:
        unittest.main()
