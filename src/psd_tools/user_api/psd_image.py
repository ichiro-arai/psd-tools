# -*- coding: utf-8 -*-
from __future__ import (
    absolute_import, unicode_literals, division, print_function)

import logging
import collections
import weakref              # FIXME: there should be weakrefs in this module
import psd_tools.reader
import psd_tools.decoder
from psd_tools.constants import (
    TaggedBlock, SectionDivider, BlendMode, TextProperty, PlacedLayerProperty,
    SzProperty, ChannelID)
from psd_tools.user_api.layers import group_layers
from psd_tools.user_api import pymaging_support
from psd_tools.user_api import pil_support
from psd_tools.user_api.embedded import Embedded
from psd_tools.user_api.effects import get_effects

logger = logging.getLogger(__name__)


Size = collections.namedtuple('Size', 'width, height')


class BBox(collections.namedtuple('BBox', 'x1, y1, x2, y2')):
    """
    Bounding box tuple representing (x1, y1, x2, y2).
    """
    @property
    def width(self):
        """Width of the bounding box."""
        return self.x2 - self.x1

    @property
    def height(self):
        """Height of the bounding box."""
        return self.y2 - self.y1


class PlacedLayerData(object):
    """Placed layer data."""
    def __init__(self, placed_layer_block):
        self._info = dict(placed_layer_block)

    @property
    def transform(self):
        return self._info[PlacedLayerProperty.TRANSFORM].items

    @property
    def size(self):
        return dict(self._info[PlacedLayerProperty.SIZE].items)


class Mask(object):
    """Mask data attached to a layer."""
    def __init__(self, layer):
        self.mask_data = layer._info.mask_data
        self._decoded_data = layer._psd.decoded_data
        self._layer_index = layer._index

    @property
    def bbox(self, real_mask=True):
        """BBox(x1, y1, x2, y2) namedtuple with mask bounding box."""
        if real_mask and self.mask_data.real_flags:
            return BBox(self.mask_data.real_left, self.mask_data.real_top,
                        self.mask_data.real_right, self.mask_data.real_bottom)
        else:
            return BBox(self.mask_data.left, self.mask_data.top,
                        self.mask_data.right, self.mask_data.bottom)

    @property
    def background_color(self):
        """Background color."""
        return self.mask_data.background_color

    @property
    def is_valid(self):
        """Returns whether the bounding box has a valid size."""
        bbox = self.bbox
        return bbox.width > 0 and bbox.height > 0

    def as_PIL(self, real_mask=True):
        """
        Returns a PIL image for the mask.

        If ``real_mask`` is True, extract real mask consisting of both bitmap
        and vector mask.

        Returns ``None`` if the mask has zero size.
        """
        if not self.is_valid:
            return None
        return pil_support.extract_layer_mask(self._decoded_data,
                                              self._layer_index,
                                              real_mask)

    def __repr__(self):
        bbox = self.bbox
        return "<%s: size=%dx%d, x=%d, y=%d>" % (
            self.__class__.__name__, bbox.width, bbox.height, bbox.x1,
            bbox.y1)


class Pattern(object):
    """Pattern data."""
    def __init__(self, pattern):
        self._pattern = pattern

    @property
    def pattern_id(self):
        """Pattern UUID."""
        return self._pattern.pattern_id

    @property
    def name(self):
        """Name of the pattern."""
        return self._pattern.name

    @property
    def width(self):
        """Width of the pattern."""
        return self._pattern.point[1]

    @property
    def height(self):
        """Height of the pattern."""
        return self._pattern.point[0]

    def as_PIL(self):
        """Returns a PIL image for this pattern."""
        return pil_support.pattern_to_PIL(self._pattern)

    def __repr__(self):
        return "<%s: name='%s' size=%dx%d>" % (
            self.__class__.__name__, self.name, self.width, self.height)


