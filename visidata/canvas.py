import math
import random

from collections import defaultdict, Counter, OrderedDict
from visidata import vd, asyncthread, colors, update_attr, clipdraw, dispwidth
from visidata import BaseSheet, Column, Progress, ColorAttr
from visidata.bezier import bezier

# see www/design/graphics.md

vd.theme_option('disp_graph_labels', True, 'show axes and legend on graph')
vd.theme_option('plot_colors', 'green red yellow cyan magenta white 38 136 168', 'list of distinct colors to use for plotting distinct objects')
vd.theme_option('disp_canvas_charset', ''.join(chr(0x2800+i) for i in range(256)), 'charset to render 2x4 blocks on canvas')
vd.theme_option('disp_graph_pixel_random', False, 'randomly choose attr from set of pixels instead of most common')
vd.theme_option('disp_zoom_incr', 2.0, 'amount to multiply current zoomlevel when zooming')
vd.theme_option('color_graph_hidden', '238 blue', 'color of legend for hidden attribute')
vd.theme_option('color_graph_selected', 'bold', 'color of selected graph points')

vd.option('auto_brush_select', True, 'automatically select source rows when brushing a region on canvas/graph', replay=True)

vd.selections = vd.StoredList(name='selections')


class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y

    def __repr__(self):
        if isinstance(self.x, int):
            return '(%d,%d)' % (self.x, self.y)
        else:
            return '(%.02f,%.02f)' % (self.x, self.y)

    @property
    def xy(self):
        return (self.x, self.y)

class Box:
    def __init__(self, x, y, w=0, h=0):
        self.xmin = x
        self.ymin = y
        self.w = w
        self.h = h

    def __repr__(self):
        return '[%s+%s,%s+%s]' % (self.xmin, self.w, self.ymin, self.h)

    @property
    def xymin(self):
        return Point(self.xmin, self.ymin)

    @property
    def xmax(self):
        return self.xmin + self.w

    @property
    def ymax(self):
        return self.ymin + self.h

    @property
    def center(self):
        return Point(self.xcenter, self.ycenter)

    @property
    def xcenter(self):
        return self.xmin + self.w/2

    @property
    def ycenter(self):
        return self.ymin + self.h/2

    def contains(self, x, y):
        return x >= self.xmin and \
               x < self.xmax and \
               y >= self.ymin and \
               y < self.ymax

def BoundingBox(x1, y1, x2, y2):
    return Box(min(x1, x2), min(y1, y2), abs(x2-x1), abs(y2-y1))


def clipline(x1, y1, x2, y2, xmin, ymin, xmax, ymax):
    'Liang-Barsky algorithm, returns [xn1,yn1,xn2,yn2] of clipped line within given area, or None'
    dx = x2-x1
    dy = y2-y1
    pq = [
        (-dx, x1-xmin),  # left
        ( dx, xmax-x1),  # right
        (-dy, y1-ymin),  # bottom
        ( dy, ymax-y1),  # top
    ]

    u1, u2 = 0, 1
    for p, q in pq:
        if p < 0:  # from outside to inside
            u1 = max(u1, q/p)
        elif p > 0:  # from inside to outside
            u2 = min(u2, q/p)
        else: #  p == 0:  # parallel to bbox
            if q < 0:  # completely outside bbox
                return None

    if u1 > u2:  # completely outside bbox
        return None

    xn1 = x1 + dx*u1
    yn1 = y1 + dy*u1

    xn2 = x1 + dx*u2
    yn2 = y1 + dy*u2

    return xn1, yn1, xn2, yn2

def iterline(x1, y1, x2, y2):
    'Yields (x, y) coords of line from (x1, y1) to (x2, y2)'
    xdiff = abs(x2-x1)
    ydiff = abs(y2-y1)
    xdir = 1 if x1 <= x2 else -1
    ydir = 1 if y1 <= y2 else -1

    r = math.ceil(max(xdiff, ydiff))
    if r == 0:  # point, not line
        yield x1, y1
    else:
        x, y = math.floor(x1), math.floor(y1)
        i = 0
        while i < r:
            x += xdir * xdiff / r
            y += ydir * ydiff / r

            yield x, y
            i += 1


def anySelected(vs, rows):
    for r in rows:
        if vs.isSelected(r):
            return True


class PlotDataset:
    '''Stores plot elements (points, lines, polylines) with stable row identity references.

    Each element is stored internally as (vertexes, attr, row) tuple.  External code
    should use the typed addPoint/addLine/addPolyline/addPolygon API instead of raw
    append().  Row identity is keyed by source.rowid() so spatial queries remain
    stable across sort/filter/zoom on the source sheet.
    '''

    def __init__(self):
        self._elements = []

    def __len__(self):
        return len(self._elements)

    def __iter__(self):
        return iter(self._elements)

    def __bool__(self):
        return bool(self._elements)

    def clear(self):
        self._elements.clear()

    def addPoint(self, x, y, attr='', row=None):
        'Record a single point plot element with stable row identity.'
        self._elements.append(([(x, y)], attr, row))

    def addLine(self, x1, y1, x2, y2, attr='', row=None):
        'Record a line segment plot element with stable row identity.'
        self._elements.append(([(x1, y1), (x2, y2)], attr, row))

    def addPolyline(self, vertexes, attr='', row=None):
        'Record a polyline (sequence of connected line segments) with stable row identity.'
        self._elements.append((list(vertexes), attr, row))

    def addPolygon(self, vertexes, attr='', row=None):
        'Record a closed polygon (line loop) with stable row identity.'
        self._elements.append((list(vertexes) + [vertexes[0]], attr, row))

    def append(self, *args):
        '''Append a raw plot element tuple.  Backward-compatible shim.

        Accepts either a single tuple ``(vertexes, attr, row)`` (list-compatible)
        or three separate arguments ``append(vertexes, attr, row)``.
        New code should use addPoint/addLine/addPolyline/addPolygon instead.
        '''
        if len(args) == 1 and isinstance(args[0], tuple) and len(args[0]) == 3:
            self._elements.append(args[0])
        elif len(args) == 3:
            self._elements.append((args[0], args[1], args[2]))
        else:
            raise TypeError('append expects (vertexes, attr, row) as a tuple or 3 separate args')

    def bbox(self):
        'Return (xmin, ymin, xmax, ymax) of all vertexes, or None if empty.'
        xmin = ymin = xmax = ymax = None
        for vertexes, attr, row in self._elements:
            for x, y in vertexes:
                if xmin is None or x < xmin: xmin = x
                if ymin is None or y < ymin: ymin = y
                if xmax is None or x > xmax: xmax = x
                if ymax is None or y > ymax: ymax = y
        if xmin is None:
            return None
        return (float(xmin), float(ymin), float(xmax), float(ymax))

    def rowsWithinDataBox(self, xmin, ymin, xmax, ymax, hiddenAttrs=None, source=None):
        '''Return rows whose plotted points fall within the given data coordinate bounding box.

        Deduplicates by source.rowid(row) so the result is stable across sort/filter/zoom.
        Works regardless of current zoom or visible region.
        '''
        if hiddenAttrs is None:
            hiddenAttrs = set()
        ret = {}
        x1, x2 = min(xmin, xmax), max(xmin, xmax)
        y1, y2 = min(ymin, ymax), max(ymin, ymax)
        for vertexes, attr, row in self._elements:
            if attr in hiddenAttrs:
                continue
            if row is None:
                continue
            for vx, vy in vertexes:
                try:
                    fx, fy = float(vx), float(vy)
                except (TypeError, ValueError):
                    continue
                if x1 <= fx <= x2 and y1 <= fy <= y2:
                    if source is not None:
                        ret[source.rowid(row)] = row
                    else:
                        ret[id(row)] = row
                    break
        return list(ret.values())


