"""In-process yt-dlp bridge used by REX Player.

Behaviour mirrors the REX Ytdlp add-on (stream selection, quality list,
alternate-audio tracks, playlist import) so the all-in-one build keeps feature
parity with the add-on-based player.

Network strategy
----------------
Requests first go out with yt-dlp's normal HTTP stack (urllib, HTTP/1.1).  Some
networks/CDNs drop that connection style (seen as "Connection reset by peer"
on Dailymotion over a mobile carrier while HTTP/2 works).  When an attempt
fails with a transport-type error and curl_cffi is available (arm64
"impersonation" flavor), extraction is retried with browser impersonation,
which negotiates HTTP/2 with a browser TLS fingerprint.  Impersonation is never
forced for requests that succeed without it.

Engine updates
--------------
The player bundles a pinned yt-dlp.  update() can download a newer stable or
nightly yt-dlp zipapp into app storage; activate() prefers it over the bundled
copy only when it is newer, and falls back to the bundled copy on any problem.
"""
import hashlib
import json
import os
import re
import sys
import traceback
import urllib.request
import zipfile
from urllib.parse import urlparse

import yt_dlp  # bundled copy; activate()/update() may swap it for a newer download

STABLE_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
NIGHTLY_URL = "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/"

MAX_PLAYLIST_ENTRIES = 5000
DEFAULT_FORMAT = "bestvideo+bestaudio/best"

# Impersonation targets tried (in order) after the plain attempt fails with a
# transport-type error.  Bounded so a genuinely offline host fails quickly.
_IMPERSONATE_FALLBACKS = ("chrome", "firefox")

_TRANSIENT_MARKERS = (
    "connection reset", "errno 104", "connection aborted", "remote end closed",
    "timed out", "timeout", "transporterror", "eof occurred", "ssl", "handshake",
    "recv failure", "curl: (", "http error 401", "http error 403", "http error 429",
)

_SAFE_HEADERS = ("user-agent", "referer", "cookie", "origin")

# host -> the request target that worked most recently in this process
_preferred_target = {}
_PLAIN_TARGET = "default"


# --------------------------------------------------------------------------
# Engine management (bundled yt-dlp, optionally replaced by a newer download)
# --------------------------------------------------------------------------

_BUNDLED_VERSION = yt_dlp.version.__version__
_override_path = None  # downloaded zipapp currently in use, if any


def _version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version or ""))


def _zip_version(path):
    with zipfile.ZipFile(path) as archive:
        source = archive.read("yt_dlp/version.py").decode("utf-8", "replace")
    match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", source)
    if not match:
        raise ValueError("yt-dlp version not found in archive")
    return match.group(1)


def _purge_yt_dlp():
    for name in [n for n in sys.modules if n == "yt_dlp" or n.startswith("yt_dlp.")]:
        del sys.modules[name]


def _swap_engine(path):
    """Load yt-dlp from `path` (a zipapp) or, with None, the bundled copy.

    If the downloaded copy cannot be imported, the bundled copy is restored and
    the original error is re-raised.
    """
    global yt_dlp, _override_path
    if _override_path and _override_path in sys.path:
        sys.path.remove(_override_path)
    _purge_yt_dlp()
    if path:
        sys.path.insert(0, path)
    try:
        import yt_dlp as module
    except Exception:
        if path and path in sys.path:
            sys.path.remove(path)
        _purge_yt_dlp()
        import yt_dlp as module
        yt_dlp = module
        _override_path = None
        raise
    yt_dlp = module
    _override_path = path


def activate(path=None):
    """Use the downloaded yt-dlp at `path` if it is newer than the bundled one."""
    error = None
    try:
        if path and os.path.isfile(path):
            version = _zip_version(path)
            if _version_key(version) > _version_key(_BUNDLED_VERSION):
                _swap_engine(path)
                return json.dumps({"source": "downloaded", "version": version})
    except Exception as exc:
        error = str(exc)
    if _override_path:
        try:
            _swap_engine(None)
        except Exception as exc:
            error = error or str(exc)
    result = {"source": "bundled", "version": yt_dlp.version.__version__}
    if error:
        result["error"] = error
    return json.dumps(result)