class _RawLayer(object):
    """
    Layer groups and layers are internally both 'layers' in PSD;
    they share some common properties.
    """
    def __init__(self, parent, index, kind):
        self._parent = parent
        self._psd = parent._psd
        self._index = index
        self._kind = kind
        self._clip_layers = []

    @property
    def name(self):
        """Layer name (as unicode). """
        return self._tagged_blocks.get(
            TaggedBlock.UNICODE_LAYER_NAME,
            self._info.name
        )

    @property
    def kind(self):
        """
        Kind of this layer, either group, pixel, shape, type, or smartobject.
        """
        return self._kind

    @property
    def visible(self):
        """Layer visibility. Doesn't take group visibility in account."""
        return self._info.flags.visible

    @property
    def visible_global(self):
        """Layer visibility. Takes group visibility in account."""
        return self.visible and self.parent.visible_global

    @property
    def layer_id(self):
        """ID of the layer."""
        return self._tagged_blocks.get(TaggedBlock.LAYER_ID)

    @property
    def opacity(self):
        """Opacity of this layer."""
        return self._info.opacity

    @property
    def parent(self):
        """Parent of this layer."""
        return self._parent

    @property
    def blend_mode(self):
        """Blend mode of this layer."""
        return self._info.blend_mode

    def has_mask(self):
        """Returns if the layer has a mask."""
        return True if self._index and self._info.mask_data else False

    @property
    def mask(self):
        """
        Returns mask associated with this layer.

        :rtype: Mask
        """
        return Mask(self) if self.has_mask() else None

    @property
    def clip_layers(self):
        """
        Returns clip layers associated with this layer.

        :rtype: list
        """
        return self._clip_layers

    @property
    def effects(self):
        """
        Effects associated with this layer.

        :rtype: psd_tools.user_api.effects.Effects
        """
        return get_effects(self)

    @property
    def _info(self):
        return self._psd._layer_info(self._index)

    @property
    def _tagged_blocks(self):
        return dict(self._info.tagged_blocks)


class _VisibleLayer(_RawLayer):
    """PSD base layer."""
    def as_PIL(self):
        """Returns a PIL image for this layer."""
        return self._psd._layer_as_PIL(self._index)

    def as_pymaging(self):
        """Returns a pymaging.Image for this PSD file."""
        return self._psd._layer_as_pymaging(self._index)

    @property
    def bbox(self):
        """BBox(x1, y1, x2, y2) namedtuple with layer bounding box."""
        info = self._info
        return BBox(info.left, info.top, info.right, info.bottom)

    def __repr__(self):
        bbox = self.bbox
        return (
            "<%s: %r, size=%dx%d, x=%d, y=%d, visible=%d, mask=%s, "
            "effects=%s>" % (
                self.__class__.__name__, self.name, bbox.width, bbox.height,
                bbox.x1, bbox.y1, self.visible, self.mask, self.effects))


class AdjustmentLayer(_VisibleLayer):
    """PSD adjustment layer wrapper."""
    def __init__(self, parent, index):
        super(AdjustmentLayer, self).__init__(parent, index, 'adjustment')

    def __repr__(self):
        return "<%s: %r, visible=%s>" % (
            self.__class__.__name__, self.name, self.visible)


class PixelLayer(_VisibleLayer):
    """PSD pixel layer wrapper."""
    def __init__(self, parent, index):
        super(PixelLayer, self).__init__(parent, index, 'pixel')


