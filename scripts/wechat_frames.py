"""Read fresh pictures of one verified foreground X11 window region over one local Docker connection."""

import os
import math
import select
import struct
import subprocess
import threading
import time
import zlib

from PIL import Image

from capture_wechat_player import DOCKER, CONTAINER, check_drive

# XGetImage addresses only the selected window rectangle. Check focus and geometry on both
# sides of each image request, before forwarding any pixels to the caller.
GUEST = r"""
import ctypes as C
import os, struct, sys, time, zlib
from PIL import Image

win, ww, wh, x, y, w, h, fps = map(int, sys.argv[1:])
lib=C.CDLL('libX11.so.6')
ptr=C.c_void_p
integer=C.c_int
long=C.c_long
ulong=C.c_ulong
def function(name, returns, args):
    f=getattr(lib,name); f.restype=returns; f.argtypes=args
    return f

class Attributes(C.Structure):
    _fields_=[(k,integer) for k in ('x','y','width','height','border_width','depth')]+[
        ('visual',ptr),('root',ulong)]+[(k,integer) for k in ('class','bit_gravity','win_gravity','backing_store')]+[
        ('backing_planes',ulong),('backing_pixel',ulong),('save_under',integer),('colormap',ulong),
        ('map_installed',integer),('map_state',integer),('all_event_masks',long),('your_event_mask',long),
        ('do_not_propagate_mask',long),('override_redirect',integer),('screen',ptr)]

class XImage(C.Structure):
    _fields_=[(k,integer) for k in ('width','height','xoffset','format')]+[('data',ptr)]+[
        (k,integer) for k in ('byte_order','bitmap_unit','bitmap_bit_order','bitmap_pad',
                             'depth','bytes_per_line','bits_per_pixel')]+[
        (k,ulong) for k in ('red','green','blue')]

display=function('XOpenDisplay',ptr,[C.c_char_p])(None)
if not display: sys.exit('X display is unavailable.')
root=function('XDefaultRootWindow',ulong,[ptr])(display)
atom=function('XInternAtom',ulong,[ptr,C.c_char_p,integer])
prop=function('XGetWindowProperty',integer,[ptr,ulong,ulong,long,long,integer,ulong,
    C.POINTER(ulong),C.POINTER(integer),C.POINTER(ulong),C.POINTER(ulong),C.POINTER(ptr)])
free=function('XFree',integer,[ptr])
attributes=function('XGetWindowAttributes',integer,[ptr,ulong,C.POINTER(Attributes)])
get_image=function('XGetImage',C.POINTER(XImage),[ptr,ulong,integer,integer,C.c_uint,C.c_uint,ulong,integer])
destroy=function('XDestroyImage',integer,[C.POINTER(XImage)])
active=atom(display,b'_NET_ACTIVE_WINDOW',1); wmclass=atom(display,b'WM_CLASS',1); pid=atom(display,b'_NET_WM_PID',1)
def property(window, key):
    actual=ulong(); fmt=integer(); count=ulong(); left=ulong(); data=ptr()
    err=prop(display,window,key,0,100,0,0,C.byref(actual),C.byref(fmt),C.byref(count),C.byref(left),C.byref(data))
    try:
        if err or not data or not count.value: return None
        if fmt.value==32: return C.cast(data,C.POINTER(ulong))[0]
        if fmt.value==8: return C.string_at(data,count.value)
        return None
    finally:
        if data: free(data)
initial_pid=property(win,pid)
def guard():
    g=Attributes()
    if (property(root,active)!=win or not attributes(display,win,C.byref(g)) or g.map_state!=2 or
            (g.width,g.height)!=(ww,wh) or initial_pid is None or property(win,pid)!=initial_pid or
            b'WeChatAppEx' not in (property(win,wmclass) or b'').split(b'\0')):
        sys.exit('The foreground WeChat window has changed, is hidden or was resized.')
guard()
if not (0<=x and 0<=y and 0<w<=ww-x and 0<h<=wh-y): sys.exit('Region outside window.')
out=sys.stdout.buffer
out.write(b'WXF2'); out.flush()
while True:
    command=os.read(0,1)
    if command==b'G': break
    if command!=b'P': sys.exit(0)
    out.write(struct.pack('!d', time.monotonic())); out.flush()
while True:
    if os.read(0,1)!=b'F': sys.exit(0)
    request=time.monotonic()
    guard()
    t0=time.monotonic()
    wall0=time.time()
    img=get_image(display,win,x,y,w,h,ulong(-1).value,2)
    wall1=time.time()
    t1=time.monotonic()
    if not img: sys.exit('Selected window image is unavailable.')
    try:
        i=img.contents
        if (i.byte_order,i.bits_per_pixel,i.red,i.green,i.blue)!=(0,32,0xff0000,0xff00,0xff):
            sys.exit('Unsupported X11 pixel format.')
        pixels=Image.frombytes('RGB',(w,h),C.string_at(i.data,i.bytes_per_line*h),'raw','BGRX',i.bytes_per_line,1)
    finally:
        destroy(img)
    guard()
    payload=zlib.compress(pixels.tobytes(),1)
    out.write(struct.pack('!dddddI',request,t0,t1,(wall0+wall1)/2,time.monotonic(),len(payload)))
    out.write(payload); out.flush()
"""


