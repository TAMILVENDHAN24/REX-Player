"""Regression tests for app/src/main/python/rex_ytdlp.py.

Run from the repository root (needs: pip install pytest yt-dlp==<pinned> curl-cffi):

    python -m pytest scripts/test_rex_ytdlp.py -v

These run on a desktop Python, not on a device.  They cover the fallback logic,
option building, and the stream/quality/audio-track selection ported from the
REX Ytdlp add-on.  They cannot prove Dailymotion works on a given carrier; that
needs a run of "Test URL Extraction" on the phone.
"""
import json
import os
import socket
import struct
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "src", "main", "python"))
import rex_ytdlp as r  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache():
    r._preferred_target.clear()
    yield
    r._preferred_target.clear()


# ---------------------------------------------------------------- options ---

def test_single_video_disables_playlist_and_sets_default_format():
    o = r._options("{}")
    assert o["noplaylist"] is True
    assert o["format"] == r.DEFAULT_FORMAT
    assert "extract_flat" not in o


def test_playlist_options():
    o = r._options("{}", playlist=True)
    assert o["noplaylist"] is False
    assert o["extract_flat"] == "in_playlist"
    assert o["playlistend"] == r.MAX_PLAYLIST_ENTRIES
    assert "format" not in o


def test_user_options_are_applied():
    o = r._options(json.dumps({
        "format": "best", "user_agent": "UA", "referer": "https://x/",
        "proxy": "socks5://h:1", "cookies_file": "/c.txt", "quickjs_path": "/q/libqjs.so",
        "geo_bypass": False,
    }))
    assert o["format"] == "best"
    assert o["http_headers"] == {"User-Agent": "UA", "Referer": "https://x/"}
    assert o["proxy"] == "socks5://h:1"
    assert o["cookiefile"] == "/c.txt"
    assert o["js_runtimes"] == {"quickjs": {"path": "/q/libqjs.so"}}
    assert o["geo_bypass"] is False


def test_extractor_args_use_yt_dlp_syntax():
    o = r._options(json.dumps({"extractor_args": "youtube:player_client=android,web"}))
    assert o["extractor_args"] == {"youtube": {"player_client": ["android", "web"]}}


def test_invalid_extractor_args_raise_instead_of_being_ignored():
    with pytest.raises(ValueError):
        r._parse_extractor_args("this is not valid")


# --------------------------------------------------------------- fallback ---