class ShapeLayer(_VisibleLayer):
    """PSD shape layer wrapper."""
    def __init__(self, parent, index):
        super(ShapeLayer, self).__init__(parent, index, 'shape')

    def as_PIL(self, vector=False):
        """Returns a PIL image for this layer."""
        if vector or (self._info.flags.pixel_data_irrelevant and
                      self._is_sizeless()):
            # TODO: Replace polygon with bezier curve.
            return pil_support.draw_polygon(self.bbox, self.anchors,
                                            self._get_color())
        else:
            return self._psd._layer_as_PIL(self._index)

    def as_pymaging(self):
        """Returns a pymaging.Image for this layer."""
        raise NotImplementedError

    @property
    def bbox(self):
        """BBox(x1, y1, x2, y2) namedtuple of the shape."""
        if not self._is_sizeless():
            info = self._info
            return BBox(info.left, info.top, info.right, info.bottom)

        # If sizeless shape, calculate bbox.
        # TODO: Compute bezier curve.
        anchors = self.anchors
        if not anchors or len(anchors) < 2:
            # Could be all pixel fill.
            return BBox(0, 0, 0, 0)
        return BBox(min([p[0] for p in anchors]),
                    min([p[1] for p in anchors]),
                    max([p[0] for p in anchors]),
                    max([p[1] for p in anchors]))

    @property
    def anchors(self):
        """Anchor points of the shape [(x, y), (x, y), ...]."""
        blocks = self._tagged_blocks
        vmsk = blocks.get(TaggedBlock.VECTOR_MASK_SETTING1,
                          blocks.get(TaggedBlock.VECTOR_MASK_SETTING2))
        if not vmsk:
            return None
        return [(int(p['anchor'][1] * self._psd.header.width),
                 int(p['anchor'][0] * self._psd.header.height))
                for p in vmsk.path if p.get('selector') in (1, 2, 4, 5)]

    def _is_sizeless(self):
        info = self._info
        bbox = BBox(info.left, info.top, info.right, info.bottom)
        return bbox.width == 0 or bbox.height == 0

    def _get_color(self, default='black'):
        soco = self._tagged_blocks.get(TaggedBlock.SOLID_COLOR_SHEET_SETTING)
        if not soco:
            logger.warning("Gradient or pattern fill not supported")
            return default
        color_data = dict(soco.data.items).get(b'Clr ')
        if color_data.classID == b'RGBC':
            colors = dict(color_data.items)
            return (int(colors[b'Rd  '].value), int(colors[b'Grn '].value),
                    int(colors[b'Bl  '].value), int(self.opacity))
        else:
            return default


class SmartObjectLayer(_VisibleLayer):
    """PSD pixel layer wrapper."""
    def __init__(self, parent, index):
        super(SmartObjectLayer, self).__init__(parent, index, 'smartobject')
        self._placed = None
        placed_block = self._placed_layer_block()
        if placed_block:
            self._placed = dict(placed_block)

    def unique_id(self):
        if self._placed:
            return self._placed.get(PlacedLayerProperty.ID).value
        else:
            return None

    def linked_data(self):
        """
        Return linked layer data.
        """
        unique_id = self.unique_id()
        return self._psd.embedded.get(unique_id) if unique_id else None

    @property
    def transform_bbox(self):
        """BBox(x1, y1, x2, y2) namedtuple with layer transform box
        (Top Left and Bottom Right corners). The tranform of a layer the
        points for all 4 corners.
        """
        placed_layer_block = self._placed_layer_block()
        if not placed_layer_block:
            return None
        placed_layer_data = PlacedLayerData(placed_layer_block)

        transform = placed_layer_data.transform
        if not transform:
            return None
        return BBox(transform[0].value, transform[1].value,
                    transform[4].value, transform[5].value)

    @property
    def placed_layer_size(self):
        """BBox(x1, y1, x2, y2) namedtuple with original
        smart object content size.
        """
        placed_layer_block = self._placed_layer_block()
        if not placed_layer_block:
            return None
        placed_layer_data = PlacedLayerData(placed_layer_block)

        size = placed_layer_data.size
        if not size:
            return None
        return Size(size[SzProperty.WIDTH].value,
                    size[SzProperty.HEIGHT].value)

    def _placed_layer_block(self):
        blocks = self._tagged_blocks
        return blocks.get(
            TaggedBlock.SMART_OBJECT_PLACED_LAYER_DATA,
            blocks.get(
                TaggedBlock.PLACED_LAYER_DATA,
                blocks.get(
                    TaggedBlock.PLACED_LAYER_OBSOLETE1,
                    blocks.get(
                        TaggedBlock.PLACED_LAYER_OBSOLETE2))))

    def __repr__(self):
        bbox = self.bbox
        return (
            "<%s: %r, size=%dx%d, x=%d, y=%d, mask=%s, visible=%d, "
            "linked=%s>") % (
            self.__class__.__name__, self.name, bbox.width, bbox.height,
            bbox.x1, bbox.y1, self.mask, self.visible,
            self.linked_data())