class CoordinateTransformer:
    '''Pure data-coordinate to plotter-pixel coordinate conversion with projection strategies.

    No knowledge of rows, selection, or rendering.  Given a visible data bounding box
    and a plotter pixel bounding box, converts between the two spaces.  Supports
    ``invert_y`` strategy for graphs where y increases upward.

    Usage:
        * ``scaleX/Y`` / ``unscaleX/Y``: single-coordinate conversion
        * ``projectElement(vertexes)``: batch projection of a plot element's vertexes
          into plotter pixel space, respecting invert_y and clipping hints
        * ``renderContext()``: returns (xmin, ymin, xmax, ymax, xfactor, yfactor,
          plotxmin, plotyref, invert_y) for tight inner loops
    '''

    def __init__(self, invert_y=False):
        self.plotviewBox = None  # Box in plotter pixel coords
        self.visibleBox = None   # Box in data coords
        self.canvasBox = None    # Box in data coords (full extent)
        self.aspectRatio = 0.0
        self.xzoomlevel = 1.0
        self.yzoomlevel = 1.0
        self.invert_y = invert_y  # if True, y axis points up (graph style)

    @property
    def xScaler(self):
        'plotter pixels per data unit along x axis.'
        if not (self.canvasBox and self.plotviewBox):
            return 1.0
        xratio = self.plotviewBox.w / (self.canvasBox.w * self.xzoomlevel)
        if self.aspectRatio:
            yratio = self.plotviewBox.h / (self.canvasBox.h * self.yzoomlevel)
            return self.aspectRatio * min(xratio, yratio)
        return xratio

    @property
    def yScaler(self):
        'plotter pixels per data unit along y axis.'
        if not (self.canvasBox and self.plotviewBox):
            return 1.0
        yratio = self.plotviewBox.h / (self.canvasBox.h * self.yzoomlevel)
        if self.aspectRatio:
            xratio = self.plotviewBox.w / (self.canvasBox.w * self.xzoomlevel)
            return min(xratio, yratio)
        return yratio

    def scaleX(self, dataX):
        'Convert data x coordinate to plotter pixel x coordinate.'
        if not (self.visibleBox and self.plotviewBox):
            return int(dataX)
        return self.plotviewBox.xmin + round((dataX - self.visibleBox.xmin) * self.xScaler)

    def scaleY(self, dataY):
        'Convert data y coordinate to plotter pixel y coordinate, respecting invert_y.'
        if not (self.visibleBox and self.plotviewBox):
            return int(dataY)
        if self.invert_y:
            return self.plotviewBox.ymax - round((dataY - self.visibleBox.ymin) * self.yScaler)
        return self.plotviewBox.ymin + round((dataY - self.visibleBox.ymin) * self.yScaler)

    def unscaleX(self, plotterX):
        'Convert plotter pixel x coordinate to data x coordinate.'
        if not (self.visibleBox and self.plotviewBox):
            return float(plotterX)
        return (plotterX - self.plotviewBox.xmin) / self.xScaler + self.visibleBox.xmin

    def unscaleY(self, plotterY):
        'Convert plotter pixel y coordinate to data y coordinate, respecting invert_y.'
        if not (self.visibleBox and self.plotviewBox):
            return float(plotterY)
        if self.invert_y:
            return (self.plotviewBox.ymax - plotterY) / self.yScaler + self.visibleBox.ymin
        return (plotterY - self.plotviewBox.ymin) / self.yScaler + self.visibleBox.ymin

    def canvasW(self, plotterWidth):
        'Convert plotter pixel width to data coordinate width.'
        return plotterWidth / self.xScaler if self.xScaler else 0.0

    def canvasH(self, plotterHeight):
        'Convert plotter pixel height to data coordinate height.'
        return plotterHeight / self.yScaler if self.yScaler else 0.0

    def renderContext(self):
        '''Return pre-computed projection parameters for tight inner render loops.

        Returns tuple ``(xmin, ymin, xmax, ymax, xfactor, yfactor, plotxmin,
        plotyref, invert_y)`` where ``plotyref`` is either plotviewBox.ymin
        (normal) or plotviewBox.ymax (inverted).
        '''
        bb = self.visibleBox
        xmin, ymin, xmax, ymax = bb.xmin, bb.ymin, bb.xmax, bb.ymax
        xfactor, yfactor = self.xScaler, self.yScaler
        plotxmin = self.plotviewBox.xmin
        plotyref = self.plotviewBox.ymax if self.invert_y else self.plotviewBox.ymin
        return (xmin, ymin, xmax, ymax, xfactor, yfactor, plotxmin, plotyref, self.invert_y)

    def projectPoint(self, x, y):
        '''Project a single (x, y) data point into plotter pixel coordinates.

        Returns ``(px, py)`` tuple.  Caller should check visibility separately.
        '''
        return self.scaleX(x), self.scaleY(y)

    def projectElement(self, vertexes):
        '''Project all vertexes of a plot element into plotter pixel space.

        Returns a list of ``(px, py)`` tuples with the same length as *vertexes*.
        No clipping is performed; use :meth:`renderContext` for clipped inner loops.
        '''
        result = []
        for x, y in vertexes:
            result.append((self.scaleX(float(x)), self.scaleY(float(y))))
        return result


class RowIdentityMixin:
    '''Shared row identity mapping for scatter plots and line charts.

    Maintains a stable row ordering by source rowid so that rows returned by
    spatial queries (rowsWithin, rowsWithinDataBox) are in a deterministic order
    even after the source sheet is sorted, filtered, or the canvas is zoomed.

    Usage: inherit from this mixin alongside Canvas/Plotter and call
    ``self.recordRowIdentity(row, order)`` when adding plot elements.
    '''

    def initRowIdentity(self):
        'Initialize the row identity store.  Call from __init__ or reset().'
        self.row_order = {}

    def recordRowIdentity(self, row, order):
        '''Record the identity and original insertion order of a source row.

        *row* is the source row object.  *order* is an integer that determines
        sort order in spatial query results (typically the enumeration index
        during data loading).
        '''
        if self.source is not None and row is not None:
            self.row_order[self.source.rowid(row)] = order

    def sortRowsBySourceOrder(self, rows):
        '''Sort *rows* by the order they were originally recorded.

        Stable across sort/filter/zoom because the key is source.rowid().
        '''
        return sorted(rows, key=lambda r: self.row_order.get(self.source.rowid(r) if self.source else id(r), 0))


