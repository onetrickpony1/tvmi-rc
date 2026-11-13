"""Unit tests for tvmi-rc. No network, no window: run with `python -m unittest discover -s tests`."""
import concurrent.futures
import datetime as dt
import http.client
import importlib.util
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_CFG = tempfile.mkdtemp(prefix="tvmi-rc-test-")
os.environ["XDG_CONFIG_HOME"] = _CFG          # keep the module's config folder and log out of the real one
os.environ["APPDATA"] = _CFG
_spec = importlib.util.spec_from_file_location(
    "tvmi_rc", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tvmi-rc.py"))
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)


class Var:
    """Stands in for a tkinter variable."""

    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def page(rows):
    return "".join(f'<div class="flex-1 -mt-1"><span class="font-light">{t}</span>'
                   f'<p class="line-clamp-1">{title}</p></div>' for t, title in rows)


def at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=M.TZ)


# ------------------------------------------------------------------ schedule
class ScheduleParsing(unittest.TestCase):
    day = dt.date(2026, 11, 13)

    def test_midnight_rollover(self):
        rows = M.parse_schedule_page(page([("22:00", "Late"), ("23:30", "News &amp; More"), ("00:30", "Night")]), self.day)
        self.assertEqual([t for _, t in rows], ["Late", "News & More", "Night"])
        self.assertEqual(rows[2][0], at(self.day + dt.timedelta(1), "00:30"))
        self.assertEqual(rows[0][0], at(self.day, "22:00"))

    def test_lengths_duplicates_and_guess(self):
        d1 = self.day + dt.timedelta(1)
        seq = {}
        for day, rows in ((self.day, [("20:00", "A"), ("21:00", "B"), ("00:30", "C")]),
                          (d1, [("00:30", "C"), ("06:00", "A"), ("07:00", "D")])):
            for start, title in M.parse_schedule_page(page(rows), day):
                seq[start] = title
        items = M.schedule_items({2: seq})
        self.assertEqual(len(items), 5)                      # C listed on both pages, kept once
        by = {(x["title"], x["start"]): x for x in items}
        a = by[("A", at(self.day, "20:00").isoformat())]
        self.assertFalse(a["guess"])
        self.assertEqual(a["end"], at(self.day, "21:00").isoformat())
        c = by[("C", at(d1, "00:30").isoformat())]
        self.assertTrue(c["guess"])                           # 5.5 h to the next listing isn't a show length
        self.assertEqual(c["end"], at(d1, "01:30").isoformat())
        a2 = by[("A", at(d1, "06:00").isoformat())]
        self.assertFalse(a2["guess"])
        d = by[("D", at(d1, "07:00").isoformat())]
        self.assertTrue(d["guess"])                           # nothing after it: 60 min

    def test_one_failed_page_keeps_the_rest(self):
        today = M.now().date()
        bad = f"/schedule/5/{today}"

        def fake_fetch(path, timeout=30):
            if path == bad:
                raise TimeoutError("timed out")
            return page([("20:00", "Show"), ("21:00", "Next")])
        with mock.patch.object(M, "fetch", fake_fetch), mock.patch.object(M.time, "sleep"):
            items, failed = M.fetch_schedule(days_back=0, days_fwd=1)
        self.assertEqual(failed, {(5, today.isoformat())})
        self.assertTrue(any(x["ch"] == 2 for x in items))
        self.assertTrue(any(x["ch"] == 5 for x in items))     # the other day of channel 5 still loaded

    def test_main_channels_failing_fails_the_refresh(self):
        def fake_fetch(path, timeout=30):
            if path.startswith(("/schedule/2/", "/schedule/3/")):
                raise ConnectionResetError("reset")
            return page([("20:00", "Show")])
        with mock.patch.object(M, "fetch", fake_fetch), mock.patch.object(M.time, "sleep"):
            with self.assertRaises(ConnectionResetError):
                M.fetch_schedule(days_back=0, days_fwd=1)

    def test_merge_keeps_old_listings_for_failed_pages(self):
        new = [{"ch": 2, "title": "N", "start": "2026-11-13T20:00:00+01:00", "end": "2026-11-13T21:00:00+01:00"}]
        old = [{"ch": 2, "title": "O", "start": "2026-11-13T22:00:00+01:00", "end": "2026-11-13T23:00:00+01:00"},
               {"ch": 3, "title": "Kept", "start": "2026-11-14T20:00:00+01:00", "end": "2026-11-14T21:00:00+01:00"},
               {"ch": 3, "title": "Dropped", "start": "2026-11-15T20:00:00+01:00", "end": "2026-11-15T21:00:00+01:00"}]
        merged = M.merge_schedule(new, old, {(3, "2026-11-14"), (2, "2026-11-13")})
        self.assertEqual([x["title"] for x in merged], ["N", "Kept"])