class TypeLayer(_VisibleLayer):
    """
    PSD type layer.

    A type layer has text information such as fonts and paragraph settings.
    """
    def __init__(self, parent, index):
        super(TypeLayer, self).__init__(parent, index, 'type')
        self._type_info = self._tagged_blocks.get(
            TaggedBlock.TYPE_TOOL_OBJECT_SETTING)
        self.text_data = dict(self._type_info.text_data.items)

    @property
    def text(self):
        """Unicode string."""
        return self.text_data[TextProperty.TXT].value

    @property
    def matrix(self):
        """Matrix [xx xy yx yy tx ty] applies affine transformation."""
        return (self._type_info.xx, self._type_info.xy, self._type_info.yx,
                self._type_info.yy, self._type_info.tx, self._type_info.ty)

    @property
    def engine_data(self):
        """Type information in engine data format."""
        return self.text_data.get(b'EngineData')

    @property
    def fontset(self):
        """Font set."""
        return self.engine_data[b'DocumentResources'][b'FontSet']

    @property
    def writing_direction(self):
        """Writing direction."""
        return self.engine_data[b'EngineDict'][
            b'Rendered'][b'Shapes'][b'WritingDirection']

    @property
    def full_text(self):
        """Raw string including trailing newline."""
        return self.engine_data[b'EngineDict'][b'Editor'][b'Text']

    def style_spans(self):
        """Returns spans by text style segments."""
        text = self.full_text
        fontset = self.fontset
        engine_data = self.engine_data
        runlength = engine_data[b'EngineDict'][b'StyleRun'][b'RunLengthArray']
        runarray = engine_data[b'EngineDict'][b'StyleRun'][b'RunArray']

        start = 0
        spans = []
        for run, size in zip(runarray, runlength):
            runtext = text[start:start + size]
            stylesheet = run[b'StyleSheet'][b'StyleSheetData'].copy()
            stylesheet[b'Text'] = runtext
            stylesheet[b'Font'] = fontset[stylesheet.get(b'Font', 0)]
            spans.append(stylesheet)
            start += size
        return spans


class Group(_RawLayer):
    """PSD layer group."""

    def __init__(self, parent, index, layers):
        super(Group, self).__init__(parent, index, 'group')
        self.layers = layers

    @property
    def closed(self):
        divider = self._tagged_blocks.get(
            TaggedBlock.SECTION_DIVIDER_SETTING, None)
        if divider is None:
            return
        return divider.type == SectionDivider.CLOSED_FOLDER

    @property
    def bbox(self):
        """
        BBox(x1, y1, x2, y2) namedtuple with a bounding box for
        all layers in this group; None if a group has no children.
        """
        return combined_bbox(self.layers)

    def as_PIL(self):
        """
        Returns a PIL image for this group.
        This is highly experimental.
        """
        return merge_layers(self.layers, respect_visibility=True)

    def _add_layer(self, child):
        self.layers.append(child)

    def __repr__(self):
        return "<%s: %r, layer_count=%d, mask=%s, visible=%d>" % (
            self.__class__.__name__, self.name, len(self.layers), self.mask,
            self.visible)


class _RootGroup(Group):
    """A fake group for holding all layers."""

    @property
    def visible(self):
        return True

    @property
    def visible_global(self):
        return True

    @property
    def name(self):
        return "_RootGroup"