class BrushSelectorMixin:
    '''Brush selection command API, separated from rendering and data storage.

    Requires the host sheet to provide:
      - source: the source Sheet with select/toggle/unselect methods
      - rowsWithinDataBox(xmin, ymin, xmax, ymax): returns rows in a data bbox
      - parseBbox(bboxstr), formatBbox(bbox): parse/format bbox strings
      - cursorBox, visibleBox: Box objects for the current cursor and view
      - options: for auto_brush_select
    '''

    def parseBbox(self, bboxstr):
        'Parse "xmin xmax ymin ymax" string into tuple of floats.'
        parts = str(bboxstr).split()
        if len(parts) != 4:
            vd.fail('expected "xmin xmax ymin ymax", got "%s"' % bboxstr)
        return tuple(float(p) for p in parts)

    def formatBbox(self, bbox):
        'Format BoundingBox or (xmin,xmax,ymin,ymax) as "xmin xmax ymin ymax" string using sheet formatters.'
        if isinstance(bbox, Box):
            return '%s %s %s %s' % (self.formatX(bbox.xmin), self.formatX(bbox.xmax),
                                    self.formatY(bbox.ymin), self.formatY(bbox.ymax))
        xmin, xmax, ymin, ymax = bbox
        return '%s %s %s %s' % (self.formatX(xmin), self.formatX(xmax),
                                self.formatY(ymin), self.formatY(ymax))

    @asyncthread
    def selectBbox(self, bboxstr, add_undo=True):
        'Select source rows whose data points fall within "xmin xmax ymin ymax".  Async.'
        xmin, xmax, ymin, ymax = self.parseBbox(bboxstr)
        rows = self.rowsWithinDataBox(xmin, ymin, xmax, ymax)
        self.source.select(rows, add_undo=add_undo)

    @asyncthread
    def stoggleBbox(self, bboxstr, add_undo=True):
        'Toggle selection of source rows whose data points fall within "xmin xmax ymin ymax".  Async.'
        xmin, xmax, ymin, ymax = self.parseBbox(bboxstr)
        rows = self.rowsWithinDataBox(xmin, ymin, xmax, ymax)
        self.source.toggle(rows, add_undo=add_undo)

    @asyncthread
    def unselectBbox(self, bboxstr, add_undo=True):
        'Unselect source rows whose data points fall within "xmin xmax ymin ymax".  Async.'
        xmin, xmax, ymin, ymax = self.parseBbox(bboxstr)
        rows = self.rowsWithinDataBox(xmin, ymin, xmax, ymax)
        self.source.unselect(rows, add_undo=add_undo)

    def brushSelect(self):
        'Select source rows within current cursor box, recording bbox for cmdlog replay.'
        if not self.cursorBox:
            return
        bboxstr = self.formatBbox(self.cursorBox)
        vd.setLastArgs(bboxstr)
        self.selectBbox(bboxstr)

    def brushToggle(self):
        'Toggle selection of source rows within current cursor box, recording bbox for cmdlog replay.'
        if not self.cursorBox:
            return
        bboxstr = self.formatBbox(self.cursorBox)
        vd.setLastArgs(bboxstr)
        self.stoggleBbox(bboxstr)

    def brushUnselect(self):
        'Unselect source rows within current cursor box, recording bbox for cmdlog replay.'
        if not self.cursorBox:
            return
        bboxstr = self.formatBbox(self.cursorBox)
        vd.setLastArgs(bboxstr)
        self.unselectBbox(bboxstr)

    def brushVisibleSelect(self):
        'Select source rows within visible canvas, recording bbox for cmdlog replay.'
        if not self.visibleBox:
            return
        bboxstr = self.formatBbox(self.visibleBox)
        vd.setLastArgs(bboxstr)
        self.selectBbox(bboxstr)

    def brushVisibleToggle(self):
        'Toggle selection of source rows within visible canvas, recording bbox for cmdlog replay.'
        if not self.visibleBox:
            return
        bboxstr = self.formatBbox(self.visibleBox)
        vd.setLastArgs(bboxstr)
        self.stoggleBbox(bboxstr)

    def brushVisibleUnselect(self):
        'Unselect source rows within visible canvas, recording bbox for cmdlog replay.'
        if not self.visibleBox:
            return
        bboxstr = self.formatBbox(self.visibleBox)
        vd.setLastArgs(bboxstr)
        self.unselectBbox(bboxstr)

    def saveNamedSelection(self, name):
        'Save current cursor bounding box as a named selection.'
        if not self.cursorBox:
            vd.fail('no cursor box to save')
        bb = self.cursorBox
        sel = {
            'name': name,
            'sheet': self.name,
            'xmin': float(bb.xmin),
            'xmax': float(bb.xmax),
            'ymin': float(bb.ymin),
            'ymax': float(bb.ymax),
        }
        vd.selections.append(sel)
        vd.status('saved selection "%s" (%d points in region)' % (name,
            len(self.rowsWithinDataBox(bb.xmin, bb.ymin, bb.xmax, bb.ymax))))

    def loadNamedSelection(self, name):
        'Load/apply a named selection by selecting its rows on the source sheet.'
        vd.selections.reload()
        for sel in vd.selections:
            if sel.name == name:
                bboxstr = '%s %s %s %s' % (sel.xmin, sel.xmax, sel.ymin, sel.ymax)
                vd.setLastArgs(bboxstr)
                self.selectBbox(bboxstr)
                vd.status('loaded selection "%s"' % name)
                return
        vd.fail('no selection named "%s"' % name)

    def deleteNamedSelection(self, name):
        'Delete a named selection from the stored list.'
        vd.selections.reload()
        for i, sel in enumerate(vd.selections):
            if sel.name == name:
                del vd.selections[i]
                p = vd.selections.path
                if p and p.exists():
                    import json
                    with p.open(mode='w', encoding='utf-8') as fp:
                        for s in vd.selections:
                            fp.write(json.dumps(dict(s)) + '\n')
                vd.status('deleted selection "%s"' % name)
                return
        vd.fail('no selection named "%s"' % name)


