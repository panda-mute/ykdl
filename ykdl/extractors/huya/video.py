# -*- coding: utf-8 -*-

from .._common import *


class HuyaVideo(VideoExtractor):
    name = 'huya video (虎牙视频)'

    quality_2_id_profile = {
        'yuanhua': ['BD', '原画'],
           '1300': ['TD', '超清'],
          #'TODO': ['HD', '高清'],
            '350': ['SD', '流畅']
    }

    def prepare(self):
        info = VideoInfo(self.name)

        self.vid = match1(self.url, 'play/(\d+)')
        html = get_content(self.url)
        if not self.vid:
            self.vid = match1(html, 'data-vid="(\d+)')
        title = match1(html, '<h1 class="video-title">(.+?)</h1>')
        info.artist = artist = match1(html,
                            "<div class='video-author'>[\s\S]+?<h3>(.+?)</h3>")
        info.title = '{title} - {artist}'.format(**vars())

        t1 = int(time.time() * 1000)
        t2 = t1 + random.randrange(5, 10)
        rnd = str(random.random()).replace('.', '')
        data = get_response('https://v-api-player-ssl.huya.com/',
                        params={
                            'callback': 'jQuery1124{rnd}_{t1}'.format(**vars()),
                            'r': 'vhuyaplay/video',
                            'vid': self.vid,
                            'format': 'mp4,m3u8',
                            '_': t2
                        }).json()
        assert data['code'] == 1, data['message']
        data = data['result']['items']

        for stream_date in data:
            ext = stream_date['format']
            quality =stream_date['definition']
            stream, video_profile = self.quality_2_id_profile[quality]
            if stream not in info.stream_types:
                info.stream_types.append(stream)
            elif ext == 'm3u8':
                # prefer mp4
                continue
            url = stream_date['transcode']['urls'][0]
            info.streams[stream] = {
                'container': ext,
                'video_profile': video_profile,
                'src': [url],
                'size' : int(stream_date['size'])
            }

        return info

site = HuyaVideo()