class PSDImage(object):
    """PSD image."""

    def __init__(self, decoded_data):
        self.header = decoded_data.header
        self.decoded_data = decoded_data

        # wrap decoded data to Layer and Group structures
        def make_layer(group, layer):
            index = layer['index']
            kind = layer['kind']
            if kind == 'group':
                child = Group(group, index, [])
                fill_group(child, layer)
            elif kind == 'adjustment':
                child = AdjustmentLayer(group, index)
            elif kind == 'type':
                child = TypeLayer(group, index)
            elif kind == 'shape':
                child = ShapeLayer(group, index)
            elif kind == 'pixel':
                child = PixelLayer(group, index)
            elif kind == 'smartobject':
                child = SmartObjectLayer(group, index)
            else:
                logger.critical("Unknown layer type (%s)" % (kind))
            return child

        def fill_group(group, data):
            for layer in data['layers']:
                child = make_layer(group, layer)
                group._add_layer(child)
                for clip in layer['clip_layers']:
                    child.clip_layers.append(make_layer(group, clip))

        self._psd = self
        fake_root_data = {'layers': group_layers(decoded_data), 'index': None}
        root = _RootGroup(self, None, [])
        fill_group(root, fake_root_data)

        self._fake_root_group = root
        self.layers = root.layers
        self.embedded = {linked.unique_id: Embedded(linked) for linked in
                         self._linked_layer_iter()}

    @classmethod
    def load(cls, path, encoding='utf8'):
        """Returns a new :class:`PSDImage` loaded from ``path``."""
        with open(path, 'rb') as fp:
            return cls.from_stream(fp, encoding)

    @classmethod
    def from_stream(cls, fp, encoding='utf8'):
        """Returns a new :class:`PSDImage` loaded from stream ``fp``."""
        decoded_data = psd_tools.decoder.parse(
            psd_tools.reader.parse(fp, encoding)
        )
        return cls(decoded_data)

    def as_PIL(self):
        """Returns a PIL image for this PSD file."""
        return pil_support.extract_composite_image(self.decoded_data)

    def as_PIL_merged(self):
        """
        Returns a PIL image for this PSD file.
        Image is obtained by merging all layers.
        This is highly experimental.
        """
        bbox = BBox(0, 0, self.header.width, self.header.height)
        return merge_layers(self.layers, bbox=bbox)

    def as_pymaging(self):
        """Returns a pymaging.Image for this PSD file."""
        return pymaging_support.extract_composite_image(self.decoded_data)

    @property
    def bbox(self):
        """
        BBox(x1, y1, x2, y2) namedtuple with a bounding box for
        all layers in this image; None if there are no image layers.

        This may differ from the image dimensions
        (img.header.width and img.header.heigth).
        """
        return combined_bbox(self.layers)

    @property
    def patterns(self):
        """Returns a dict of pattern (texture) data in PIL.Image."""
        blocks = self._tagged_blocks
        patterns = blocks.get(
            TaggedBlock.PATTERNS1,
            blocks.get(
                TaggedBlock.PATTERNS2,
                blocks.get(TaggedBlock.PATTERNS3, [])))
        return {p.pattern_id: Pattern(p) for p in patterns}

    @property
    def _tagged_blocks(self):
        return dict(self.decoded_data.layer_and_mask_data.tagged_blocks)

    def _layer_info(self, index):
        layers = self.decoded_data.layer_and_mask_data.layers.layer_records
        return layers[index]

    def _layer_as_PIL(self, index):
        return pil_support.extract_layer_image(self.decoded_data, index)

    def _layer_as_pymaging(self, index):
        return pymaging_support.extract_layer_image(self.decoded_data, index)

    def _linked_layer_iter(self):
        """Iterate over linked layers (smart objects / embedded files)."""
        from psd_tools.decoder.linked_layer import LinkedLayerCollection
        for block in self.decoded_data.layer_and_mask_data.tagged_blocks:
            if isinstance(block.data, LinkedLayerCollection):
                for layer in block.data.linked_list:
                    yield layer

    def print_tree(self, layers=None, indent=0, indent_width=2, **kwargs):
        """Print the layer tree structure."""
        if not layers:
            layers = self.layers
            print(((' ' * indent) + "{}").format(self), **kwargs)
            indent = indent + indent_width
        for l in layers:
            for clip in l.clip_layers:
                print(((' ' * indent) + "/{}").format(clip), **kwargs)
            print(((' ' * indent) + "{}").format(l), **kwargs)
            if isinstance(l, Group):
                self.print_tree(l.layers, indent + indent_width)

    def __repr__(self):
        return "<%s: size=%dx%d, layer_count=%d>" % (
            self.__class__.__name__, self.header.width, self.header.height,
            len(self.layers))