# ------------------------------------------------------------------ HLS
class Hls(unittest.TestCase):
    def test_media_playlist(self):
        segs = M.parse_media_playlist("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:100\n#EXTINF:12.0,\nseg_100.ts\n"
                                      "#EXTINF:11.5,\nseg_101.ts\n")
        self.assertEqual(segs, [(100, "seg_100.ts", 12.0), (101, "seg_101.ts", 11.5)])
        with self.assertRaises(RuntimeError):
            M.parse_media_playlist("#EXT-X-KEY:METHOD=AES-128,URI=\"k\"\n#EXTINF:12,\na.ts\n")

    def test_best_variant(self):
        master = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow/index.m3u8\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=3000000\nhigh/index.m3u8\n")
        self.assertEqual(M.best_variant("https://h/x/master.m3u8", master), "https://h/x/high/index.m3u8")
        self.assertEqual(M.best_variant("https://h/x/media.m3u8", "#EXTINF:1,\na.ts"), "https://h/x/media.m3u8")

    def test_seq_pattern(self):
        p = M.seq_pattern(1234, "v/1234/chunk_1234.ts")
        self.assertEqual(p, "v/1234/chunk_{seq}.ts")
        self.assertEqual(M.seq_uri(p, 1200), "v/1234/chunk_1200.ts")
        self.assertIsNone(M.seq_pattern(5, "chunk.ts"))
        self.assertIsNone(M.seq_uri(None, 5))

    def test_jump_takes_only_what_the_clock_says_is_missing(self):
        segs = [(500 + i, f"c_{500 + i}.ts", 12.0) for i in range(5)]
        walked = []

        def walk(oldest, n):
            walked.append((oldest, n))
            return list(range(oldest - n, oldest))
        todo = M.jump_todo(segs, "c_{seq}.ts", 20, walk)
        self.assertEqual([q for q, _, _ in todo], [502, 503, 504])     # 20 s = 2 chunks, plus one
        self.assertEqual(walked, [])
        todo = M.jump_todo(segs, "c_{seq}.ts", 120, walk)
        self.assertEqual(walked, [(500, 6)])                           # 11 chunks: 5 listed, 6 older
        self.assertEqual(todo[0], (494, "c_494.ts", 12.0))
        self.assertEqual(len(todo), 11)
        walked.clear()
        M.jump_todo(segs, "c_{seq}.ts", 99999, walk)
        self.assertEqual(walked, [(500, 90)])                          # TVMi keeps about 90 chunks

    def recorder(self):
        r = M.HlsRecorder.__new__(M.HlsRecorder)
        app = mock.Mock()
        app.q = queue.Queue()
        job = mock.Mock(errors=__import__("collections").deque(maxlen=5), alt=False, app=app, label="t")
        r.job, r.stop_ev, r.lock = job, threading.Event(), threading.RLock()
        r.fails, r.incident_t, r.notified, r.retrying = 0, None, False, False
        r.missed_secs, r.host, r.media, r.renew = 0.0, "h", "https://h/a/media.m3u8", False
        return r

    def test_failures_dont_pull_the_playlist_from_under_the_workers(self):
        r = self.recorder()
        for _ in range(3):
            r.failed(TimeoutError("x"))
        self.assertEqual(r.media, "https://h/a/media.m3u8")
        self.assertTrue(r.renew)
        self.assertTrue(r.job.alt)
        self.assertEqual(r.pick_host([], "https://other/a/m.m3u8"), "other")

    def test_stopped_recorder_leaves_the_job_alone(self):
        r = self.recorder()
        r.stop_ev.set()
        r.failed(TimeoutError("x"))
        self.assertEqual(r.fails, 0)
        self.assertEqual(len(r.job.errors), 0)
        fut = concurrent.futures.Future()
        fut.set_result(b"data")
        r.pool = mock.Mock(submit=mock.Mock(return_value=fut))
        r.pending = {"segments": 1, "secs": 12.0, "first": 1, "last": 1}
        r.close_gap = mock.Mock()
        r.out = mock.Mock()
        with self.assertRaises(M.StopRequested):
            r.download([(2, "c_2.ts", 12.0)], "https://h/a/media.m3u8")
        r.close_gap.assert_not_called()
        r.out.write.assert_not_called()