#  - width/height are exactly equal to the number of pixels displayable, and can change at any time.
#  - needs to refresh from source on resize
class Plotter(BaseSheet):
    'pixel-addressable display of entire terminal with (x,y) integer pixel coordinates'
    columns=[Column('_')]  # to eliminate errors outside of draw()
    rowtype='pixels'
    def __init__(self, *names, **kwargs):
        super().__init__(*names, **kwargs)
        self.labels = []  # (x, y, text, attr, row)
        self.hiddenAttrs = set()
        self.needsRefresh = False
        self.resetCanvasDimensions(1, 1)  #2171

    @property
    def nRows(self):
        return (self.plotwidth* self.plotheight)

    def resetCanvasDimensions(self, windowHeight, windowWidth):
        'sets total available canvas dimensions to (windowHeight, windowWidth) (in char cells)'
        self.plotwidth = windowWidth*2
        self.plotheight = (windowHeight-1)*4  # exclude status line

        # pixels[y][x] = { attr: list(rows), ... }
        self.pixels = [[defaultdict(list) for x in range(self.plotwidth)] for y in range(self.plotheight)]

    def plotpixel(self, x, y, attr:"str|ColorAttr=''", row=None):
        self.pixels[y][x][attr].append(row)

    def plotline(self, x1, y1, x2, y2, attr:"str|ColorAttr=''", row=None):
        for x, y in iterline(x1, y1, x2, y2):
            self.plotpixel(math.ceil(x), math.ceil(y), attr, row)

    def plotlabel(self, x, y, text, attr:"str|ColorAttr=''", row=None):
        self.labels.append((x, y, text, attr, row))

    def plotlegend(self, i, txt, attr:"str|ColorAttr=''", width=15):
        # move it 1 character to the left b/c the rightmost column can't be drawn to
        self.plotlabel(self.plotwidth-(width+1)*2, i*4, txt, attr)

    @property
    def plotterCursorBox(self):
        'Returns pixel bounds of cursor as a Box.  Override to provide a cursor.'
        return Box(0,0,0,0)

    @property
    def plotterMouse(self):
        return Point(*self.plotterFromTerminalCoord(self.mouseX, self.mouseY))

    def plotterFromTerminalCoord(self, x, y):
        return x*2, y*4

    def getPixelAttrRandom(self, x, y) -> str:
        'weighted-random choice of colornum at this pixel.'
        c = list(attr for attr, rows in self.pixels[y][x].items()
                         for r in rows if attr and attr not in self.hiddenAttrs)
        return random.choice(c) if c else 0

    def getPixelAttrMost(self, x, y) -> str:
        'most common colornum at this pixel.'
        r = self.pixels[y][x]
        if not r:
            return 0
        c = [(len(rows), attr, rows) for attr, rows in r.items() if attr and attr not in self.hiddenAttrs]
        if not c:
            return 0
        _, attr, rows = max(c)
        return attr

    def hideAttr(self, attr:str, hide=True):
        if hide:
            self.hiddenAttrs.add(attr)
        else:
            self.hiddenAttrs.remove(attr)
        self.plotlegends()

    def rowsWithin(self, plotter_bbox, invert_y=False):
        'return list of deduped rows within plotter_bbox'
        ret = {}

        x_start = max(0, plotter_bbox.xmin)
        if len(self.pixels) == 0: return []
        x_end = min(len(self.pixels[0]), plotter_bbox.xmax)

        y_start = max(0, plotter_bbox.ymin)
        y_end = min(len(self.pixels), plotter_bbox.ymax)
        if invert_y:
            y_range = range(y_end-1, y_start-1, -1)
        else:
            y_range = range(y_start, y_end)

        for x in range(x_start, x_end):
            for y in y_range:
                for attr, rows in self.pixels[y][x].items():
                    if attr not in self.hiddenAttrs:
                        for r in rows:
                            ret[self.source.rowid(r)] = r
        return list(ret.values())

    def draw(self, scr):
        windowHeight, windowWidth = scr.getmaxyx()
        if self.needsRefresh:
            self.render(windowHeight, windowWidth)

        self.draw_pixels(scr)
        self.draw_labels(scr)

    def draw_empty(self, scr):
        # use draw_empty() when calling draw_pixels() with clear_empty_squares=False
        cursorBBox = self.plotterCursorBox
        for char_y in range(0, self.plotheight//4):
            for char_x in range(0, self.plotwidth//2):
                cattr = ColorAttr()
                ch = ' '
                # draw cursor
                if cursorBBox.contains(char_x*2, char_y*4) or \
                    cursorBBox.contains(char_x*2+1, char_y*4+3):
                    cattr = update_attr(cattr, colors.color_current_row)
                scr.addstr(char_y, char_x, ch, cattr.attr)

    def draw_pixels(self, scr, clear_empty_squares=True):
        disp_canvas_charset = self.options.disp_canvas_charset or ' o'
        disp_canvas_charset += (256 - len(disp_canvas_charset)) * disp_canvas_charset[-1]
        if self.pixels:
            cursorBBox = self.plotterCursorBox
            getPixelAttr = self.getPixelAttrRandom if self.options.disp_graph_pixel_random else self.getPixelAttrMost

            for char_y in range(0, self.plotheight//4):
                for char_x in range(0, self.plotwidth//2):
                    block_attrs = [
                        getPixelAttr(char_x*2  , char_y*4  ),
                        getPixelAttr(char_x*2  , char_y*4+1),
                        getPixelAttr(char_x*2  , char_y*4+2),
                        getPixelAttr(char_x*2+1, char_y*4  ),
                        getPixelAttr(char_x*2+1, char_y*4+1),
                        getPixelAttr(char_x*2+1, char_y*4+2),
                        getPixelAttr(char_x*2  , char_y*4+3),
                        getPixelAttr(char_x*2+1, char_y*4+3),
                    ]

                    pow2 = 1
                    braille_num = 0
                    for c in block_attrs:
                        if c:
                            braille_num += pow2
                        pow2 *= 2

                    ch = disp_canvas_charset[braille_num]
                    if braille_num != 0:
                        color = Counter(c for c in block_attrs if c).most_common(1)[0][0]
                        cattr = colors.get_color(color)
                    else:
                        cattr = ColorAttr()
                        # don't erase empty squares, useful for subclasses that draw elements like reflines
                        # before pixels are drawn
                        if not clear_empty_squares:
                            continue

                    # draw cursor
                    if cursorBBox.contains(char_x*2, char_y*4) or \
                       cursorBBox.contains(char_x*2+1, char_y*4+3):
                        cattr = update_attr(cattr, colors.color_current_row)

                    if cattr.attr:
                        scr.addstr(char_y, char_x, ch, cattr.attr)

    def draw_labels(self, scr):
        def _mark_overlap_text(labels, textobj):
            def _overlaps(a, b):
                a_x1, _, a_txt, _, _ = a
                b_x1, _, b_txt, _, _ = b
                a_x2 = a_x1 + dispwidth(a_txt, literal=True)
                b_x2 = b_x1 + dispwidth(b_txt, literal=True)
                if a_x1 < b_x1 < a_x2 or a_x1 < b_x2 < a_x2 or \
                   b_x1 < a_x1 < b_x2 or b_x1 < a_x2 < b_x2:
                   return True
                else:
                   return False

            label_fldraw = [textobj, True]
            labels.append(label_fldraw)
            for o in labels:
                if _overlaps(o[0], textobj):
                    o[1] = False
                    label_fldraw[1] = True

        if self.options.disp_graph_labels:
            labels_by_line = defaultdict(list) # y -> text labels

            for pix_x, pix_y, txt, attr, row in self.labels:
                if attr in self.hiddenAttrs:
                    continue
                char_y = int(pix_y/4)
                char_x = int(pix_x/2)
                if row is not None:
                    char_x -= math.ceil(dispwidth(txt, literal=True)/2)*2
                o = (char_x, char_y, txt, attr, row)
                _mark_overlap_text(labels_by_line[char_y], o)

            for line in labels_by_line.values():
                for o, fldraw in line:
                    if fldraw:
                        char_x, char_y, txt, attr, row = o
                        cattr = colors.get_color(attr)
                        clipdraw(scr, char_y, char_x, txt, cattr, dispwidth(txt, literal=True), literal=True)
                        cursorBBox = self.plotterCursorBox
                        for c in txt:
                            w = dispwidth(c, literal=True)
                            # draw cursor if the cursor contains the midpoint of the character cell
                            if cursorBBox.contains(char_x*2+1, char_y*4+2):
                                char_attr = update_attr(cattr, colors.color_current_row)
                                clipdraw(scr, char_y, char_x, c, char_attr, w, literal=True)
                            char_x += w


# - has a cursor, of arbitrary position and width/height (not restricted to current zoom)
class Canvas(BrushSelectorMixin, Plotter):
    'zoomable/scrollable virtual canvas with (x,y) coordinates in arbitrary units'
    rowtype = 'plots'
    leftMarginPixels = 10*2
    rightMarginPixels = 4*2
    topMarginPixels = 0*4
    bottomMarginPixels = 1*4  # reserve bottom line for x axis
    guide = '# Canvas\n'

    def __init__(self, *names, **kwargs):
        self.left_margin = self.leftMarginPixels
        super().__init__(*names, **kwargs)

        self.cursorBox = None   # bounding box of cursor, in canvas units
        self.needsRefresh = False

        self._coord = CoordinateTransformer()  # screen/data coordinate conversion
        self.plotData = PlotDataset()  # chart data model with stable row identity
        self.gridlabels = []  # list of (grid_x, grid_y, label, fgcolornum, row)

        self.legends = OrderedDict()   # txt: attr  (visible legends only)
        self.plotAttrs = {}   # key: attr  (all keys, for speed)
        self.reset()

    @property
    def canvasBox(self):
        return self._coord.canvasBox

    @canvasBox.setter
    def canvasBox(self, value):
        self._coord.canvasBox = value

    @property
    def visibleBox(self):
        return self._coord.visibleBox

    @visibleBox.setter
    def visibleBox(self, value):
        self._coord.visibleBox = value

    @property
    def plotviewBox(self):
        return self._coord.plotviewBox

    @plotviewBox.setter
    def plotviewBox(self, value):
        self._coord.plotviewBox = value

    @property
    def aspectRatio(self):
        return self._coord.aspectRatio

    @aspectRatio.setter
    def aspectRatio(self, value):
        self._coord.aspectRatio = value

    @property
    def xzoomlevel(self):
        return self._coord.xzoomlevel

    @xzoomlevel.setter
    def xzoomlevel(self, value):
        self._coord.xzoomlevel = value

    @property
    def yzoomlevel(self):
        return self._coord.yzoomlevel

    @yzoomlevel.setter
    def yzoomlevel(self, value):
        self._coord.yzoomlevel = value

    @property
    def polylines(self):
        'Backward-compatible alias for plotData.  Supports iteration, len, bool, clear, append.'
        return self.plotData

    @polylines.setter
    def polylines(self, value):
        'Backward-compatible setter: replaces plotData contents with given iterable.'
        self.plotData = PlotDataset()
        for item in value:
            vertexes, attr, row = item
            self.plotData.append(vertexes, attr, row)

    @property
    def nRows(self):
        return len(self.plotData)

    def reset(self):
        'clear everything in preparation for a fresh reload()'
        self.plotData.clear()
        self.canvasBox = None
        self.visibleBox = None
        self.cursorBox = None
        self.left_margin = self.leftMarginPixels
        self.legends.clear()
        self.legendwidth = 0
        self.plotAttrs.clear()
        self.unusedAttrs = list(self.options.plot_colors.split())

    def plotColor(self, k) -> str:
        attr = self.plotAttrs.get(k, None)
        if attr is None:
            if self.unusedAttrs:
                attr = self.unusedAttrs.pop(0)
                legend = ' '.join(str(x) for x in k)
            else:
                lastlegend, attr = list(self.legends.items())[-1]
                del self.legends[lastlegend]
                legend = '[other]'

            self.legendwidth = max(self.legendwidth, dispwidth(legend, literal=True))
            self.legends[legend] = attr
            self.plotAttrs[k] = attr
        return attr

    def resetCanvasDimensions(self, windowHeight, windowWidth):
        old_plotsize = None
        realign_cursor = False
        if hasattr(self, 'plotwidth') and hasattr(self, 'plotheight'):
            old_plotsize = [self.plotheight, self.plotwidth]
            if hasattr(self, 'cursorBox') and self.cursorBox and self.visibleBox:
                # if the cursor is at the origin, realign it with the origin after the resize
                if self.cursorBox.xmin == self.visibleBox.xmin and self.cursorBox.ymin == self.calcBottomCursorY():
                    realign_cursor = True
        super().resetCanvasDimensions(windowHeight, windowWidth)
        # if window is not big enough to contain a particular margin, pretend that margin is 0
        pvbox_x = pvbox_y = 0
        if self.plotwidth > self.left_margin:
            pvbox_x = self.left_margin
        if self.plotheight > self.topMarginPixels:
            pvbox_y = self.topMarginPixels
        if hasattr(self, 'legendwidth'):
            # +4 = 1 empty space after the graph + 2 characters for the legend prefixes of "1:", "2:", etc +
            #      1 character for the empty rightmost column
            new_margin = max(self.rightMarginPixels, (self.legendwidth+4)*2)
            pvbox_xmax = self.plotwidth-new_margin-1
            # ensure the graph data takes up at least 3/4 of the width of the screen no matter how wide the legend gets
            pvbox_xmax = max(pvbox_xmax, math.ceil(self.plotwidth * 3/4)//2*2 + 1)
        else:
            pvbox_xmax = self.plotwidth-self.rightMarginPixels-1
        self.left_margin = min(self.left_margin, math.ceil(self.plotwidth * 1/3)//2*2)
        # enforce a minimum plotview box size of 1x1
        pvbox_xmax = max(pvbox_xmax, 1)
        pvbox_ymax = max(self.plotheight-self.bottomMarginPixels-1, 1)
        self.plotviewBox = BoundingBox(pvbox_x, pvbox_y, pvbox_xmax, pvbox_ymax)
        if [self.plotheight, self.plotwidth] != old_plotsize:
            if hasattr(self, 'cursorBox') and self.cursorBox:
                self.setCursorSizeInPlotterPixels(2, 4)
            if realign_cursor:
                self.cursorBox.ymin = self.calcBottomCursorY()

    @property
    def statusLine(self):
        extra = ''
        if self.cursorBox and self.plotData and self.source:
            try:
                n = len(self.rowsWithinDataBox(self.cursorBox.xmin, self.cursorBox.ymin,
                                               self.cursorBox.xmax, self.cursorBox.ymax))
                extra = ' (%d %s selected)' % (n, self.source.rowtype)
            except Exception:
                pass
        return 'canvas %s visible %s cursor %s%s' % (self.canvasBox, self.visibleBox, self.cursorBox, extra)

    @property
    def canvasMouse(self):
        x = self.plotterMouse.x
        y = self.plotterMouse.y
        if not self.canvasBox: return None
        p = Point(self.unscaleX(x), self.unscaleY(y))
        return p

    def setCursorSize(self, p):
        'sets width based on diagonal corner p'
        if not p: return
        self.cursorBox = BoundingBox(self.cursorBox.xmin, self.cursorBox.ymin, p.x, p.y)
        self.cursorBox.w = max(self.cursorBox.w, self.canvasCharWidth)
        self.cursorBox.h = max(self.cursorBox.h, self.canvasCharHeight)

    def setCursorSizeInPlotterPixels(self, w, h):
        self.setCursorSize(Point(self.cursorBox.xmin + w/2 * self.canvasCharWidth,
                                 self.cursorBox.ymin + h/4 * self.canvasCharHeight))

    def formatX(self, v):
        return str(v)

    def formatY(self, v):
        return str(v)

    def parseX(self, txt):
        return float(txt)

    def parseY(self, txt):
        return float(txt)

    def moveToCol(self, colstr):
        xmin, xmax = map(float, map(self.parseX, colstr.split()))
        self.cursorBox.xmin = xmin
        self.cursorBox.w = xmax-xmin
        return True

    def moveToRow(self, rowstr):
        ymin, ymax = map(float, map(self.parseY, rowstr.split()))
        self.cursorBox.ymin = ymin
        self.cursorBox.h = ymax-ymin
        return True

    def commandCursor(sheet, execstr):
        'Return (col, row) of cursor suitable for cmdlog replay of execstr.'
        contains = lambda s, *substrs: any((a in s) for a in substrs)
        colname, rowname = '', ''
        if contains(execstr, 'plotterCursorBox', 'brushSelect', 'brushToggle', 'brushUnselect', 'dive-cursor', 'delete-cursor', 'select-cursor', 'stoggle-cursor', 'unselect-cursor', 'saveNamedSelection', 'save-selection'):
            bb = sheet.cursorBox
            if bb:
                colname = '%s %s' % (sheet.formatX(bb.xmin), sheet.formatX(bb.xmax))
                rowname = '%s %s' % (sheet.formatY(bb.ymin), sheet.formatY(bb.ymax))
        elif contains(execstr, 'plotterVisibleBox', 'brushVisible', 'dive-visible', 'delete-visible', 'select-visible', 'stoggle-visible', 'unselect-visible'):
            bb = sheet.visibleBox
            if bb:
                colname = '%s %s' % (sheet.formatX(bb.xmin), sheet.formatX(bb.xmax))
                rowname = '%s %s' % (sheet.formatY(bb.ymin), sheet.formatY(bb.ymax))
        return colname, rowname

    @property
    def canvasCharWidth(self):
        'Width in canvas units of a single char in the terminal'
        return self.visibleBox.w*2/self.plotviewBox.w

    @property
    def canvasCharHeight(self):
        'Height in canvas units of a single char in the terminal'
        return self.visibleBox.h*4/self.plotviewBox.h

    @property
    def plotterVisibleBox(self):
        return BoundingBox(self.scaleX(self.visibleBox.xmin),
                           self.scaleY(self.visibleBox.ymin),
                           self.scaleX(self.visibleBox.xmax),
                           self.scaleY(self.visibleBox.ymax))

    @property
    def plotterCursorBox(self):
        if self.cursorBox is None:
            return Box(0,0,0,0)
        return BoundingBox(self.scaleX(self.cursorBox.xmin),
                           self.scaleY(self.cursorBox.ymin),
                           self.scaleX(self.cursorBox.xmax),
                           self.scaleY(self.cursorBox.ymax))

    def startCursor(self):
        cm = self.canvasMouse
        if cm:
            self.cursorBox = Box(*cm.xy)
            return True
        else:
            return None

    def point(self, x, y, attr:"str|ColorAttr=''", row=None):
        'Add a point plot element.  Delegates to PlotDataset.addPoint.'
        self.plotData.addPoint(x, y, attr, row)

    def line(self, x1, y1, x2, y2, attr:"str|ColorAttr=''", row=None):
        'Add a line segment plot element.  Delegates to PlotDataset.addLine.'
        self.plotData.addLine(x1, y1, x2, y2, attr, row)

    def polyline(self, vertexes, attr:"str|ColorAttr=''", row=None):
        'Add a polyline (sequence of connected line segments).  Delegates to PlotDataset.addPolyline.'
        self.plotData.addPolyline(vertexes, attr, row)

    def polygon(self, vertexes, attr:"str|ColorAttr=''", row=None):
        'Add a closed polygon (line loop).  Delegates to PlotDataset.addPolygon.'
        self.plotData.addPolygon(vertexes, attr, row)

    def qcurve(self, vertexes, attr:"str|ColorAttr=''", row=None):
        'Draw quadratic curve from vertexes[0] to vertexes[2] with control point at vertexes[1]'
        if len(vertexes) != 3:
            vd.fail('need exactly 3 points for qcurve (got %d)' % len(vertexes))

        x1, y1 = vertexes[0]
        x2, y2 = vertexes[1]
        x3, y3 = vertexes[2]

        for x, y in bezier(x1, y1, x2, y2, x3, y3):
            self.point(x, y, attr, row)

    def label(self, x, y, text, attr:"str|ColorAttr=''", row=None):
        self.gridlabels.append((x, y, text, attr, row))

    def fixPoint(self, plotterPoint, canvasPoint):
        'adjust visibleBox.xymin so that canvasPoint is plotted at plotterPoint'
        self.visibleBox.xmin = canvasPoint.x - self.canvasW(plotterPoint.x-self.plotviewBox.xmin)
        self.visibleBox.ymin = canvasPoint.y - self.canvasH(plotterPoint.y-self.plotviewBox.ymin)
        self.resetBounds()

    def zoomTo(self, bbox):
        'set visible area to bbox, maintaining aspectRatio if applicable'
        self.fixPoint(self.plotviewBox.xymin, bbox.xymin)
        self.xzoomlevel=bbox.w/self.canvasBox.w
        self.yzoomlevel=bbox.h/self.canvasBox.h
        self.resetBounds()

    def incrZoom(self, incr):
        self.xzoomlevel *= incr
        self.yzoomlevel *= incr

        self.resetBounds()

    def resetBounds(self, refresh=True):
        'create canvasBox and cursorBox if necessary, and set visibleBox w/h according to zoomlevels.  then redisplay legends.'
        if not self.canvasBox:
            bbox = self.plotData.bbox()
            if bbox:
                xmin, ymin, xmax, ymax = bbox
            else:
                xmin = ymin = xmax = ymax = 0.0
            if xmin == xmax:
                xmax += 1
                if xmin == xmax:  #handle large floats that were unchanged by += 1
                    xmin = xmin * 0.99  #the alternative of increasing xmax could hit infinity
            if ymin == ymax:
                ymax += 1
                if ymin == ymax:
                    ymin = ymin * 0.99
            self.canvasBox = BoundingBox(float(xmin), float(ymin), float(xmax), float(ymax))

        w = self.calcVisibleBoxWidth()
        h = self.calcVisibleBoxHeight()
        if not self.visibleBox:
            # initialize minx/miny, but w/h must be set first to center properly
            self.visibleBox = Box(0, 0, w, h)
            self.visibleBox.xmin = self.canvasBox.xmin + (self.canvasBox.w / 2) * (1 - self.xzoomlevel)
            self.visibleBox.ymin = self.canvasBox.ymin + (self.canvasBox.h / 2) * (1 - self.yzoomlevel)
        else:
            self.visibleBox.w = w
            self.visibleBox.h = h

        if not self.cursorBox:
            cb_xmin = self.visibleBox.xmin
            cb_ymin = self.calcBottomCursorY()
            self.cursorBox = Box(cb_xmin, cb_ymin, self.canvasCharWidth, self.canvasCharHeight)

        self.plotlegends()
        if refresh:
            self.refresh()

    def calcTopCursorY(self):
        'ymin for the cursor that will align its top with the top edge of the graph'
        # + (1/4*self.canvasCharHeight) shifts the cursor up by 1 plotter pixel.
        # That shift makes the cursor contain the top data point.
        # Otherwise, the top data point would have y == plotterCursorBox.ymax,
        # which would not be inside plotterCursorBox. Shifting the cursor makes
        # plotterCursorBox.ymax > y for that top point.
        return self.visibleBox.ymax - self.cursorBox.h + (1/4*self.canvasCharHeight)

    def calcBottomCursorY(self):
        'ymin for the cursor that will align its bottom with the bottom edge of the graph'
        return self.visibleBox.ymin

    def plotlegends(self):
        # display labels
        for i, (legend, attr) in enumerate(self.legends.items()):
            self.addCommand(str(i+1), f'toggle-{i+1}', f'hideAttr("{attr}", "{attr}" not in hiddenAttrs)', f'toggle display of "{legend}"')
            if attr in self.hiddenAttrs:
                attr = 'graph_hidden'
            # add 2 characters to width to account for '1:' '2:' etc
            self.plotlegend(i, '%s:%s'%(i+1,legend), attr, width=self.legendwidth+2)

    def checkCursor(self):
        'override Sheet.checkCursor'
        if self.visibleBox and self.cursorBox:
            if self.cursorBox.h < self.canvasCharHeight:
                self.cursorBox.h = self.canvasCharHeight*3/4
            if self.cursorBox.w < self.canvasCharWidth:
                self.cursorBox.w = self.canvasCharWidth*3/4

        return False

    @property
    def xScaler(self):
        return self._coord.xScaler

    @property
    def yScaler(self):
        return self._coord.yScaler

    def calcVisibleBoxWidth(self):
        w = self.canvasBox.w * self.xzoomlevel
        if self.aspectRatio:
            h = self.canvasBox.h * self.yzoomlevel
            xratio = self.plotviewBox.w / w
            yratio = self.plotviewBox.h / h
            if xratio <= yratio:
                return w / self.aspectRatio
            else:
                return self.plotviewBox.w / (self.aspectRatio * yratio)
        else:
            return w

    def calcVisibleBoxHeight(self):
        h = self.canvasBox.h * self.yzoomlevel
        if self.aspectRatio:
            w = self.canvasBox.w * self.yzoomlevel
            xratio = self.plotviewBox.w / w
            yratio = self.plotviewBox.h / h
            if xratio < yratio:
                return self.plotviewBox.h / xratio
            else:
                return h
        else:
            return h

    def scaleX(self, dataX) -> int:
        'Convert data x coordinate to plotter pixel x coordinate.  Delegates to CoordinateTransformer.'
        return self._coord.scaleX(dataX)

    def scaleY(self, dataY) -> int:
        'Convert data y coordinate to plotter pixel y coordinate.  Delegates to CoordinateTransformer.'
        return self._coord.scaleY(dataY)

    def unscaleX(self, plotterX):
        'Convert plotter pixel x coordinate to data x coordinate.  Delegates to CoordinateTransformer.'
        return self._coord.unscaleX(plotterX)

    def unscaleY(self, plotterY):
        'Convert plotter pixel y coordinate to data y coordinate.  Delegates to CoordinateTransformer.'
        return self._coord.unscaleY(plotterY)

    def canvasW(self, plotterWidth):
        'Convert plotter pixel width to data coordinate width.  Delegates to CoordinateTransformer.'
        return self._coord.canvasW(plotterWidth)

    def canvasH(self, plotterHeight):
        'Convert plotter pixel height to data coordinate height.  Delegates to CoordinateTransformer.'
        return self._coord.canvasH(plotterHeight)

    def refresh(self):
        'triggers render() on next draw()'
        self.needsRefresh = True

    def render(self, h, w):
        'resets plotter, cancels previous render threads, spawns a new render'
        self.needsRefresh = False
        vd.cancelThread(*(t for t in self.currentThreads if t.name == 'render_async'))
        self.labels.clear()
        self.resetCanvasDimensions(h, w)
        self.resetBounds(refresh=False)
        self.render_async()

    @asyncthread
    def render_async(self):
        self.plot_elements()

    def plot_elements(self):
        '''Plot points, lines, and labels onto the plotter.

        All coordinate projection is delegated to self._coord (CoordinateTransformer).
        This method only iterates PlotDataset elements, clips to the visible data box,
        and dispatches projected pixel coordinates to plotpixel/plotline/plotlabel.
        The invert_y strategy (for graphs) is determined by self._coord.invert_y.
        '''
        self.resetBounds(refresh=False)

        # All projection parameters come from the transformer; Canvas is strategy-agnostic.
        xmin, ymin, xmax, ymax, xfactor, yfactor, plotxmin, plotyref, invert_y = self._coord.renderContext()

        for vertexes, attr, row in Progress(self.plotData, 'rendering'):
            if len(vertexes) == 1:  # single point
                x1, y1 = vertexes[0]
                x1, y1 = float(x1), float(y1)
                if xmin <= x1 <= xmax and ymin <= y1 <= ymax:
                    px = plotxmin + round((x1 - xmin) * xfactor)
                    if invert_y:
                        py = plotyref - round((y1 - ymin) * yfactor)
                    else:
                        py = plotyref + round((y1 - ymin) * yfactor)
                    self.plotpixel(px, py, attr, row)
                continue

            prev_x, prev_y = vertexes[0]
            for x, y in vertexes[1:]:
                r = clipline(prev_x, prev_y, x, y, xmin, ymin, xmax, ymax)
                if r:
                    cx1, cy1, cx2, cy2 = r
                    px1 = plotxmin + float(cx1 - xmin) * xfactor
                    px2 = plotxmin + float(cx2 - xmin) * xfactor
                    if invert_y:
                        py1 = plotyref - float(cy1 - ymin) * yfactor
                        py2 = plotyref - float(cy2 - ymin) * yfactor
                    else:
                        py1 = plotyref + float(cy1 - ymin) * yfactor
                        py2 = plotyref + float(cy2 - ymin) * yfactor
                    self.plotline(px1, py1, px2, py2, attr, row)
                prev_x, prev_y = x, y

        for x, y, text, attr, row in Progress(self.gridlabels, 'labeling'):
            self.plotlabel(self._coord.scaleX(x), self._coord.scaleY(y), text, attr, row)

    def rowsWithinDataBox(self, xmin, ymin, xmax, ymax):
        'Return rows whose plotted points fall within the given data coordinate bounding box.  Works regardless of zoom, filter, or sort.'
        return self.plotData.rowsWithinDataBox(xmin, ymin, xmax, ymax,
                                               hiddenAttrs=self.hiddenAttrs,
                                               source=self.source)

    @asyncthread
    def deleteSourceRows(self, rows):
        rows = list(rows)
        self.source.copyRows(rows)
        rowids = {self.source.rowid(r):True for r in rows}
        self.source.deleteBy(lambda r,rowids=rowids: self.source.rowid(r) in rowids)
        self.reload()

Plotter.addCommand('v', 'visibility', 'options.disp_graph_labels = not options.disp_graph_labels', 'toggle disp_graph_labels option')

Canvas.addCommand(None, 'go-left', 'if cursorBox: sheet.cursorBox.xmin -= cursorBox.w', 'move cursor left by its width')
Canvas.addCommand(None, 'go-right', 'if cursorBox: sheet.cursorBox.xmin += cursorBox.w', 'move cursor right by its width' )
Canvas.addCommand(None, 'go-up', 'if cursorBox: sheet.cursorBox.ymin -= cursorBox.h', 'move cursor up by its height')
Canvas.addCommand(None, 'go-down', 'if cursorBox: sheet.cursorBox.ymin += cursorBox.h', 'move cursor down by its height')
Canvas.addCommand(None, 'go-leftmost', 'if cursorBox: sheet.cursorBox.xmin = visibleBox.xmin', 'move cursor to left edge of visible canvas')
Canvas.addCommand(None, 'go-rightmost', 'if cursorBox: sheet.cursorBox.xmin = visibleBox.xmax-cursorBox.w+(1/2*canvasCharWidth)', 'move cursor to right edge of visible canvas')
Canvas.addCommand(None, 'go-top',    'if cursorBox: sheet.cursorBox.ymin = sheet.calcTopCursorY()', 'move cursor to top edge of visible canvas')
Canvas.addCommand(None, 'go-bottom', 'if cursorBox: sheet.cursorBox.ymin = sheet.calcBottomCursorY()', 'move cursor to bottom edge of visible canvas')

Canvas.addCommand(None, 'go-pagedown', 't=(visibleBox.ymax-visibleBox.ymin); sheet.cursorBox.ymin += t; sheet.visibleBox.ymin += t; refresh()', 'move cursor down to next visible page')
Canvas.addCommand(None, 'go-pageup', 't=(visibleBox.ymax-visibleBox.ymin); sheet.cursorBox.ymin -= t; sheet.visibleBox.ymin -= t; refresh()', 'move cursor up to previous visible page')

Canvas.addCommand('zh', 'go-left-small', 'sheet.cursorBox.xmin -= canvasCharWidth', 'move cursor left one character')
Canvas.addCommand('zl', 'go-right-small', 'sheet.cursorBox.xmin += canvasCharWidth', 'move cursor right one character')
Canvas.addCommand('zj', 'go-down-small', 'sheet.cursorBox.ymin += canvasCharHeight', 'move cursor down one character')
Canvas.addCommand('zk', 'go-up-small', 'sheet.cursorBox.ymin -= canvasCharHeight', 'move cursor up one character')

Canvas.addCommand('gH', 'resize-cursor-halfwide', 'sheet.cursorBox.w /= 2', 'halve cursor width')
Canvas.addCommand('gL', 'resize-cursor-doublewide', 'sheet.cursorBox.w *= 2', 'double cursor width')
Canvas.addCommand('gJ','resize-cursor-halfheight', 'sheet.cursorBox.h /= 2', 'halve cursor height')
Canvas.addCommand('gK', 'resize-cursor-doubleheight', 'sheet.cursorBox.h *= 2', 'double cursor height')

Canvas.addCommand('H', 'resize-cursor-thinner', 'sheet.cursorBox.w -= canvasCharWidth', 'decrease cursor width by one character')
Canvas.addCommand('L', 'resize-cursor-wider', 'sheet.cursorBox.w += canvasCharWidth', 'increase cursor width by one character')
Canvas.addCommand('J', 'resize-cursor-taller', 'sheet.cursorBox.h += canvasCharHeight', 'increase cursor height by one character')
Canvas.addCommand('K', 'resize-cursor-shorter', 'sheet.cursorBox.h -= canvasCharHeight', 'decrease cursor height by one character')
Canvas.addCommand('zz', 'zoom-cursor', 'zoomTo(cursorBox)', 'set visible bounds to cursor')

Canvas.addCommand('-', 'zoomout-cursor', 'tmp=cursorBox.center; incrZoom(options.disp_zoom_incr); fixPoint(plotviewBox.center, tmp)', 'zoom out from cursor center')
Canvas.addCommand('+', 'zoomin-cursor', 'tmp=cursorBox.center; incrZoom(1.0/options.disp_zoom_incr); fixPoint(plotviewBox.center, tmp)', 'zoom into cursor center')
Canvas.addCommand('_', 'zoom-all', 'sheet.canvasBox = None; sheet.visibleBox = None; sheet.xzoomlevel=sheet.yzoomlevel=1.0; resetBounds()', 'zoom to fit full extent')
Canvas.addCommand('z_', 'set-aspect', 'sheet.aspectRatio = float(input("aspect ratio=", value=aspectRatio)); refresh()', 'set aspect ratio')

# set cursor box with left click
Canvas.addCommand('BUTTON1_PRESSED', 'start-cursor', 'startCursor()', 'start cursor box with left mouse button press')
Canvas.addCommand('BUTTON1_RELEASED', 'end-cursor', 'cm=canvasMouse; setCursorSize(cm) if cm else None; sheet.brushSelect() if cm and options.auto_brush_select else None', 'end cursor box with left mouse button release; auto-select source rows if auto_brush_select is enabled')
Canvas.addCommand('BUTTON1_CLICKED', 'remake-cursor', 'startCursor(); cm=canvasMouse; setCursorSize(cm) if cm else None; sheet.brushSelect() if cm and options.auto_brush_select else None', 'end cursor box with left mouse button release; auto-select source rows if auto_brush_select is enabled')
Canvas.bindkey('BUTTON1_DOUBLE_CLICKED', 'remake-cursor')
Canvas.bindkey('BUTTON1_TRIPLE_CLICKED', 'remake-cursor')

Canvas.addCommand('BUTTON3_PRESSED', 'start-move', 'cm=canvasMouse; sheet.anchorPoint = cm if cm else None', 'mark grid point to move')
Canvas.addCommand('BUTTON3_RELEASED', 'end-move', 'fixPoint(plotterMouse, anchorPoint) if anchorPoint else None', 'mark canvas anchor point')
# A click does not actually move the canvas, but gives useful UI feedback. It helps users understand that they can do press-drag-release.
Canvas.addCommand('BUTTON3_CLICKED', 'move-canvas',  '', 'move canvas (in place)')
Canvas.bindkey('BUTTON3_DOUBLE_CLICKED', 'move-canvas')
Canvas.bindkey('BUTTON3_TRIPLE_CLICKED', 'move-canvas')

Canvas.addCommand('ScrollUp', 'zoomin-mouse', 'cm=canvasMouse; incrZoom(1.0/options.disp_zoom_incr) if cm else fail("cannot zoom in on unplotted canvas"); fixPoint(plotterMouse, cm)', 'zoom in with scroll wheel')
Canvas.addCommand('ScrollDown', 'zoomout-mouse', 'cm=canvasMouse; incrZoom(options.disp_zoom_incr) if cm else fail("cannot zoom out on unplotted canvas"); fixPoint(plotterMouse, cm)', 'zoom out with scroll wheel')

Canvas.addCommand('s', 'select-cursor', 'sheet.brushSelect()', 'select rows on source sheet contained within canvas cursor; records bbox for replay')
Canvas.addCommand('t', 'stoggle-cursor', 'sheet.brushToggle()', 'toggle selection of rows on source sheet contained within canvas cursor; records bbox for replay')
Canvas.addCommand('u', 'unselect-cursor', 'sheet.brushUnselect()', 'unselect rows on source sheet contained within canvas cursor; records bbox for replay')
Canvas.addCommand('Enter', 'dive-cursor', 'bboxstr=sheet.formatBbox(sheet.cursorBox); vd.setLastArgs(bboxstr); xmin,xmax,ymin,ymax=sheet.parseBbox(bboxstr); vs=copy(source); vs.rows=list(sheet.rowsWithinDataBox(xmin,ymin,xmax,ymax)); vd.push(vs)', 'open sheet of source rows contained within canvas cursor; records bbox for replay')
Canvas.addCommand('d', 'delete-cursor', 'bboxstr=sheet.formatBbox(sheet.cursorBox); vd.setLastArgs(bboxstr); xmin,xmax,ymin,ymax=sheet.parseBbox(bboxstr); deleteSourceRows(sheet.rowsWithinDataBox(xmin,ymin,xmax,ymax))', 'delete rows on source sheet contained within canvas cursor; records bbox for replay')

Canvas.addCommand('gs', 'select-visible', 'sheet.brushVisibleSelect()', 'select rows on source sheet visible on screen; records bbox for replay')
Canvas.addCommand('gt', 'stoggle-visible', 'sheet.brushVisibleToggle()', 'toggle selection of rows on source sheet visible on screen; records bbox for replay')
Canvas.addCommand('gu', 'unselect-visible', 'sheet.brushVisibleUnselect()', 'unselect rows on source sheet visible on screen; records bbox for replay')
Canvas.addCommand('gEnter', 'dive-visible', 'bboxstr=sheet.formatBbox(sheet.visibleBox); vd.setLastArgs(bboxstr); xmin,xmax,ymin,ymax=sheet.parseBbox(bboxstr); vs=copy(source); vs.rows=list(sheet.rowsWithinDataBox(xmin,ymin,xmax,ymax)); vd.push(vs)', 'open sheet of source rows visible on screen; records bbox for replay')
Canvas.addCommand('gd', 'delete-visible', 'bboxstr=sheet.formatBbox(sheet.visibleBox); vd.setLastArgs(bboxstr); xmin,xmax,ymin,ymax=sheet.parseBbox(bboxstr); deleteSourceRows(sheet.rowsWithinDataBox(xmin,ymin,xmax,ymax))', 'delete rows on source sheet visible on screen; records bbox for replay')

Canvas.addCommand('', 'select-bbox', 'bbox=input("select bbox xmin xmax ymin ymax: ", defaultLast=True); sheet.selectBbox(bbox)', 'select rows within data bounding box "xmin xmax ymin ymax"')
Canvas.addCommand('', 'stoggle-bbox', 'bbox=input("toggle bbox xmin xmax ymin ymax: ", defaultLast=True); sheet.stoggleBbox(bbox)', 'toggle rows within data bounding box "xmin xmax ymin ymax"')
Canvas.addCommand('', 'unselect-bbox', 'bbox=input("unselect bbox xmin xmax ymin ymax: ", defaultLast=True); sheet.unselectBbox(bbox)', 'unselect rows within data bounding box "xmin xmax ymin ymax"')

Canvas.addCommand('"s', 'save-selection', 'name=input("save selection as: "); sheet.saveNamedSelection(name)', 'save current cursor region as a named selection')
Canvas.addCommand('"l', 'load-selection', 'vd.selections.reload(); names=[s.name for s in vd.selections]; name=input("load selection: ", completions=names) if names else fail("no saved selections"); sheet.loadNamedSelection(name)', 'apply a saved named selection to select rows on the source sheet')
Canvas.addCommand('"d', 'delete-selection', 'vd.selections.reload(); names=[s.name for s in vd.selections]; name=input("delete selection: ", completions=names) if names else fail("no saved selections"); sheet.deleteNamedSelection(name)', 'delete a saved named selection')

vd.addGlobals({
    'Canvas': Canvas,
    'Plotter': Plotter,
    'BoundingBox': BoundingBox,
    'Box': Box,
    'Point': Point,
    'PlotDataset': PlotDataset,
    'CoordinateTransformer': CoordinateTransformer,
    'RowIdentityMixin': RowIdentityMixin,
    'BrushSelectorMixin': BrushSelectorMixin,
    'selections': vd.selections,
})

vd.addMenuItems('''
    Plot > Resize cursor > height > double > resize-cursor-doubleheight
    Plot > Resize cursor > height > half > resize-cursor-halfheight
    Plot > Resize cursor > height > shorter > resize-cursor-shorter
    Plot > Resize cursor > height > taller > resize-cursor-taller
    Plot > Resize cursor > width > double > resize-cursor-doublewide
    Plot > Resize cursor > width > half > resize-cursor-halfwide
    Plot > Resize cursor > width > thinner > resize-cursor-thinner
    Plot > Resize cursor > width > wider > resize-cursor-wider
    Plot > Resize graph > X axis > resize-x-input
    Plot > Resize graph > Y axis > resize-y-input
    Plot > Resize graph > aspect ratio > set-aspect
    Plot > Zoom > out > zoomout-cursor
    Plot > Zoom > in > zoomin-cursor
    Plot > Zoom > cursor > zoom-all
    Plot > Select > cursor region > select-cursor
    Plot > Select > cursor region > toggle > stoggle-cursor
    Plot > Select > cursor region > unselect > unselect-cursor
    Plot > Select > visible region > select-visible
    Plot > Select > visible region > toggle > stoggle-visible
    Plot > Select > visible region > unselect > unselect-visible
    Plot > Select > by bbox input > select-bbox
    Plot > Select > by bbox input > toggle > stoggle-bbox
    Plot > Select > by bbox input > unselect > unselect-bbox
    Plot > Named selection > save cursor > save-selection
    Plot > Named selection > load > load-selection
    Plot > Named selection > delete > delete-selection
    View > Open subsheet > from cursor > dive-cursor
    View > Open subsheet > visible region > dive-visible
    Edit > Delete > under cursor > delete-cursor
    Edit > Delete > visible region > delete-visible
''')