def combined_bbox(layers):
    """
    Returns a bounding box for ``layers`` or None if this is not possible.
    """
    bboxes = [layer.bbox for layer in layers
              if layer.bbox is not None and
              layer.bbox.width > 0 and layer.bbox.height > 0]
    if not bboxes:
        return None

    lefts, tops, rights, bottoms = zip(*bboxes)
    return BBox(min(lefts), min(tops), max(rights), max(bottoms))


def merge_layers(layers, respect_visibility=True,
                 skip_layer=lambda layer: False, bbox=None):
    """
    Merges layers together (the first layer is on top).

    By default hidden layers are not rendered;
    pass ``respect_visibility=False`` to render them.

    In order to skip some layers pass ``skip_layer`` function which
    should take ``layer`` as an argument and return True or False.

    If ``bbox`` is not None, it should be a 4-tuple with coordinates;
    returned image will be restricted to this rectangle.

    This is highly experimental.
    """

    # FIXME: this currently assumes PIL
    from PIL import Image

    if bbox is None:
        bbox = combined_bbox(layers)

    if bbox is None:
        return None

    result = Image.new(
        "RGBA",
        (bbox.width, bbox.height),
        color=(255, 255, 255, 0)  # fixme: transparency is incorrect
    )

    for layer in reversed(layers):

        if layer is None:
            continue

        if layer.bbox.width == 0 and layer.bbox.height == 0:
            continue

        if skip_layer(layer):
            continue

        if not layer.visible and respect_visibility:
            continue

        if isinstance(layer, psd_tools.Group):
            layer_image = merge_layers(
                layer.layers, respect_visibility, skip_layer)
        else:
            layer_image = layer.as_PIL()

        layer_image = pil_support.apply_opacity(layer_image, layer.opacity)

        x, y = layer.bbox.x1 - bbox.x1, layer.bbox.y1 - bbox.y1
        w, h = layer_image.size

        if x < 0 or y < 0:  # image doesn't fit the bbox
            x_overflow = - min(x, 0)
            y_overflow = - min(y, 0)
            logger.debug("cropping.. (%s, %s)", x_overflow, y_overflow)
            layer_image = layer_image.crop((x_overflow, y_overflow, w, h))
            x += x_overflow
            y += y_overflow

        if w+x > bbox.width or h+y > bbox.height:
            # FIXME
            logger.debug("cropping..")

        if layer.blend_mode == BlendMode.NORMAL:
            if layer_image.mode == 'RGBA':
                tmp = Image.new("RGBA", result.size, color=(255, 255, 255, 0))
                tmp.paste(layer_image, (x, y))
                result = Image.alpha_composite(result, tmp)
            elif layer_image.mode == 'RGB':
                result.paste(layer_image, (x, y))
            else:
                logger.warning(
                    "layer image mode is unsupported for merging: %s",
                    layer_image.mode)
                continue
        else:
            logger.warning("Blend mode is not implemented: %s",
                           BlendMode.name_of(layer.blend_mode))
            continue

    return result