# ------------------------------------------------------------------ files and paths
class Paths(unittest.TestCase):
    def test_safe_name(self):
        self.assertEqual(M.safe_name('a/b: "c"?'), "a_b_ _c__")
        self.assertEqual(M.safe_name("Aux"), "_Aux")
        self.assertEqual(M.safe_name("con.txt"), "_con.txt")
        self.assertEqual(M.safe_name("Console"), "Console")
        self.assertEqual(M.safe_name("COM1"), "_COM1")
        self.assertEqual(len(M.safe_name("x" * 500)), 100)
        self.assertEqual(M.safe_name(" . "), "recording")

    def test_portable_and_resolve(self):
        home = os.path.expanduser("~")
        self.assertEqual(M.portable_path(os.path.join(home, "Videos", "TV")), "~/Videos/TV")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(M.resolve_path(d, "/fallback"), (os.path.normpath(d), False))
            self.assertEqual(M.resolve_path(os.path.join(d, "new"), "/fallback"), (os.path.join(os.path.normpath(d), "new"), False))
            self.assertEqual(M.resolve_path(os.path.join(d, "no", "such"), "/fallback"), ("/fallback", True))
        foreign = "/home/me/Videos" if M.IS_WIN else r"C:\Users\me\Videos"
        self.assertEqual(M.resolve_path(foreign, "fb"), ("fb", True))
        self.assertEqual(M.resolve_path("", "fb"), ("fb", False))

    def test_part_numbers(self):
        with tempfile.TemporaryDirectory() as d:
            j = M.Job.__new__(M.Job)
            j.outdir, j.base, j.part, j.files, j.plock = d, "Show", 0, [], threading.Lock()
            open(os.path.join(d, "Show.ts"), "w").close()          # left from an earlier run
            got = []
            ts = [threading.Thread(target=lambda: got.append(j.next_part_path())) for _ in range(4)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(sorted(os.path.basename(p) for p in got),
                             ["Show part2.ts", "Show part3.ts", "Show part4.ts", "Show part5.ts"])
            j.files.remove(os.path.join(d, "Show part3.ts"))      # a part that never got going was tidied away
            self.assertEqual(os.path.basename(j.next_part_path()), "Show part3.ts")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg and ffprobe")
class Conversion(unittest.TestCase):
    def test_radio_conversion_passes_and_a_short_one_fails(self):
        with tempfile.TemporaryDirectory() as d:
            ts, mkv, short = (os.path.join(d, n) for n in ("r.ts", "r.mkv", "s.mkv"))
            run = lambda *a: subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *a], check=True)
            run("-f", "lavfi", "-i", "sine=frequency=440:duration=30", "-c:a", "aac", "-b:a", "64k", "-f", "mpegts", ts)
            run("-i", ts, "-map", "0:v?", "-map", "0:a?", "-c", "copy", "-f", "matroska", mkv)
            run("-i", ts, "-t", "10", "-c", "copy", "-f", "matroska", short)
            self.assertLess(os.path.getsize(mkv), 0.9 * os.path.getsize(ts))   # what the old size check rejected
            probe = shutil.which("ffprobe")
            self.assertTrue(M.conversion_ok(probe, ts, mkv))
            self.assertFalse(M.conversion_ok(probe, ts, short))


