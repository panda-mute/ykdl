# -*- coding: utf-8 -*-

from ._common import *
from .singlemultimedia import contentTypes


# TODO: subtitles support
# REF: https://developer.mozilla.org/zh-CN/docs/Web/API/WebVTT_API

pattern1 = r'''(?ix)
["'](
    (?:https?:|\\?/)[^"']+?\.
    (
        m3u8?                       | # !H5 list/HLS
        mp4|webm                    | # video/audio
        f4v|flv|ts                  | # !H5 video
        mov|qt|m4[pv]|og[mv]        | # video
        ogg|3gp|mpe?g               | # video/audio
        mp3|flac|wave?|oga|aac|weba   # audio
    )
    (?:\?[^"']+)?
)["']
'''
pattern2 = r'''(?ix)
<(?:video|audio|source)[^>]+?src=["'](
    (?:https?:|\\?/)[^"']+
)["']
[^>]+?(?:type=["'](
    (?:video|audio)/[^"']+
)["'])?
'''

class GeneralSimple(VideoExtractor):
    name = 'GeneralSimple (通用简单)'

    def prepare(self):
        info = VideoInfo(self.name)

        html = get_content(self.url)

        info.title = match1(html, '<meta property="og:title" content="([^"]+)',
                                  '<title>(.+?)</title>')

        ext = ctype = None
        for i in range(2):
            _ = match(html, pattern1)
            url, ext = _ and _ or (_, _)
            if url is None:
                _ = match(html, pattern2)
                url, ctype = _ and _ or (_, _)
            if url:
                break
            elif i == 0:
                html = unquote(unescape(html))

        if url:
            url = json.loads('"{url}"'.format(**vars()))
            url = match1(url, '.+(https?://.+)') or url  # redirect clear
            if url[:2] == '//':
                url = self.url[:self.url.find('/')] + url
            elif url[0] == '/':
                url = self.url[:self.url.find('/', 9)] + url
            if ext is None:
                ext = contentTypes.get(ctype) or url_info(url)[1] or (
                        str(ctype).lower().startswith('audio') and 'mp3' or 'mp4')
            if ext[:3] == 'm3u':
                info.stream_types, info.streams = load_m3u8_playlist(url)
            else:
                info.stream_types.append('current')
                info.streams['current'] = {
                    'container': ext,
                    'video_profile': 'current',
                    'src': [url],
                    'size': 0
                }
            self.parser = lambda *a: info
            return info

site = GeneralSimple()
