#!/usr/bin/env python3
"""TVMi Recorder (tvmi-rc): record live TVMi channels (tvmi.mt/live/N), by hand or automatically.

Follow shows and it reads the schedule of every channel (TV and radio), waits for each airing
(a little early to a little late), and records it. Manual recording is still there too.
Works on Linux and Windows. Needs: Python 3 with tkinter, and ffmpeg (on PATH, or
ffmpeg / ffmpeg.exe placed next to this file). No hard-coded paths: safe to move anywhere.
Settings, followed shows and history live in the user's config folder.
"""
import base64
import collections
import concurrent.futures
import datetime as dt
import html
import hmac
import ipaddress
import json
import logging
import logging.handlers
import os
import queue
import random
import re
import shutil
import signal
import socket
import statistics
import secrets
import ssl
import subprocess
import sys
import struct
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import zlib
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Malta")     # schedule times are Maltese local time
except Exception:                      # no tz database (common on Windows): assume PC clock is Maltese
    TZ = None

BASE = "https://tvmi.mt"
UA = {"User-Agent": "Mozilla/5.0"}
CHANNELS = {2: "TVM", 3: "TVM+", 4: "TVMsport+", 5: "Magic", 6: "Radju Malta", 7: "Radju Malta 2"}
SCHEDULE_CHANNELS = tuple(CHANNELS)             # every channel has a schedule page (Magic's is usually empty)
ALL_SHOWS_CHANNELS = (2, 3)                     # "Record ALL shows" only covers the two main TV channels
MAX_TRUSTED = dt.timedelta(minutes=120)   # longer gaps between listings are not treated as a show length
SCHEDULE_VERSION = 3
IS_WIN = os.name == "nt"
HOME = os.path.expanduser("~")


def now():
    return dt.datetime.now(TZ)


def cfg_dir():
    root = (os.environ.get("APPDATA") if IS_WIN else None) or os.environ.get("XDG_CONFIG_HOME") \
        or os.path.join(HOME, ".config")
    d = os.path.join(root, "TVMi Recorder" if IS_WIN else "tvmi-recorder")
    os.makedirs(d, exist_ok=True)
    return d


CFG = cfg_dir()
SETTINGS_FILE = os.path.join(CFG, "settings.json")
SCHEDULE_FILE = os.path.join(CFG, "schedule.json")
LOG_FILE = os.path.join(CFG, "tvmi-recorder.log")
LOCK_FILE = os.path.join(CFG, "running.lock")
RAISE_FILE = os.path.join(CFG, "show-window")      # touched by a second copy: the running one shows its window

# Debug log: every message shown in the window, plus ffmpeg's own messages, exit codes, network
# probes and errors. Rotates at 2 MB (keeps 3 old files). Session tokens are always blanked out.
_logger = logging.getLogger("tvmi")
_logger.setLevel(logging.DEBUG)
_logger.propagate = False
try:
    _h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
    _logger.addHandler(_h)
except OSError:
    pass
_TOKEN = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")


_HOOK = re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+")


def dlog(msg, level=logging.INFO):
    _logger.log(level, _HOOK.sub("<discord webhook>", _TOKEN.sub("<token>", str(msg))))


def open_path(path):
    """Open a file or folder with the system's default app."""
    try:
        if IS_WIN:
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        dlog(f"couldn't open {path}: {e}", logging.WARNING)


def _thread_excepthook(args):
    dlog("Uncaught exception in a background thread:\n" + "".join(
        traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)), logging.ERROR)


threading.excepthook = _thread_excepthook


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def norm(title):
    return re.sub(r"\s+", " ", title).strip().casefold()


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}


def safe_name(s, limit=100):
    """A file or folder name that works on Windows, macOS and Linux. Long titles are cut, so the whole path stays
    well inside Windows' 260-character limit."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")
    s = s[:limit].rstrip(" .")
    if s.split(".")[0].strip().upper() in _RESERVED:      # "Aux" or "con.txt" can't be a name on Windows
        s = "_" + s
    return s or "recording"


def clean_follows(raw, channels=None):
    """Followed shows from a settings file: well-formed, and each show once (by its normalised title). A file with
    "Xarabank" and "xarabank " used to stop the app from starting."""
    channels = channels or {0, *CHANNELS}
    out, seen = [], set()
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict) or not isinstance(f.get("title"), str) or not f["title"].strip():
            continue
        title = " ".join(f["title"].split())
        if norm(title) in seen:
            continue
        seen.add(norm(title))
        ch = f.get("channel", 0)
        out.append({"title": title, "channel": ch if isinstance(ch, int) and ch in channels else 0,
                    "per_day": f.get("per_day") if f.get("per_day") in ("first", "all") else "first"})
    return out


def run_bounded(fn, timeout, what="the save folder"):
    """Run fn in a helper thread and wait at most `timeout` seconds. A network drive that has hung (after sleep, or
    a dropped VPN) can block a disk call for minutes, and the window's thread is also the scheduler."""
    box, done = {}, threading.Event()

    def work():
        try:
            box["ok"] = fn()
        except BaseException as e:
            box["err"] = e
        done.set()
    threading.Thread(target=work, daemon=True).start()
    if not done.wait(timeout):
        raise OSError(f"{what} isn't responding (a disconnected network drive?)")
    if "err" in box:
        raise box["err"]
    return box.get("ok")


def portable_path(p):
    """Save-folder path for export: home-relative (~/Videos) so it means the same on any OS."""
    p = os.path.expanduser(p.strip())
    home = os.path.normpath(HOME)
    n = os.path.normpath(p)
    if n == home or n.startswith(home + os.sep):
        return "~" + n[len(home):].replace(os.sep, "/")
    return p


def resolve_path(p, fallback):
    """Imported save folder for this machine, or `fallback` if it can't be right here
    (a Linux path on Windows, a drive letter on Linux, or a folder whose parent doesn't exist)."""
    p = (p or "").strip()
    foreign = bool(p) and ((IS_WIN and p.startswith("/")) or (not IS_WIN and re.match(r"^([A-Za-z]:[\\/]|\\\\)", p)))
    if not p or foreign:
        return fallback, bool(p)
    full = os.path.normpath(os.path.expanduser(p))
    if os.path.isdir(full) or os.path.isdir(os.path.dirname(full)):
        return (p if p.startswith("~") else full), False
    return fallback, True


class LowSpace(OSError):
    pass


def check_space(path):
    """Raise LowSpace unless the drive holding `path` has at least min(10% of the drive, 50 GB) free."""
    p = os.path.abspath(os.path.expanduser(path))
    while not os.path.exists(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    u = shutil.disk_usage(p)
    need = min(u.total * 0.10, 50 * 1024 ** 3)
    if u.free < need:
        raise LowSpace(f"Not enough free disk space on the drive for {p}: {u.free / 1024 ** 3:.1f} GB free, "
                       f"at least {need / 1024 ** 3:.1f} GB needed (10% of the drive or 50 GB, whichever is lower).")


def find_ffmpeg():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (shutil.which("ffmpeg"), os.path.join(here, "ffmpeg.exe"), os.path.join(here, "ffmpeg")):
        if cand and os.path.isfile(cand):
            return cand
    return None


def find_ffprobe(ffmpeg):
    """ffprobe on PATH, or next to ffmpeg."""
    found = shutil.which("ffprobe")
    if found:
        return found
    if ffmpeg:
        cand = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + (".exe" if IS_WIN else ""))
        if os.path.isfile(cand):
            return cand
    return None


def media_duration(ffprobe, path, timeout=60):
    """Seconds of media in a file, or None if it can't be measured."""
    if not ffprobe:
        return None
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           **({"creationflags": 0x08000000} if IS_WIN else {}))
        return float(r.stdout.strip())
    except Exception:
        return None


def conversion_ok(ffprobe, src, dst):
    """Is the .mkv a complete copy of the .ts? Compares lengths with ffprobe. File sizes can't tell: Matroska drops
    MPEG-TS's packet overhead, so a good radio .mkv is often only 80% of the .ts."""
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        return False
    a, b = media_duration(ffprobe, src), media_duration(ffprobe, dst)
    if a and b:
        return b >= a * 0.97 - 2
    return os.path.getsize(dst) >= 0.5 * os.path.getsize(src)   # no ffprobe: only catch a clearly truncated file


class InstanceLock:
    """Only one copy of the app may run: two copies record every followed show twice and overwrite each other's
    settings. The lock is an OS file lock, so it goes away by itself if the app crashes."""

    def __init__(self, path):
        self.path, self.fh = path, None

    def acquire(self):
        fh = open(self.path, "a+")
        try:
            if IS_WIN:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def release(self):
        if self.fh:
            try:
                if IS_WIN:
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.fh.close()
            self.fh = None


def _child_dies_with_parent():
    """preexec_fn for ffmpeg on Linux: ffmpeg gets SIGTERM (it then closes the file properly) if this app dies,
    instead of recording on for ever with nobody to stop it."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes
        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except Exception:
        return None
    parent = os.getpid()

    def fn():
        prctl(1, signal.SIGTERM)                 # PR_SET_PDEATHSIG
        if os.getppid() != parent:               # the app died before prctl took effect
            os.kill(os.getpid(), signal.SIGTERM)
    return fn


class _WinKillJob:
    """A Windows Job Object that kills the processes in it when this app exits, however it exits."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]
        self.k32 = k32
        self.handle = k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = Extended()
        info.Basic.LimitFlags = 0x2000                            # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def add(self, proc):
        return bool(self.k32.AssignProcessToJobObject(self.handle, int(proc._handle)))


_KILL_JOB = []


def tie_to_app(proc):
    """Windows: put a child process in the app's kill-on-exit job (Linux uses _child_dies_with_parent)."""
    if not IS_WIN:
        return
    try:
        if not _KILL_JOB:
            _KILL_JOB.append(_WinKillJob())
        if not _KILL_JOB[0].add(proc):
            dlog("couldn't tie ffmpeg to the app (it may keep running if the app is killed)", logging.WARNING)
    except Exception as e:
        dlog(f"couldn't tie ffmpeg to the app: {e}", logging.WARNING)


# ------------------------------------------------------------------ TVMi access
def _has_certifi():
    try:
        import certifi  # noqa: F401
        return True
    except Exception:
        return False