def source():
    return "downloaded" if _override_path else "bundled"


def _opener(proxy):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def _fetch(opener, url, timeout):
    return opener.open(urllib.request.Request(url, headers={"User-Agent": "REX-Player"}), timeout=timeout)


def _published_sha256(sums_text):
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == "yt-dlp":
            return parts[0].lower()
    return None


def update(dest_path, nightly=False, proxy=None):
    """Download the latest yt-dlp (stable or nightly) and switch to it if newer.

    The download is verified against the SHA2-256SUMS file published next to it
    and must contain a readable yt_dlp/version.py.  Returns a JSON string with
    success/updated/version/message; never raises.
    """
    channel = "nightly" if nightly else "stable"
    base = NIGHTLY_URL if nightly else STABLE_URL
    tmp = dest_path + ".part"
    try:
        opener = _opener(proxy)
        with _fetch(opener, base + "SHA2-256SUMS", 30) as response:
            expected = _published_sha256(response.read().decode("utf-8", "replace"))
        if not expected:
            raise ValueError("no checksum published for yt-dlp")
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        digest = hashlib.sha256()
        with _fetch(opener, base + "yt-dlp", 60) as response, open(tmp, "wb") as out:
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("checksum mismatch, download discarded")
        latest = _zip_version(tmp)
        current = yt_dlp.version.__version__
        if _version_key(latest) <= _version_key(current):
            os.remove(tmp)
            return json.dumps({
                "success": True, "updated": False, "version": current,
                "message": "Already up to date (yt-dlp %s; latest %s is %s)." % (current, channel, latest),
            })
        os.replace(tmp, dest_path)
        info = json.loads(activate(dest_path))
        if info.get("source") != "downloaded":
            raise RuntimeError(info.get("error") or "downloaded yt-dlp could not be loaded")
        return json.dumps({
            "success": True, "updated": True, "version": latest,
            "message": "Updated yt-dlp %s -> %s (%s)." % (current, latest, channel),
        })
    except Exception as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return json.dumps({"success": False, "updated": False, "message": "Update failed: %s" % exc})


# --------------------------------------------------------------------------
# Engine info
# --------------------------------------------------------------------------

def _impersonation_handler_available():
    """True only if yt-dlp can actually load its curl_cffi request handler."""
    try:
        from yt_dlp.networking import _curlcffi  # noqa: F401
        return True
    except Exception:
        return False


def _curl_cffi_version():
    try:
        import curl_cffi
        return getattr(curl_cffi, "__version__", "unknown")
    except Exception:
        return None


def version():
    return yt_dlp.version.__version__


def supports_impersonation():
    return _impersonation_handler_available()


def engine_info():
    """Diagnostic snapshot, safe to include in bug reports."""
    return json.dumps({
        "yt_dlp": version(),
        "yt_dlp_source": source(),
        "impersonation_available": _impersonation_handler_available(),
        "curl_cffi": _curl_cffi_version(),
    })


# --------------------------------------------------------------------------
# Small helpers (mirror org.json optionalString/optDouble/optInt semantics)
# --------------------------------------------------------------------------

def _s(d, key):
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    if v is None or isinstance(v, (dict, list)):
        return None
    text = str(v).strip()
    return text if text and text.lower() != "null" else None


def _num(d, key, default=0.0):
    if not isinstance(d, dict):
        return default
    v = d.get(key)
    if v is None or isinstance(v, bool):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(d, key, default=0):
    return int(_num(d, key, float(default)))


def _is_http(url):
    return bool(url) and url.lower().startswith(("http://", "https://"))


def _parse_height(text):
    if not text or not str(text).strip():
        return None
    m = re.search(r"(\d{3,4})p?", str(text))
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------

