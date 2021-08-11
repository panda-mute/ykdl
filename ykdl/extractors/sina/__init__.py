#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re

def get_extractor(url):
    if 'open.sina' in url:
        from . import openc as s
    elif '.ivideo.sina' in url:
        from . import embed as s
    else:
        from . import video as s

    return s.site, url
