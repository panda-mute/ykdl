import os
import sys
import gzip
import zlib
import json
import socket
import functools
from io import BytesIO
from logging import getLogger
from collections import defaultdict
from http.client import HTTPResponse as _HTTPResponse
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, install_opener, build_opener, \
                           HTTPRedirectHandler as _HTTPRedirectHandler, \
                           AbstractHTTPHandler, URLError, HTTPError
try:
    from queue import SimpleQueue as Queue, Empty  # py37 and above
except ImportError:
    from queue import Queue, Empty

from .match import match1
from .xml2dict import xml2dict

logger = getLogger(__name__)


# Add HTTP persistent connections feature into urllib.request

_http_prefixes = 'https://', 'http://'
_http_conn_cache = defaultdict(Queue)
_headers_template = {
    'Host': '',
    'User-Agent': '',
    'Accept': '*/*'
}

def _split_conn_key(url):
    '''"scheme://host/path" --> "scheme://host"'''
    pp = url.find('/', 9)
    if pp > 0:
        return url[:pp]
    return url

def hit_conn_cache(url):
    '''Whether the giving URL does match a item exist in HTTP connection cache.'''
    if not url.startswith(_http_prefixes):
        raise ValueError('input should be a URL')
    return _split_conn_key(url) in _http_conn_cache

def clear_conn_cache():
    '''Clear the HTTP connection cache which is used by persistent connections.'''
    _http_conn_cache.clear()

def _do_open(self, http_class, req, **http_conn_args):
    '''Return an HTTPResponse object for the request, using http_class.

    http_class must implement the HTTPConnection API from http.client.
    
    There has some codes to handle persistent connections.
    '''
    host = req.host
    if not host:
        raise URLError('no host given')

    timeout = req.timeout
    conn_key = _split_conn_key(req._full_url)
    queue = _http_conn_cache[conn_key]

    try:
        h = queue.get_nowait()
    except Empty:
        h = http_class(host, timeout=timeout, **http_conn_args)
    else:
        h.sock.setblocking(False)
        try:
            h.sock.recv(1)
            h.close()  # drop legacy and disconnection
        except:
            if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
                timeout = socket.getdefaulttimeout()
            h.sock.settimeout(timeout)

    h.set_debuglevel(self._debuglevel)

    # keep the sequence in template
    headers = _headers_template.copy()
    headers.update(req.headers)
    headers.update(req.unredirected_hdrs)
    headers = {k.title(): v for k, v in headers.items()}

    for hdr in ('Connection', 'Proxy-Connection'):  # always do, ignore input
        headers.pop(hdr, None)

    if req._tunnel_host:
        # urllib.request only use header Proxy-Authorization
        # Move all tunnel headers which user input, that has be needed
        tunnel_headers = {k: v for k, v in headers.items()
                          if k.startswith('Proxy-')}
        for hdr in tunnel_headers:
            headers.pop(hdr)
        if h.sock is None:  # add reuse check to bypass reset error
            h.set_tunnel(req._tunnel_host, headers=tunnel_headers)

    req_args = {}
    if hasattr(http_class, '_is_textIO'):  # py35 and below are False
                                           # uncommonly use in our modules
        req_args['encode_chunked'] = req.has_header('Transfer-encoding')
    try:
        try:
            h.request(req.get_method(), req.selector, req.data, headers,
                      **req_args)
        except OSError as err:  # timeout error
            raise URLError(err)
        r = h.getresponse()
    except:
        h.close()
        raise

    # Use functools.partial to avoid circular references
    r.queue_put = functools.partial(queue.put, h)

    r.url = req.get_full_url()
    r.msg = r.reason
    return r

def _close_conn(self):
    fp, self.fp = self.fp, None
    try:
        fp.close()
    finally:
        if hasattr(self, 'queue_put'):
            self.queue_put()    # last request is over, ready for reuse
            del self.queue_put  # clear, can be run only once

AbstractHTTPHandler.do_open = _do_open   #
_HTTPResponse._close_conn = _close_conn  # monkey patch, but secure


# Custom HTTP redirect handler