def _parse_extractor_args(value):
    """Parse a CLI-style --extractor-args value using yt-dlp's own parser."""
    try:
        parsed = yt_dlp.parse_options(["--extractor-args", value]).ydl_opts
        return parsed.get("extractor_args") or {}
    except (SystemExit, Exception) as exc:  # optparse exits on bad input
        raise ValueError("Invalid extractor arguments %r: %s" % (value, exc))


def _options(raw, playlist=False):
    request = json.loads(raw or "{}")
    headers = {}
    if request.get("user_agent"):
        headers["User-Agent"] = request["user_agent"]
    if request.get("referer"):
        headers["Referer"] = request["referer"]
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": not playlist,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "geo_bypass": bool(request.get("geo_bypass", True)),
        "http_headers": headers,
    }
    if playlist:
        opts["extract_flat"] = "in_playlist"
        opts["playlistend"] = MAX_PLAYLIST_ENTRIES
    else:
        opts["format"] = request.get("format") or DEFAULT_FORMAT
    if request.get("proxy"):
        opts["proxy"] = request["proxy"]
    if request.get("cookies_file"):
        opts["cookiefile"] = request["cookies_file"]
    if request.get("extractor_args"):
        opts["extractor_args"] = _parse_extractor_args(request["extractor_args"])
    if request.get("quickjs_path"):
        # yt-dlp needs a JS runtime to solve YouTube n/sig challenges.
        opts["js_runtimes"] = {"quickjs": {"path": request["quickjs_path"]}}
    return opts


# --------------------------------------------------------------------------
# Extraction with fallback
# --------------------------------------------------------------------------

def _is_transient(exc):
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _short(exc, limit=160):
    first = (str(exc).strip().splitlines() or [""])[0]
    if first.startswith("ERROR: "):
        first = first[len("ERROR: "):]
    return first if len(first) <= limit else first[:limit - 1] + "…"


