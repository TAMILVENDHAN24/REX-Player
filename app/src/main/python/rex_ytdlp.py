"""In-process yt-dlp bridge used by REX Player."""
import json
import traceback

import yt_dlp


def version():
    return yt_dlp.version.__version__


def _options(raw):
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
        "noplaylist": False,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "geo_bypass": bool(request.get("geo_bypass", True)),
        "http_headers": headers,
    }
    if request.get("format"):
        opts["format"] = request["format"]
    if request.get("proxy"):
        opts["proxy"] = request["proxy"]
    if request.get("cookies_file"):
        opts["cookiefile"] = request["cookies_file"]
    if request.get("extractor_args"):
        opts["extractor_args"] = {"default": request["extractor_args"].split()}
    return opts


def _formats(info):
    return [f for f in info.get("formats", []) if f.get("url")]


def resolve(raw_url, raw_options):
    try:
        with yt_dlp.YoutubeDL(_options(raw_options)) as ydl:
            info = ydl.extract_info(raw_url, download=False)
        if not info:
            return json.dumps({"is_success": False, "error_message": "No media information returned"})
        formats = _formats(info)
        requested = info.get("requested_formats") or []
        selected_video = next((f for f in requested if f.get("vcodec") not in (None, "none")), None)
        selected_audio = next((f for f in requested if f.get("acodec") not in (None, "none")), None)
        if not selected_video:
            selected_video = next((f for f in reversed(formats) if f.get("vcodec") not in (None, "none")), None)
        if not selected_audio:
            selected_audio = next((f for f in reversed(formats) if f.get("acodec") not in (None, "none") and f.get("vcodec") == "none"), None)
        if not selected_video and selected_audio:
            selected_video = selected_audio
        if not selected_video:
            selected_video = info if info.get("url") else None
        if not selected_video:
            return json.dumps({"is_success": False, "error_message": "No playable video format was found"})

        qualities = []
        for f in sorted(formats, key=lambda item: (item.get("height") or 0, item.get("tbr") or 0), reverse=True):
            video = f.get("vcodec") not in (None, "none")
            audio_only = f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
            if not video and not audio_only:
                continue
            qualities.append({
                "id": str(f.get("format_id", "best")),
                "label": str(f.get("format_note") or (f"{f.get('height')}p" if f.get("height") else "Audio" if audio_only else "Best")),
                "height": f.get("height") or 0,
                "width": f.get("width") or 0,
                "fps": f.get("fps") or 0,
                "codec": f.get("vcodec") or f.get("acodec"),
                "bitrate": int((f.get("tbr") or 0) * 1000),
                "video_url": f.get("url") if video else None,
                "audio_url": f.get("url") if audio_only else None,
                "is_dash": bool(video and selected_audio),
                "is_audio_only": audio_only,
            })
        headers = dict(info.get("http_headers") or selected_video.get("http_headers") or {})
        subtitles = {}
        for language, tracks in (info.get("subtitles") or {}).items():
            if tracks:
                subtitles[language] = tracks[0].get("url", "")
        result = {
            "is_success": True,
            "video_url": selected_video.get("url"),
            "audio_url": selected_audio.get("url") if selected_audio else None,
            "title": info.get("title"),
            "duration": int(info.get("duration") or 0),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader") or info.get("channel"),
            "http_headers": headers,
            "subtitles": subtitles,
            "video_qualities_json": json.dumps(qualities),
            "audio_tracks_json": "[]",
        }
        return json.dumps(result)
    except Exception as exc:
        return json.dumps({"is_success": False, "error_message": str(exc) or traceback.format_exc()})


def playlist(raw_url, raw_options):
    try:
        opts = _options(raw_options)
        opts["extract_flat"] = "in_playlist"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(raw_url, download=False)
        entries = []
        for entry in (info or {}).get("entries") or []:
            if not entry:
                continue
            item_url = entry.get("webpage_url") or entry.get("url")
            if item_url and not item_url.startswith(("http://", "https://")):
                item_url = "https://www.youtube.com/watch?v=" + item_url
            if item_url:
                entries.append({
                    "id": entry.get("id"), "url": item_url,
                    "title": entry.get("title") or "Untitled",
                    "artist": entry.get("uploader") or entry.get("channel"),
                    "thumbnail": entry.get("thumbnail"),
                    "duration": int(entry.get("duration") or 0),
                })
        return json.dumps({"is_success": True, "source_url": raw_url,
                           "title": (info or {}).get("title"), "entries_json": json.dumps(entries)})
    except Exception as exc:
        return json.dumps({"is_success": False, "source_url": raw_url, "error_message": str(exc)})
