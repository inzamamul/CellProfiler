# coding=utf-8
"""CellProfilerGUI package

The CellProfilerGUI package holds the viewer and controller portions
of the cell profiler program
"""
from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import

from future import standard_library
standard_library.install_aliases()
from builtins import *
import cellprofiler.icons
import os
import sys

cp_image = None


def get_cp_image():
    """The CellProfiler icon as a wx.Image"""
    global cp_image
    if cp_image is None:
        cp_image = cellprofiler.icons.get_builtin_image('CellProfilerIcon')
    return cp_image


def get_cp_bitmap(size=None):
    """The CellProfiler icon as a wx.Bitmap"""
    import wx
    img = get_cp_image()
    if size is not None:
        img.Rescale(size, size, wx.IMAGE_QUALITY_HIGH)
    return wx.BitmapFromImage(img)


def get_cp_icon(size=None):
    """The CellProfiler icon as a wx.Icon"""
    import wx
    if sys.platform.startswith('win'):
        path = os.path.join(cellprofiler.icons.get_builtin_images_path(), "CellProfilerIcon.ico")
        icon = wx.EmptyIcon()
        icon.LoadFile(path, wx.BITMAP_TYPE_ICO)
        return icon
    icon = wx.EmptyIcon()
    icon.CopyFromBitmap(get_cp_bitmap(size))
    return icon


BV_DOWN = "down"
BV_UP   = "up"


def draw_item_selection_rect(window, dc, rect, flags):
    """Replacement for RendererNative.DrawItemSelectionRect

    window - draw in this window

    dc - device context to use for drawing

    rect - draw selection UI inside this rectangle

    flags - a combination of wx.CONTROL_SELECTED, wx.CONTROL_CURRENT and
            wx.CONTROL_FOCUSED

    This function fixes a bug in the Carbon implementation for drawing
    with wx.CONTROL_CURRENT and not wx.CONTROL_SELECTED.
    """
    # Bug in carbon DrawItemSelectionRect uses
    # uninitialized color for the rectangle
    # if it's not selected.
    #
    # Optimistically, I've coded it so that it
    # might work in Cocoa
    #
    import wx
    if sys.platform != 'darwin':
        wx.RendererNative.Get().DrawItemSelectionRect(
                window, dc, rect, flags)
    elif flags & wx.CONTROL_SELECTED:
        if flags & wx.CONTROL_FOCUSED:
            color = wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHT)
        else:
            color = wx.SystemSettings.GetColour(wx.SYS_COLOUR_INACTIVECAPTION)
        old_brush = dc.Brush
        new_brush = wx.Brush(color)
        dc.Brush = new_brush
        dc.Pen = wx.TRANSPARENT_PEN
        dc.DrawRectangleRect(rect)
        dc.Brush = old_brush
        new_brush.Destroy()
    elif flags & wx.CONTROL_CURRENT:
        #
        # On the Mac, draw a rectangle with the highlight pen and a null
        # brush.
        #
        if flags & wx.CONTROL_FOCUSED:
            pen_color = wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHT)
        else:
            pen_color = wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT)
        old_brush = dc.Brush
        dc.Brush = wx.TRANSPARENT_BRUSH
        old_pen = dc.Pen
        dc.Pen = wx.Pen(pen_color, width=2)
        dc.DrawRectangle(rect.Left, rect.Top, rect.Width, rect.Height)