class HTTPRedirectHandler(_HTTPRedirectHandler):
    '''Log all responses during redirect, support specify max redirections
    
    MUST call from get_response(), or fallback to original HTTPRedirectHandler
    '''
    max_repeats = 2
    max_redirections = 5
    rmethod = 'GET', 'HEAD', 'POST'  # allow redirect POST method
    amcodes = 301, 302, 303          # codes redirect alterable method
    fmcodes = 307, 308               # codes redirect fixedly method
    rcodes = amcodes + fmcodes

    def __init__(self):
        for code in self.rcodes:
            setattr(self, 'http_error_%d' % code, self.http_error_code)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # If does not request from this module, go to original method
        if not hasattr(req, 'locations'):
            if code == 308 and not hasattr(_HTTPRedirectHandler, 'http_error_308'):
                return
            return super().redirect_request(req, fp, code, msg, headers, newurl)

        logger.debug('Redirect to URL: ' + newurl)
        req.locations.append(newurl)

        method = req.get_method()
        if method not in self.rmethod:
            raise HTTPError(req.full_url, code, msg, headers, fp)

        data = req.data  # is used by fixedly method redirections
        newheaders = {k.lower(): v for k, v in req.headers.items()}
        if code in self.rmethod:
            for header in ('content-length', 'content-type', 'transfer-encoding'):
                newheaders.pop(header, None)
            data = None
            if method != 'HEAD':
                method = 'GET'

        # Useless in our modules, memo for somebody may needs
        #newurl = newurl.replace(' ', '%20')

        newreq = Request(newurl, data=data, headers=newheaders,
                         origin_req_host=req.origin_req_host,
                         unverifiable=True, method=method)

        # Important attributes MUST be passed to new request
        newreq.headget = req.headget
        newreq.locations = req.locations
        newreq.responses = req.responses
        return newreq

    def http_error_code(self, req, fp, code, msg, headers):
        # If does not request from this module, go to original method
        if not hasattr(req, 'locations'):
            if code == 308 and not hasattr(_HTTPRedirectHandler, 'http_error_308'):
                return
            return super().http_error_302(req, fp, code, msg, headers)

        max_redirections = getattr(req, 'max_redirections', None)
        if max_redirections is not None:
            self.max_redirections = max_redirections
        req.responses.append(HTTPResponse(req, fp, finish=False))
        try:
            newres = super().http_error_302(req, fp, code, msg, headers)
        except HTTPError:
            if req.headget or fp._method == 'HEAD':
                fp.url = req.locations[-1]  # fake response, reuse last one
                return fp
            raise
        return newres


# Custom HTTP response

class HTTPResponse:
    def __init__(self, request, response, encoding=None, *, finish=True):
        '''Wrap urllib.request.Request and http.client.HTTPResponse.

        Params:
            `encoding` is used by decode responsed content.

            `finish`, only has effect on redirections.

                `True` (default)
                    is used by last response which return from opener.
                `False` (explicit)
                    is used by redirections which call from our handler.

            `request` and `response` referred to see get_response() codes.
        '''
        self.request = request
        self.method = response._method
        self.url = response.url
        self.locations = request.locations
        self.status = response.status
        self.reason = response.reason
        self.headers = self.msg = headers = response.headers
        self.raw = data = not request.headget and response.read() or b''
        response.close()
        if data:
            # Handle HTTP compression for gzip and deflate (zlib)
            ce = None
            if 'Content-Encoding' in headers:
                ce = headers['Content-Encoding']
            else:
                payload = headers.get_payload()
                if isinstance(payload, list):
                    payload = payload[0]
                if isinstance(payload, str):
                    ce =  match1(payload, '(?i)content-encoding:\s*([\w-]+)')
            if ce == 'gzip':
                data = ungzip(data)
            elif ce == 'deflate':
                data = undeflate(data)
        self.content = data
        self._encoding = encoding
        if finish and self.locations:
            self._responses = request.responses
        else:
            self._responses = []

    def __repr__(self):
        return '<%s object at %s>' % (type(self).__name__, hex(id(self)))

    def __str__(self):
        return self.text

    def close(self):
        '''HTTP response always has been closed in init, do nothing here.'''
        pass

    @property
    def responses(self):
        '''Return a list include all redirect responses, but redirect responses
        can only return itself.
        '''
        return self._responses + [self]  # avoid circular reference

    @property
    def encoding(self):
        return self._encoding

    @encoding.getter
    def encoding(self, encoding):
        '''Set encoding will reset attribute `text`'''
        self._encoding = encoding
        try:
            del self._text
        except AttributeError:
            pass

    @property
    def text(self):
        '''Return the decoded text, encoding can be specify or auto-detect.'''
        try:
            return self._text
        except AttributeError:
            pass
        def decode(encoding):
            if isinstance(encoding, bytes):
                encoding = encoding.decode()
            if isinstance(encoding, str):
                try:
                    self._text = self.content.decode(encoding, errors='replace')
                except:
                    logger.debug('Try decode with encoding %r fail', encoding)
                else:
                    return True
        decode(self._encoding) or \
        decode(self.headers.get_content_charset()) or \
        'json' in self.headers.get_content_subtype().lower() and \
        decode('utf-8') or \
        decode(match1(self.content[:1024],
                      b'(?i)<meta[^>]+charset=["\']?([\w-]+)',
                      b'(?i)<\\?xml[^>]+encoding=["\']?([\w-]+)')) or \
        decode('utf-8')  # fallback
        assert hasattr(self, '_text'), 'Decode fail, URL: ' + self.url
        return self._text

    def json(self):
        '''Return a object which deserialize from JSON document.'''
        logger.debug('parse JSON from %r:\n%s', self.url, self.text)
        try:
            return json.loads(self.text)
        except json.decoder.JSONDecodeError:
            # try remove callback
            text = match1(self.text, '^(?!\d)\w+\((.+?)\);?$',
                                     '^(?!\d)\w+=(\{.+?\});?$',
                                     '^(?!\d)\w+=(\[.+?\]);?$',)
            if text is None:
                raise
            return json.loads(text)

    def xml(self):
        '''Return a dict object which parse from XML document.'''
        logger.debug('parse XML from %r:\n%s', self.url, self.text)
        return xml2dict(self.text)