class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL; behaviour is scripted per impersonation target."""
    calls = []
    script = {}  # target-or-None -> exception instance or info dict

    def __init__(self, opts):
        imp = opts.get("impersonate")
        self.target = str(imp) if imp is not None else None
        FakeYDL.calls.append(self.target)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        outcome = FakeYDL.script[self.target]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @staticmethod
    def sanitize_info(info):
        return info


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYDL.calls = []
    FakeYDL.script = {}
    monkeypatch.setattr(r.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(r, "_impersonation_handler_available", lambda: True)
    return FakeYDL


RESET = Exception("ERROR: [dailymotion] x: Unable to download JSON metadata: "
                  "[Errno 104] Connection reset by peer")


def test_transport_failure_retries_with_impersonation_and_remembers_it(fake_ydl):
    fake_ydl.script = {None: RESET, "chrome": {"id": "ok"}}
    assert r._extract("https://www.dailymotion.com/video/x1", {}) == {"id": "ok"}
    assert fake_ydl.calls == [None, "chrome"]
    # Next call for the same host starts with the target that worked.
    fake_ydl.calls.clear()
    r._extract("https://www.dailymotion.com/video/x2", {})
    assert fake_ydl.calls == ["chrome"]


def test_plain_success_never_touches_impersonation(fake_ydl):
    fake_ydl.script = {None: {"id": "ok"}}
    r._extract("https://example.com/v", {})
    assert fake_ydl.calls == [None]


def test_non_transport_error_is_not_retried_and_message_is_preserved(fake_ydl):
    boom = Exception("ERROR: [dailymotion] x: Video unavailable")
    fake_ydl.script = {None: boom}
    with pytest.raises(r.ExtractionFailed) as e:
        r._extract("https://www.dailymotion.com/video/x1", {})
    assert fake_ydl.calls == [None]
    assert "Video unavailable" in str(e.value)
    assert "Attempts:" not in str(e.value)


def test_all_attempts_fail_reports_every_attempt_and_engine(fake_ydl):
    fake_ydl.script = {None: RESET, "chrome": RESET, "firefox": RESET}
    with pytest.raises(r.ExtractionFailed) as e:
        r._extract("https://www.dailymotion.com/video/x1", {})
    msg = str(e.value)
    assert "Connection reset by peer" in msg
    assert "default:" in msg and "chrome:" in msg and "firefox:" in msg
    assert "Engine:" in msg and "impersonation_available" in msg


def test_without_curl_cffi_only_one_attempt(fake_ydl, monkeypatch):
    monkeypatch.setattr(r, "_impersonation_handler_available", lambda: False)
    fake_ydl.script = {None: RESET}
    with pytest.raises(r.ExtractionFailed):
        r._extract("https://www.dailymotion.com/video/x1", {})
    assert fake_ydl.calls == [None]


def test_unusable_target_is_skipped_not_fatal(monkeypatch):
    class Picky(FakeYDL):
        def __init__(self, opts):
            if opts.get("impersonate") is not None:
                raise Exception("Impersonate target not available")
            super().__init__(opts)
    FakeYDL.calls, FakeYDL.script = [], {None: RESET}
    monkeypatch.setattr(r.yt_dlp, "YoutubeDL", Picky)
    monkeypatch.setattr(r, "_impersonation_handler_available", lambda: True)
    with pytest.raises(r.ExtractionFailed) as e:
        r._extract("https://www.dailymotion.com/video/x1", {})
    assert "cannot start" in str(e.value)


def test_resolve_returns_failure_json_with_real_error(fake_ydl):
    fake_ydl.script = {None: Exception("ERROR: nope"), "chrome": Exception("ERROR: nope")}
    out = json.loads(r.resolve("https://example.com/v", "{}"))
    assert out["is_success"] is False and "nope" in out["error_message"]


# ---------------------------------------------- real yt-dlp / curl_cffi ---

def test_real_yt_dlp_accepts_impersonation_targets():
    pytest.importorskip("curl_cffi")
    import yt_dlp
    from yt_dlp.networking.impersonate import ImpersonateTarget
    assert r.supports_impersonation() is True
    for client in r._IMPERSONATE_FALLBACKS:
        with yt_dlp.YoutubeDL({"quiet": True, "impersonate": ImpersonateTarget(client)}):
            pass


def test_end_to_end_plain_client_reset_then_impersonated_succeeds():
    """Local server drops clients without browser client-hints (like a CDN
    dropping a non-browser connection) and serves browser-impersonated ones."""
    pytest.importorskip("curl_cffi")
    seen = []

    def serve(sock):
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            conn.settimeout(5)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except Exception:
                pass
            head = data.decode("latin1").lower()
            ok = "sec-ch-ua" in head
            seen.append(ok)
            if not ok:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                conn.close()
                continue
            body = b"\x00" * 2048
            resp = (b"HTTP/1.1 200 OK\r\nContent-Type: video/mp4\r\nContent-Length: %d\r\n"
                    b"Connection: close\r\n\r\n" % len(body))
            conn.sendall(resp + (b"" if head.startswith("head") else body))
            conn.close()

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    threading.Thread(target=serve, args=(srv,), daemon=True).start()
    try:
        url = "http://127.0.0.1:%d/clip.mp4" % srv.getsockname()[1]
        out = json.loads(r.resolve(url, "{}"))
    finally:
        srv.close()
    assert out["is_success"] is True, out
    assert seen[0] is False and seen[-1] is True
    assert r._preferred_target.get("127.0.0.1") == "chrome"


# ------------------------------------------------ stream selection etc. ---

def fmt(fid, **kw):
    d = {"format_id": fid, "url": "https://cdn/%s?t=1" % fid}
    d.update(kw)
    return d


def test_muxed_selection_does_not_attach_a_second_audio_track():
    # Regression: the earlier bundled bridge paired a muxed video with an
    # unrelated audio-only format, producing double audio.
    info = {"title": "T", "url": "https://cdn/muxed.mp4", "formats": [
        fmt("m", vcodec="avc1", acodec="aac", height=720, url="https://cdn/muxed.mp4"),
        fmt("a", vcodec="none", acodec="mp4a", abr=128)]}
    out = r._stream_result(info)
    assert out["is_success"] and out["audio_url"] is None
    assert out["video_url"] == "https://cdn/muxed.mp4"


def test_dash_selection_orders_video_and_audio_by_codec():
    v = fmt("v", vcodec="avc1", acodec="none", height=1080)
    a = fmt("a", vcodec="none", acodec="opus", abr=130)
    for pair in ([v, a], [a, v]):
        out = r._stream_result({"requested_formats": pair, "formats": [v, a]})
        assert out["video_url"] == v["url"] and out["audio_url"] == a["url"]


def test_headers_are_filtered_and_subtitles_limited_to_vtt_srt():
    info = {"url": "https://cdn/x.mp4",
            "http_headers": {"User-Agent": "UA", "Accept-Language": "en", "Referer": "R", "Cookie": " "},
            "subtitles": {"en": [{"ext": "ttml", "url": "u0"}, {"ext": "vtt", "url": "u1"}],
                          "fr": [{"ext": "json3", "url": "u2"}]}}
    out = r._stream_result(info)
    assert out["http_headers"] == {"User-Agent": "UA", "Referer": "R"}
    assert out["subtitles"] == {"en": "u1"}


def test_no_url_gives_clear_failure():
    out = r._stream_result({"title": "Only metadata"})
    assert out["is_success"] is False and "Only metadata" in out["error_message"]


def test_qualities_grouping_labels_codec_choice_and_audio_only():
    audio = fmt("a", vcodec="none", acodec="opus", abr=130)
    formats = [
        fmt("v1", vcodec="vp09.00", acodec="none", height=1080, fps=30, tbr=2000),
        fmt("v2", vcodec="avc1.64", acodec="none", height=1080, fps=30, tbr=1500),
        fmt("v3", vcodec="avc1.64", acodec="none", height=1080, fps=60, tbr=3000),
        fmt("v4", vcodec="vp09.00", acodec="none", height=2160, fps=30, tbr=9000),
        fmt("sb", vcodec="avc1", ext="mhtml", height=90),
        audio,
    ]
    q = r._qualities({"formats": formats}, formats[0]["url"], None)
    labels = [x["label"] for x in q]
    assert labels == ["2160p (4K)", "1080p60", "1080p", "Audio Only"]
    p1080 = next(x for x in q if x["label"] == "1080p")
    assert p1080["id"] == "v1"  # selected video url wins its group
    assert p1080["codec"] == "VP9" and p1080["is_dash"] and p1080["audio_url"] == audio["url"]
    p60 = next(x for x in q if x["label"] == "1080p60")
    assert p60["codec"] == "AVC"
    assert q[-1]["is_audio_only"] and q[-1]["video_url"] == audio["url"]


def test_best_h264_preferred_up_to_1080_when_nothing_selected():
    formats = [fmt("vp", vcodec="vp09", acodec="none", height=720, tbr=900),
               fmt("av", vcodec="avc1", acodec="none", height=720, tbr=500)]
    q = r._qualities({"formats": formats}, "https://other", None)
    assert q[0]["id"] == "av"


def test_audio_tracks_one_per_language_and_primary_language_excluded():
    formats = [
        fmt("en1", vcodec="none", acodec="opus", language="en", abr=50),
        fmt("en2", vcodec="none", acodec="aac", language="en", abr=128),
        fmt("de1", vcodec="none", acodec="opus", language="de", abr=70),
        fmt("de2", vcodec="none", acodec="opus", language="de", abr=130),
        fmt("xx1", vcodec="none", acodec="opus", abr=50),
        fmt("xx2", vcodec="none", acodec="aac", abr=128),
        fmt("vid", vcodec="avc1", acodec="none", height=720),
    ]
    primary = formats[1]["url"]  # en aac 128 is the primary audio
    tracks = r._audio_tracks({"formats": formats}, primary)
    langs = [t["lang"] for t in tracks]
    assert "en" not in langs                       # primary's language dropped
    assert langs.count("de") == 1                  # best de kept
    assert len([t for t in tracks if t["lang"] is None]) == 1  # no-lang collapsed
    assert tracks[0]["bitrate"] >= tracks[-1]["bitrate"]
    de = next(t for t in tracks if t["lang"] == "de")
    assert de["bitrate"] == 130000 and de["label"] == "de • opus • 130kbps"


def test_audio_label_fallbacks():
    assert r._audio_label(None, None, None, 0) == "Audio"
    assert r._audio_label(None, "opus", "251", 0.5) == "opus • opus"
    assert r._audio_label("en", "aac", "140", 0) == "en • aac • 140"


def test_playlist_entries_dedupe_and_fallbacks():
    info = {"title": "PL", "entries": [
        {"id": "abc", "title": "One", "uploader": "U", "duration": 12.7},
        {"id": "abc", "title": "dup"},
        {"url": "https://site/v/2", "title": "Two", "creator": "C"},
        {"title": "no url or id"},
    ]}
    out = r._playlist_result("https://src", info)
    entries = json.loads(out["entries_json"])
    assert [e["url"] for e in entries] == ["https://www.youtube.com/watch?v=abc", "https://site/v/2"]
    assert entries[0]["duration"] == 12 and entries[0]["artist"] == "U"
    assert entries[0]["thumbnail"].endswith("/abc/hqdefault.jpg")
    assert out["title"] == "PL"


def test_playlist_without_entries_fails_clearly():
    assert r._playlist_result("u", {"title": "x"})["is_success"] is False


def test_unreachable_host_returns_real_error_not_empty(monkeypatch):
    """Original symptom guard: a failing extraction must report why."""
    monkeypatch.setattr(r, "_impersonation_handler_available", lambda: False)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens -> connection refused
    out = json.loads(r.resolve("http://127.0.0.1:%d/v.mp4" % port, json.dumps({})))
    assert out["is_success"] is False
    assert out["error_message"].strip()
    assert "empty" not in out["error_message"].lower()


# ------------------------------------------------------- engine updates ---

import hashlib  # noqa: E402
import http.server  # noqa: E402
import zipfile  # noqa: E402


import yt_dlp as _bundled_yt_dlp  # noqa: E402

# Captured at import: after a swap, `import yt_dlp` would point inside a zip.
_BUNDLED_PKG_DIR = os.path.dirname(_bundled_yt_dlp.__file__)


def _make_zipapp(path, version):
    """Zip the bundled yt_dlp package, stamping a different version."""
    pkg = _BUNDLED_PKG_DIR
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(pkg):
            for name in files:
                if not name.endswith(".py"):
                    continue
                full = os.path.join(root, name)
                arc = os.path.join("yt_dlp", os.path.relpath(full, pkg))
                if arc == os.path.join("yt_dlp", "version.py"):
                    z.writestr(arc, "__version__ = '%s'\nRELEASE_GIT_HEAD = 'x'\nVARIANT = None\n"
                                    "UPDATE_HINT = None\nCHANNEL = 'stable'\nORIGIN = 'x'\n_pkg_version = '%s'\n"
                               % (version, version))
                else:
                    z.write(full, arc)


@pytest.fixture
def release_server(tmp_path):
    """Serves <base>/yt-dlp and <base>/SHA2-256SUMS from a temp dir."""
    served = tmp_path / "srv"
    served.mkdir()

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(served), **kw)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/" % httpd.server_address[1]

    def publish(version, corrupt_sum=False):
        _make_zipapp(str(served / "yt-dlp"), version)
        digest = hashlib.sha256((served / "yt-dlp").read_bytes()).hexdigest()
        if corrupt_sum:
            digest = "0" * 64
        (served / "SHA2-256SUMS").write_text("%s  yt-dlp\n%s  yt-dlp.exe\n" % (digest, "1" * 64))

    yield base, publish
    httpd.shutdown()
    r._swap_engine(None)  # always leave the bundled engine active for other tests


def test_fake_zipapp_helper_really_contains_the_package(tmp_path):
    path = str(tmp_path / "z")
    _make_zipapp(path, "2000.01.01")
    assert r._zip_version(path) == "2000.01.01"
    with zipfile.ZipFile(path) as z:
        assert len(z.namelist()) > 100


def test_version_key_orders_stable_and_nightly():
    assert r._version_key("2026.10.26") < r._version_key("2026.11.01")
    assert r._version_key("2026.10.26") < r._version_key("2026.10.26.1")
    assert r._version_key("2026.11.07.232754") > r._version_key("2026.11.07")


def test_update_downloads_verifies_and_activates_newer_engine(release_server, tmp_path, monkeypatch):
    base, publish = release_server
    publish("2999.01.01")
    monkeypatch.setattr(r, "STABLE_URL", base)
    dest = str(tmp_path / "files" / "ytdlp" / "yt-dlp")
    bundled = r.version()
    out = json.loads(r.update(dest, False, None))
    assert out["success"] and out["updated"] and out["version"] == "2999.01.01", out
    assert "%s -> 2999.01.01" % bundled in out["message"]
    assert r.version() == "2999.01.01" and r.source() == "downloaded"
    assert os.path.isfile(dest) and not os.path.exists(dest + ".part")
    # the swapped engine still works for extraction options
    assert r._options("{}")["noplaylist"] is True
    assert json.loads(r.engine_info())["yt_dlp_source"] == "downloaded"


def test_update_uses_nightly_channel_url(release_server, tmp_path, monkeypatch):
    base, publish = release_server
    publish("2999.02.02.123456")
    monkeypatch.setattr(r, "NIGHTLY_URL", base)
    out = json.loads(r.update(str(tmp_path / "yt-dlp"), True, None))
    assert out["success"] and out["version"] == "2999.02.02.123456" and "(nightly)" in out["message"]


def test_update_rejects_checksum_mismatch_and_keeps_current_engine(release_server, tmp_path, monkeypatch):
    base, publish = release_server
    publish("2999.01.01", corrupt_sum=True)
    monkeypatch.setattr(r, "STABLE_URL", base)
    dest = str(tmp_path / "yt-dlp")
    before = r.version()
    out = json.loads(r.update(dest, False, None))
    assert out["success"] is False and "checksum" in out["message"].lower()
    assert r.version() == before and r.source() == "bundled"
    assert not os.path.exists(dest) and not os.path.exists(dest + ".part")


def test_update_reports_already_up_to_date(release_server, tmp_path, monkeypatch):
    base, publish = release_server
    publish(r._BUNDLED_VERSION)
    monkeypatch.setattr(r, "STABLE_URL", base)
    dest = str(tmp_path / "yt-dlp")
    out = json.loads(r.update(dest, False, None))
    assert out["success"] and out["updated"] is False and "Already up to date" in out["message"]
    assert not os.path.exists(dest)


def test_update_network_failure_returns_error_not_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "STABLE_URL", "http://127.0.0.1:9/")  # discard port: refused
    out = json.loads(r.update(str(tmp_path / "yt-dlp"), False, None))
    assert out["success"] is False and out["message"].startswith("Update failed:")


def test_activate_prefers_newer_download_and_ignores_older_or_broken(release_server, tmp_path):
    base, publish = release_server
    newer = str(tmp_path / "newer")
    _make_zipapp(newer, "2999.01.01")
    assert json.loads(r.activate(newer))["source"] == "downloaded"
    assert r.version() == "2999.01.01"
    # an app update shipped a newer bundled yt-dlp than the stored download -> ignore the download
    older = str(tmp_path / "older")
    _make_zipapp(older, "2000.01.01")
    res = json.loads(r.activate(older))
    assert res["source"] == "bundled" and r.version() == r._BUNDLED_VERSION
    assert "error" not in res  # ignored on purpose, not because it failed to load
    # corrupt file -> bundled engine, with the reason reported
    broken = tmp_path / "broken"
    broken.write_bytes(b"not a zip")
    res = json.loads(r.activate(str(broken)))
    assert res["source"] == "bundled" and "error" in res
    # missing file -> bundled
    assert json.loads(r.activate(str(tmp_path / "missing")))["source"] == "bundled"


def test_zip_that_imports_badly_falls_back_to_bundled(tmp_path):
    bad = tmp_path / "bad"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("yt_dlp/version.py", "__version__ = '2999.01.01'\n")
        z.writestr("yt_dlp/__init__.py", "raise RuntimeError('broken engine')\n")
    res = json.loads(r.activate(str(bad)))
    assert res["source"] == "bundled" and "broken engine" in res["error"]
    assert r.version() == r._BUNDLED_VERSION and r.source() == "bundled"
    assert sys.modules.get("yt_dlp") is r.yt_dlp  # module state is consistent again
    r._swap_engine(None)


@pytest.mark.skipif(not os.environ.get("REX_TEST_NETWORK"), reason="set REX_TEST_NETWORK=1 to hit GitHub")
@pytest.mark.parametrize("nightly", [False, True])
def test_real_github_release_downloads_and_loads(tmp_path, nightly):
    dest = str(tmp_path / "yt-dlp")
    out = json.loads(r.update(dest, nightly, None))
    try:
        assert out["success"], out
        if out["updated"]:
            assert r.source() == "downloaded" and r.version() == out["version"]
            r._options("{}")
            assert r._impersonation_handler_available() in (True, False)
    finally:
        r._swap_engine(None)


# ------------------------------------------- 401 retry and failure steps ---

UNAUTHORIZED = Exception("ERROR: [dailymotion] x: Unable to download JSON metadata: "
                         "HTTP Error 401: Unauthorized (caused by <HTTPError 401: Unauthorized>)")


def test_dailymotion_tries_impersonation_first(fake_ydl):
    fake_ydl.script = {None: UNAUTHORIZED, "chrome": {"id": "ok"}}
    assert r._extract("https://www.dailymotion.com/video/x1", {}) == {"id": "ok"}
    assert fake_ydl.calls == ["chrome"]


def _serve_status(status_for):
    """Tiny HTTP server; status_for(head_text) -> 200 (video) or another status."""
    def serve(sock):
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            conn.settimeout(5)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except Exception:
                pass
            head = data.decode("latin1").lower()
            code = status_for(head)
            if code == 200:
                body = b"\x00" * 2048
                resp = (b"HTTP/1.1 200 OK\r\nContent-Type: video/mp4\r\nContent-Length: %d\r\n"
                        b"Connection: close\r\n\r\n" % len(body))
                conn.sendall(resp + (b"" if head.startswith("head") else body))
            else:
                conn.sendall(b"HTTP/1.1 %d Nope\r\nContent-Length: 0\r\nConnection: close\r\n\r\n" % code)
            conn.close()

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    threading.Thread(target=serve, args=(srv,), daemon=True).start()
    return srv, "http://127.0.0.1:%d/clip.mp4" % srv.getsockname()[1]


def test_end_to_end_plain_gets_401_then_impersonated_succeeds():
    pytest.importorskip("curl_cffi")
    srv, url = _serve_status(lambda head: 200 if "sec-ch-ua" in head else 401)
    try:
        out = json.loads(r.resolve(url, "{}"))
    finally:
        srv.close()
    assert out["is_success"] is True, out
    assert r._preferred_target.get("127.0.0.1") == "chrome"


def test_failure_message_reports_status_attempts_and_last_steps():
    pytest.importorskip("curl_cffi")
    srv, url = _serve_status(lambda head: 401)  # nothing can get through
    try:
        out = json.loads(r.resolve(url, "{}"))
    finally:
        srv.close()
    msg = out["error_message"]
    assert out["is_success"] is False
    assert "401" in msg
    assert "Attempts:" in msg and "default:" in msg and "chrome:" in msg
    assert "Last steps:" in msg and "[generic]" in msg
    assert "Engine:" in msg