def _host(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _is_dailymotion_host(host):
    host = (host or "").lower().rstrip(".")
    labels = host.split(".")
    return host == "dai.ly" or host.endswith(".dai.ly") or (
        len(labels) >= 2 and labels[-2] == "dailymotion"
    )


def _plan(host):
    plan = [None]
    if _impersonation_handler_available():
        plan += list(_IMPERSONATE_FALLBACKS)
        preferred = _preferred_target.get(host)
        if preferred == _PLAIN_TARGET:
            preferred = None
        elif preferred is None and _is_dailymotion_host(host):
            preferred = "chrome"
        if preferred in plan:
            plan.remove(preferred)
            plan.insert(0, preferred)
    return plan


class ExtractionFailed(Exception):
    """Carries yt-dlp's original error text plus a summary of every attempt."""


class _StepLog:
    """yt-dlp logger that remembers the last status lines (e.g. which API call
    was being made), so a failure can say where it happened."""

    def __init__(self):
        self.lines = []

    def _add(self, message):
        text = re.sub(r"\s+", " ", str(message)).strip()
        if text and not text.startswith(("[debug]", "ERROR:")):
            self.lines.append(text[:140])

    debug = info = warning = error = _add


def _extract(url, base_opts):
    host = _host(url)
    attempts = []
    last_exc = None
    last_steps = []
    saw_transient = False

    for target in _plan(host):
        label = target or "default"
        opts = dict(base_opts)
        steps = _StepLog()
        opts["logger"] = steps
        if target:
            try:
                from yt_dlp.networking.impersonate import ImpersonateTarget
                opts["impersonate"] = ImpersonateTarget(target)
            except Exception as exc:
                attempts.append("%s: unavailable (%s)" % (label, _short(exc)))
                continue
        try:
            # yt-dlp raises here if the requested target cannot be used.
            ydl = yt_dlp.YoutubeDL(opts)
        except Exception as exc:
            attempts.append("%s: cannot start (%s)" % (label, _short(exc)))
            continue
        try:
            with ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            last_exc = exc
            last_steps = steps.lines[-6:]
            attempts.append("%s: %s" % (label, _short(exc)))
            if _is_transient(exc):
                saw_transient = True
                continue
            break
        if info is None:
            attempts.append("%s: yt-dlp returned no data" % label)
            last_exc = RuntimeError("yt-dlp returned no data")
            break
        if target:
            _preferred_target[host] = target
        elif target is None:
            if _is_dailymotion_host(host) and attempts:
                _preferred_target[host] = _PLAIN_TARGET
            elif _preferred_target.get(host) != _PLAIN_TARGET:
                _preferred_target.pop(host, None)
        return ydl.sanitize_info(info)

    message = str(last_exc) if last_exc is not None else "; ".join(attempts)
    if len(attempts) > 1 or saw_transient:
        message += "\n\nAttempts: " + "; ".join(attempts)
        if last_steps:
            message += "\nLast steps: " + " | ".join(last_steps)
        message += "\nEngine: " + engine_info()
    raise ExtractionFailed(message)


# --------------------------------------------------------------------------
# Result building (ported 1:1 from the REX Ytdlp add-on)
# --------------------------------------------------------------------------

def _collect_headers(source, target):
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        if key.lower() in _SAFE_HEADERS and value is not None and str(value).strip():
            target[key] = str(value)


def _audio_lang(f):
    lang = _s(f, "language") or _s(f, "lang")
    if lang:
        return lang
    note = _s(f, "format_note")
    if note and len(note) <= 12 and not any(c.isdigit() for c in note):
        return note
    return _s(f.get("http_headers"), "X-Language")


def _normalize_audio_lang(f):
    lang = _audio_lang(f)
    lang = lang.lower().strip() if lang else None
    return lang or None


def _audio_label(lang, acodec, format_id, abr):
    if abr >= 1:
        quality = "%dkbps" % int(abr)
    elif abr > 0 and acodec and "opus" in acodec.lower():
        quality = "opus"
    else:
        quality = None
    parts = [p for p in (lang, acodec, quality or format_id) if p is not None]
    return " • ".join(parts) or "Audio"


def _audio_tracks(info, primary_audio_url):
    formats = info.get("formats") or []
    if not formats:
        return []

    primary_lang = None
    if primary_audio_url:
        for f in formats:
            if isinstance(f, dict) and _s(f, "url") == primary_audio_url:
                primary_lang = _normalize_audio_lang(f)
                break

    best_per_lang = {}
    seen_urls = set()
    for f in formats:
        if not isinstance(f, dict):
            continue
        vcodec = _s(f, "vcodec")
        if vcodec is not None and vcodec != "none":
            continue  # video or muxed
        acodec = _s(f, "acodec")
        if acodec is None or acodec == "none":
            continue
        url = _s(f, "url")
        if url is None or not _is_http(url):
            continue
        if primary_audio_url and url == primary_audio_url:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        lang = _audio_lang(f)
        lang_norm = lang.lower().strip() if lang else None
        if lang_norm is not None and lang_norm == primary_lang:
            continue

        key = lang_norm if lang_norm is not None else "\x00no-lang"
        abr = _num(f, "abr", _num(f, "tbr", 0.0))
        candidate = {
            "url": url,
            "lang": lang,
            "codec": acodec,
            "bitrate": int(abr * 1000),
            "label": _audio_label(lang, acodec, _s(f, "format_id"), abr),
        }
        existing = best_per_lang.get(key)
        if existing is None or candidate["bitrate"] > existing["bitrate"]:
            best_per_lang[key] = candidate

    return sorted(best_per_lang.values(), key=lambda t: -t["bitrate"])


def _vcodec_score(vcodec, height):
    vc = vcodec.lower()
    if height <= 1080 and (vc.startswith("avc") or vc.startswith("h264")):
        return 3
    if height > 1080 and (vc.startswith("vp9") or vc.startswith("vp09")):
        return 3
    if vc.startswith("vp9") or vc.startswith("vp09"):
        return 2
    if vc.startswith("av01") or vc.startswith("av1"):
        return 1
    return 0


def _friendly_codec(vcodec):
    vc = vcodec.lower()
    if vc.startswith("avc"):
        return "AVC"
    if vc.startswith("vp9") or vc.startswith("vp09"):
        return "VP9"
    if vc.startswith("av01") or vc.startswith("av1"):
        return "AV1"
    return vcodec


def _qualities(info, selected_video_url, selected_audio_url):
    formats = info.get("formats") or []
    if not formats:
        return []

    best_audio_url = selected_audio_url if selected_audio_url and selected_audio_url.strip() else None
    if not best_audio_url:
        highest = -1.0
        for f in formats:
            if not isinstance(f, dict):
                continue
            vcodec = _s(f, "vcodec")
            acodec = _s(f, "acodec")
            url = _s(f, "url")
            if url is None or not _is_http(url):
                continue
            if (vcodec is None or vcodec == "none") and (acodec is not None and acodec != "none"):
                abr = _num(f, "abr", _num(f, "tbr", 0.0))
                if abr > highest:
                    highest = abr
                    best_audio_url = url

    candidates = []
    for i, f in enumerate(formats):
        if not isinstance(f, dict):
            continue
        vcodec = _s(f, "vcodec")
        if vcodec is None or vcodec == "none":
            continue
        ext = _s(f, "ext") or ""
        note = _s(f, "format_note") or ""
        protocol = _s(f, "protocol") or ""
        if ext.lower() == "mhtml" or "storyboard" in note.lower() or "mhtml" in protocol.lower():
            continue
        url = _s(f, "url")
        if url is None or not _is_http(url):
            continue
        height = _int(f, "height", 0)
        if height <= 0:
            height = _parse_height(note) or _parse_height(_s(f, "resolution")) or 0
        if height <= 0:
            continue
        acodec = _s(f, "acodec")
        candidates.append({
            "format_id": _s(f, "format_id") or str(i),
            "height": height,
            "width": _int(f, "width", 0),
            "fps": int(_num(f, "fps", 0.0)),
            "vcodec": vcodec,
            "url": url,
            "video_only": acodec is None or acodec == "none",
            "bitrate": int(_num(f, "tbr", _num(f, "vbr", 0.0)) * 1000),
        })

    if not candidates:
        return []

    groups = {}
    for c in candidates:
        groups.setdefault((c["height"], c["fps"] >= 50), []).append(c)

    selected_base = selected_video_url.split("?")[0] if selected_video_url else None
    results = []
    for (height, is60), group in groups.items():
        best = next((c for c in group if c["url"] == selected_video_url), None)
        if best is None and selected_base is not None:
            best = next((c for c in group if c["url"].split("?")[0] == selected_base), None)
        if best is None:
            for c in group:  # first maximum wins, like Kotlin's maxWithOrNull
                if best is None or (_vcodec_score(c["vcodec"], height), c["bitrate"]) > \
                        (_vcodec_score(best["vcodec"], height), best["bitrate"]):
                    best = c
        label = "%dp" % height
        if is60:
            label += "60"
        if height >= 2160:
            label += " (4K)"
        elif height >= 1440:
            label += " (2K)"
        results.append({
            "id": best["format_id"],
            "label": label,
            "height": height,
            "width": best["width"],
            "fps": best["fps"],
            "codec": _friendly_codec(best["vcodec"]),
            "bitrate": best["bitrate"],
            "video_url": best["url"],
            "audio_url": best_audio_url if best["video_only"] else None,
            "is_dash": bool(best["video_only"] and best_audio_url),
            "is_audio_only": False,
        })

    results.sort(key=lambda q: (-q["height"], -q["fps"]))
    if best_audio_url:
        results.append({
            "id": "audio_only", "label": "Audio Only", "height": 0, "width": 0,
            "fps": 0, "codec": None, "bitrate": 0, "video_url": best_audio_url,
            "audio_url": None, "is_dash": False, "is_audio_only": True,
        })
    return results


def _stream_result(info):
    headers = {}
    _collect_headers(info.get("http_headers"), headers)

    subtitles = {}
    for lang, tracks in (info.get("subtitles") or {}).items():
        for track in tracks or []:
            if not isinstance(track, dict):
                continue
            sub_url = _s(track, "url")
            if sub_url and (track.get("ext") or "") in ("vtt", "srt"):
                subtitles[lang] = sub_url
                break

    video_url = audio_url = None
    requested = info.get("requested_formats") or []
    if len(requested) >= 2 and isinstance(requested[0], dict) and isinstance(requested[1], dict):
        f0, f1 = requested[0], requested[1]
        v0 = _s(f0, "vcodec") not in (None, "none")
        v1 = _s(f1, "vcodec") not in (None, "none")
        if v0 and not v1:
            video_url, audio_url = _s(f0, "url"), _s(f1, "url")
        elif v1 and not v0:
            video_url, audio_url = _s(f1, "url"), _s(f0, "url")
        else:
            video_url, audio_url = _s(f0, "url"), _s(f1, "url")
        if not headers:
            _collect_headers(f0.get("http_headers"), headers)
    else:
        video_url = _s(info, "url")

    if not video_url:
        title = _s(info, "title")
        return {
            "is_success": False,
            "error_message": "yt-dlp finished but returned no playable stream URL"
                             + (" for %r" % title if title else ""),
        }

    return {
        "is_success": True,
        "video_url": video_url,
        "audio_url": audio_url,
        "title": _s(info, "title"),
        "duration": int(_num(info, "duration", 0.0)),
        "thumbnail": _s(info, "thumbnail"),
        "uploader": _s(info, "uploader") or _s(info, "channel"),
        "http_headers": headers,
        "subtitles": subtitles,
        "video_qualities_json": json.dumps(_qualities(info, video_url, audio_url)),
        "audio_tracks_json": json.dumps(_audio_tracks(info, audio_url)),
    }


def _playlist_result(source_url, info):
    raw_entries = info.get("entries")
    if raw_entries is None:
        return {"is_success": False, "source_url": source_url,
                "error_message": "No playlist items found"}
    entries, seen = [], set()
    for item in list(raw_entries)[:MAX_PLAYLIST_ENTRIES]:
        if not isinstance(item, dict):
            continue
        item_id = _s(item, "id")
        url = next((u for u in (_s(item, k) for k in ("webpage_url", "original_url", "url"))
                    if u and _is_http(u)), None)
        if url is None and item_id:
            url = "https://www.youtube.com/watch?v=" + item_id
        if url is None or url in seen:
            continue
        seen.add(url)
        title = next((t for t in (_s(item, k) for k in ("title", "fulltitle", "id")) if t),
                     "Video %d" % (len(entries) + 1))
        artist = next((a for a in (_s(item, k) for k in
                                   ("artist", "creator", "uploader", "channel")) if a), None)
        thumbnail = _s(item, "thumbnail") or (
            "https://i.ytimg.com/vi/%s/hqdefault.jpg" % item_id if item_id else None)
        entries.append({
            "id": item_id, "url": url, "title": title, "artist": artist,
            "thumbnail": thumbnail, "duration": int(_num(item, "duration", 0.0)),
        })
    return {
        "is_success": True,
        "source_url": source_url,
        "title": _s(info, "title") or "Playlist",
        "entries_json": json.dumps(entries),
    }


# --------------------------------------------------------------------------
# Entry points called from Kotlin (YtDlClient)
# --------------------------------------------------------------------------

def resolve(raw_url, raw_options):
    try:
        info = _extract(raw_url, _options(raw_options))
        return json.dumps(_stream_result(info))
    except Exception as exc:
        return json.dumps({"is_success": False,
                           "error_message": str(exc) or traceback.format_exc()})


def playlist(raw_url, raw_options):
    try:
        info = _extract(raw_url, _options(raw_options, playlist=True))
        return json.dumps(_playlist_result(raw_url, info))
    except Exception as exc:
        return json.dumps({"is_success": False, "source_url": raw_url,
                           "error_message": str(exc) or traceback.format_exc()})