for _ in ('getheader', 'getheaders', 'info', 'geturl', 'getcode'):
    setattr(HTTPResponse, _, getattr(_HTTPResponse, _))


# utils

__all__ = ['add_default_handler', 'install_default_handlers', 'fake_headers',
           'reset_headers', 'add_header', 'get_response', 'get_head_response',
           'get_location', 'get_location_and_header', 'get_content_and_location',
           'get_content', 'url_info']

_opener = None
_default_handlers = []

def add_default_handler(handler):
    '''Added handlers will be used via install_default_handlers().

    Notice:
        this is use to setting GLOBAL (urllib) HTTP proxy and HTTPS verify,
        use it carefully.
    '''
    if isinstance(handler, type):
        handler = handler()
    if isinstance(handler, _HTTPRedirectHandler):
        logger.warning('HTTPRedirectHandler is not custom!')
        return
    remove_default_handler(handler)
    _default_handlers.append(handler)
    logger.debug('Add %s to default handlers', handler)

def remove_default_handler(handler):
    if not isinstance(handler, type):
        handler = type(handler)
    for default_handler in _default_handlers:
        if isinstance(default_handler, handler):
            _default_handlers.remove(default_handler)
            logger.debug('Remove %s from default handlers', default_handler)
            break

def install_default_handlers():
    '''Install the default handlers to urllib.request as its opener.'''
    global _opener
    # Always use our custom HTTPRedirectHandler
    _opener = build_opener(HTTPRedirectHandler, *_default_handlers)
    install_opener(_opener)

_default_fake_headers = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
    'Accept-Language': 'zh-CN,zh;q=0.8,en-US;q=0.5,en;q=0.3',
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:60.1) Gecko/20100101 Firefox/60.1'
}
fake_headers = _default_fake_headers.copy()

def reset_headers():
    '''Reset the fake_headers to default keys and values.'''
    fake_headers.clear()
    fake_headers.update(_default_fake_headers)

def add_header(key, value):
    '''Set the fake_headers[key] to value.'''
    global fake_headers
    fake_headers[key] = value

def ungzip(data):
    '''Decompresses data for Content-Encoding: gzip.'''
    return gzip.GzipFile(fileobj=BytesIO(data)).read()

def undeflate(data):
    '''Decompresses data for Content-Encoding: deflate.'''
    decompressobj = zlib.decompressobj(-zlib.MAX_WBITS)
    return decompressobj.decompress(data) + decompressobj.flush()