def make_ssl_context():
    """System certificates, or the `certifi` bundle if that package is installed
    (helps on Windows PCs whose certificate store is out of date)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = make_ssl_context()


def fetch(path, timeout=30):
    req = urllib.request.Request(BASE + path, headers=UA)
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


SEEN_HOSTS = set()      # TVMi stream servers seen so far (they hold the same video, sizes match across them)


def stream_url(ch, alt=False, timeout=30):
    """Fetch a fresh session token and build the HLS master URL. TVMi gives a second
    server (data-dist-host-alt1); alt=True uses it, for when the first one keeps failing."""
    page = fetch(f"/live/{ch}", timeout) or ""
    host = re.search(r'data-dist-host="([^"]+)', page)
    host2 = re.search(r'data-dist-host-alt1="([^"]+)', page)
    jwt = re.search(r'data-jwt="([^"]+)', page)
    if not host or not jwt:
        raise RuntimeError(f"channel {ch}: no stream found on the page")
    SEEN_HOSTS.update(x.group(1) for x in (host, host2) if x)
    h = host2.group(1) if (alt and host2) else host.group(1)
    return f"https://{h}/{jwt.group(1)}/live/{ch}/master.m3u8"


_INPUT_OPTS = {}


def resilience_opts(ffmpeg):
    """ffmpeg input options that ride out network hiccups, limited to the ones this ffmpeg knows.
    By default ffmpeg gives up on the whole recording if a single segment download fails."""
    if ffmpeg in _INPUT_OPTS:
        return _INPUT_OPTS[ffmpeg]
    kw = {"creationflags": 0x08000000} if IS_WIN else {}

    def help_text(what):
        try:
            r = subprocess.run([ffmpeg, "-hide_banner", "-h", what], capture_output=True, text=True,
                               timeout=20, stdin=subprocess.DEVNULL, **kw)
            return r.stdout + r.stderr
        except Exception:
            return ""
    hls, http = help_text("demuxer=hls"), help_text("protocol=http")
    opts = ["-rw_timeout", "20000000"]                      # a stalled read (20 s) is an error, not a hang
    if "http_persistent" in hls:
        opts += ["-http_persistent", "0"]                   # the server drops idle connections; don't warn about it every segment
    if "seg_max_retry" in hls:
        opts += ["-seg_max_retry", "10"]                    # retry a failed segment instead of quitting
    for name, val in (("reconnect", "1"), ("reconnect_streamed", "1"), ("reconnect_on_network_error", "1"),
                      ("reconnect_on_http_error", "5xx"), ("reconnect_delay_max", "10")):
        if name in http:
            opts += ["-" + name, val]
    _INPUT_OPTS[ffmpeg] = opts
    return opts


MAIN_CHANNELS = (2, 3)       # a refresh that got nothing for these two is a failed refresh


def parse_schedule_page(page, day):
    """[(start datetime, title), ...] from one channel's schedule page for `day`."""
    out, last, add = [], -1, 0
    for blk in (page or "").split('class="flex-1 -mt-1"')[1:]:
        t = re.search(r'font-light">\s*(\d\d):(\d\d)', blk)
        title = re.search(r'line-clamp-1">\s*(.*?)\s*<', blk, re.S)
        if not (t and title):
            continue
        mins = int(t[1]) * 60 + int(t[2])
        if mins < last:
            add = 1                       # listed after midnight, belongs to the next date
        last = mins
        start = dt.datetime.combine(day + dt.timedelta(add), dt.time(mins // 60, mins % 60), tzinfo=TZ)
        out.append((start, " ".join(html.unescape(title.group(1)).split())))
    return out


def fetch_schedule(days_back=1, days_fwd=7, timeout=15):
    """Programmes on every channel (TV and radio) as dicts: ch, title, start, end (ISO strings), plus the
    (channel, date) pages that couldn't be loaded. One failed page no longer throws the whole refresh away: only a
    refresh that got nothing at all for TVM and TVM+ counts as failed."""
    pages, failed, last_err = {}, set(), None
    today = now().date()
    for ch in SCHEDULE_CHANNELS:
        seq = {}
        for off in range(-days_back, days_fwd + 1):
            day = today + dt.timedelta(off)
            try:
                page = fetch(f"/schedule/{ch}/{day}", timeout)
            except Exception as e:
                failed.add((ch, day.isoformat()))
                last_err = e
                dlog(f"schedule: channel {ch}, {day}: {type(e).__name__}: {e}", logging.WARNING)
                page = None
            time.sleep(0.2)
            for start, title in parse_schedule_page(page, day):
                seq[start] = title
        pages[ch] = seq
    days = days_back + days_fwd + 1
    if all(sum(1 for c, _ in failed if c == ch) == days for ch in MAIN_CHANNELS):
        raise last_err or RuntimeError("no schedule pages could be loaded")
    return schedule_items(pages), failed


def schedule_items(pages):
    """{ch: {start: title}} -> programme dicts. The pages only give start times, so a programme ends when the
    next one starts."""
    out = []
    for ch, seq in pages.items():
        starts = sorted(seq)
        for i, st in enumerate(starts):
            nxt = starts[i + 1] if i + 1 < len(starts) else None
            out.append({"ch": ch, "title": seq[st], "start": st, "raw": (nxt - st) if nxt else None})
    # The site lists start times only, so "until the next listing" is only trustworthy when that
    # listing is close. Overnight loops and unlisted stretches leave hours-long gaps that aren't
    # the show's length. For those, use the show's usual length from its other airings, else 60 min.
    trusted = {}
    for x in out:
        if x["raw"] and x["raw"] <= MAX_TRUSTED:
            trusted.setdefault(norm(x["title"]), []).append(x["raw"])
    res = []
    for x in out:
        guess = not (x["raw"] and x["raw"] <= MAX_TRUSTED)
        if guess:
            usual = trusted.get(norm(x["title"]))
            length = statistics.median_low(usual) if usual else dt.timedelta(minutes=60)
        else:
            length = x["raw"]
        res.append({"ch": x["ch"], "title": x["title"], "start": x["start"].isoformat(),
                    "end": (x["start"] + length).isoformat(), "guess": guess})
    return res


def merge_schedule(new, old, failed):
    """Keep the previous listings for pages that failed this time, so a flaky page doesn't hide a followed show."""
    if not failed:
        return list(new)
    have = {(x["ch"], x["start"][:10]) for x in new}
    kept = [x for x in old if (x["ch"], x["start"][:10]) in failed and (x["ch"], x["start"][:10]) not in have]
    return sorted(list(new) + kept, key=lambda x: (x["start"], x["ch"]))


# ------------------------------------------------------------------ built-in recorder
class StopRequested(Exception):
    pass


def http_get(url, stop, timeout=12, limit=45):
    """Download a URL completely (bytes), giving up if `stop` is set or it drags on for more than `limit` seconds."""
    t0, chunks = time.time(), []
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout, context=SSL_CTX) as r:
        while True:
            if stop.is_set():
                raise StopRequested()
            block = r.read(65536)
            if not block:
                break
            chunks.append(block)
            if time.time() - t0 > limit:
                raise TimeoutError(f"the download was still going after {limit} s")
    return b"".join(chunks)


def parse_media_playlist(text):
    """[(sequence number, uri, seconds), ...] from an HLS media playlist."""
    seq, dur, segs = 0, 12.0, []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line:
            raise RuntimeError("the stream is encrypted, which this recorder can't handle")
        elif line and not line.startswith("#"):
            segs.append((seq + len(segs), line, dur))
    return segs


def seq_pattern(seq, uri):
    """'chunk_{seq}.ts' from chunk 1234's uri 'chunk_1234.ts', so chunks that left the playlist can be asked for by
    number. None if the number isn't in the uri."""
    s = str(seq)
    if s not in uri:
        return None
    i = uri.rfind(s)
    return uri[:i] + "{seq}" + uri[i + len(s):]


def seq_uri(pattern, seq):
    return pattern.replace("{seq}", str(seq)) if pattern else None


def jump_todo(segs, pattern, since, walk_back):
    """After the chunk numbering jumped: the chunks to take from the new stream. Covers the time since the last chunk
    was written (`since` seconds, plus one chunk), using the playlist and, if that isn't enough, older chunks found
    with walk_back(oldest listed, how many)."""
    typical = segs[-1][2] or 12.0
    need = int(-(-max(since, 0.0) // typical)) + 1
    if need <= len(segs):
        return list(segs[-need:])
    older = walk_back(segs[0][0], min(90, need - len(segs))) if pattern else []
    return [(q, seq_uri(pattern, q), typical) for q in older] + list(segs)


class SegmentGone(Exception):
    """The chunk is no longer on any server we asked (it is older than TVMi keeps them)."""


def with_host(url, host):
    return urllib.parse.urlsplit(url)._replace(netloc=host).geturl()


def best_variant(master_url, master_text):
    """The media playlist URL with the highest bandwidth in a master playlist (or the URL itself if it's already a media playlist)."""
    best, bw, pending = None, -1, None
    for line in master_text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            pending = int(m.group(1)) if m else 0
        elif line and not line.startswith("#") and pending is not None:
            if pending > bw:
                best, bw = line, pending
            pending = None
    return urllib.parse.urljoin(master_url, best) if best else master_url


class HlsRecorder:
    """Records a live HLS stream by downloading its segments itself and appending them, in order, to ONE .ts file.

    * Chunks are numbered, so after a stall it resumes exactly where it stopped.
    * TVMi's servers keep chunks for about 18 minutes after they leave the playlist (which only lists the last 60 s),
      so anything missed is fetched afterwards by number, from any server. Only a chunk that is gone everywhere is a
      real gap, and then the file is split there.
    * 3 chunks download at once, and a failed attempt is retried on a different server, so one slow connection can't
      hold everything up. It looks like a subprocess to Job."""
    POLL = 4.0
    WORKERS = 3
    AHEAD = 4
    DRAIN_MAX = 60          # seconds allowed to finish the last chunks after the scheduled end
    MAX_FORWARD = 150       # a playlist this many chunks (30 min) ahead of where we were isn't "catching up": its numbering jumped
    MAX_BACK = 30           # ...nor is one this far behind (TVMi keeps ~18 min = ~90 chunks, so real catch-up is always below MAX_FORWARD)

    def __init__(self, job, path, first_url=None, start_back=3):
        self.job, self.path, self.first_url, self.start_back = job, path, first_url, start_back
        self.stop_ev = threading.Event()
        self.lock = threading.RLock()
        self.returncode = None
        self.pid = "built-in"
        self.retrying = False
        self.segments = self.bytes = 0
        self.last_seq = None
        self.newest = None
        self.pattern = None
        self.draining = False
        self.drain_target = None
        self.drain_deadline = 0.0
        self.missed_secs = 0.0
        self.fails = 0
        self.incident_t = None
        self.notified = False
        self.missed_at_start = 0.0
        self.host = self.host_at_start = ""
        self.media = None
        self.renew = False
        self.out = None
        self.pending = None
        self.part_secs = 0.0
        self.last_write = time.time()
        job.part_stats[path] = {"secs": 0.0, "bytes": 0}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.WORKERS)
        self.thread = threading.Thread(target=self.main, daemon=True)
        self.thread.start()

    # -- the bits Job uses, like subprocess.Popen
    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired("hls recorder", timeout)
        return self.returncode

    def request_stop(self, drain=False):
        """drain=True (scheduled end): first save everything up to the newest chunk that exists right now."""
        if drain and self.newest is not None and self.last_seq is not None and self.returncode is None:
            self.drain_target = self.newest
            self.drain_deadline = time.time() + self.DRAIN_MAX
            self.draining = True
        else:
            self.stop_ev.set()

    def kill(self):
        self.stop_ev.set()

    # -- internals
    def log(self, msg, level=logging.INFO):
        dlog(f"[{self.job.label}] hls: {msg}", level)

    def failed(self, err):
        with self.lock:
            self._failed(err)

    def _failed(self, err):
        j = self.job
        if self.stop_ev.is_set():                            # paused or stopped: a newer recorder may own the job now
            return
        self.fails += 1
        if self.incident_t is None:
            self.incident_t, self.missed_at_start, self.host_at_start = time.time(), self.missed_secs, self.host
        msg = f"{type(err).__name__}: {err}"
        j.errors.append(msg[:160])
        self.log(msg + f" (failure {self.fails})", logging.WARNING)
        if self.fails % 3 == 0:                              # keep failing: fetch a fresh token and try the other server
            self.renew = True                                # main() swaps the playlist; the workers keep their copy
            j.alt = not j.alt
        if not self.notified and time.time() - self.incident_t >= 5:
            self.notified, self.retrying = True, True
            self.log(f"DROP: no data for {time.time() - self.incident_t:.0f} s; {self.segments} segments ({self.bytes / 1e6:.1f} MB) "
                     f"written so far; last errors: {list(j.errors)}"
                     + (f"; meaning: {explain_error(j.errors)}" if explain_error(j.errors) else ""), logging.WARNING)
            j.app.q.put(lambda: j.app.log(f"{j.label}: connection problem, retrying. TVMi keeps about 18 minutes of video, so nothing is lost if it recovers within that."))
            j.app.notify_drop(j)
            threading.Thread(target=j.probe_network, daemon=True).start()

    def recovered(self):
        with self.lock:
            self._recovered()

    def _recovered(self):
        j = self.job
        if self.incident_t is None or self.stop_ev.is_set():
            return
        stall = time.time() - self.incident_t
        if self.notified:
            missed = self.missed_secs - self.missed_at_start
            switched = bool(self.host_at_start) and self.host != self.host_at_start
            j.add_drop(stall, switched, missed)
            j.add_incident({"start": self.incident_t, "end": time.time(), "stall": stall, "missed": missed,
                            "errors": list(j.errors), "switched": switched})
            self.log(f"recovered after {stall:.0f} s; " + (f"missed about {missed:.0f} s of video" if missed else "nothing missed"))
            j.app.q.put(lambda: j.app.log(f"{j.label}: recovered after {stall:.0f} s, "
                                          + (f"about {missed:.0f} s of video was missed" if missed else "nothing was missed")))
            j.app.notify_recovered(j, stall, missed)
        else:
            self.log(f"short blip ({stall:.1f} s), nothing lost")
        self.incident_t, self.notified, self.retrying, self.fails = None, False, False, 0

    def close_gap(self):
        """Called just before writing the first segment after a gap: end this file, start the next part."""
        j, p = self.job, self.pending
        self.pending = None
        before, secs_before = self.path, self.part_secs
        self.out.close()
        self.path = j.next_part_path()
        self.out = open(self.path, "ab")
        j.part_stats[self.path] = {"secs": 0.0, "bytes": 0}
        self.part_secs = 0.0
        j.gaps.append({"segments": p["segments"], "secs": p["secs"], "first": p["first"], "last": p["last"],
                       "file_before": before, "file_after": self.path, "secs_into_file": secs_before,
                       "est_start": self.last_write, "est_end": self.last_write + p["secs"], "tail": False,
                       "est": p.get("est", False)})
        self.log(f"gap of about {p['secs']:.0f} s: this file ends here, the next part starts after it", logging.WARNING)
        j.app.q.put(lambda: j.app.log(f"{j.label}: about {p['secs']:.0f} s of video could not be recovered; "
                                      f"continuing in {os.path.basename(self.path)}"))

    def chunk_exists(self, seq, media):
        """Does a chunk with this number exist on the server? (asks for a single byte)"""
        tried = []
        for _ in range(2):
            host = self.pick_host(tried, media)
            tried.append(host)
            url = with_host(urllib.parse.urljoin(media, seq_uri(self.pattern, seq)), host)
            try:
                req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-0"})
                with urllib.request.urlopen(req, timeout=6, context=SSL_CTX) as r:
                    r.read(1)
                return True
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                if e.code == 416:                              # server ignores/rejects the range but the file is there
                    return True
                return False
            except Exception:
                return False
        return False

    def walk_back(self, oldest, media, limit=90):
        """Chunks of the (restarted) stream that are older than the oldest one listed: they exist back to the moment the
        stream restarted and not beyond. Returns their numbers, oldest first."""
        if not self.pattern:
            return []
        found = []
        for q in range(oldest - 1, oldest - 1 - limit, -1):
            if self.stop_ev.is_set() or not self.chunk_exists(q, media):
                break
            found.append(q)
        return found[::-1]

    def pick_host(self, tried, media):
        """Main server first, then others we know of, then ask the site for more."""
        first = urllib.parse.urlsplit(media).netloc
        if first not in tried:
            return first
        for _ in range(2):
            others = [h for h in SEEN_HOSTS if h not in tried]
            if others:
                return random.choice(others)
            try:
                stream_url(self.job.ch, bool(random.getrandbits(1)), timeout=6)     # learns the site's current servers
            except Exception:
                break
        return first

    def fetch_segment(self, seq, uri, media):
        """One chunk: up to 4 attempts, each on a different server if possible. Raises SegmentGone if it's gone everywhere.
        `media` is the playlist URL when the chunk was queued: main() may swap self.media meanwhile."""
        tried, last, gone = [], None, 0
        for limit in (20, 20, 30, 45):
            if self.stop_ev.is_set():
                raise StopRequested()
            host = self.pick_host(tried, media)
            tried.append(host)
            url = with_host(urllib.parse.urljoin(media, uri), host)
            try:
                return http_get(url, self.stop_ev, timeout=8, limit=limit)
            except StopRequested:
                raise
            except urllib.error.HTTPError as e:
                if e.code == 404:                              # this server doesn't have it: try another
                    gone += 1
                    if gone >= 3:
                        raise SegmentGone(seq)
                    continue
                last = e
            except Exception as e:
                last = e
            self.failed(last)
            self.stop_ev.wait(0.5)
        if last is None:
            raise SegmentGone(seq)
        raise last

    def add_gap(self, lost, secs, first_seq, last_seq, estimated=False):
        """Video that is gone from every server. The file is split when the next segment is written."""
        with self.lock:
            p = self.pending or {"segments": 0, "secs": 0.0, "first": first_seq, "last": last_seq, "est": False}
            p["est"] = p["est"] or estimated
            p["segments"] += lost
            p["secs"] += secs
            p["last"] = last_seq
            self.pending = p
            self.missed_secs += secs

    def download(self, todo, media):
        """Fetch the chunks in `todo` (seq, uri or None, seconds) in parallel and write them in order."""
        j = self.job
        pending, it = collections.deque(), iter(todo)

        def fill():
            while len(pending) < self.AHEAD:
                nxt = next(it, None)
                if nxt is None:
                    return
                q, u, d = nxt
                pending.append((q, d, self.pool.submit(self.fetch_segment, q, u, media) if u else None))
        fill()
        try:
            while pending:
                q, d, fut = pending.popleft()
                try:
                    if fut is None:
                        raise SegmentGone(q)
                    data = fut.result()
                except SegmentGone:
                    self.add_gap(1, d, q, q)
                    self.last_seq = q
                    self.log(f"chunk {q} is gone from every server (older than TVMi keeps them), counted as missed", logging.WARNING)
                    fill()
                    continue
                if self.stop_ev.is_set():                   # paused or stopped: don't start new parts under a newer recorder
                    raise StopRequested()
                if self.pending:
                    self.close_gap()
                self.out.write(data)
                self.out.flush()
                self.last_seq = q
                self.last_write = time.time()
                self.segments += 1
                self.bytes += len(data)
                self.part_secs += d
                st = j.part_stats[self.path]
                st["secs"], st["bytes"] = self.part_secs, st["bytes"] + len(data)
                fill()
        except BaseException:
            for _, _, f in pending:
                if f:
                    f.cancel()
            raise

    def main(self):
        j = self.job
        try:
            self.out = open(self.path, "ab")
            while not self.stop_ev.is_set():
                try:
                    if self.media is None or self.renew:
                        self.renew = False
                        master = self.first_url or stream_url(j.ch, j.alt, timeout=10)
                        self.first_url = None
                        self.host = master.split("/")[2]
                        self.media = best_variant(master, http_get(master, self.stop_ev).decode("utf-8", "replace"))
                    media = self.media
                    segs = parse_media_playlist(http_get(media, self.stop_ev).decode("utf-8", "replace"))
                    if not segs:
                        raise RuntimeError("the playlist has no segments")
                    self.newest = segs[-1][0]
                    listed = {x[0]: (x[1], x[2]) for x in segs}
                    typical = segs[-1][2]
                    if self.pattern is None:
                        self.pattern = seq_pattern(segs[-1][0], segs[-1][1])
                    if self.last_seq is None:
                        todo = segs[-self.start_back:]
                    elif self.newest - self.last_seq > self.MAX_FORWARD or self.newest - self.last_seq < -self.MAX_BACK:
                        # The numbering jumped (the stream or a server restarted, or another server counts differently).
                        # Chunk numbers can no longer say what was missed, so go back to live and estimate from the clock.
                        # (Without this the recorder asked for thousands of chunks that never existed and called them lost.)
                        typical_s = segs[-1][2]
                        old_seq = self.last_seq
                        # Take the new stream back to the last chunk we wrote, judged by the clock, and no further. After a
                        # restart that is everything since it began. If instead a server simply numbers chunks differently,
                        # anything older would be video already in the file, appended a second time.
                        todo = jump_todo(segs, self.pattern, time.time() - self.last_write,
                                         lambda oldest, n: self.walk_back(oldest, media, n))
                        have = len(todo) * typical_s
                        est = max(0.0, time.time() - have - self.last_write)
                        self.log(f"chunk numbers jumped from {old_seq} to {self.newest} (the stream restarted): taking all "
                                 f"{len(todo)} chunk(s) of the new stream ({have:.0f} s); about {est:.0f} s estimated missing from the clock",
                                 logging.WARNING)
                        self.job.errors.append(f"chunk numbering jumped ({old_seq} -> {self.newest}), stream restarted")
                        if est >= typical_s:
                            self.add_gap(max(1, round(est / typical_s)), est, 0, 0, estimated=True)
                    else:
                        todo = []
                        for q in range(self.last_seq + 1, self.newest + 1):
                            if q in listed:
                                todo.append((q, listed[q][0], listed[q][1]))
                            else:                              # fell out of the playlist: fetch it by number
                                todo.append((q, seq_uri(self.pattern, q), typical))
                        lag = len(todo) * typical
                        if lag >= 30:
                            self.job.max_lag = max(self.job.max_lag, lag)
                            self.log(f"{len(todo)} chunk(s) behind live (about {lag:.0f} s), catching up")
                    self.download(todo, media)
                    self.recovered()
                    if self.draining and (self.last_seq >= self.drain_target or time.time() > self.drain_deadline):
                        break
                    self.stop_ev.wait(1.0 if self.draining else self.POLL)
                except StopRequested:
                    break
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403):               # token expired: start over with a fresh one
                        self.media = None
                    self.failed(e)
                    self.stop_ev.wait(min(1 + self.fails, 5))
                except OSError as e:
                    if isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError)) or "timed out" in str(e):
                        self.failed(e)
                        self.stop_ev.wait(min(1 + self.fails, 5))
                    else:
                        raise                              # e.g. the disk: give up, Job reports it
                except Exception as e:
                    self.failed(e)
                    self.stop_ev.wait(min(1 + self.fails, 5))
                if self.draining and time.time() > self.drain_deadline:
                    break
            self.returncode = 0
        except Exception as e:
            j.errors.append(f"{type(e).__name__}: {e}"[:160])
            self.log("stopped by an error:\n" + traceback.format_exc(), logging.ERROR)
            self.returncode = 1
        finally:
            self.pool.shutdown(wait=False, cancel_futures=True)
            if self.pending:                                # the recording ended while some video was still unreachable
                p = self.pending
                j.gaps.append({"segments": p["segments"], "secs": p["secs"], "first": p["first"], "last": p["last"],
                               "file_before": self.path, "file_after": "", "secs_into_file": self.part_secs,
                               "est_start": self.last_write, "est_end": self.last_write + p["secs"], "tail": True,
                               "est": p.get("est", False)})
            if self.out:
                self.out.close()


# ------------------------------------------------------------------ one recording
class DropStats:
    """A running summary of a recording's drops. A flaky connection can drop thousands of times, so the drops
    themselves aren't kept."""

    def __init__(self):
        self.count, self.worst, self.missed, self.backup, self.measured = 0, 0.0, 0.0, False, True

    def add(self, gap, backup, missed=None):
        self.count += 1
        self.worst = max(self.worst, gap)
        self.backup = self.backup or bool(backup)
        if missed is None:
            self.measured = False          # ffmpeg engine: how much was lost isn't known
        else:
            self.missed += missed

    def summary(self):
        return {"count": self.count, "worst": self.worst, "missed": self.missed, "backup": self.backup,
                "measured": self.measured}