# ------------------------------------------------------------------ network guards
class Guards(unittest.TestCase):
    def test_client_allowed(self):
        for ip in ("127.0.0.1", "::1", "192.168.1.5", "10.1.2.3", "100.100.1.1", "::ffff:10.0.0.1", "fd7a:115c:a1e0::1",
                   "fe80::1%eth0"):
            self.assertTrue(M.client_allowed(ip), ip)
        for ip in ("8.8.8.8", "2001:4860::8888", "nonsense", ""):
            self.assertFalse(M.client_allowed(ip), ip)

    def test_host_allowed(self):
        for h in ("192.168.1.2:9001", "[::1]:9001", "localhost:9001", "pc", "pc.local", "pc.tail1234.ts.net"):
            self.assertTrue(M.host_allowed(h), h)
        for h in ("evil.com", "evil.com:9001", "rebind.example.org"):
            self.assertFalse(M.host_allowed(h), h)

    def test_login_limiter(self):
        t = [1000.0]
        lim = M.LoginLimiter(clock=lambda: t[0])
        for _ in range(4):
            lim.failed("1.2.3.4")
        self.assertEqual(lim.wait_left("1.2.3.4"), 0)
        lim.failed("1.2.3.4")
        self.assertEqual(lim.wait_left("1.2.3.4"), 30)
        self.assertEqual(lim.wait_left("5.6.7.8"), 0)
        t[0] += 31
        lim.failed("1.2.3.4")
        self.assertEqual(lim.wait_left("1.2.3.4"), 60)
        lim.succeeded("1.2.3.4")
        self.assertEqual(lim.wait_left("1.2.3.4"), 0)

    def test_password(self):
        a, b = M.new_password(), M.new_password()
        self.assertEqual(len(a), 12)
        self.assertNotEqual(a, b)

    def test_defaults_keep_the_page_on_this_computer(self):
        self.assertFalse(M.DEFAULTS["web_lan"])


class WebServerTests(unittest.TestCase):
    def serve(self, password, lan=False):
        app = mock.Mock()
        app.q = queue.Queue()
        ws = M.WebServer(app)
        self.assertEqual(ws.start(0, lan, password, host="127.0.0.1"), "")
        self.addCleanup(ws.stop)
        return ws, ws.httpd.server_address[1]

    def request(self, port, method, path, headers):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.putrequest(method, path)
        for k, v in headers.items():
            c.putheader(k, v)
        c.endheaders()
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_lan_needs_a_password(self):
        self.assertIn("password", M.WebServer(mock.Mock()).start(0, True, "", host="127.0.0.1"))

    def test_negative_content_length(self):
        _, port = self.serve("")
        self.assertEqual(self.request(port, "POST", "/api/stop",
                                      {"Content-Type": "application/json", "Content-Length": "-1"}), 400)

    def test_wrong_passwords_are_slowed_down(self):
        _, port = self.serve("right")
        import base64
        auth = lambda pw: {"Authorization": "Basic " + base64.b64encode(f"u:{pw}".encode()).decode()}
        self.assertEqual(self.request(port, "GET", "/api/log", {}), 401)
        for _ in range(5):
            self.assertEqual(self.request(port, "GET", "/api/log", auth("wrong")), 401)
        self.assertEqual(self.request(port, "GET", "/api/log", auth("right")), 429)
        self.assertEqual(self.request(port, "GET", "/favicon.svg", {}), 200)


class CallUi(unittest.TestCase):
    def test_timed_out_request_never_runs(self):
        app, ran = mock.Mock(), []
        app.q = queue.Queue()
        code, body = M.call_ui(app, lambda: ran.append(1), timeout=0.1)
        self.assertEqual(code, 503)
        self.assertIn("Nothing was done", body["error"])
        app.q.get()()                                   # the window's thread frees up later
        self.assertEqual(ran, [])

    def test_answered_request(self):
        app = mock.Mock()
        app.q = queue.Queue()
        threading.Timer(0.05, lambda: app.q.get()()).start()
        self.assertEqual(M.call_ui(app, lambda: {"message": "ok"}, timeout=2), (200, {"message": "ok"}))

    def test_errors(self):
        app = mock.Mock()
        app.q = queue.Queue()

        def boom():
            raise M.WebError(409, "busy")
        threading.Timer(0.05, lambda: app.q.get()()).start()
        self.assertEqual(M.call_ui(app, boom, timeout=2), (409, {"error": "busy"}))


# ------------------------------------------------------------------ app logic without a window
def fake_app():
    app = M.App.__new__(M.App)
    app.v_pre, app.v_post = Var("3"), Var("10")
    app.v_all, app.v_all_day, app.v_all_news = Var(False), Var(M.PER_DAY["first"]), Var(True)
    app.follows, app.hist, app.jobs, app.sched = [], {}, [], []
    app.log = lambda msg: None
    app.save = lambda: None
    return app


