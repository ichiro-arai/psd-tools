# -*- coding: utf-8 -*-
from __future__ import absolute_import

import pytest
from psd_tools import PSDImage
from psd_tools.user_api.psd_image import SmartObjectLayer

from .utils import decode_psd

FILE_NAMES = (
    'smart-object-vector-mask.psd',
)


@pytest.mark.parametrize('filename', FILE_NAMES)
def test_smart_object(filename):
    psd = PSDImage(decode_psd(filename))
    assert SmartObjectLayer == psd.layers[0].__class__