class Job:
    """One ffmpeg recording of one channel. Reconnects on drops (new part, fresh token),
    can be paused (new part on resume) and stops itself at end_at / after `limit` seconds."""

    def __init__(self, app, ch, outdir, base, label, end_at=None, limit=0, key=None, test=False):
        self.test = test
        self.app, self.ch, self.outdir, self.base = app, ch, outdir, base
        self.label, self.end_at, self.limit, self.key = label, end_at, limit, key
        self.state = "connecting"      # connecting | recording | paused | reconnecting | done
        self.proc = None
        self.gen = 0                   # bumped on stop/pause so stale fetch results are ignored
        self.part = 0
        self.elapsed = 0.0
        self.seg = 0.0
        self.retry_at = 0.0
        self.file = ""
        self.files = []
        self.drops = DropStats()       # how often the stream dropped, the longest stall, video missed
        self.incidents = []            # connection problems for the report file (the first MAX_INCIDENTS)
        self.more_incidents = 0        # ...and how many more there were
        self.gaps = []                 # video that could not be recovered (each one splits the file)
        self.part_stats = {}           # file -> {"secs": video seconds, "bytes": ...} (built-in recorder)
        self.first_start = 0.0
        self.end_wall = 0.0
        self.max_lag = 0.0             # longest the built-in recorder fell behind live (seconds)
        self.drop_errors = []
        self.plock = threading.Lock()
        self.engine = ""
        self.drop_times = collections.deque(maxlen=20)
        self.drop_t = 0.0
        self.last_each = 0.0
        self.alerted_at = 0.0
        self.announced = False
        self.problem = ""              # why it stopped early, if it did (shown in Discord)
        self.problem_kind = ""         # "low disk space" or "stopped early", for the Upcoming list
        self.size = 0                  # size of the current part, kept up to date off the window's thread
        self.info = {}                 # for followed shows: {"scheduled": "00:01 – 00:31"}
        self.alt = False               # after a drop, try TVMi's second server
        self.errors = collections.deque(maxlen=5)   # ffmpeg's last messages, shown when it drops
        self.done_at = 0.0
        run_bounded(lambda: (check_space(outdir), os.makedirs(outdir, exist_ok=True)), 8)
        self.begin_part()

    MAX_INCIDENTS = 100

    def add_drop(self, gap, backup, missed=None):
        self.drops.add(gap, backup, missed)

    def add_incident(self, incident):
        if len(self.incidents) < self.MAX_INCIDENTS:
            self.incidents.append(incident)
        else:
            self.more_incidents += 1

    def next_part_path(self):
        """Reserve the next free part file: '<base>.ts' first, then '<base> part2.ts' and so on. Every new part (resume,
        reconnect, or a gap in the built-in recorder) comes through here, under one lock."""
        with self.plock:
            n = 1
            while True:
                path = os.path.join(self.outdir, f"{self.base}{'' if n == 1 else f' part{n}'}.ts")
                if path not in self.files and not os.path.exists(path):
                    break
                n += 1
            self.part = n
            self.file = path
            self.files.append(path)
        return path

    @property
    def retrying(self):
        return bool(getattr(self.proc, "retrying", False))

    def rec_time(self):
        return self.elapsed + (time.monotonic() - self.seg if self.state == "recording" else 0)

    def begin_part(self):
        self.state = "connecting"
        self.gen += 1
        gen = self.gen

        use_ffmpeg = self.app.use_ffmpeg()

        def work():
            # Everything that can hang (the network, a disk that stopped answering, running ffmpeg -h) happens here,
            # not on the window's thread, which also starts and stops recordings on time.
            try:
                check_space(self.outdir)
            except LowSpace as e:
                self.app.q.put(lambda err=e: self.low_space(gen, err))
                return
            except OSError:
                pass                                  # the folder itself is checked when the file is opened
            try:
                url = stream_url(self.ch, self.alt)
            except Exception as e:
                self.app.q.put(lambda err=e: self.fetch_failed(gen, err))
                return
            opts = resilience_opts(self.app.ffmpeg) if use_ffmpeg else []
            path = self.next_part_path() if gen == self.gen else ""
            self.app.q.put(lambda: self.launch(gen, url, path, opts))
        threading.Thread(target=work, daemon=True).start()

    def low_space(self, gen, err):
        if gen != self.gen:
            return
        self.app.log(f"{self.label}: stopped. {err}")
        self.problem, self.problem_kind = str(err), "low disk space"
        self.stop(user=False)

    def fetch_failed(self, gen, err):
        if gen != self.gen:
            return
        self.app.log(f"{self.label}: connection problem ({err}), retrying")
        self.state = "reconnecting"
        self.retry_at = time.monotonic() + 5

    def launch(self, gen, url, path, opts):
        if gen != self.gen or not path:
            return
        self.file = path
        self.size = 0
        self.engine = "ffmpeg" if opts else "built-in"
        if self.engine == "built-in":
            self.proc = HlsRecorder(self, self.file, url)
        else:
            kw = {"creationflags": 0x08000000} if IS_WIN else {"preexec_fn": _child_dies_with_parent()}
            try:
                self.proc = subprocess.Popen(
                    [self.app.ffmpeg, "-loglevel", "warning", "-y", *opts, "-i", url,
                     "-c", "copy", "-f", "mpegts", self.file],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **kw)
            except OSError as e:
                self.app.log(f"{self.label}: can't start ffmpeg ({e})")
                self.problem, self.problem_kind = f"Couldn't start ffmpeg: {e}", "stopped early"
                self.stop(user=False)
                return
            tie_to_app(self.proc)
        host = url.split("/")[2]
        switched = bool(getattr(self, "last_host", "")) and host != self.last_host
        self.last_host = host
        self.errors.clear()
        self.last_proc = self.proc
        self.started_at = time.time()
        dlog(f"[{self.label}] recorder started ({self.engine}, pid {self.proc.pid}), channel {self.ch}, server {url.split('/')[2]}"
             f"{' (different from the last one)' if switched else ''}, part {self.part}, until "
             f"{self.end_at.isoformat(timespec='minutes') if self.end_at else (str(self.limit) + ' s' if self.limit else 'stopped by hand')}")
        if self.engine == "ffmpeg":
            threading.Thread(target=self.read_errors, args=(self.proc,), daemon=True).start()
        self.app.log(f"Recording {self.label} → {os.path.basename(self.file)}")
        self.seg = time.monotonic()
        self.state = "recording"
        if not self.first_start:
            self.first_start = time.time()
        if self.drop_t:
            self.add_drop(time.time() - self.drop_t, switched)
            self.add_incident({"start": self.drop_t, "end": time.time(), "stall": time.time() - self.drop_t, "missed": None,
                               "errors": list(self.drop_errors), "switched": switched})
            self.drop_t = 0.0
        if not self.announced:
            self.announced = True
            if not self.test:
                self.app.notify_started(self)

    def diagnose_drop(self, file, errors):
        """Runs in the background after an unexpected ffmpeg exit: tidy away a part that never got going, write the
        facts to the log, then probe the network."""
        try:                                       # a part that never got going is just clutter
            if not os.path.exists(file) or os.path.getsize(file) < 20000:
                if os.path.exists(file):
                    os.remove(file)
                with self.plock:
                    if file in self.files:
                        self.files.remove(file)
        except OSError:
            pass
        try:
            rc = self.last_proc.returncode if getattr(self, "last_proc", None) else "?"
            size = os.path.getsize(file)
            idle = time.time() - os.path.getmtime(file)
            free = shutil.disk_usage(self.outdir).free / 1024 ** 3
            dlog(f"[{self.label}] DROP: the recorder ({getattr(self, 'engine', '?')}) exited with code {rc} after {time.time() - self.started_at:.0f} s; "
                 f"file {os.path.basename(file)} is {size / 1e6:.1f} MB and was last written {idle:.0f} s ago; "
                 f"{free:.1f} GB free; last ffmpeg messages: {errors or 'none'}"
                 + (f"; meaning: {explain_error(errors)}" if explain_error(errors) else ""), logging.WARNING)
        except Exception as e:
            dlog(f"[{self.label}] DROP (couldn't gather details: {e})", logging.WARNING)
        self.probe_network()

    def probe_network(self):
        t0 = time.time()
        try:
            page = fetch(f"/live/{self.ch}") or ""
            dlog(f"[{self.label}] probe: tvmi.mt/live/{self.ch} answered in {time.time() - t0:.1f} s ({len(page)} bytes)")
            for alt in (False, True):
                t1 = time.time()
                try:
                    u = stream_url(self.ch, alt)
                    r = urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=10, context=SSL_CTX)
                    body = r.read(2000).decode("utf-8", "replace")
                    dlog(f"[{self.label}] probe: {'backup' if alt else 'main'} server {u.split('/')[2]} -> HTTP {r.status} in "
                         f"{time.time() - t1:.1f} s, playlist {'looks fine' if '#EXTM3U' in body else 'is NOT a playlist'}")
                except Exception as e:
                    dlog(f"[{self.label}] probe: {'backup' if alt else 'main'} server FAILED after {time.time() - t1:.1f} s: {e}",
                         logging.WARNING)
        except Exception as e:
            dlog(f"[{self.label}] probe: tvmi.mt itself FAILED after {time.time() - t0:.1f} s: {e} "
                 f"(so this is a network / DNS / certificate problem on this PC)", logging.WARNING)

    def read_errors(self, proc):
        """Drain ffmpeg's messages (so it never blocks), keeping the last few."""
        try:
            for line in proc.stderr:
                line = line.decode("utf-8", "replace").strip()
                if line:
                    self.errors.append(line[:160])
                    dlog(f"[{self.label}] ffmpeg: {line[:400]}", logging.WARNING)
        except (OSError, ValueError):
            pass

    def end_proc(self, after=None, drain=False):
        """Ask ffmpeg to finish cleanly (valid file) without blocking the window.
        `after`, if given, runs in the background once ffmpeg has exited."""
        p, self.proc = self.proc, None
        if not p:
            if after:
                t = threading.Thread(target=after)
                t.start()
                self.app.add_reaper(t)
            return
        try:
            if isinstance(p, HlsRecorder):
                p.request_stop(drain=drain)
            elif IS_WIN:
                p.stdin.write(b"q")
                p.stdin.flush()
            else:
                p.send_signal(signal.SIGINT)
        except Exception:
            pass

        def reap():
            try:
                p.wait(timeout=(HlsRecorder.DRAIN_MAX + 15) if (drain and isinstance(p, HlsRecorder)) else 8)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            if after:
                after()
        t = threading.Thread(target=reap)
        t.start()
        self.app.add_reaper(t)

    def pause_resume(self):
        if self.state == "recording":
            self.elapsed = self.rec_time()
            self.gen += 1
            self.end_proc()
            self.state = "paused"
            self.app.log(f"{self.label}: paused (paused time isn't recorded)")
        elif self.state == "paused":
            self.begin_part()

    def stop(self, user=True):
        if self.state == "done":
            return
        dlog(f"[{self.label}] stopping ({'by you' if user is True else 'quitting the app' if user == 'quit' else 'scheduled end / limit / error'}); "
             f"recorded {self.rec_time():.0f} s in {self.part} part(s)")
        self.elapsed = self.rec_time()
        self.end_wall = time.time()
        self.gen += 1
        after = self.app.finish_task(self, user)
        # at the scheduled end, let the built-in recorder finish the chunks up to that moment (a slow connection can lag behind live)
        self.end_proc(after, drain=(user is False and not self.problem))
        self.state = "done"
        self.done_at = time.monotonic()
        self.app.job_finished(self, user)

    def tick(self, nowdt):
        if self.state == "done":
            return
        if self.state == "recording" and self.proc and self.proc.poll() is not None:
            self.elapsed = self.rec_time()
            self.proc = None
            self.drop_errors = list(self.errors)
            threading.Thread(target=self.diagnose_drop, args=(self.file, list(self.errors)), daemon=True).start()
            why = f" ({' | '.join(list(self.errors)[-2:])})" if self.errors else ""
            self.alt = not self.alt
            meaning = explain_error(self.errors)
            self.app.log(f"{self.label}: stream dropped{why}; reconnecting" + (f". Meaning: {meaning}" if meaning else ""))
            self.state = "reconnecting"
            self.retry_at = time.monotonic() + 1
            self.drop_t = time.time()
            self.app.notify_drop(self)
        if self.state == "reconnecting" and time.monotonic() >= self.retry_at:
            self.begin_part()
        if (self.end_at and nowdt >= self.end_at) or (self.limit and self.rec_time() >= self.limit):
            self.stop(user=False)


# ------------------------------------------------------------------ autostart
def set_autostart(on):
    script = os.path.abspath(__file__)
    if IS_WIN:
        p = os.path.join(os.environ.get("APPDATA", HOME), "Microsoft", "Windows", "Start Menu",
                         "Programs", "Startup", "TVMi Recorder.bat")
        pyw = re.sub(r"python\.exe$", "pythonw.exe", sys.executable, flags=re.I)
        text = f'@echo off\r\nstart "" "{pyw}" "{script}" --minimized\r\n'
    else:
        p = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"),
                         "autostart", "tvmi-recorder.desktop")
        text = ("[Desktop Entry]\nType=Application\nName=TVMi Recorder\nTerminal=false\n"
                f'Exec={sys.executable} "{script}" --minimized\n')
    try:
        if on:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        elif os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


# ------------------------------------------------------------------ Discord notifications
DISCORD_URL = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")
DC_KINDS = (("start", "Recording started"), ("finish", "Recording finished / stopped"),
            ("fail", "Not recorded / stopped early"), ("drops", "Stream keeps dropping"),
            ("drop_each", "Every single drop"), ("sched", "Schedule not updating"),
            ("app", "App started / stopped"))
BLUE, GREEN, RED, AMBER, GREY = 0x2563EB, 0x2EA043, 0xD73A49, 0xE3A008, 0x6B7280


def fmt_len(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec} s"
    h, m = divmod(sec // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min"


def explain_error(lines):
    """Plain-English meaning of ffmpeg's cryptic network errors (-138 is Windows' code for a timeout)."""
    text = " ".join(lines)
    if re.search(r"download was still going|too slow", text, re.I):
        return "the connection was too slow: a 12-second chunk of video took longer to download than it takes to play"
    if re.search(r"Error number -(?:138|110|10060)\b|timed out", text, re.I):
        return "the connection stalled for more than 20 seconds (a timeout)"
    if re.search(r"Error number -(?:104|10054)\b|reset by peer|connection reset", text, re.I):
        return "the server closed the connection"
    return ""


def fmt_exact(sec):
    """Like fmt_len but keeps the seconds: 2 min 24 s."""
    sec = int(round(sec))
    h, rest = divmod(sec, 3600)
    m, sc = divmod(rest, 60)
    return " ".join(x for x in (f"{h} h" if h else "", f"{m} min" if m else "", f"{sc} s" if sc or not (h or m) else "") if x)


def fmt_size(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def make_embed(title, color, description="", fields=()):
    e = {"title": title[:256], "color": color, "timestamp": dt.datetime.now(dt.timezone.utc).isoformat()}
    if description:
        e["description"] = description[:4000]
    if fields:
        e["fields"] = [{"name": n[:256], "value": (str(v) or "-")[:1024], "inline": True} for n, v in fields][:25]
    return {"username": "TVMi Recorder", "embeds": [e]}


def deliver(url, payload):
    """POST to a Discord webhook. Returns (ok, message). Waits out Discord's rate limit."""
    body, last = json.dumps(payload).encode("utf-8"), "unknown error"
    for _ in range(4):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                              "User-Agent": "TVMi-Recorder (personal recording app)"})
        try:
            urllib.request.urlopen(req, timeout=15, context=SSL_CTX).read()
            return True, "sent"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                time.sleep(min(wait, 30))
                last = "rate limited by Discord"
                continue
            return False, f"Discord answered HTTP {e.code}" + (" (the webhook was deleted or the URL is wrong)" if e.code in (401, 404) else "")
        except Exception as e:
            last = str(e)
            time.sleep(2)
    return False, last


class Notifier:
    """Sends embeds to a Discord webhook from a background thread, so Discord can never slow a recording."""

    def __init__(self):
        self.cfg = {"url": "", "kinds": frozenset()}
        self.q = queue.Queue()
        self.worker = None
        self.lock = threading.Lock()

    def configure(self, url, kinds):
        self.cfg = {"url": (url or "").strip(), "kinds": frozenset(kinds)}

    def send(self, kind, title, color, description="", fields=()):
        cfg = self.cfg
        if kind not in cfg["kinds"] or not DISCORD_URL.match(cfg["url"]):
            return
        self.q.put((cfg["url"], make_embed(title, color, description, fields)))
        with self.lock:                          # one worker, so messages arrive in order
            if not self.worker:
                self.worker = threading.Thread(target=self.run, daemon=True)
                self.worker.start()

    def run(self):
        while True:
            url, payload = self.q.get()
            try:
                ok, msg = deliver(url, payload)
                dlog(f"discord: {payload['embeds'][0]['title']} -> {msg}", logging.INFO if ok else logging.WARNING)
            except Exception:
                dlog("discord: unexpected error:\n" + traceback.format_exc(), logging.WARNING)
            finally:
                self.q.task_done()

    def flush(self, timeout=6):
        end = time.time() + timeout
        while self.q.unfinished_tasks and time.time() < end:
            time.sleep(0.1)


# ------------------------------------------------------------------ web control
# A small web page (served from inside this app) that does what the window does, so the recorder
# can be run from a phone or another computer. Only this PC, the local network and Tailscale
# addresses may connect; an optional password adds Basic authentication.
class WebError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


WEB_KEYS = ("web", "web_lan", "web_port", "web_pw", "dc_url")     # never exported / imported
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def client_allowed(addr):
    """Loopback, private (LAN), link-local and Tailscale addresses only, never a public one."""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local
                or (ip.version == 4 and ip in _TAILSCALE_NET)
                or (ip.version == 6 and ip in ipaddress.ip_network("fd7a:115c:a1e0::/48")))


def host_allowed(host_header):
    """Refuse Host headers that look like a public website name (DNS-rebinding defence)."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        host = host[1:].split("]")[0]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return "." not in host or host.endswith((".local", ".lan", ".ts.net", ".home.arpa", ".localdomain"))


def my_addresses():
    """{ip: label} for this computer's LAN and Tailscale addresses (may take a few seconds)."""
    found = {}

    def add(ip):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return
        if a.version == 4 and not (a.is_loopback or a.is_link_local):
            found[ip] = "Tailscale" if a in _TAILSCALE_NET else "local network"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    ts = shutil.which("tailscale")
    if not ts and IS_WIN and os.path.exists(r"C:\Program Files\Tailscale\tailscale.exe"):
        ts = r"C:\Program Files\Tailscale\tailscale.exe"
    if ts:
        try:
            r = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
                               **({"creationflags": 0x08000000} if IS_WIN else {}))
            for line in r.stdout.split():
                add(line.strip())
        except Exception:
            pass
    return found


def call_ui(app, fn, timeout=10):
    """Run fn on the window's thread (Tk must only be touched there) and wait for the answer. If it hasn't started
    within `timeout`, it never will: the 503 means nothing happened, so pressing Record again can't record twice."""
    box, done, lock, flags = {}, threading.Event(), threading.Lock(), {"started": False, "cancelled": False}

    def run():
        with lock:
            if flags["cancelled"]:
                return
            flags["started"] = True
        try:
            box["ok"] = fn()
        except WebError as e:
            box["err"] = (e.code, str(e))
        except (ValueError, TypeError, KeyError) as e:
            box["err"] = (400, f"Bad request: {e}")
        except Exception as e:
            dlog("web request failed:\n" + traceback.format_exc(), logging.ERROR)
            box["err"] = (500, f"{type(e).__name__}: {e}")
        done.set()
    app.q.put(run)
    if not done.wait(timeout):
        with lock:
            if not flags["started"]:
                flags["cancelled"] = True
                return 503, {"error": "The recorder is busy and didn't take the request (is a dialog open on the PC?). "
                                      "Nothing was done."}
        if not done.wait(timeout):                       # it started just in time: let it finish
            return 503, {"error": "The recorder is taking a long time over this. It may still happen."}
    if "err" in box:
        return box["err"][0], {"error": box["err"][1]}
    return 200, box["ok"]


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Browsers often close a connection mid-response; that isn't worth a traceback on the console.
        err = sys.exc_info()[1]
        if not isinstance(err, (ConnectionError, TimeoutError)):
            dlog(f"web: error handling a request from {client_address[0]}:\n{traceback.format_exc()}", logging.WARNING)


def new_password():
    """A password that is easy to type on a phone: 12 letters and digits, no look-alikes."""
    return "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(12))


class LoginLimiter:
    """Slows down password guessing: after 5 wrong passwords from one address, that address must wait (30 s, then
    doubling up to an hour) before trying again. A right password clears it."""
    FREE, FIRST, MAX = 5, 30.0, 3600.0

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock, self.state = clock, threading.Lock(), {}

    def wait_left(self, ip):
        with self.lock:
            st = self.state.get(ip)
            return max(0.0, st["until"] - self.clock()) if st else 0.0

    def failed(self, ip):
        with self.lock:
            st = self.state.setdefault(ip, {"n": 0, "until": 0.0})
            st["n"] += 1
            if st["n"] >= self.FREE:
                st["until"] = self.clock() + min(self.MAX, self.FIRST * 2 ** (st["n"] - self.FREE))
            if len(self.state) > 1000:                 # don't grow without limit
                self.state.pop(next(iter(self.state)))

    def succeeded(self, ip):
        with self.lock:
            self.state.pop(ip, None)