class Follows(unittest.TestCase):
    def test_clean_follows(self):
        got = M.clean_follows([{"title": "Xarabank"}, {"title": "xarabank "}, {"title": "  "}, "junk",
                               {"title": "Kif Inti", "channel": 99, "per_day": "never"},
                               {"title": "Radio", "channel": 6, "per_day": "all"}, {"title": 5}])
        self.assertEqual(got, [{"title": "Xarabank", "channel": 0, "per_day": "first"},
                               {"title": "Kif Inti", "channel": 0, "per_day": "first"},
                               {"title": "Radio", "channel": 6, "per_day": "all"}])
        self.assertEqual(M.clean_follows(None), [])

    def import_into(self, data):
        app = fake_app()
        with tempfile.TemporaryDirectory() as d:
            for name in ("v_out", "v_pre", "v_post", "v_auto", "v_login", "v_mkv", "v_del", "v_ch", "v_h", "v_m",
                         "v_name", "v_ffmpeg_engine"):
                setattr(app, name, Var(d if name == "v_out" else ""))
            app.v_dc = {k: Var(True) for k, _ in M.DC_KINDS}
            app.fill_follows = mock.Mock()
            with mock.patch.object(M, "set_autostart"):
                return app, M.App.apply_import(app, data)

    def test_import_removes_duplicates(self):
        app, (ok, msg, note) = self.import_into(
            {"app": "tvmi-recorder", "s": {}, "follows": [{"title": "Xarabank"}, {"title": "xarabank "}]})
        self.assertTrue(ok)
        self.assertEqual([f["title"] for f in app.follows], ["Xarabank"])

    def test_import_rejects_bad_files(self):
        _, (ok, msg, _) = self.import_into({"app": "something else"})
        self.assertFalse(ok)
        _, (ok, msg, _) = self.import_into({"app": "tvmi-recorder", "s": {"pre": "lots"}})
        self.assertFalse(ok)


class Planner(unittest.TestCase):
    def setUp(self):
        self.app = fake_app()
        self.app.follows = [{"title": "Show", "channel": 0, "per_day": "first"}]
        self.day = dt.date(2026, 11, 13)
        self.first = {"ch": 2, "title": "Show", "start": at(self.day, "20:00"), "end": at(self.day, "21:00"), "guess": False}
        self.repeat = {"ch": 3, "title": "Show", "start": at(self.day, "23:00"), "end": at(self.day, "23:59"), "guess": False}
        self.app.sched = [self.first, self.repeat]

    def statuses(self, when):
        rows, _, _ = self.app.plan(at(self.day, when))
        return [st for _, _, st, _ in rows]

    def test_first_airing_and_repeat(self):
        self.assertEqual(self.statuses("19:00"), ["waiting", "repeat (skipped)"])
        self.assertEqual(self.statuses("19:58"), ["start", "repeat (skipped)"])

    def test_failed_first_airing_lets_the_repeat_record(self):
        self.app.hist[M.sched_key(self.first)] = {"title_n": "show", "date": "2026-11-13", "done": True, "skip": "error"}
        self.assertEqual(self.statuses("19:58"), ["skipped (error)", "waiting"])
        self.app.hist[M.sched_key(self.first)]["skip"] = "stopped early"
        self.assertEqual(self.statuses("19:58"), ["stopped early", "waiting"])

    def test_recorded_resume_interrupted(self):
        key = M.sched_key(self.first)
        self.app.hist[key] = {"title_n": "show", "date": "2026-11-13", "done": True}
        self.assertEqual(self.statuses("21:30"), ["recorded", "repeat (skipped)"])
        self.app.hist[key]["done"] = False
        self.assertEqual(self.statuses("20:30"), ["resume", "repeat (skipped)"])
        self.assertEqual(self.statuses("22:00"), ["interrupted", "repeat (skipped)"])
        self.app.jobs = [mock.Mock(state="recording", key=key)]
        self.assertEqual(self.statuses("20:30"), ["recording", "repeat (skipped)"])

    def test_missed_and_channel_filter(self):
        self.assertEqual(self.statuses("22:30"), ["missed", "waiting"])
        self.app.follows[0]["channel"] = 3
        self.assertEqual(self.statuses("19:00"), ["waiting"])

    def test_record_all_skips_news(self):
        self.app.follows = []
        self.app.v_all.set(True)
        self.app.sched = [self.first, {**self.first, "title": "Aħbarijiet", "start": at(self.day, "21:00")},
                          {**self.first, "ch": 6, "title": "Radio show"}]
        rows, _, _ = self.app.plan(at(self.day, "19:00"))
        self.assertEqual([it["title"] for it, _, _, _ in rows], ["Show"])

    def running_job(self):
        key = M.sched_key(self.first)
        self.app.hist[key] = {"title_n": "show", "date": "2026-11-13", "done": False}
        job = mock.Mock(state="recording", key=key, end_at=self.first["end"] + dt.timedelta(minutes=10), label="Show")
        self.app.jobs = [job]
        return job

    def test_moved_show_keeps_recording_to_the_new_end(self):
        job = self.running_job()
        moved = {**self.first, "start": at(self.day, "20:10"), "end": at(self.day, "21:30")}
        self.app.sched = [moved, self.repeat]
        self.app.reconcile_jobs()
        self.assertEqual(job.key, M.sched_key(moved))
        self.assertEqual(job.end_at, at(self.day, "21:40"))
        self.assertIn(job.key, self.app.hist)
        self.assertEqual(len(self.app.hist), 1)
        self.app.v_all_day.set(M.PER_DAY["all"])
        self.app.follows[0]["per_day"] = "all"
        self.assertEqual(self.statuses("20:30"), ["recording", "waiting"])    # not a second recording

    def test_earlier_end_doesnt_cut_the_recording(self):
        job = self.running_job()
        self.app.sched = [{**self.first, "start": at(self.day, "19:55"), "end": at(self.day, "20:40")}]
        self.app.reconcile_jobs()
        self.assertEqual(job.end_at, at(self.day, "21:10"))

    def test_next_episode_isnt_taken_over(self):
        job = self.running_job()
        nxt = {**self.first, "start": at(self.day, "21:00"), "end": at(self.day, "22:00")}
        self.app.sched = [self.first, nxt]
        self.app.reconcile_jobs()
        self.assertEqual(job.key, M.sched_key(self.first))
        self.assertEqual(job.end_at, at(self.day, "21:10"))