def get_response(url, headers={}, data=None, params=None, method='GET',
                      max_redirections=None, encoding=None,
                      default_headers=fake_headers):
    '''Fetch the response of giving URL.

    Params: both `params` and `data` always use "UTF-8" as encoding.

    Returns response, If redirections > max_redirections > 0 (stop on limit),
    this is a fake response except its attribute `url`.
    '''
    global _opener
    url = url.split('#', 1)[0]  # remove fragment if exist, it's useless
    if params: 
        url, _, query = url.partition('?')
        if hasattr(params, 'decode'):
            params = params.decode()
        if query:
            # first both to dict
            if not isinstance(params, (str, dict)):
                params = urlencode(params, doseq=True)
            query = parse_qs(query, keep_blank_values=True, strict_parsing=True)
            if not isinstance(params, dict):
                params = parse_qs(params, keep_blank_values=True)
            # then update/overlay
            query.update(params)
        else:
            query = params
        if not isinstance(query, str):
            query = urlencode(query, doseq=True)
        url = '{url}?{query}'.format(**vars())
    headget = method == 'HEADGET'  # if True the response will be closed
    if headget:                    # without read content
        method = 'GET'
    elif method != 'HEAD':
        logger.debug('get_response> URL: ' + url)
    if default_headers:
        _headers = default_headers.copy()
        _headers.update(headers)
        headers = _headers
    if data:
        headers = {k.capitalize(): v for k, v in headers.items()}
        ctype = headers.get('Content-type')
        form = False
        if isinstance(data, str):
            data = data.encode()
        if not hasattr(data, 'read'):
            try:
                mv = memoryview(data)
            except TypeError:
                try:
                    data = urlencode(data, doseq=True).encode()
                    form = True
                except TypeError:
                    pass
            else:
                if len(mv) < 1024:  # ISSUE: whether that limit is too small?
                    bs = mv.tobytes()
                    eq = bs.count(b'=')
                    sp = bs.count(b'&')
                    form = eq and eq == sp + 1
        if not (ctype or form):
            raise ValueError(
                'Inputed data is not type of "application/x-www-form-urlencoded"'
                ', the "Content-Type" header MUST be gave.')
        if data and method == 'GET':
            method = 'POST'
    req = Request(url, headers=headers, data=data, method=method)
    req.headget = headget
    req.max_redirections = max_redirections
    req.redirect_dict = {}  # init here allow disable redirect
    req.locations = []
    req.responses = responses = []
    if encoding == 'ignore':
        encoding = None
    if _opener is None:
        install_default_handlers()
    try:
        response = HTTPResponse(req, _opener.open(req), encoding)
    finally:
        for r in responses:
            del r.request.responses  # clear circular reference
    return response

def get_head_response(url, headers={}, params=None, max_redirections=0,
                      default_headers=fake_headers):
    '''Fetch the response of giving URL in HEAD mode.

    Returns response, If redirections > max_redirections > 0 (stop on limit),
    this is fake except its attribute `url`.
    '''
    logger.debug('get_head_response> URL: ' + url)
    try:
        response = get_response(url, headers=headers, params=params,
                                method='HEAD',
                                max_redirections=max_redirections,
                                default_headers=default_headers)
    except IOError as e:
        # Maybe HEAD method is not supported, retry
        if match1(str(e), 'HTTP Error (40[345])'):
            logger.debug('get_head_response> HEAD failed, try GET')
            response = get_response(url, headers=headers, params=params,
                                    method='HEADGET',
                                    max_redirections=max_redirections,
                                    default_headers=default_headers)
        else:
            raise
    return response

def get_location(*args, **kwargs):
    '''Try fetch the redirected location of giving URL.

    Params: same as get_head_response().

    Returns URL.
    '''
    response = get_head_response(*args, **kwargs)
    return response.url

def get_location_and_header(*args, **kwargs):
    '''**DEPRECATED**
    Try fetch the redirected location and the headers of giving URL.

    Params: same as get_head_response().
            If redirections > max_redirections > 0, returned headers is fake.

    Returns URL and headers.
    '''
    response = get_head_response(*args, **kwargs)
    return response.url, response.headers

def get_content_and_location(*args, **kwargs):
    '''**DEPRECATED**
    Try fetch the content and the redirected location of giving URL.

    Params: same as get_response().

    Returns content (encoding=='ignore') or decoded content, and URL.
    '''
    response = get_response(*args, **kwargs)
    if kwargs.get('encoding') == 'ignore':
        return response.content, response.url
    return response.text, response.url

def get_content(*args, **kwargs):
    '''Fetch the content of giving URL.

    Params: same as get_response().

    Returns content (encoding=='ignore') or decoded content.
    '''
    return get_content_and_location(*args, **kwargs)[0]

def url_info(url, headers=None, size=False):
    # TODO: modify to return named(filename, ext, size, ...)
    # in case url is http(s)://host/a/b/c.dd?ee&fff&gg
    # below is to get c.dd
    f = url.split('?')[0].split('/')[-1]
    # check . in c.dd, get dd if true
    if '.' in f:
        ext = f.split('.')[-1]
    else:
        ext = ''
    return '', ext, 0