class WebServer:
    def __init__(self, app):
        self.app, self.httpd = app, None
        self.limiter = LoginLimiter()

    def start(self, port, lan, password, host=None):
        """Returns "" on success, or a message saying why it couldn't start."""
        self.stop()
        app, limiter = self.app, self.limiter
        if lan and not password:
            return "The web page needs a password before other devices can connect."

        class Handler(BaseHTTPRequestHandler):
            server_version = "TVMiRecorder"

            def log_message(self, *args):
                pass

            def send(self, code, body, ctype="application/json", extra=()):
                if not isinstance(body, bytes):
                    body = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                text = ctype.startswith("text/") or ctype == "application/json"
                self.send_header("Content-Type", ctype + ("; charset=utf-8" if text else ""))
                self.send_header("Content-Length", str(len(body)))
                if not any(k == "Cache-Control" for k, _ in extra):
                    self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def guard(self):
                ip = self.client_address[0]
                if not client_allowed(ip):
                    dlog(f"web: refused {ip} (not a local / Tailscale address)", logging.WARNING)
                    self.send(403, {"error": "Only this computer, the local network and Tailscale may connect."})
                    return False
                if not host_allowed(self.headers.get("Host")):
                    dlog(f"web: refused {ip}, odd Host header {self.headers.get('Host')!r}", logging.WARNING)
                    self.send(403, {"error": "Unexpected host name. Use the IP address shown in the app."})
                    return False
                if password and self.path.split("?")[0] not in ICON_PATHS:      # icons aren't secret; browsers fetch them without a login
                    wait = limiter.wait_left(ip)
                    if wait:
                        self.send(429, {"error": f"Too many wrong passwords. Try again in {wait:.0f} s."},
                                  extra=[("Retry-After", str(int(wait) + 1))])
                        return False
                    try:
                        given = base64.b64decode(self.headers.get("Authorization", "")[6:]).decode("utf-8").split(":", 1)[1]
                    except Exception:
                        given = None
                    if given is None or not hmac.compare_digest(given.encode(), password.encode()):
                        if given is not None:
                            limiter.failed(ip)
                            dlog(f"web: wrong password from {ip}", logging.WARNING)
                        self.send(401, {"error": "Password needed."}, extra=[("WWW-Authenticate", 'Basic realm="TVMi Recorder"')])
                        return False
                    limiter.succeeded(ip)
                return True

            def do_GET(self):
                if not self.guard():
                    return
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    self.send(200, INDEX_HTML.encode("utf-8"), "text/html", extra=[
                        ("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; form-action 'none'")])
                elif path == "/favicon.svg":
                    self.send(200, ICON_SVG.encode(), "image/svg+xml", extra=[("Cache-Control", "max-age=86400")])
                elif path in ("/favicon.ico", "/apple-touch-icon.png"):
                    self.send(200, icon_png(), "image/png", extra=[("Cache-Control", "max-age=86400")])
                elif path == "/api/state":
                    self.send(*call_ui(app, app.web_state))
                elif path == "/api/log":
                    self.send(200, {"text": tail_log()})
                elif path == "/api/export":
                    code, res = call_ui(app, app.export_dict)
                    self.send(code, res, extra=[("Content-Disposition", 'attachment; filename="tvmi-rc-settings.json"')])
                else:
                    self.send(404, {"error": "Not found"})

            def do_POST(self):
                if not self.guard():
                    return
                if not self.path.startswith("/api/"):
                    return self.send(404, {"error": "Not found"})
                # A JSON content type can't be sent by another website without a permission check, so this blocks
                # other pages in your browser from driving the recorder.
                if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                    return self.send(415, {"error": "JSON only"})
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if n < 0:
                        raise ValueError
                    if n > 2_000_000:
                        return self.send(413, {"error": "Too large"})
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError
                except (ValueError, json.JSONDecodeError):
                    return self.send(400, {"error": "Bad JSON"})
                action = self.path[5:].split("?")[0]
                dlog(f"web: {self.client_address[0]} -> {action}")
                code, res = call_ui(app, lambda: app.web_action(action, body))
                self.send(code, res)

        try:
            self.httpd = QuietServer((host or ("0.0.0.0" if lan else "127.0.0.1"), port), Handler)
        except OSError as e:
            self.httpd = None
            return f"Couldn't start the web page on port {port}: {e}"
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return ""

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#2563eb"/>'
            '<path d="M25 18v28l24-14z" fill="#fff"/></svg>')
_icon_png = []


def icon_png(size=180):
    """The play-button icon as a PNG (for iPhones' home screen and old browsers), drawn in plain Python."""
    if _icon_png:
        return _icon_png[0]
    ss, rad = 3, 14 / 64
    tri = ((25 / 64, 18 / 64), (25 / 64, 46 / 64), (49 / 64, 32 / 64))
    (ax, ay), (bx, by), (cx, cy) = tri
    den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            box = white = 0
            for sy in range(ss):
                for sx in range(ss):
                    px, py = (x + (sx + .5) / ss) / size, (y + (sy + .5) / ss) / size
                    dx, dy = max(abs(px - .5) - (.5 - rad), 0), max(abs(py - .5) - (.5 - rad), 0)
                    if dx * dx + dy * dy > rad * rad:
                        continue
                    box += 1
                    l1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / den
                    l2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / den
                    if l1 >= 0 and l2 >= 0 and 1 - l1 - l2 >= 0:
                        white += 1
            if not box:
                raw += b"\0\0\0\0"
                continue
            w = white / box
            raw += bytes((round(37 + (255 - 37) * w), round(99 + (255 - 99) * w), round(235 + (255 - 235) * w),
                          round(255 * box / (ss * ss))))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    _icon_png.append(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    return _icon_png[0]


ICON_PATHS = ("/favicon.svg", "/favicon.ico", "/apple-touch-icon.png")


def tail_log(max_lines=300):
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-max_lines:])
    except OSError:
        return "(no log yet)"


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TVMi Recorder</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="theme-color" content="#2563eb">
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1b1f24;--mut:#667085;--line:#e1e4e8;--acc:#2563eb;--accfg:#fff;--bad:#c62828;--ok:#1b7f3b;--warn:#b26a00}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1f26;--fg:#e6e8eb;--mut:#98a2b3;--line:#2a313a;--acc:#5b8def;--accfg:#0b1220;--bad:#ff6b6b;--ok:#4cc38a;--warn:#f0b34a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 16px 6px;max-width:980px;margin:0 auto}
h1{font-size:19px;margin:0}h2{font-size:15px;margin:0 0 10px}
#conn{color:var(--bad);font-size:13px}
nav{display:flex;gap:6px;padding:6px 16px;max-width:980px;margin:0 auto;overflow-x:auto}
nav button{background:none;border:1px solid transparent;color:var(--mut);padding:7px 13px;border-radius:8px;font:inherit;cursor:pointer;white-space:nowrap}
nav button.on{background:var(--card);color:var(--fg);border-color:var(--line);font-weight:600}
main{max-width:980px;margin:0 auto;padding:6px 16px 40px}
section{display:none}section.on{display:block}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:10px 0}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--mut);font-weight:500;font-size:13px}tr:last-child td{border-bottom:0}
.tw{overflow-x:auto}
.job{padding:10px 0;border-bottom:1px solid var(--line)}.job:last-child{border-bottom:0}.jt{display:flex;justify-content:space-between;gap:10px;font-weight:600;overflow-wrap:anywhere}

