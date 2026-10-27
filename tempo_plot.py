"""TEMPO-owned plot axes tolerate resize callbacks during Qt teardown."""
import pyqtgraph as pg
import math
from PyQt6 import sip
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import QApplication


def own_plot_resources(view):
    """Give a closing graphics view ownership of its detached plot controls.

    pyqtgraph's scene items keep Python references to parentless menus and
    WidgetGroups. Deleting the scene does not delete those Qt objects. Leaving
    them to QApplication's unordered wrapper cleanup can delete the widgets
    and their signal controllers in different orders on different runs.
    """
    resources = []
    graphs = view.findChildren(pg.GraphicsView)
    if isinstance(view, pg.GraphicsView):
        graphs.append(view)
    for graph in graphs:
        scene = graph.scene()
        if scene is None:
            continue
        for item in scene.items():
            if isinstance(item, pg.PlotItem):
                menu = getattr(item, 'ctrlMenu', None)
                if menu is not None and not sip.isdeleted(menu):
                    menu.setParent(graph, menu.windowFlags())
                    resources.append(menu)
                group = getattr(item, 'stateGroup', None)
                if group is not None and not sip.isdeleted(group):
                    group.setParent(item)
                    resources.append(group)
            if isinstance(item, pg.ViewBox):
                menu = getattr(item, 'menu', None)
                if menu is not None and not sip.isdeleted(menu):
                    menu.setParent(graph, menu.windowFlags())
                    resources.append(menu)
                    for group in getattr(menu, 'widgetGroups', ()):
                        if not sip.isdeleted(group):
                            group.setParent(menu)
                            resources.append(group)
    return resources


class LifecycleAxisItem(pg.AxisItem):
    def _disposed(self):
        label = getattr(self, 'label', None)
        return sip.isdeleted(self) or label is None or sip.isdeleted(label)

    def resizeEvent(self, event=None):
        if self._disposed():
            return
        try:
            super().resizeEvent(event)
        except RuntimeError:
            # Label geometry can re-enter Qt layout processing and finish deletion
            # after the entry check. Suppress only an actually disposed axis/label.
            if not self._disposed():
                raise


def _axis_items(plot, supplied):
    items = dict(supplied or {})
    for side in ('left', 'right', 'top', 'bottom'):
        if side not in items and side not in plot.axes:
            items[side] = LifecycleAxisItem(side)
    return items


def _configure_axes(plot, supplied, setter):
    hidden = [side for side in ('top', 'right') if side not in plot.axes and side not in (supplied or {})]
    setter(_axis_items(plot, supplied))
    # pyqtgraph treats explicitly supplied axes as visible. The implicit guard
    # replacements must retain the original default of hidden top/right axes.
    for side in hidden:
        plot.hideAxis(side)


class PlotItem(pg.PlotItem):
    def setAxisItems(self, axisItems=None):
        _configure_axes(self, axisItems, super().setAxisItems)


class ColorBarItem(pg.ColorBarItem):
    def setAxisItems(self, axisItems=None):
        # PlotItem construction dispatches here before ColorBarItem configures its
        # frame and right-hand axis, so all four axes retain the library's setup.
        _configure_axes(self, axisItems, super().setAxisItems)


class PlotWidget(pg.PlotWidget):
    def __init__(self, *args, plotItem=None, **kwargs):
        super().__init__(*args, plotItem=plotItem if plotItem is not None else PlotItem(), **kwargs)

    def fit_label_height(self, minimum):
        """A rotated title needs its measured text width in vertical plot space."""
        left = self.getPlotItem().getAxis('left')
        bottom = self.getPlotItem().getAxis('bottom')
        # Hidden QGraphicsTextItems can retain the previous inherited font until
        # the scene is shown. Measure the font the user will actually see.
        for axis in (left, bottom):
            axis.label.setFont(self.font())
            axis.label.document().adjustSize()
        tick_font = bottom.style.get('tickFont') or QApplication.font()
        required = (left.label.boundingRect().width() +
                    bottom.label.boundingRect().height() +
                    QFontMetrics(tick_font).height() + 24)
        self.setFixedHeight(max(minimum, math.ceil(required)))


def add_plot(layout, row=None, col=None, rowspan=1, colspan=1, **kwargs):
    plot = PlotItem(**kwargs)
    target = getattr(layout, 'ci', layout)
    target.addItem(plot, row=row, col=col, rowspan=rowspan, colspan=colspan)
    return plot


def add_item(layout, item, row=None, col=None, rowspan=1, colspan=1):
    target = getattr(layout, 'ci', layout)
    target.addItem(item, row=row, col=col, rowspan=rowspan, colspan=colspan)
    return item