# ------------------------------------------------------------------ odds and ends
class Misc(unittest.TestCase):
    def test_instance_lock(self):
        path = os.path.join(_CFG, "test.lock")
        a, b = M.InstanceLock(path), M.InstanceLock(path)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_drop_stats(self):
        d = M.DropStats()
        for _ in range(5000):
            d.add(3.0, False, 0.0)
        d.add(40.0, True, 12.0)
        self.assertEqual(d.summary(), {"count": 5001, "worst": 40.0, "missed": 12.0, "backup": True, "measured": True})
        d.add(5.0, False)
        self.assertFalse(d.summary()["measured"])

    def test_incidents_are_capped(self):
        j = M.Job.__new__(M.Job)
        j.incidents, j.more_incidents = [], 0
        for n in range(250):
            j.add_incident({"n": n})
        self.assertEqual((len(j.incidents), j.more_incidents), (M.Job.MAX_INCIDENTS, 250 - M.Job.MAX_INCIDENTS))

    def test_one_notifier_worker(self):
        n = M.Notifier()
        n.configure("https://discord.com/api/webhooks/1/abc", ["start"])
        started = []
        real = threading.Thread

        def counting(*a, **kw):
            if kw.get("target") == n.run:
                started.append(1)
            return real(*a, **kw)
        with mock.patch.object(M, "deliver", lambda url, p: (time.sleep(0.01), (True, "sent"))[1]), \
                mock.patch.object(M.threading, "Thread", counting):
            ts = [real(target=lambda: [n.send("start", "t", 0) for _ in range(5)]) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            n.flush(5)                                  # inside the patch: never reach the real Discord
        self.assertEqual(len(started), 1)
        self.assertEqual(n.q.unfinished_tasks, 0)

    def test_run_bounded(self):
        self.assertEqual(M.run_bounded(lambda: 5, 1), 5)
        with self.assertRaises(OSError):
            M.run_bounded(lambda: time.sleep(2), 0.1)
        with self.assertRaises(ValueError):
            M.run_bounded(lambda: int("x"), 1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_child_dies_with_parent_is_ready(self):
        self.assertTrue(callable(M._child_dies_with_parent()))

    @unittest.skipUnless(M.IS_WIN, "Windows only")
    def test_windows_kill_job(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(M._WinKillJob().add(p))
        finally:
            p.kill()


if __name__ == "__main__":
    unittest.main()
