package xyz.mpv.rex.domain.ytdl

import android.content.Context
import android.net.Uri
import android.os.Bundle
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONObject
import xyz.mpv.rex.domain.ytdl.ipc.YtdlIpcConverter
import xyz.mpv.rex.domain.ytdl.model.ResolvedPlaylist
import xyz.mpv.rex.domain.ytdl.model.ResolvedStream
import xyz.mpv.rex.domain.ytdl.model.StreamExtractionOptions
import xyz.mpv.rex.domain.ytdl.model.YtdlpStatus
import xyz.mpv.rex.preferences.YtdlPreferences
import xyz.mpv.rex.utils.media.HttpUtils

class YtDlClient(
    private val context: Context,
    private val preferences: YtdlPreferences? = null,
) {
    private val pythonMutex = Mutex()

    fun isAddonInstalled(): Boolean = true
    fun isAddonAuthentic(): Boolean = true

    fun isKnownWebPlatform(url: String): Boolean {
        val host = runCatching { Uri.parse(url).host?.lowercase() }.getOrNull() ?: return false
        return KNOWN_WEB_PLATFORMS.any { host == it || host.endsWith(".$it") } ||
            preferences?.getParsedCustomDomains().orEmpty().any { host == it || host.endsWith(".$it") }
    }

    fun requiresYtdl(url: String): Boolean {
        val uri = runCatching { Uri.parse(url) }.getOrNull() ?: return false
        if (uri.scheme?.lowercase() !in setOf("http", "https")) return false
        val host = uri.host?.lowercase().orEmpty()
        if (host == "localhost" || host == "127.0.0.1" || host.startsWith("192.168.") || host.startsWith("10.")) return false
        if (HttpUtils.isDirectMediaUrl(uri)) return false
        return isKnownWebPlatform(url)
    }

    private fun module() : Python {
        if (!Python.isStarted()) Python.start(AndroidPlatform(context.applicationContext))
        return Python.getInstance()
    }

    private fun optionsJson(options: StreamExtractionOptions): String = JSONObject().apply {
        put("format", options.format)
        put("user_agent", options.userAgent)
        put("referer", options.referer)
        put("proxy", options.proxy)
        put("cookies_file", options.cookiesFilePath)
        put("extractor_args", options.extractorArgs)
        put("geo_bypass", options.geoBypass)
    }.toString()

    private fun resultBundle(json: String): Bundle {
        val obj = JSONObject(json)
        return Bundle().apply {
            putBoolean("is_success", obj.optBoolean("is_success"))
            putString("video_url", obj.optString("video_url").takeUnless { it == "null" })
            putString("audio_url", obj.optString("audio_url").takeUnless { it == "null" })
            putString("title", obj.optString("title").takeUnless { it == "null" })
            putInt("duration", obj.optInt("duration"))
            putString("thumbnail", obj.optString("thumbnail").takeUnless { it == "null" })
            putString("uploader", obj.optString("uploader").takeUnless { it == "null" })
            putString("error_message", obj.optString("error_message").takeUnless { it == "null" })
            putString("video_qualities_json", obj.optString("video_qualities_json", "[]"))
            putString("audio_tracks_json", obj.optString("audio_tracks_json", "[]"))
            putString("entries_json", obj.optString("entries_json", "[]"))
            putString("source_url", obj.optString("source_url"))
            val headers = Bundle()
            obj.optJSONObject("http_headers")?.let { values ->
                values.keys().forEach { key -> headers.putString(key, values.optString(key)) }
            }
            putBundle("http_headers", headers)
            val subtitles = Bundle()
            obj.optJSONObject("subtitles")?.let { values ->
                values.keys().forEach { key -> subtitles.putString(key, values.optString(key)) }
            }
            putBundle("subtitles", subtitles)
        }
    }

    suspend fun isEngineReady(): Boolean = withContext(Dispatchers.IO) {
        runCatching { pythonMutex.withLock { module().getModule("rex_ytdlp").callAttr("version") }; true }
            .onFailure { Log.e(TAG, "Bundled yt-dlp runtime failed to start", it) }.getOrDefault(false)
    }

    suspend fun supportsBrowserImpersonation(): Boolean = withContext(Dispatchers.IO) {
        runCatching {
            pythonMutex.withLock { module().getModule("rex_ytdlp").callAttr("supports_impersonation").toJava(Boolean::class.javaObjectType) }
        }.getOrDefault(false)
    }

    suspend fun getStatus(): YtdlpStatus = withContext(Dispatchers.IO) {
        runCatching {
            val version = pythonMutex.withLock { module().getModule("rex_ytdlp").callAttr("version").toJava(String::class.java) }
            YtdlpStatus(isInstalled = true, version = version, channel = "Bundled", origin = "REX Player")
        }.getOrElse {
            Log.e(TAG, "Unable to read bundled yt-dlp version", it)
            YtdlpStatus.NotInstalled
        }
    }

    suspend fun resolveStream(url: String, options: StreamExtractionOptions = StreamExtractionOptions()): ResolvedStream = withContext(Dispatchers.IO) {
        runCatching {
            val json = pythonMutex.withLock {
                module().getModule("rex_ytdlp").callAttr("resolve", url, optionsJson(options)).toJava(String::class.java)
            }
            YtdlIpcConverter.parseResolvedStream(resultBundle(json))
        }.getOrElse {
            Log.e(TAG, "Bundled yt-dlp failed to resolve $url", it)
            ResolvedStream.failure(it.message ?: "yt-dlp extraction failed")
        }
    }

    suspend fun extractPlaylist(url: String, options: StreamExtractionOptions = StreamExtractionOptions()): ResolvedPlaylist = withContext(Dispatchers.IO) {
        runCatching {
            val json = pythonMutex.withLock {
                module().getModule("rex_ytdlp").callAttr("playlist", url, optionsJson(options)).toJava(String::class.java)
            }
            YtdlIpcConverter.parseResolvedPlaylist(resultBundle(json))
        }.getOrElse {
            Log.e(TAG, "Bundled yt-dlp failed to extract playlist $url", it)
            ResolvedPlaylist.failure(url, it.message ?: "Playlist extraction failed")
        }
    }

    fun unbind() = Unit

    companion object {
        private const val TAG = "YtDlClient"
        val KNOWN_WEB_PLATFORMS = setOf(
            "youtube.com", "youtu.be", "yt.be", "youtube-nocookie.com", "vimeo.com", "twitch.tv",
            "dailymotion.com", "dai.ly", "tiktok.com", "twitter.com", "x.com", "facebook.com",
            "fb.watch", "instagram.com", "bilibili.com", "b23.tv", "soundcloud.com", "odysee.com",
            "rumble.com", "streamable.com", "reddit.com", "v.redd.it", "nicovideo.jp", "kick.com",
        )
    }
}