class FrameTime(float):
    """Host monotonic acquisition time with the matching X11 wall-clock sample."""

    def __new__(cls, sent, received, request, begin, end, wall, reply):
        if not all(math.isfinite(x) for x in (sent, received, request, begin, end, wall, reply)):
            raise RuntimeError('Invalid X11 clock sample.')
        # Host send/receive bracket this exact acquisition. Subtract the observed guest
        # work at both ends, including compression; never extrapolate a startup offset.
        earliest = sent + begin - request
        latest = received - (reply-end)
        if not request <= begin <= end <= reply or not sent <= received:
            raise RuntimeError('Invalid X11 acquisition timestamps.')
        if end-begin > .06 or latest < earliest-.002 or latest-earliest > .10:
            raise TimeoutError('X11 acquisition could not be timed within 100 ms.')
        value = min(received, max(sent, (earliest+latest)/2))
        stamp = super().__new__(cls, value)
        stamp.guest_wall = wall
        return stamp


class FrameStream:
    """read() returns the next available fresh (host-monotonic timestamp, RGB PIL image)."""

    def __init__(self, window, size, box, fps=8):
        self.window = int(str(window), 0)
        self.size = tuple(size)
        self.box = tuple(box)
        self.fps = int(fps)
        if len(self.size) != 2 or len(self.box) != 4 or not 1 <= self.fps <= 20:
            raise ValueError('Expected window size, X,Y,width,height, and 1..20 fps.')
        x, y, w, h = self.box
        if min(x,y) < 0 or min(w,h) <= 0 or x+w > self.size[0] or y+h > self.size[1] or w*h > 3_000_000:
            raise ValueError('Capture region falls outside the selected window.')
        self._proc = self._thread = self._error = self._latest = None
        self._generation = self._delivered = 0
        self._closing = False
        self._stop_sampling = threading.Event()
        self._condition = threading.Condition()

    def _exact(self, length, deadline=None):
        chunks = bytearray()
        while len(chunks) < length:
            if deadline is not None:
                remaining = deadline-time.monotonic()
                if remaining <= 0 or not select.select([self._proc.stdout], [], [], remaining)[0]:
                    raise TimeoutError('The X11 sampler did not respond.')
            chunk = os.read(self._proc.stdout.fileno(), length-len(chunks))
            if not chunk:
                raise RuntimeError('The X11 window sampler ended.')
            chunks += chunk
        return chunks

    def __enter__(self):
        check_drive()
        try:
            self._proc = subprocess.Popen(DOCKER+['exec','-i','-u','abc',CONTAINER,'python3','-c',GUEST,
                *map(str,(self.window,*self.size,*self.box,self.fps))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,bufsize=0)
            deadline = time.monotonic()+5
            if self._exact(4,deadline) != b'WXF2':
                raise RuntimeError('Invalid X11 sampler handshake.')
            samples = []
            for _ in range(3):
                before=time.monotonic()
                self._proc.stdin.write(b'P')
                guest=struct.unpack('!d',self._exact(8,deadline))[0]
                after=time.monotonic()
                samples.append((after-before,(before+after)/2-guest))
            self.sync_rtt, self.offset = min(samples)
            self._proc.stdin.write(b'G')
            self._thread = threading.Thread(target=self._receive,daemon=True)
            self._thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def _receive(self):
        try:
            w,h=self.box[2:]
            next_request = time.monotonic()
            last = None
            while not self._stop_sampling.wait(max(0., next_request-time.monotonic())):
                sent = time.monotonic()
                self._proc.stdin.write(b'F')
                request, begin, end, wall, reply, size = struct.unpack('!dddddI', self._exact(44))
                received = time.monotonic()
                if size > 3*w*h+8192:
                    raise RuntimeError('Invalid image packet size.')
                raw=zlib.decompress(self._exact(size))
                if len(raw)!=3*w*h:
                    raise RuntimeError('Incomplete image packet.')
                image=Image.frombytes('RGB',(w,h),raw)
                next_request = max(sent, next_request) + 1/self.fps
                try:
                    stamp = FrameTime(sent, received, request, begin, end, wall, reply)
                except TimeoutError:
                    continue
                if last is not None and (stamp <= last or wall <= last.guest_wall
                        or abs((wall-last.guest_wall) - (stamp-last)) > .10):
                    raise RuntimeError('X11 clocks moved discontinuously between observed frames.')
                last = stamp
                with self._condition:
                    self._generation+=1
                    self._latest=(stamp,image)
                    self._condition.notify_all()
        except (OSError, ValueError, RuntimeError, zlib.error) as exc:
            with self._condition:
                self._error=exc
                self._condition.notify_all()

    def read(self, timeout=1.0):
        deadline=time.monotonic()+timeout
        with self._condition:
            while True:
                if self._error is not None: raise RuntimeError(str(self._error))
                if self._generation>self._delivered:
                    self._delivered=self._generation
                    if 0 <= time.monotonic()-self._latest[0] <= max(.35, 2/self.fps):
                        return self._latest
                remaining=deadline-time.monotonic()
                if remaining<=0: raise TimeoutError('No fresh image from the confirmed player.')
                self._condition.wait(remaining)

    def close(self):
        if self._proc is None or self._closing:
            return
        # Keep the reader draining stdout while the guest acknowledges EOF, even for large regions.
        self._stop_sampling.set()
        self._proc.stdin.close()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=3)
        self._closing=True
        if self._thread: self._thread.join(timeout=2)
        self._proc.stdout.close()
        self._proc.stderr.close()

    def __exit__(self,*exc):
        self.close()