.b{background:var(--acc);color:var(--accfg);border:0;border-radius:8px;padding:7px 13px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}
.b.g{background:transparent;color:var(--fg);border:1px solid var(--line)}
.b.d{background:var(--bad);color:#fff}
button.b:disabled{opacity:.5}
input,select{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:6px 9px;max-width:100%}
input[type=number]{width:74px}input[type=text],input[type=search]{width:100%}
label{display:block;margin:8px 0 3px;color:var(--mut);font-size:13px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.row>.grow{flex:1;min-width:160px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:720px){.cols{grid-template-columns:1fr}}
.list{max-height:340px;overflow:auto;border:1px solid var(--line);border-radius:8px}
.item{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:6px 10px;border-bottom:1px solid var(--line)}
.item:last-child{border-bottom:0}.item .t{flex:1;min-width:0;overflow-wrap:anywhere}
.mut{color:var(--mut);font-size:13px}.warn{color:var(--warn)}
.st{font-weight:600}.st.recording{color:var(--ok)}.st.paused,.st.reconnecting,.st.connecting{color:var(--warn)}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;max-height:520px;font:12px/1.4 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
#toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:9px 16px;border-radius:9px;max-width:90vw;opacity:0;transition:opacity .2s;pointer-events:none}
#toast.show{opacity:.96}#toast.bad{background:var(--bad);color:#fff}
.chk{display:flex;gap:8px;align-items:center;margin:8px 0;color:var(--fg);font-size:15px}.chk input{width:auto}
</style></head><body>
<header><h1>TVMi Recorder</h1><span id="conn"></span></header>
<nav id="tabs">
<button data-t="rec">Recording</button><button data-t="follow">Follow shows</button>
<button data-t="up">Upcoming</button><button data-t="set">Settings</button><button data-t="log">Log</button>
</nav>
<main>
<div id="noffmpeg" class="card warn" hidden>ffmpeg wasn't found on the recorder's computer, so nothing can be recorded until it is installed.</div>

<section id="t-rec">
 <div class="card" id="now"><h2>Recording now</h2>
  <div id="jobs"></div>
  <div id="nojobs" class="mut">Nothing is recording.</div>
 </div>
 <div class="card"><h2>Record now</h2>
  <div class="row">
   <div><label for="ch">Channel</label><select id="ch"><option value="2">TVM</option><option value="3">TVM+</option><option value="4">TVMsport+</option><option value="5">Magic</option><option value="6">Radju Malta</option><option value="7">Radju Malta 2</option></select></div>
   <div><label for="h">Stop after (hours)</label><input id="h" type="number" min="0" max="99" value="0"></div>
   <div><label for="m">minutes</label><input id="m" type="number" min="0" max="59" value="0"></div>
  </div>
  <label for="name">File name (blank = automatic)</label><input id="name" type="text" maxlength="100">
  <p class="mut">0 hours and 0 minutes means it records until you press Stop. It starts from about a minute before now, which is all the stream keeps.</p>
  <div class="row"><button class="b" id="rec">● Record</button><button class="b g" id="test">Test (1 min)</button></div>
 </div>
</section>

<section id="t-follow">
 <div class="card"><label class="chk"><input type="checkbox" id="auto"> Record followed shows automatically</label>
  <div class="row" style="margin-top:8px"><button class="b g" id="allbtn">● Record ALL shows: off</button>
   <select id="allday"><option value="first">First airing each day</option><option value="all">Every airing</option></select>
   <label class="chk" style="margin:0"><input type="checkbox" id="allnews"> Skip news bulletins</label></div>
  <p class="mut" style="margin:8px 0 0">When on, every show on the TVM and TVM+ schedules is recorded (not radio, Magic or TVMsport+), including ones that appear later. Shows you follow yourself keep their own settings.</p></div>
 <div class="cols">
  <div class="card"><h2>Shows on the schedule this week</h2>
   <input id="q" type="search" placeholder="Search shows…"><div class="list" id="shows" style="margin-top:8px"></div></div>
  <div class="card"><h2>Shows you follow</h2><div class="list" id="follows"></div>
   <div class="row" style="margin-top:8px"><input class="grow" id="add" type="text" placeholder="Not listed? Type the exact name"><button class="b g" id="addb">Add</button></div></div>
 </div>
 <p class="mut">TVMi doesn't say which airings are repeats, so “First airing each day” records only the first time a show appears on a given date. Choose “Every airing”, or a single channel, to change that.</p>
</section>

<section id="t-up">
 <div class="card"><div class="row"><button class="b g" id="refresh">Refresh schedule</button><span class="mut" id="sched"></span></div>
  <div class="tw"><table><thead><tr><th>Show</th><th>When (recording window)</th><th>Channel</th><th>Status</th></tr></thead><tbody id="up"></tbody></table></div>
  <div id="noup" class="mut">No followed shows coming up. Follow some on the “Follow shows” tab.</div></div>
</section>

<section id="t-set">
 <div class="card"><h2>Settings</h2>
  <label for="out">Save recordings in (a folder on the recorder's computer)</label><input id="out" type="text">
  <div class="row"><div><label for="pre">Start early by (minutes)</label><input id="pre" type="number" min="0" max="60"></div>
   <div><label for="post">Keep recording after the end (minutes)</label><input id="post" type="number" min="0" max="120"></div></div>
  <label class="chk"><input type="checkbox" id="mkv"> Convert each finished recording to .mkv (lossless)</label>
  <label class="chk" style="margin-left:24px"><input type="checkbox" id="delts"> Delete the .ts file once the .mkv has been made and checked</label>
  <label class="chk"><input type="checkbox" id="login"> Start the app when I log in to that computer</label>
  <div class="row"><button class="b" id="save">Save settings</button></div>
 </div>
 <div class="card"><h2>Export / import</h2>
  <div class="row"><a class="b g" href="/api/export" download>Export settings &amp; followed shows</a>
   <label class="b g" style="margin:0;cursor:pointer">Import…<input id="imp" type="file" accept=".json,application/json" hidden></label></div>
  <p class="mut">The web page's own port and password can only be changed in the app's window.</p>
 </div>
</section>

<section id="t-log">
 <div class="card"><div class="row"><h2 style="margin:0" class="grow">Debug log</h2><span class="mut">updates every few seconds</span></div><pre id="log"></pre></div>
</section>
</main>
<div id="toast"></div>
<script>
"use strict";
const $=s=>document.querySelector(s);
const ST={connecting:'connecting…',recording:'recording',paused:'paused',reconnecting:'reconnecting…',done:'finished'};
const CH={2:'TVM',3:'TVM+',4:'TVMsport+',5:'Magic',6:'Radju Malta',7:'Radju Malta 2'},CHF={0:'Any channel',2:'TVM only',3:'TVM+ only',4:'TVMsport+ only',5:'Magic only',6:'Radju Malta only',7:'Radju Malta 2 only'},PD={first:'First airing each day',all:'Every airing'};
let S=null,first=true,keyFollows='',keyShows='',tab='rec',tt;
function h(tag,attrs,...kids){const e=document.createElement(tag);for(const[k,v]of Object.entries(attrs||{})){if(k.startsWith('on'))e.addEventListener(k.slice(2),v);else if(v===true)e.setAttribute(k,'');else if(v!==false&&v!=null)e.setAttribute(k,v)}for(const c of kids.flat())if(c!=null)e.append(c instanceof Node?c:document.createTextNode(String(c)));return e}
async function api(path,body){const r=await fetch('/api/'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let j={};try{j=await r.json()}catch(e){}if(!r.ok)throw new Error(j.error||('Error '+r.status));return j}
function toast(msg,bad){const t=$('#toast');t.textContent=msg;t.className='show'+(bad?' bad':'');clearTimeout(tt);tt=setTimeout(()=>t.className='',bad?5000:2500)}
async function act(action,body,ok){try{const r=await api(action,body||{});toast(r.message||ok||'Done');await poll();return true}catch(e){toast(e.message,true);return false}}
const fmt=s=>Math.floor(s/3600)+':'+String(Math.floor(s%3600/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');
function show(t){tab=t;for(const s of document.querySelectorAll('section'))s.classList.toggle('on',s.id==='t-'+t);for(const b of document.querySelectorAll('nav button'))b.classList.toggle('on',b.dataset.t===t);location.hash=t;if(t==='log')loadLog()}
for(const b of document.querySelectorAll('nav button'))b.onclick=()=>show(b.dataset.t);

function renderJobs(){const box=$('#jobs');box.replaceChildren();
 for(const j of S.jobs){const live=j.state!=='done';
  const until=/^\d\d:\d\d$/.test(j.until)?'until '+j.until:j.until;
  box.append(h('div',{class:'job'},
   h('div',{class:'jt'},h('span',{},j.label),h('span',{class:'st '+j.state},ST[j.state]||j.state)),
   h('div',{class:'mut'},[j.channel,fmt(j.recorded)+' recorded',until,j.part?('part '+j.part+' · '+j.size+' MB'):''].filter(Boolean).join(' · ')),
   live?h('div',{class:'row',style:'margin-top:8px'},
     (j.state==='recording'||j.state==='paused')?h('button',{class:'b g',onclick:()=>act('pause',{id:j.id})},j.state==='paused'?'▶ Resume':'❚❚ Pause'):null,
     h('button',{class:'b d',onclick:()=>{if(confirm('Stop this recording?'))act('stop',{id:j.id})}},'■ Stop')):null))}
 $('#nojobs').hidden=S.jobs.length>0}
function renderUp(){const tb=$('#up');tb.replaceChildren();
 for(const u of S.upcoming)tb.append(h('tr',{},h('td',{},u.title),h('td',{},u.when),h('td',{},u.channel),h('td',{},u.status)));
 $('#noup').hidden=S.upcoming.length>0;$('#sched').textContent=S.schedule.fetching?'Updating schedule…':(S.schedule.updated?'Schedule updated '+S.schedule.updated:'')}
function renderFollows(){const k=JSON.stringify(S.follows);if(k===keyFollows)return;keyFollows=k;const box=$('#follows');box.replaceChildren();
 if(!S.follows.length)box.append(h('div',{class:'item mut'},'You aren’t following anything yet.'));
 for(const f of S.follows){
  const c=h('select',{onchange:()=>opts()},Object.entries(CHF).map(([v,l])=>h('option',{value:v,selected:+v===f.channel},l)));
  const d=h('select',{onchange:()=>opts()},Object.entries(PD).map(([v,l])=>h('option',{value:v,selected:v===f.per_day},l)));
  const opts=()=>act('follow_options',{title:f.title,channel:+c.value,per_day:d.value});
  box.append(h('div',{class:'item',style:'flex-wrap:wrap'},h('span',{class:'t'},f.title),h('div',{class:'row'},c,d,h('button',{class:'b g',onclick:()=>act('unfollow',{title:f.title})},'Unfollow'))))}}
function renderShows(){const q=$('#q').value.trim().toLowerCase();const fol=new Set(S.follows.map(f=>f.title.toLowerCase()));
 const list=S.shows.filter(t=>!fol.has(t.toLowerCase())&&(!q||t.toLowerCase().includes(q))).slice(0,300);const k=JSON.stringify([list,q]);if(k===keyShows)return;keyShows=k;
 const box=$('#shows');box.replaceChildren();if(!list.length)box.append(h('div',{class:'item mut'},S.shows.length?'No matches.':'No schedule loaded yet.'));
 for(const t of list)box.append(h('div',{class:'item'},h('span',{class:'t'},t),h('button',{class:'b g',onclick:()=>act('follow',{title:t})},'Follow')))}
function fillSettings(){const s=S.settings;$('#out').value=s.out;$('#pre').value=s.pre;$('#post').value=s.post;$('#mkv').checked=s.mkv;$('#delts').checked=s.del_ts;$('#login').checked=s.login}
function render(){$('#noffmpeg').hidden=S.settings.ffmpeg;renderJobs();renderUp();renderFollows();renderShows();
 if(document.activeElement!==$('#auto'))$('#auto').checked=S.settings.auto;
 const ab=$('#allbtn');ab.textContent='● Record ALL shows: '+(S.settings.all?'ON':'off');ab.className='b'+(S.settings.all?'':' g');
 if(document.activeElement!==$('#allday'))$('#allday').value=S.settings.all_per_day;if(document.activeElement!==$('#allnews'))$('#allnews').checked=S.settings.all_skip_news;if(first){first=false;fillSettings()}}
async function poll(){try{S=await api('state');$('#conn').textContent='';render()}catch(e){$('#conn').textContent='Can’t reach the recorder: '+e.message}}
async function loadLog(){if(tab!=='log')return;try{const r=await api('log');const p=$('#log');const end=p.scrollTop+p.clientHeight>=p.scrollHeight-30;p.textContent=r.text;if(end)p.scrollTop=p.scrollHeight}catch(e){}}

$('#rec').onclick=()=>act('record',{channel:+$('#ch').value,h:+$('#h').value||0,m:+$('#m').value||0,name:$('#name').value});
$('#test').onclick=()=>act('test',{channel:+$('#ch').value},'Test started: recording 1 minute…');
$('#auto').onchange=e=>act('settings',{auto:e.target.checked});
$('#allbtn').onclick=()=>act('settings',{all:!S.settings.all},S.settings.all?'Stopped recording all shows':'Now recording ALL shows');
$('#allday').onchange=e=>act('settings',{all_per_day:e.target.value});
$('#allnews').onchange=e=>act('settings',{all_skip_news:e.target.checked});
$('#q').oninput=()=>{if(S)renderShows()};
$('#addb').onclick=async()=>{const t=$('#add').value.trim();if(t&&await act('follow',{title:t}))$('#add').value=''};
$('#add').onkeydown=e=>{if(e.key==='Enter')$('#addb').click()};
$('#refresh').onclick=()=>act('refresh',{});
$('#save').onclick=async()=>{if(await act('settings',{out:$('#out').value,pre:+$('#pre').value,post:+$('#post').value,mkv:$('#mkv').checked,del_ts:$('#delts').checked,login:$('#login').checked},'Settings saved'))fillSettings()};
$('#imp').onchange=async e=>{const f=e.target.files[0];e.target.value='';if(!f)return;let d;try{d=JSON.parse(await f.text())}catch(x){return toast('That isn’t a settings file',true)}
 if(confirm('Replace the current settings and followed shows with the ones in this file?')&&await act('import',{data:d}))fillSettings()};

show(location.hash.slice(1)||'rec');if(!document.querySelector('nav button.on'))show('rec');
poll();setInterval(poll,2000);setInterval(loadLog,3000);
</script></body></html>
"""


# ------------------------------------------------------------------ the app
DEFAULTS = {"engine": "hls", "out": os.path.join(HOME, "Videos"), "pre": 3, "post": 10, "auto": True,
            "login": False, "mkv": False, "del_ts": True, "web": True, "web_lan": False, "web_port": 9001, "web_pw": "", "dc_url": "",
            **{"dc_" + k: True for k, _ in DC_KINDS},
            "channel": 2, "h": 0, "m": 0, "name": "",
            "all": False, "all_per_day": "first", "all_skip_news": True}
NEWS_RE = re.compile(r"a[ħh]bar|news", re.I)       # bulletins: "Record ALL shows" leaves these out unless told not to
PER_DAY = {"first": "First airing each day", "all": "Every airing"}
SKIP_LABEL = {"low disk space": "skipped (low disk)", "error": "skipped (error)", "stopped early": "stopped early"}


def sched_key(it):
    """How a listing is known in the history: channel, start time and show."""
    return f'{it["ch"]}|{it["start"].isoformat()}|{norm(it["title"])}'
CH_FILTER = {0: "Any channel", **{n: f"{name} only" for n, name in CHANNELS.items()}}


class App:
    def __init__(self, root):
        self.root = root
        root.title("TVMi Recorder")
        root.minsize(760, 640)
        self.ffmpeg = find_ffmpeg()
        self.q = queue.Queue()          # worker threads hand results to the Tk thread through this
        self.reapers = []
        self.jobs = []
        self.data = load_json(SETTINGS_FILE, {})
        s = dict(DEFAULTS)
        s.update(self.data.get("s", {}))
        self.follows = clean_follows(self.data.get("follows", []))
        self.hist = self.data.get("hist", {})
        self.closing = False
        self.fast_quit = False         # quitting for the OS (logout, shutdown): no .mkv conversion, don't wait long
        self.term = False              # set by SIGTERM / SIGHUP
        self.converting = set()        # ffmpeg conversions running now, so "Quit now" can stop them
        self.save_job = None
        cache = load_json(SCHEDULE_FILE, {})
        fresh = cache.get("v") == SCHEDULE_VERSION       # older caches have the old end-time logic
        self.sched = self.parse_sched(cache.get("items", [])) if fresh else []
        self.sched_time = cache.get("fetched", 0) if fresh else 0
        self.fetching = False
        self.retry_sched = 0
        self.last_plan = 0.0

        self.v_out = tk.StringVar(value=s["out"])
        self.v_pre = tk.StringVar(value=str(s["pre"]))
        self.v_post = tk.StringVar(value=str(s["post"]))
        self.v_auto = tk.BooleanVar(value=s["auto"])
        self.v_login = tk.BooleanVar(value=s["login"])
        self.v_ffmpeg_engine = tk.BooleanVar(value=(s["engine"] == "ffmpeg"))
        self.v_mkv = tk.BooleanVar(value=s["mkv"])
        self.v_del = tk.BooleanVar(value=s["del_ts"])
        self.notifier = Notifier()
        self.v_dc_url = tk.StringVar(value=s["dc_url"])
        self.v_dc = {k: tk.BooleanVar(value=bool(s["dc_" + k])) for k, _ in DC_KINDS}
        self.v_dc_status = tk.StringVar()
        self.refresh_notifier()
        self.sched_alerted = False
        self.started_wall = time.time()
        self.v_all = tk.BooleanVar(value=bool(s["all"]))
        self.v_all_day = tk.StringVar(value=PER_DAY.get(s["all_per_day"], PER_DAY["first"]))
        self.v_all_news = tk.BooleanVar(value=bool(s["all_skip_news"]))
        self.v_web = tk.BooleanVar(value=s["web"])
        self.v_weblan = tk.BooleanVar(value=s["web_lan"])
        self.v_webport = tk.StringVar(value=str(s["web_port"]))
        self.v_webpw = tk.StringVar(value=s["web_pw"])
        self.v_ch = tk.StringVar(value=self.ch_label(s["channel"]))
        self.v_h = tk.StringVar(value=str(s["h"]))
        self.v_m = tk.StringVar(value=str(s["m"]))
        self.v_name = tk.StringVar(value=s["name"])
        self.v_sched = tk.StringVar()
        self.v_filter = tk.StringVar()
        self.v_add = tk.StringVar()
        self.v_fch = tk.StringVar()
        self.v_fday = tk.StringVar()
        self.v_filter.trace_add("write", lambda *_: self.fill_shows())
        for v in (self.v_pre, self.v_post, self.v_out):      # typed values, not only arrow clicks and Browse
            v.trace_add("write", lambda *_: self.save_soon())

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.build_follow(nb)
        self.build_upcoming(nb)
        self.build_manual(nb)
        self.build_settings(nb)
        self.build_bottom(root)
        root.protocol("WM_DELETE_WINDOW", self.close)
        # Logging out or shutting down: Windows sends this (Tk turns WM_QUERYENDSESSION into it), Linux sends SIGTERM.
        # Don't ask "Stop them and quit?" then: the OS will close the app anyway.
        root.protocol("WM_SAVE_YOURSELF", lambda: self.close(confirm=False, fast=True))
        for name in ("SIGTERM", "SIGHUP"):
            if hasattr(signal, name):
                try:
                    signal.signal(getattr(signal, name), lambda *_: setattr(self, "term", True))
                except (ValueError, OSError):
                    pass

        if not self.ffmpeg and self.v_ffmpeg_engine.get():
            messagebox.showwarning("TVMi Recorder", "ffmpeg wasn't found.\n\nInstall it (Windows: winget install ffmpeg; "
                                   "Linux: your package manager) or put ffmpeg next to this file, then restart.")
        if self.v_login.get():
            set_autostart(True)
        self.fill_shows()
        self.fill_follows()
        root.report_callback_exception = self.report_callback_exception
        self.web = WebServer(self)
        self.raise_seen = self.raise_stamp()
        threading.Thread(target=self.log_startup, args=(self.startup_facts(),), daemon=True).start()
        threading.Thread(target=self.watch_sizes, daemon=True).start()
        self.start_web()
        self.pump()
        self.notifier.send("app", "🟢 TVMi Recorder started", GREEN,
                           f"Following {len(self.follows)} show{'s' if len(self.follows) != 1 else ''}"
                           f"{' and recording ALL shows' if self.v_all.get() else ''}. "
                           f"Automatic recording is {'on' if self.v_auto.get() else 'off'}.")
        self.log("Ready. Keep this window open (minimised is fine): recordings only happen while it's running.")
        self.tick()

    def startup_facts(self):
        return {"engine": self.v_ffmpeg_engine.get(), "out": self.v_out.get(), "pre": self.v_pre.get(),
                "post": self.v_post.get(), "auto": self.v_auto.get(), "mkv": self.v_mkv.get(),
                "follows": [f["title"] for f in self.follows]}

    def log_startup(self, facts):
        """Runs in the background: running ffmpeg can take a while."""
        ver = ""
        if self.ffmpeg:
            try:
                r = subprocess.run([self.ffmpeg, "-version"], capture_output=True, text=True, timeout=20,
                                   stdin=subprocess.DEVNULL, **({"creationflags": 0x08000000} if IS_WIN else {}))
                ver = r.stdout.splitlines()[0] if r.stdout else ""
            except Exception as e:
                ver = f"(couldn't run: {e})"
        dlog("=" * 70)
        dlog(f"Started. Python {sys.version.split()[0]} on {sys.platform}; tk {tk.TkVersion}; time zone data: {'yes' if TZ else 'NO'}; "
             f"certifi: {'yes' if 'certifi' in sys.modules or _has_certifi() else 'no'}")
        dlog(f"recording method: {'ffmpeg' if facts['engine'] else 'built-in recorder'}")
        dlog(f"ffmpeg: {self.ffmpeg} | {ver}" + ("" if self.ffmpeg else " (not found: needed only for .mkv conversion and the Test file check)"))
        if self.ffmpeg:
            dlog(f"ffmpeg resilience options: {' '.join(resilience_opts(self.ffmpeg))}")
        dlog(f"save folder: {facts['out']} | margins: {facts['pre']} min early, {facts['post']} min late | "
             f"auto: {facts['auto']} | mkv: {facts['mkv']} | follows: {facts['follows']}")
        dlog(f"config folder: {CFG}")

    # ---------- persistence
    def day_key(self, label):
        return next((k for k, v in PER_DAY.items() if v == label), "first")

    def all_changed(self):
        if self.v_all.get() and not self.v_auto.get():
            self.v_auto.set(True)
            self.log("Automatic recording was off, so it has been switched on for \"Record ALL shows\".")
        self.save()
        self.last_plan = 0
        state = (f"ON ({PER_DAY[self.day_key(self.v_all_day.get())].lower()}"
                 f"{', news bulletins skipped' if self.v_all_news.get() else ''})") if self.v_all.get() else "off"
        self.log(f"Record ALL shows: {state}")

    def channel_num(self, label):
        m = re.match(r"\d+", label)
        return int(m.group()) if m else 2

    def ch_label(self, n):
        return f"{n} - {CHANNELS.get(n, '?')}"

    def num(self, var, default, lo=0, hi=999):
        try:
            return max(lo, min(hi, int(var.get())))
        except ValueError:
            return default

    def save(self):
        s = {"out": self.v_out.get(), "pre": self.num(self.v_pre, 3, 0, 60), "post": self.num(self.v_post, 10, 0, 120),
             "auto": self.v_auto.get(), "login": self.v_login.get(), "mkv": self.v_mkv.get(), "del_ts": self.v_del.get(),
             "engine": "ffmpeg" if self.v_ffmpeg_engine.get() else "hls", "channel": self.channel_num(self.v_ch.get()),
             "h": self.num(self.v_h, 0), "m": self.num(self.v_m, 0), "name": self.v_name.get(),
             "all": self.v_all.get(), "all_per_day": self.day_key(self.v_all_day.get()), "all_skip_news": self.v_all_news.get(),
             "web": self.v_web.get(), "web_lan": self.v_weblan.get(), "web_port": self.num(self.v_webport, 9001, 1024, 65535),
             "web_pw": self.v_webpw.get(), "dc_url": self.v_dc_url.get().strip(),
             **{"dc_" + k: v.get() for k, v in self.v_dc.items()}}
        self.refresh_notifier()
        cutoff = (now() - dt.timedelta(days=60)).date().isoformat()
        self.hist = {k: v for k, v in self.hist.items() if v["date"] >= cutoff}
        try:
            save_json(SETTINGS_FILE, {"s": s, "follows": self.follows, "hist": self.hist})
        except OSError:
            pass

    LOG_ROWS = 1000

    def log(self, msg):
        dlog(msg)
        self.logbox.insert("end", f"{now():%H:%M:%S}  {msg}")
        extra = self.logbox.size() - self.LOG_ROWS          # the app runs for weeks: keep only the latest rows
        if extra > 0:
            self.logbox.delete(0, extra - 1)
        self.logbox.see("end")

    def save_soon(self):
        """Save a moment after the last keystroke, so a typed value isn't lost if the app doesn't exit cleanly."""
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(800, self.saved_soon)

    def saved_soon(self):
        self.save_job = None
        self.save()

    def add_reaper(self, t):
        self.reapers = [r for r in self.reapers if r.is_alive()] + [t]

    def watch_sizes(self):
        """Background thread: keep each recording's file size up to date. A hung network drive can block this for
        minutes; on the window's thread that would stop recordings from ending on time."""
        while True:
            for j in list(self.jobs):
                if j.state != "done" and j.file:
                    try:
                        j.size = os.path.getsize(j.file)
                    except OSError:
                        pass
            time.sleep(3)

    @staticmethod
    def raise_stamp():
        try:
            return os.path.getmtime(RAISE_FILE)
        except OSError:
            return 0.0

    def check_raise(self):
        """Another copy was started and asked this one to show its window."""
        stamp = self.raise_stamp()
        if stamp != self.raise_seen:
            self.raise_seen = stamp
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()

    def report_callback_exception(self, exc, val, tb):
        dlog("Uncaught exception in the window:\n" + "".join(traceback.format_exception(exc, val, tb)), logging.ERROR)
        self.log(f"Internal error: {val} (details are in the log file)")

    # ---------- tabs
    def build_follow(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Follow shows")
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Checkbutton(top, text="Record followed shows automatically", variable=self.v_auto,
                        command=self.save).pack(side="left")
        allrow = ttk.Frame(f)
        allrow.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(allrow, text="● Record ALL shows", style="Toolbutton", variable=self.v_all,
                        command=self.all_changed).pack(side="left")
        ttk.Label(allrow, text="  Record:").pack(side="left")
        cb = ttk.Combobox(allrow, textvariable=self.v_all_day, values=list(PER_DAY.values()), state="readonly", width=22)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.all_changed())
        ttk.Checkbutton(allrow, text="Skip news bulletins", variable=self.v_all_news, command=self.all_changed).pack(side="left", padx=8)
        ttk.Label(f, foreground="gray40", wraplength=720, justify="left",
                  text="When on, every show on the TVM and TVM+ schedules is recorded (not radio, Magic or TVMsport+), including ones that appear later. "
                       "Shows you follow yourself keep their own settings."
                  ).pack(anchor="w", pady=(4, 0))
        cols = ttk.Frame(f)
        cols.pack(fill="both", expand=True, pady=8)
        cols.columnconfigure((0, 2), weight=1, uniform="c")
        cols.rowconfigure(1, weight=1)
        ttk.Label(cols, text="Shows on the schedule this week").grid(row=0, column=0, sticky="w")
        ttk.Label(cols, text="Shows you follow").grid(row=0, column=2, sticky="w")
        left = ttk.Frame(cols)
        left.grid(row=1, column=0, sticky="nsew")
        ttk.Entry(left, textvariable=self.v_filter).pack(fill="x")
        self.shows = tk.Listbox(left, height=10, exportselection=False)
        self.shows.pack(fill="both", expand=True, pady=(4, 0))
        self.shows.bind("<Double-1>", lambda e: self.follow_selected())
        mid = ttk.Frame(cols)
        mid.grid(row=1, column=1, padx=8)
        ttk.Button(mid, text="Follow ▶", command=self.follow_selected).pack(pady=4)
        ttk.Button(mid, text="◀ Unfollow", command=self.unfollow).pack(pady=4)
        right = ttk.Frame(cols)
        right.grid(row=1, column=2, sticky="nsew")
        self.ftree = ttk.Treeview(right, columns=("ch", "day"), height=8, selectmode="browse")
        self.ftree.heading("#0", text="Show")
        self.ftree.heading("ch", text="Channel")
        self.ftree.heading("day", text="Record")
        self.ftree.column("#0", width=170)
        self.ftree.column("ch", width=85)
        self.ftree.column("day", width=130)
        self.ftree.pack(fill="both", expand=True)
        self.ftree.bind("<<TreeviewSelect>>", lambda e: self.follow_selected_changed())
        opt = ttk.Frame(right)
        opt.pack(fill="x", pady=(6, 0))
        self.c_fch = ttk.Combobox(opt, textvariable=self.v_fch, values=list(CH_FILTER.values()), state="readonly", width=18)
        self.c_fday = ttk.Combobox(opt, textvariable=self.v_fday, values=list(PER_DAY.values()), state="readonly", width=20)
        self.c_fch.pack(side="left")
        self.c_fday.pack(side="left", padx=6)
        for c in (self.c_fch, self.c_fday):
            c.bind("<<ComboboxSelected>>", lambda e: self.follow_options_changed())
        add = ttk.Frame(f)
        add.pack(fill="x")
        ttk.Label(add, text="Not listed? Type the exact show name:").pack(side="left")
        ttk.Entry(add, textvariable=self.v_add, width=30).pack(side="left", padx=6)
        ttk.Button(add, text="Add", command=self.add_typed).pack(side="left")
        ttk.Label(f, foreground="gray40", wraplength=720, justify="left",
                  text="TVMi doesn't say which airings are repeats, so “First airing each day” records only the first "
                       "time a show appears on a given date; choose “Every airing” (or a single channel) to change that."
                  ).pack(anchor="w", pady=(8, 0))

    def build_upcoming(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Upcoming")
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Button(top, text="Refresh schedule", command=self.refresh_schedule).pack(side="left")
        ttk.Label(top, textvariable=self.v_sched).pack(side="left", padx=10)
        self.utree = ttk.Treeview(f, columns=("when", "ch", "status"), height=12)
        for c, t, w in (("#0", "Show", 220), ("when", "When (recording window)", 280), ("ch", "Channel", 70), ("status", "Status", 150)):
            self.utree.heading(c, text=t)
            self.utree.column(c, width=w)
        self.utree.pack(fill="both", expand=True, pady=(8, 0))

    def build_manual(self, nb):
        f = ttk.Frame(nb, padding=14)
        nb.add(f, text="Record now")
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Channel").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=self.v_ch, values=[self.ch_label(n) for n in CHANNELS], state="readonly",
                     width=18).grid(row=0, column=1, sticky="w")
        ttk.Label(f, text="Stop after").grid(row=1, column=0, sticky="w", pady=4)
        d = ttk.Frame(f)
        d.grid(row=1, column=1, sticky="w")
        ttk.Spinbox(d, from_=0, to=99, width=4, textvariable=self.v_h).pack(side="left")
        ttk.Label(d, text="h ").pack(side="left")
        ttk.Spinbox(d, from_=0, to=59, width=4, textvariable=self.v_m).pack(side="left")
        ttk.Label(d, text="min    (0 and 0 = until you press Stop)").pack(side="left")
        ttk.Label(f, text="File name").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v_name).grid(row=2, column=1, sticky="we")
        ttk.Label(f, text="(blank = automatic)").grid(row=2, column=2, sticky="w", padx=6)
        ttk.Label(f, text="Save to").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v_out).grid(row=3, column=1, sticky="we")
        ttk.Button(f, text="Browse…", command=self.browse).grid(row=3, column=2, padx=6)
        rb = ttk.Frame(f)
        rb.grid(row=4, column=1, sticky="w", pady=14)
        ttk.Button(rb, text="● Record", command=self.start_manual).pack(side="left")
        ttk.Button(rb, text="Test (1 min)", command=self.start_test).pack(side="left", padx=8)
        ttk.Label(f, foreground="gray40", wraplength=640, justify="left",
                  text="Recording starts from about a minute before now (that's all the stream keeps). Use Pause / Stop "
                       "in the list below. Pausing skips the paused time and continues in a new file."
                  ).grid(row=5, column=0, columnspan=3, sticky="w")

    def build_settings(self, nb):
        # The Settings tab is long, so it scrolls instead of making the whole window taller than the screen.
        outer = ttk.Frame(nb)
        nb.add(outer, text="Settings")
        canvas = tk.Canvas(outer, height=380, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        f = ttk.Frame(canvas, padding=14)
        win = canvas.create_window((0, 0), window=f, anchor="nw")
        f.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))

        def wheel(e):
            if canvas.bbox("all") and canvas.bbox("all")[3] > canvas.winfo_height():
                canvas.yview_scroll(-1 if (e.num == 4 or e.delta > 0) else 1, "units")
        canvas.bind("<Enter>", lambda e: (canvas.bind_all("<MouseWheel>", wheel), canvas.bind_all("<Button-4>", wheel),
                                          canvas.bind_all("<Button-5>", wheel)))
        canvas.bind("<Leave>", lambda e: (canvas.unbind_all("<MouseWheel>"), canvas.unbind_all("<Button-4>"),
                                          canvas.unbind_all("<Button-5>")))
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Save recordings in").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v_out).grid(row=0, column=1, sticky="we")
        ttk.Button(f, text="Browse…", command=self.browse).grid(row=0, column=2, padx=6)
        ttk.Label(f, text="Start recording early by").grid(row=1, column=0, sticky="w", pady=4)
        a = ttk.Frame(f)
        a.grid(row=1, column=1, sticky="w")
        ttk.Spinbox(a, from_=0, to=60, width=4, textvariable=self.v_pre, command=self.save).pack(side="left")
        ttk.Label(a, text="minutes (catches shows that start early)").pack(side="left", padx=4)
        ttk.Label(f, text="Keep recording after the end for").grid(row=2, column=0, sticky="w", pady=4)
        b = ttk.Frame(f)
        b.grid(row=2, column=1, sticky="w")
        ttk.Spinbox(b, from_=0, to=120, width=4, textvariable=self.v_post, command=self.save).pack(side="left")
        ttk.Label(b, text="minutes (catches shows that run late)").pack(side="left", padx=4)
        ttk.Checkbutton(f, text="Convert each finished recording to .mkv (lossless, no quality change)", variable=self.v_mkv,
                        command=self.save).grid(row=3, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Checkbutton(f, text="Delete the .ts file once the .mkv has been made and checked", variable=self.v_del,
                        command=self.save).grid(row=4, column=0, columnspan=3, sticky="w", padx=(22, 0))
        misc = ttk.Frame(f)
        misc.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(misc, text="Start this app when I log in (minimised)", variable=self.v_login,
                        command=self.toggle_login).pack(anchor="w")
        ttk.Checkbutton(misc, text="Record with ffmpeg instead of the built-in recorder (older method: a dropped connection splits the file)",
                        variable=self.v_ffmpeg_engine, command=self.save).pack(anchor="w", pady=(4, 0))
        eb = ttk.Frame(f)
        eb.grid(row=6, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(eb, text="Export settings & followed shows…", command=self.export_settings).pack(side="left")
        ttk.Button(eb, text="Import…", command=self.import_settings).pack(side="left", padx=8)
        ttk.Button(eb, text="Open debug log", command=lambda: open_path(LOG_FILE) if os.path.exists(LOG_FILE) else None
                   ).pack(side="left")
        ttk.Button(eb, text="Log folder", command=lambda: open_path(CFG)).pack(side="left", padx=8)
        wf = ttk.LabelFrame(f, text="Web control (from a phone or another computer)", padding=8)
        wf.grid(row=7, column=0, columnspan=3, sticky="we", pady=(12, 0))
        ttk.Checkbutton(wf, text="Run the web page", variable=self.v_web).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(wf, text="Let other devices on my network / Tailscale connect", variable=self.v_weblan
                        ).grid(row=0, column=1, columnspan=4, sticky="w", padx=12)
        ttk.Label(wf, text="Port").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(wf, from_=1024, to=65535, width=7, textvariable=self.v_webport).grid(row=1, column=1, sticky="w", padx=12)
        ttk.Label(wf, text="Password").grid(row=1, column=2, sticky="e")
        pw = ttk.Entry(wf, textvariable=self.v_webpw, show="•", width=18)
        pw.grid(row=1, column=3, sticky="w", padx=6)
        self.v_webpw_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(wf, text="show", variable=self.v_webpw_show,
                        command=lambda: pw.config(show="" if self.v_webpw_show.get() else "•")).grid(row=1, column=4)
        ttk.Button(wf, text="Apply", command=self.apply_web).grid(row=1, column=5, padx=6)
        self.web_text = tk.Text(wf, height=3, width=72, borderwidth=0, wrap="none", background=self.root.cget("background"))
        self.web_text.grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))
        self.web_text.config(state="disabled")
        ttk.Label(wf, foreground="gray40", wraplength=620, justify="left",
                  text="Off by default: only this computer can open the page. Let other devices connect and they need "
                       "the password (one is made for you if it's blank). Your local network and Tailscale can connect, "
                       "never the wider internet. 5 wrong passwords lock that device out for a while."
                  ).grid(row=3, column=0, columnspan=6, sticky="w", pady=(4, 0))
        df = ttk.LabelFrame(f, text="Discord notifications", padding=8)
        df.grid(row=8, column=0, columnspan=3, sticky="we", pady=(12, 0))
        ttk.Label(df, text="Webhook URL").grid(row=0, column=0, sticky="w")
        self.dc_entry = ttk.Entry(df, textvariable=self.v_dc_url, show="•", width=48)
        self.dc_entry.grid(row=0, column=1, columnspan=2, sticky="we", padx=6)
        self.dc_entry.bind("<FocusOut>", lambda e: self.save())
        self.v_dc_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(df, text="show", variable=self.v_dc_show,
                        command=lambda: self.dc_entry.config(show="" if self.v_dc_show.get() else "•")).grid(row=0, column=3)
        ttk.Button(df, text="Send test message", command=self.test_discord).grid(row=0, column=4, padx=6)
        for n, (k, label) in enumerate(DC_KINDS):
            ttk.Checkbutton(df, text=label, variable=self.v_dc[k], command=self.save
                            ).grid(row=1 + n // 2, column=(n % 2) * 2, columnspan=2, sticky="w", padx=(0, 12))
        ttk.Label(df, textvariable=self.v_dc_status, wraplength=560, justify="left").grid(
            row=5, column=0, columnspan=5, sticky="w", pady=(4, 0))
        ttk.Label(df, foreground="gray40", wraplength=620, justify="left",
                  text="In Discord: channel settings → Integrations → Webhooks → New Webhook → Copy URL. The URL is kept "
                       "in settings.json in plain text and is never exported."
                  ).grid(row=6, column=0, columnspan=5, sticky="w", pady=(4, 0))
        ttk.Label(f, foreground="gray40", wraplength=640, justify="left",
                  text="A show's length is taken from the schedule: it ends when the next programme starts. Followed shows "
                       "go in a folder of their own, named after the show. If the PC sleeps or the app is closed, nothing "
                       "is recorded. Schedule times are Maltese time"
                       + ("" if TZ else " (no time-zone data found, so this PC's clock is assumed to be on Maltese time)")
                       + "."
                  ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(12, 0))

    def build_bottom(self, root):
        b = ttk.LabelFrame(root, text="Recording now", padding=6)
        b.pack(fill="x", padx=8, pady=6)
        self.jtree = ttk.Treeview(b, columns=("ch", "state", "time", "until"), height=4, selectmode="browse")
        for c, t, w in (("#0", "Recording", 230), ("ch", "Channel", 70), ("state", "State", 110),
                        ("time", "Recorded", 80), ("until", "Until", 90)):
            self.jtree.heading(c, text=t)
            self.jtree.column(c, width=w)
        self.jtree.pack(side="left", fill="x", expand=True)
        btn = ttk.Frame(b)
        btn.pack(side="left", padx=(8, 0))
        ttk.Button(btn, text="❚❚ Pause / ▶ Resume", command=self.pause_selected).pack(fill="x", pady=2)
        ttk.Button(btn, text="■ Stop", command=self.stop_selected).pack(fill="x", pady=2)
        self.logbox = tk.Listbox(root, height=5)
        self.logbox.pack(fill="x", padx=8, pady=(0, 8))

    # ---------- schedule
    def parse_sched(self, items):
        out = []
        for i in items:
            try:
                out.append({"ch": i["ch"], "title": i["title"], "start": dt.datetime.fromisoformat(i["start"]),
                            "end": dt.datetime.fromisoformat(i["end"]), "guess": bool(i.get("guess"))})
            except (KeyError, ValueError):
                pass
        return sorted(out, key=lambda x: x["start"])

    def refresh_schedule(self):
        if self.fetching:
            return
        self.fetching = True
        self.v_sched.set("Updating schedule…")

        def work():
            try:
                items, failed = fetch_schedule()
            except Exception as e:
                self.q.put(lambda err=e: self.schedule_failed(err))
                return
            self.q.put(lambda: self.schedule_loaded(items, failed))
        threading.Thread(target=work, daemon=True).start()

    def schedule_failed(self, err):
        self.fetching = False
        self.retry_sched = time.time() + 300
        self.log(f"Couldn't update the schedule ({err}); will try again in 5 minutes")
        hours = (time.time() - (self.sched_time or self.started_wall)) / 3600
        if hours >= 6 and not self.sched_alerted:
            self.sched_alerted = True
            self.notifier.send("sched", "⚠️ Schedule isn't updating", AMBER,
                               f"Couldn't reach tvmi.mt for {hours:.0f} hours ({err}). Followed shows may be missed.")

    def schedule_loaded(self, items, failed=()):
        self.fetching = False
        if not items:
            return self.schedule_failed("no programmes found")
        if failed:
            old = [{"ch": x["ch"], "title": x["title"], "start": x["start"].isoformat(), "end": x["end"].isoformat(),
                    "guess": x["guess"]} for x in self.sched]
            items = merge_schedule(items, old, set(failed))
            pages = ", ".join(f"{CHANNELS.get(c, c)} {d}" for c, d in sorted(failed))
            self.log(f"Some schedule pages didn't load ({pages}); kept the previous listings for those")
        self.sched = self.parse_sched(items)
        self.sched_alerted = False
        self.sched_time = time.time()
        try:
            save_json(SCHEDULE_FILE, {"v": SCHEDULE_VERSION, "fetched": self.sched_time, "items": items})
        except OSError:
            pass
        self.log(f"Schedule updated: {len(items)} programmes ({sum(1 for i in items if i.get('guess'))} with a guessed length)")
        self.fill_shows()
        self.last_plan = 0

    # ---------- follow list
    def fill_shows(self):
        flt = norm(self.v_filter.get())
        followed = {norm(f["title"]) for f in self.follows}
        titles = sorted({i["title"] for i in self.sched}, key=str.casefold)
        self.shows.delete(0, "end")
        for t in titles:
            if (not flt or flt in norm(t)) and norm(t) not in followed:
                self.shows.insert("end", t)

    def fill_follows(self):
        self.ftree.delete(*self.ftree.get_children())
        for f in sorted(self.follows, key=lambda f: f["title"].casefold()):
            self.ftree.insert("", "end", iid=norm(f["title"]), text=f["title"],
                              values=(CH_FILTER[f["channel"]], PER_DAY[f["per_day"]]))
        self.fill_shows()
        self.last_plan = 0

    def add_follow(self, title):
        if title and norm(title) not in {norm(f["title"]) for f in self.follows}:
            self.follows.append({"title": title, "channel": 0, "per_day": "first"})
            self.save()
            self.fill_follows()

    def follow_selected(self):
        for t in [self.shows.get(i) for i in self.shows.curselection()]:
            self.add_follow(t)

    def add_typed(self):
        t = " ".join(self.v_add.get().split())
        self.v_add.set("")
        self.add_follow(t)

    def selected_follow(self):
        sel = self.ftree.selection()
        return next((f for f in self.follows if sel and norm(f["title"]) == sel[0]), None)

    def unfollow(self):
        f = self.selected_follow()
        if f:
            self.follows.remove(f)
            self.save()
            self.fill_follows()

    def follow_selected_changed(self):
        f = self.selected_follow()
        if f:
            self.v_fch.set(CH_FILTER[f["channel"]])
            self.v_fday.set(PER_DAY[f["per_day"]])

    def follow_options_changed(self):
        f = self.selected_follow()
        if not f:
            return
        f["channel"] = next(k for k, v in CH_FILTER.items() if v == self.v_fch.get())
        f["per_day"] = next(k for k, v in PER_DAY.items() if v == self.v_fday.get())
        keep = norm(f["title"])
        self.save()
        self.fill_follows()
        self.ftree.selection_set(keep)

    # ---------- planner
    def reconcile_jobs(self):
        """TVMi often moves times during the day. A recording that has started keeps its listing even if the new schedule
        moved it: it is matched to the moved listing (same channel and show, overlapping times), and its end moves later
        if the show now ends later. Without this the end was cut off, or "Every airing" recorded the show twice."""
        post = dt.timedelta(minutes=self.num(self.v_post, 10, 0, 120))
        keys = {sched_key(it) for it in self.sched}
        for j in self.jobs:
            if j.state == "done" or not j.key or j.key in keys or not j.end_at:
                continue
            try:
                ch, start_iso, tn = j.key.split("|", 2)
                ch, old_start = int(ch), dt.datetime.fromisoformat(start_iso)
            except ValueError:
                continue
            cands = [it for it in self.sched if it["ch"] == ch and norm(it["title"]) == tn
                     and sched_key(it) not in self.hist and it["start"] < j.end_at and it["end"] > old_start]
            if not cands:
                continue
            it = min(cands, key=lambda x: abs((x["start"] - old_start).total_seconds()))
            new = sched_key(it)
            if j.key in self.hist:
                self.hist[new] = self.hist.pop(j.key)
                self.hist[new]["date"] = it["start"].date().isoformat()
            j.key = new
            j.info = {"scheduled": f"{it['start']:%H:%M} – {it['end']:%H:%M}" + (" (length guessed)" if it.get("guess") else "")}
            if it["end"] + post > j.end_at:              # never earlier: a few spare minutes are cheaper than a cut-off ending
                j.end_at = it["end"] + post
            self.log(f"{j.label}: the schedule moved it to {it['start']:%H:%M} – {it['end']:%H:%M}; "
                     f"recording until {j.end_at:%H:%M}")
            self.save()

    def plan(self, nowdt):
        """Rows for followed shows: (item, key, status, follow), plus the pre/post margins."""
        pre = dt.timedelta(minutes=self.num(self.v_pre, 3, 0, 60))
        post = dt.timedelta(minutes=self.num(self.v_post, 10, 0, 120))
        fl = {norm(f["title"]): f for f in self.follows}
        all_on, all_day, skip_news = self.v_all.get(), self.day_key(self.v_all_day.get()), self.v_all_news.get()
        active = {j.key for j in self.jobs if j.state != "done" and j.key}
        chosen = {(h["title_n"], h["date"]) for h in self.hist.values() if not h.get("skip")}   # a failed airing doesn't block the repeat
        rows = []
        for it in self.sched:
            f = fl.get(norm(it["title"]))                    # a show you follow keeps its own settings
            if not f and all_on and it["ch"] in ALL_SHOWS_CHANNELS and not (skip_news and NEWS_RE.search(it["title"])):
                f = {"title": it["title"], "channel": 0, "per_day": all_day}     # "Record ALL shows"
            if not f or (f["channel"] and f["channel"] != it["ch"]):
                continue
            if it["end"] + post < nowdt - dt.timedelta(hours=2):
                continue
            tn, date = norm(it["title"]), it["start"].date().isoformat()
            key = sched_key(it)
            h = self.hist.get(key)
            over = it["end"] + post <= nowdt
            if h:
                status = (SKIP_LABEL.get(h["skip"], "skipped (error)") if h.get("skip") else "recorded") if h["done"] \
                    else ("recording" if key in active else ("interrupted" if over else "resume"))
            elif f["per_day"] == "first" and (tn, date) in chosen:
                status = "repeat (skipped)"
            elif over:
                status = "missed"
            elif it["start"] - pre <= nowdt:
                status = "start"
            else:
                status = "waiting"
            if status in ("waiting", "start"):
                chosen.add((tn, date))
            rows.append((it, key, status, f))
        return rows, pre, post

    def run_planner(self, nowdt):
        self.reconcile_jobs()
        rows, pre, post = self.plan(nowdt)
        auto_on, can, started = self.v_auto.get(), self.can_record(), 0
        keep = {key for _, key, _, _ in rows}
        stale = [iid for iid in self.utree.get_children() if iid not in keep]
        if stale:
            self.utree.delete(*stale)
        for n, (it, key, status, f) in enumerate(rows):
            w0, w1 = it["start"] - pre, it["end"] + post
            label = {"start": "starting…", "resume": "resuming…"}.get(status, status)
            if not auto_on and status in ("start", "resume", "waiting"):
                label = "NOT recording: automatic recording is off"
            elif not can and status in ("start", "resume", "waiting"):
                label = "NOT recording: ffmpeg is missing"
            vals = (f"{w0:%a %d %b %H:%M} – {w1:%H:%M}" + (" (length guessed)" if it.get("guess") else ""),
                    CHANNELS[it["ch"]], label)
            if self.utree.exists(key):                   # update in place, so the list doesn't jump while you read it
                self.utree.item(key, text=it["title"], values=vals)
                self.utree.move(key, "", n)
            else:
                self.utree.insert("", n, iid=key, text=it["title"], values=vals)
            if status in ("start", "resume") and auto_on and can:
                try:
                    self.start_auto(it, key, post)
                    started += 1
                except Exception as e:                       # one bad airing must not stop the others (or the scheduler)
                    dlog(f"couldn't start {it['title']}:\n" + traceback.format_exc(), logging.ERROR)
                    self.hist.setdefault(key, {"title_n": norm(it["title"]), "date": it["start"].date().isoformat(), "done": True})
                    self.hist[key].update(done=True, skip="error")
                    self.save()
                    self.log(f"{it['title']}: couldn't start ({type(e).__name__}: {e})")
                    self.notify_problem(it, f"Couldn't start the recording: {type(e).__name__}: {e}")
        if started:
            self.last_plan = 0                               # redraw at once so "starting…" turns into "recording"

    def start_auto(self, it, key, post):
        title = it["title"]
        outdir = os.path.join(os.path.expanduser(self.v_out.get()), safe_name(title))
        base = f'{safe_name(title)} - {it["start"]:%Y-%m-%d %H%M}'
        self.hist[key] = {"title_n": norm(title), "date": it["start"].date().isoformat(), "done": False}
        self.save()
        dlog(f"auto-recording {title}: channel {it['ch']}, scheduled {it['start']:%a %d %b %H:%M}-{it['end']:%H:%M}"
             f"{' (length guessed)' if it.get('guess') else ''}, until {(it['end'] + post):%H:%M}")
        try:
            job = Job(self, it["ch"], outdir, base, title, end_at=it["end"] + post, key=key)
            job.info = {"scheduled": f"{it['start']:%H:%M} – {it['end']:%H:%M}" + (" (length guessed)" if it.get("guess") else "")}
            self.jobs.append(job)
        except LowSpace as e:
            self.hist[key].update(done=True, skip="low disk space")
            self.save()
            self.log(f"{title}: NOT recorded. {e}")
            self.notify_problem(it, str(e))
        except OSError as e:
            self.hist[key].update(done=True, skip="error")
            self.save()
            self.log(f"{title}: can't use folder {outdir} ({e})")
            self.notify_problem(it, f"Can't use the folder {outdir}: {e}")

    def finish_task(self, job, user):
        """What to do once ffmpeg has exited for good: convert to .mkv (if wanted), then tell Discord.
        Returns a function to run in the background, or None for test recordings."""
        if job.test:
            return lambda: self.check_test(job)        # once the last chunks are in, so the length is right
        convert = self.v_mkv.get() and bool(self.ffmpeg) and not self.fast_quit
        delete, ffmpeg, ffprobe = self.v_del.get(), self.ffmpeg, find_ffprobe(self.ffmpeg)
        label = job.label
        facts = {"label": label, "channel": CHANNELS.get(job.ch, str(job.ch)), "recorded": job.elapsed,
                 "drops": job.drops.summary(), "outdir": job.outdir, "problem": job.problem,
                 "errors": list(job.errors), "info": dict(job.info), "user": user, "mkv": convert,
                 "base": job.base, "engine": job.engine, "first_start": job.first_start, "end_wall": job.end_wall}
        facts_lag = lambda: job.max_lag
        say = lambda msg: self.q.put(lambda: self.log(msg))
        kw = {"creationflags": 0x08000000} if IS_WIN else {}

        def run():
            # Take the file list NOW, not when the recording was stopped: at a scheduled end the built-in recorder keeps
            # finishing chunks for up to a minute and can start a new part in that time (this left a part2.ts unconverted
            # and out of the report on 2026-10-02).
            files = list(job.files)
            made, failed = {}, False
            if convert:
                for f in files:
                    try:
                        if not os.path.isfile(f) or os.path.getsize(f) < 20000:
                            continue
                        stem = os.path.splitext(f)[0]
                        out, n = stem + ".mkv", 2
                        while os.path.exists(out):
                            out, n = f"{stem} ({n}).mkv", n + 1
                        tmp = out + ".partial"
                        if self.fast_quit:
                            break
                        p = subprocess.Popen([ffmpeg, "-loglevel", "error", "-y", "-i", f, "-map", "0:v?", "-map", "0:a?",
                                              "-c", "copy", "-f", "matroska", tmp],
                                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
                        self.converting.add(p)
                        try:
                            p.wait()
                        finally:
                            self.converting.discard(p)
                        if p.returncode == 0 and conversion_ok(ffprobe, f, tmp):
                            os.replace(tmp, out)
                            made[f] = out
                            if delete:
                                os.remove(f)
                            say(f"{label}: converted to {os.path.basename(out)}" + ("" if delete else " (.ts kept)"))
                        else:
                            failed = True
                            if os.path.exists(tmp):
                                os.remove(tmp)
                            say(f"{label}: .mkv conversion of {os.path.basename(f)} failed, the .ts was kept")
                    except OSError as e:
                        failed = True
                        say(f"{label}: conversion problem ({e}); the .ts was kept")
            size = count = 0
            for f in files:
                path = next((c for c in (f, made.get(f)) if c and os.path.exists(c)), None)
                if path and os.path.getsize(path) > 20000:
                    size += os.path.getsize(path)
                    count += 1
            facts["converted"] = "failed" if failed else ("yes" if made else "no")
            # the recorder thread has finished by now, so its gap / incident lists are complete
            facts["incidents"], facts["gaps"], facts["part_stats"] = list(job.incidents), list(job.gaps), dict(job.part_stats)
            facts["more_incidents"] = job.more_incidents
            facts["files"] = files
            facts["max_lag"] = facts_lag()
            facts["report"] = self.write_report(facts, made)
            if user != "quit":                       # closing the app: the "stopped" message covers it
                self.notify_finished(facts, size, count)
        return run

    def write_report(self, f, made):
        """A plain-text report next to the recordings whenever there were connection problems, even if nothing was lost.
        Returns the file's path, or "" if nothing needed reporting."""
        incidents, gaps = f["incidents"], f["gaps"]
        if not incidents and not gaps:
            return ""
        clock = lambda ts: dt.datetime.fromtimestamp(ts, TZ).strftime("%H:%M:%S")
        day = lambda ts: dt.datetime.fromtimestamp(ts, TZ).strftime("%a %d %b %Y %H:%M:%S")
        name = lambda path: os.path.basename(made.get(path) or path) if path else "(none)"
        L = ["TVMi Recorder - connection report", "=" * 60,
             f"Show:       {f['label']}", f"Channel:    {f['channel']}"]
        if f["info"]:
            L.append(f"Scheduled:  {f['info']['scheduled']}")
        L += [f"Recorded:   {day(f['first_start'])}  to  {day(f['end_wall'])}",
              f"Method:     {'ffmpeg' if f['engine'] == 'ffmpeg' else 'built-in recorder'}", ""]
        lost = sum(g["secs"] for g in gaps)
        if gaps:
            L.append(f"RESULT: VIDEO IS MISSING. {len(gaps)} gap(s), about {fmt_exact(lost)} in total. "
                     f"The recording was split into separate files at each gap.")
        elif f["engine"] == "ffmpeg":
            L.append("RESULT: the connection dropped and ffmpeg was restarted each time. That starts a new file with some overlap; "
                     "how much video (if any) was lost is not measured by this method.")
        else:
            L.append("RESULT: NOTHING IS MISSING. The connection had problems, but everything that was missed was fetched "
                     "afterwards (TVMi keeps about 18 minutes of video), so the video is complete.")
        if f.get("max_lag", 0) >= 30:
            L.append(f"At its worst the recorder was {fmt_exact(f['max_lag'])} behind live (after a stall or slow downloads) and caught up again.")
        L += ["", "FILES"]
        for n, path in enumerate([x for x in f["files"] if os.path.exists(made.get(x) or x) or os.path.exists(x)], 1):
            shown = made.get(path) if made.get(path) and os.path.exists(made[path]) else path
            size = os.path.getsize(shown) / 1e6 if os.path.exists(shown) else 0
            secs = f["part_stats"].get(path, {}).get("secs")
            L.append(f"  {n}. {os.path.basename(shown)}  -  " + (f"{fmt_exact(secs)} of video, " if secs else "") + f"{size:.0f} MB")
        if gaps:
            L += ["", "GAPS (video that could not be recovered)"]
            for n, g in enumerate(gaps, 1):
                if g.get("est"):
                    L.append(f"  Gap {n}: roughly {fmt_exact(g['secs'])} may be missing. This is an ESTIMATE from the clock: the stream's chunk "
                             f"numbering jumped (the stream or a server restarted), so the exact chunks can't be identified. "
                             f"Nothing may actually be missing.")
                else:
                    L.append(f"  Gap {n}: about {fmt_exact(g['secs'])} is missing ({g['segments']} segment(s) of the stream, "
                             f"numbered {g['first']} to {g['last']}).")
                if g["tail"]:
                    L.append(f"     Where: at the very end. The recording stopped while the connection was still down, so the last "
                             f"part of the programme was never captured. (after {fmt_exact(g['secs_into_file'])} of video in "
                             f"{name(g['file_before'])})")
                else:
                    L.append(f"     Where: between the end of {name(g['file_before'])} (after {fmt_exact(g['secs_into_file'])} of "
                             f"video) and the start of {name(g['file_after'])}.")
                L.append(f"     When:  around {clock(g['est_start'])} to {clock(g['est_end'])} on the clock (give or take about 15 s).")
        L += ["", "CONNECTION PROBLEMS"]
        for n, i in enumerate(incidents, 1):
            missed = "" if i["missed"] is None else (" Nothing was lost." if i["missed"] < 1 else f" About {fmt_exact(i['missed'])} was lost.")
            L.append(f"  {n}. {clock(i['start'])} to {clock(i['end'])} (stalled for {fmt_exact(i['stall'])}).{missed}")
            if i["errors"]:
                L.append(f"     Error: {' | '.join(i['errors'][-2:])}")
                meaning = explain_error(i["errors"])
                if meaning:
                    L.append(f"     Meaning: {meaning}.")
            if i["switched"]:
                L.append("     It carried on using a different TVMi server.")
        if f.get("more_incidents"):
            L.append(f"  ...and {f['more_incidents']} more, not listed.")
        base = os.path.join(f["outdir"], f"{f['base']} - report")
        path, k = base + ".txt", 2
        while os.path.exists(path):
            path, k = f"{base} ({k}).txt", k + 1
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(L) + "\n")
        except OSError as e:
            dlog(f"couldn't write the report: {e}", logging.WARNING)
            return ""
        self.q.put(lambda: self.log(f"{f['label']}: wrote {os.path.basename(path)}"))
        return path

    # ---------- Discord messages (all safe to call from any thread)
    def notify_started(self, job):
        info = job.info
        if info:
            fields = [("Channel", CHANNELS.get(job.ch, job.ch)), ("Scheduled", info["scheduled"]),
                      ("Recording until", f"{job.end_at:%H:%M}")]
        else:
            fields = [("Channel", CHANNELS.get(job.ch, job.ch)),
                      ("Stops", f"after {fmt_len(job.limit)}" if job.limit else "when you stop it")]
        self.notifier.send("start", "🔴 Recording started", BLUE, f"**{job.label}**", fields)

    def notify_finished(self, f, size, count):
        show = f"**{f['label']}**"
        if count == 0 or size < 200_000:
            why = f["problem"] or "No video was received."
            if f["errors"]:
                why += ("\nLast ffmpeg message: " if f["engine"] == "ffmpeg" else "\nLast error: ") + f["errors"][-1]
            self.notifier.send("fail", "❌ Not recorded", RED, f"{show}\n\n{why}")
            return
        lines = [show, ""]
        drops = f["drops"]
        if drops["count"]:
            worst = drops["worst"]
            backup = ", once on a different server" if drops["backup"] else ""
            if drops["measured"]:
                lost = drops["missed"]
                result = "nothing missed" if lost < 1 else f"about {fmt_len(lost)} of video missed"
                lines.append(f"Drops: {drops['count']} (longest stall {worst:.0f} s, {result}{backup})")
            else:
                lines.append(f"Drops: {drops['count']} (longest gap {worst:.0f} s{backup})")
        else:
            lines.append("Drops: none")
        gaps = f.get("gaps", [])
        if gaps:
            lines.append(f"⚠️ **Missing video:** about {fmt_len(sum(g['secs'] for g in gaps))} in {len(gaps)} gap(s). "
                         f"The recording was split at each gap.")
        lines.append(f"Saved to: `{f['outdir']}`")
        if f.get("report"):
            lines.append(f"Report: `{os.path.basename(f['report'])}`")
        if f["mkv"]:
            lines.append({"yes": "Converted to .mkv: yes", "failed": "Converted to .mkv: **failed** (the .ts files were kept)",
                          "no": "Converted to .mkv: no"}[f["converted"]])
        fields = [("Length", fmt_len(f["recorded"])), ("Size", fmt_size(size)), ("Files", count)]
        if f["problem"]:
            lines.insert(1, f"⚠️ Stopped early: {f['problem']}")
            self.notifier.send("fail", "❌ Recording stopped early", RED, "\n".join(lines), fields)
        elif f["user"] is True:
            self.notifier.send("finish", "⏹ Recording stopped", GREY, "\n".join(lines), fields)
        elif gaps:
            self.notifier.send("finish", "⚠️ Recording finished with gaps", AMBER, "\n".join(lines), fields)
        else:
            self.notifier.send("finish", "✅ Recording finished", GREEN, "\n".join(lines), fields)

    def notify_recovered(self, job, stall, missed):
        self.notifier.send("drop_each", "✅ Stream recovered", GREEN,
                           f"**{job.label}**\nBack after {fmt_len(stall)}. "
                           + (f"About {fmt_len(missed)} of video was missed." if missed >= 1 else "Nothing was missed."))

    def notify_drop(self, job):
        if job.test:
            return
        now_t = time.time()
        job.drop_times.append(now_t)
        why = " | ".join(list(job.errors)[-2:]) or "no message from ffmpeg"
        if now_t - job.last_each >= 30:                       # one message per half minute at most
            job.last_each = now_t
            self.notifier.send("drop_each", "⚠️ Stream dropped", AMBER,
                               f"**{job.label}**\n"
                               + ("Retrying. TVMi keeps about 18 minutes of video, so nothing is lost if it recovers within that."
                                  if getattr(job, "engine", "") == "built-in" else "Reconnecting straight away.")
                               + f"\nReason: {why}"
                               + (f"\nMeaning: {explain_error(job.errors)}." if explain_error(job.errors) else ""))
        recent = [t for t in job.drop_times if now_t - t <= 900]
        if len(recent) >= 3 and now_t - job.alerted_at >= 1800:
            job.alerted_at = now_t
            mins = max(1, round((now_t - recent[0]) / 60))
            self.notifier.send("drops", "⚠️ Stream keeps dropping", AMBER,
                               f"**{job.label}**\n\n{len(recent)} drops in the last {mins} minute{'s' if mins != 1 else ''}. "
                               f"Still recording.\nLast error: {why}")

    def notify_problem(self, it, reason):
        self.notifier.send("fail", "❌ Not recorded", RED,
                           f"**{it['title']}** · {CHANNELS.get(it['ch'], it['ch'])} · scheduled {it['start']:%H:%M}\n\n{reason}")

    def use_ffmpeg(self):
        return self.v_ffmpeg_engine.get() and bool(self.ffmpeg)

    def can_record(self):
        return bool(self.ffmpeg) or not self.v_ffmpeg_engine.get()

    def refresh_notifier(self):
        self.notifier.configure(self.v_dc_url.get(), [k for k, v in self.v_dc.items() if v.get()])

    def test_discord(self):
        url = self.v_dc_url.get().strip()
        if not DISCORD_URL.match(url):
            self.v_dc_status.set("That doesn't look like a Discord webhook URL (https://discord.com/api/webhooks/…).")
            return
        self.save()
        self.v_dc_status.set("Sending…")

        def work():
            ok, msg = deliver(url, make_embed("✅ Test message", GREEN, "Discord notifications are working."))
            self.q.put(lambda: self.v_dc_status.set("Sent. Check your Discord channel." if ok else f"Failed: {msg}"))
        threading.Thread(target=work, daemon=True).start()

    def job_finished(self, job, user):
        if job.key and job.key in self.hist and user != "quit":     # quitting mid-show: resume next launch
            self.hist[job.key]["done"] = True
            if job.problem:                                         # low disk, ffmpeg wouldn't start: not "recorded"
                self.hist[job.key]["skip"] = job.problem_kind or "stopped early"
            self.save()
        if job.test:
            return
        where = f"; saved in {job.outdir}" if job.part else ""
        self.log(f"{job.label}: {'finished' if user is False else 'stopped'}{where}")

    # ---------- manual recording
    def browse(self):
        d = filedialog.askdirectory(initialdir=self.v_out.get() or HOME)
        if d:
            self.v_out.set(d)
            self.save()

    def begin_manual(self, ch, limit, name):
        """Start a manual recording. Returns (True, message) or (False, why not). Used by the window and the web page."""
        if self.closing:
            return False, "The app is closing."
        if not self.can_record():
            return False, "ffmpeg is not installed."
        if ch not in CHANNELS:
            return False, "Unknown channel."
        out = os.path.expanduser(self.v_out.get().strip() or ".")
        name = (name or "").strip()
        base = safe_name(name) if name else f"tvmi-live{ch}-{now():%Y%m%d-%H%M%S}"
        try:
            self.jobs.append(Job(self, ch, out, base, f"Manual: {CHANNELS[ch]}", limit=limit))
        except LowSpace as e:
            return False, str(e)
        except OSError as e:
            return False, f"Can't use that folder: {e}"
        self.save()
        return True, f"Recording {CHANNELS[ch]} started."

    def begin_test(self, ch):
        """Record 1 minute of a channel into <save folder>/Test, then check the file. Returns (ok, message)."""
        if self.closing:
            return False, "The app is closing."
        if not self.can_record():
            return False, "ffmpeg is not installed."
        if ch not in CHANNELS:
            return False, "Unknown channel."
        out = os.path.join(os.path.expanduser(self.v_out.get().strip() or "."), "Test")
        try:
            self.jobs.append(Job(self, ch, out, f"test-{CHANNELS[ch]}-{now():%H%M%S}",
                                 f"Test: {CHANNELS[ch]}", limit=60, test=True))
        except LowSpace as e:
            return False, str(e)
        except OSError as e:
            return False, f"Can't use that folder: {e}"
        self.log("Test started: recording 1 minute...")
        return True, "Test started: recording 1 minute…"

    def start_manual(self):
        ok, msg = self.begin_manual(self.channel_num(self.v_ch.get()),
                                    self.num(self.v_h, 0) * 3600 + self.num(self.v_m, 0) * 60, self.v_name.get())
        if not ok:
            messagebox.showerror("TVMi Recorder", msg)

    def start_test(self):
        ok, msg = self.begin_test(self.channel_num(self.v_ch.get()))
        if not ok:
            messagebox.showerror("TVMi Recorder", msg)

    def check_test(self, job):
        """Report whether the test produced a playable file. Runs in the background once the recorder has finished."""
        msg = "Test FAILED: nothing was recorded. Check your connection, ffmpeg and the channel."
        try:
            if job.part and os.path.exists(job.file) and os.path.getsize(job.file) > 20000:
                size = os.path.getsize(job.file) / 1e6
                secs = media_duration(find_ffprobe(self.ffmpeg), job.file, timeout=30)
                length = f", {secs:.0f} s of video" if secs else ""
                msg = f"Test PASSED: {size:.1f} MB{length} saved as {job.file}"
        except OSError as e:
            msg = f"Test FAILED: couldn't check the file ({e})."
        self.q.put(lambda: self.log(msg))

    def selected_job(self):
        """The row you clicked; if none is selected and only one recording is running, that one."""
        sel = self.jtree.selection()
        job = next((j for j in self.jobs if sel and str(id(j)) == sel[0]), None)
        if job is None:
            live = [j for j in self.jobs if j.state != "done"]
            if len(live) == 1:
                return live[0]
            messagebox.showinfo("TVMi Recorder", "Click the recording you mean in the list first."
                                if live else "Nothing is recording right now.")
        return job

    def pause_selected(self):
        j = self.selected_job()
        if j:
            dlog(f"[{j.label}] Pause/Resume pressed (state was {j.state})")
            j.pause_resume()

    def stop_selected(self):
        j = self.selected_job()
        if j:
            dlog(f"[{j.label}] Stop pressed (state was {j.state})")
            j.stop(user=True)

    def export_dict(self):
        """Settings and followed shows (no history, no web password) in a form that works on any OS."""
        self.save()
        d = load_json(SETTINGS_FILE, {})
        st = {k: v for k, v in d.get("s", {}).items() if k not in WEB_KEYS}
        if "out" in st:
            st["out"] = portable_path(st["out"])
        return {"app": "tvmi-recorder", "s": st, "follows": d.get("follows", [])}

    def export_settings(self):
        path = filedialog.asksaveasfilename(
            title="Export settings", defaultextension=".json", initialfile="tvmi-rc-settings.json",
            filetypes=[("Settings file", "*.json")])
        if not path:
            return
        try:
            save_json(path, self.export_dict())
        except OSError as e:
            return messagebox.showerror("TVMi Recorder", f"Couldn't write the file:\n{e}")
        self.log(f"Exported settings and {len(self.follows)} followed show(s) to {path}")

    def apply_import(self, d):
        """Load an exported settings dict. Returns (ok, message, note)."""
        if not isinstance(d, dict) or d.get("app") != "tvmi-recorder":
            return False, "That isn't a TVMi Recorder settings file.", ""
        s = dict(DEFAULTS)
        s.update({k: v for k, v in d.get("s", {}).items() if k not in WEB_KEYS})
        try:
            out, changed = resolve_path(str(s["out"]), self.v_out.get())
            pre, post = str(int(s["pre"])), str(int(s["post"]))
            chan, h, m = int(s["channel"]), str(int(s["h"])), str(int(s["m"]))
            follows = clean_follows(d.get("follows", []), set(CH_FILTER))
        except (ValueError, TypeError, KeyError):
            return False, "The settings in that file are malformed.", ""
        note = ""
        if changed:
            note = (f"The save folder in that file ({s['out']}) doesn't exist on this computer, "
                    f"so the current one is kept: {out}")
        self.v_out.set(out)
        self.v_pre.set(pre)
        self.v_post.set(post)
        self.v_auto.set(bool(s["auto"]))
        self.v_login.set(bool(s["login"]))
        self.v_mkv.set(bool(s["mkv"]))
        self.v_del.set(bool(s["del_ts"]))
        self.v_ch.set(self.ch_label(chan))
        self.v_h.set(h)
        self.v_m.set(m)
        self.v_name.set(str(s["name"]))
        for k, v in self.v_dc.items():
            v.set(bool(s.get("dc_" + k, True)))
        self.v_ffmpeg_engine.set(s.get("engine") == "ffmpeg")
        self.v_all.set(bool(s.get("all", False)))
        self.v_all_day.set(PER_DAY.get(s.get("all_per_day"), PER_DAY["first"]))
        self.v_all_news.set(bool(s.get("all_skip_news", True)))
        self.follows = follows
        set_autostart(self.v_login.get())
        self.fill_follows()
        self.save()
        msg = f"Imported settings and {len(follows)} followed show(s)."
        self.log(msg)
        return True, msg, note

    def import_settings(self):
        path = filedialog.askopenfilename(title="Import settings", filetypes=[("Settings file", "*.json")])
        if not path:
            return
        d = load_json(path, None)
        if not isinstance(d, dict) or d.get("app") != "tvmi-recorder":
            return messagebox.showerror("TVMi Recorder", "That isn't a TVMi Recorder settings file.")
        if not messagebox.askyesno("TVMi Recorder", "Replace your current settings and followed shows with the ones in this file?"):
            return
        ok, msg, note = self.apply_import(d)
        if not ok:
            messagebox.showerror("TVMi Recorder", msg)
        elif note:
            messagebox.showinfo("TVMi Recorder", note + "\n\nChange it in Settings if you like.")

    # ---------- web control
    def drain_queue(self):
        """Run work handed over by background threads / web requests; one failing item must not block the rest."""
        while not self.q.empty():
            try:
                self.q.get()()
            except Exception:
                dlog("error in queued work:\n" + traceback.format_exc(), logging.ERROR)

    def pump(self):
        try:
            self.drain_queue()
            if self.term and not self.closing:
                dlog("asked to quit by the system (SIGTERM / SIGHUP)")
                self.close(confirm=False, fast=True)
        finally:
            self.root.after(100, self.pump)

    def show_web_text(self, text):
        self.web_text.config(state="normal")
        self.web_text.delete("1.0", "end")
        self.web_text.insert("1.0", text)
        self.web_text.config(state="disabled")

    def start_web(self):
        if not self.v_web.get():
            self.web.stop()
            self.show_web_text("The web page is off.")
            return
        port = self.num(self.v_webport, 9001, 1024, 65535)
        lan = self.v_weblan.get()
        made = ""
        if lan and not self.v_webpw.get().strip():         # other devices always need a password
            made = new_password()
            self.v_webpw.set(made)
            self.save()
        err = self.web.start(port, lan, self.v_webpw.get())
        if err:
            self.log(err)
            self.show_web_text(err)
            return
        if not lan:
            self.show_web_text(f"http://127.0.0.1:{port}   (this computer only)")
            self.log(f"Web page running on http://127.0.0.1:{port} (this computer only)")
            return
        self.show_web_text(f"Starting on port {port}…")

        def work():
            addrs = my_addresses()
            lines = [f"http://{ip}:{port}   ({label})" for ip, label in sorted(addrs.items(), key=lambda kv: kv[1])]
            lines = lines or [f"http://127.0.0.1:{port}   (no network address found)"]

            def show():
                self.show_web_text("\n".join(lines + ([f"Password (made for you, change it below): {made}"] if made else [])))
                self.log("Web page running: " + "  ".join(l.split()[0] for l in lines))
            self.q.put(show)
        threading.Thread(target=work, daemon=True).start()

    def apply_web(self):
        self.save()
        self.start_web()

    def apply_settings(self, d):
        """Change settings from the web page (same fields as the Settings tab)."""
        if "out" in d:
            out = str(d["out"]).strip()
            full = os.path.normpath(os.path.expanduser(out))
            try:
                ok = bool(out) and run_bounded(lambda: os.path.isdir(full) or os.path.isdir(os.path.dirname(full)), 5)
            except OSError as e:
                raise WebError(400, f"Can't check that folder: {e}")
            if not ok:
                raise WebError(400, "That folder doesn't exist on the recorder's computer.")
            self.v_out.set(out)
        for key, var, lo, hi in (("pre", self.v_pre, 0, 60), ("post", self.v_post, 0, 120)):
            if key in d:
                var.set(str(max(lo, min(hi, int(d[key])))))
        for key, var in (("auto", self.v_auto), ("mkv", self.v_mkv), ("del_ts", self.v_del)):
            if key in d:
                var.set(bool(d[key]))
        if "login" in d and bool(d["login"]) != self.v_login.get():
            self.v_login.set(bool(d["login"]))
            set_autostart(self.v_login.get())
        if "all" in d:
            self.v_all.set(bool(d["all"]))
            if d["all"] and "auto" not in d and not self.v_auto.get():
                self.v_auto.set(True)             # asking to record everything means automatic recording should be on
        if "all_per_day" in d:
            if d["all_per_day"] not in PER_DAY:
                raise WebError(400, "Unknown option.")
            self.v_all_day.set(PER_DAY[d["all_per_day"]])
        if "all_skip_news" in d:
            self.v_all_news.set(bool(d["all_skip_news"]))
        self.save()
        self.last_plan = 0

    def web_state(self):
        nowdt = now()
        jobs = [{"id": str(id(j)), "label": j.label, "channel": CHANNELS.get(j.ch, str(j.ch)),
                 "state": "reconnecting" if (j.state == "recording" and j.retrying) else j.state,
                 "recorded": int(j.rec_time()), "part": j.part, "size": round(j.size / 1e6, 1),
                 "until": f"{j.end_at:%H:%M}" if j.end_at else (f"stops after {j.limit // 60} min" if j.limit else "until stopped")}
                for j in self.jobs]
        rows, pre, post = self.plan(nowdt)
        upcoming = [{"title": it["title"], "channel": CHANNELS[it["ch"]], "status": {"start": "starting…", "resume": "resuming…"}.get(st, st),
                     "when": f'{it["start"] - pre:%a %d %b %H:%M} – {it["end"] + post:%H:%M}' + (" (length guessed)" if it.get("guess") else "")}
                    for it, key, st, f in rows]
        return {"jobs": jobs, "upcoming": upcoming, "follows": self.follows,
                "shows": sorted({i["title"] for i in self.sched}, key=str.casefold),
                "schedule": {"fetching": self.fetching,
                             "updated": f"{dt.datetime.fromtimestamp(self.sched_time):%a %d %b %H:%M}" if self.sched_time else ""},
                "settings": {"out": self.v_out.get(), "pre": self.num(self.v_pre, 3, 0, 60), "post": self.num(self.v_post, 10, 0, 120),
                             "auto": self.v_auto.get(), "mkv": self.v_mkv.get(), "del_ts": self.v_del.get(),
                             "login": self.v_login.get(), "ffmpeg": self.can_record(), "all": self.v_all.get(),
                             "all_per_day": self.day_key(self.v_all_day.get()), "all_skip_news": self.v_all_news.get()}}

    def web_action(self, action, b):
        """Everything the web page can do. Runs on the window's thread; returns a dict or raises WebError."""
        def text(key, n=200):
            return str(b.get(key, "")).strip()[:n]

        def number(key, lo, hi):
            try:
                return max(lo, min(hi, int(b.get(key, 0) or 0)))
            except (TypeError, ValueError):
                raise WebError(400, f"'{key}' must be a number")

        def job():
            j = next((j for j in self.jobs if str(id(j)) == text("id")), None)
            if not j or j.state == "done":
                raise WebError(404, "That recording has already finished.")
            return j

        def follow():
            f = next((f for f in self.follows if norm(f["title"]) == norm(text("title"))), None)
            if not f:
                raise WebError(404, "You aren't following that show.")
            return f

        if action == "record":
            ch = number("channel", 0, 99)
            self.v_ch.set(self.ch_label(ch))
            self.v_h.set(str(number("h", 0, 99)))
            self.v_m.set(str(number("m", 0, 59)))
            self.v_name.set(text("name", 100))
            ok, msg = self.begin_manual(ch, number("h", 0, 99) * 3600 + number("m", 0, 59) * 60, text("name", 100))
        elif action == "test":
            ok, msg = self.begin_test(number("channel", 0, 99))
        elif action == "pause":
            j = job()
            if j.state not in ("recording", "paused"):
                raise WebError(409, f"Can't pause while it's {j.state}.")
            j.pause_resume()
            ok, msg = True, "Paused." if j.state == "paused" else "Resumed."
        elif action == "stop":
            job().stop(user=True)
            ok, msg = True, "Stopped."
        elif action == "follow":
            if not text("title"):
                raise WebError(400, "Give the show's name.")
            self.add_follow(" ".join(text("title").split()))
            ok, msg = True, f"Following {text('title')}."
        elif action == "unfollow":
            self.follows.remove(follow())
            self.save()
            self.fill_follows()
            ok, msg = True, "Unfollowed."
        elif action == "follow_options":
            f = follow()
            ch, per = number("channel", 0, 99), text("per_day")
            if ch not in CH_FILTER or per not in PER_DAY:
                raise WebError(400, "Unknown option.")
            f["channel"], f["per_day"] = ch, per
            self.save()
            self.fill_follows()
            ok, msg = True, "Saved."
        elif action == "refresh":
            self.refresh_schedule()
            ok, msg = True, "Updating the schedule…"
        elif action == "settings":
            self.apply_settings(b)
            ok, msg = True, "Saved."
        elif action == "import":
            ok, msg, note = self.apply_import(b.get("data"))
            if not ok:
                raise WebError(400, msg)
            msg = (msg + " " + note).strip()
        else:
            raise WebError(404, "Unknown action.")
        if not ok:
            raise WebError(409, msg)
        return {"message": msg}

    def toggle_login(self):
        set_autostart(self.v_login.get())
        self.save()

    # ---------- heartbeat
    def tick(self):
        """The heartbeat (every second). Whatever goes wrong inside, it must always schedule itself again, otherwise
        recordings never end and nothing new ever starts."""
        try:
            self.tick_work()
        except Exception:
            dlog("the scheduler heartbeat hit an error (carrying on):\n" + traceback.format_exc(), logging.ERROR)
            if time.time() - getattr(self, "last_tick_error", 0) > 60:
                self.last_tick_error = time.time()
                self.log("Internal scheduler error (details are in the debug log); carrying on.")
        finally:
            self.root.after(1000, self.tick)

    def tick_work(self):
        self.drain_queue()
        nowdt = now()
        for j in self.jobs:
            try:
                j.tick(nowdt)
            except Exception:
                dlog(f"[{j.label}] error in the recording's own tick:\n" + traceback.format_exc(), logging.ERROR)
        self.jobs = [j for j in self.jobs if j.state != "done" or time.monotonic() - j.done_at < 60]
        self.draw_jobs()
        self.check_raise()
        if self.closing:
            return
        if not self.fetching and time.time() - self.sched_time > 3 * 3600 and time.time() > self.retry_sched:
            self.refresh_schedule()
        if self.sched_time and not self.fetching:
            self.v_sched.set(f"Schedule updated {dt.datetime.fromtimestamp(self.sched_time):%a %d %b %H:%M}")
        if time.time() - self.last_plan > 20:
            self.last_plan = time.time()
            self.run_planner(nowdt)

    def draw_jobs(self):
        for j in self.jobs:
            iid = str(id(j))
            t = int(j.rec_time())
            state = {"connecting": "connecting…", "reconnecting": "reconnecting…", "done": "finished"}.get(j.state, j.state)
            if j.state == "recording" and j.retrying:
                state = "retrying…"
            until = f"{j.end_at:%H:%M}" if j.end_at else (f"{j.limit // 60} min" if j.limit else "Stop button")
            vals = (CHANNELS.get(j.ch, j.ch), state, f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}", until)
            if not self.jtree.exists(iid):
                self.jtree.insert("", "end", iid=iid, text=j.label, values=vals)
                if not self.jtree.selection():
                    self.jtree.selection_set(iid)
            else:
                self.jtree.item(iid, values=vals)
        live = {str(id(j)) for j in self.jobs}
        for iid in self.jtree.get_children():
            if iid not in live:
                self.jtree.delete(iid)

    def close(self, confirm=True, fast=False):
        """Quit. `fast` (logout, shutdown, SIGTERM): no question, no .mkv conversion, and don't wait long."""
        if self.closing:
            if fast:
                self.quit_now()
            return
        if confirm and any(j.state != "done" for j in self.jobs) and not messagebox.askyesno(
                "TVMi Recorder", "Recordings are running. Stop them and quit?"):
            return
        self.closing = True
        if fast:
            self.quit_now()
        running = sum(1 for j in self.jobs if j.state != "done" and not j.test)
        for j in self.jobs:
            j.stop(user="quit")
        self.web.stop()
        self.save()
        self.notifier.send("app", "⚪ TVMi Recorder stopped", GREY,
                           f"{running} recording{'s were' if running != 1 else ' was'} still running and {'were' if running != 1 else 'was'} stopped."
                           if running else "The app was closed. Nothing was recording.")
        self.finish_closing(time.monotonic())

    def quit_now(self):
        """Stop waiting for .mkv conversions: the .ts files are kept."""
        self.fast_quit = True
        for p in list(self.converting):
            try:
                p.kill()
            except OSError:
                pass

    def finish_closing(self, started, box=None):
        """Wait for recordings to finish (and be converted) without freezing the window, then exit."""
        alive = [t for t in self.reapers if t.is_alive()]
        waited = time.monotonic() - started
        if (alive and not (self.fast_quit and waited > 20)) or (self.notifier.q.unfinished_tasks and waited < 8):
            if alive and box is None and waited > 1:
                box = self.closing_box()
            if box is not None:
                box[1].set(f"Finishing {len(alive)} recording(s)" + (", converting to .mkv…" if self.converting else "…")
                           if alive else "Sending notifications…")
            self.root.after(300, lambda: self.finish_closing(started, box))
            return
        INSTANCE.release()
        self.root.destroy()

    def closing_box(self):
        top = tk.Toplevel(self.root)
        top.title("TVMi Recorder")
        top.resizable(False, False)
        top.protocol("WM_DELETE_WINDOW", lambda: None)
        text = tk.StringVar()
        ttk.Label(top, textvariable=text, padding=(16, 14, 16, 4)).pack(anchor="w")
        ttk.Label(top, foreground="gray40", padding=(16, 0, 16, 8), wraplength=360, justify="left",
                  text="The app closes by itself when this is done. Quit now keeps the .ts files unconverted.").pack(anchor="w")
        ttk.Button(top, text="Quit now", command=self.quit_now).pack(pady=(0, 12))
        top.transient(self.root)
        return top, text


INSTANCE = InstanceLock(LOCK_FILE)


def main():
    if not INSTANCE.acquire():
        # Already running (often: started at login, then opened from the menu). Show that copy instead. When this
        # copy was itself started at login, there's nothing to show.
        if "--minimized" not in sys.argv:
            try:
                with open(RAISE_FILE, "w", encoding="utf-8") as f:
                    f.write(str(time.time()))
            except OSError:
                pass
        dlog("another copy is already running; not starting a second one")
        return
    root = tk.Tk()
    App(root)
    if "--minimized" in sys.argv:
        root.iconify()
    root.mainloop()


if __name__ == "__main__":
    main()
