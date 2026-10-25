
package com.playlistdl.app

import android.app.Activity
import android.os.Bundle
import android.graphics.Color
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.EditText
import android.widget.Button
import android.widget.ScrollView
import android.provider.MediaStore
import android.content.ContentValues
import android.net.Uri
import java.io.File
import java.io.FileInputStream
import java.util.concurrent.TimeUnit
import dev.ffmpegkit_maintained.ytdlp.YtDlp
import dev.ffmpegkit_maintained.ytdlp.YtDlpRequest
import com.arthenica.ffmpegkit.FFmpegKit

class MainActivity : Activity() {

    private lateinit var urlInput: EditText
    private lateinit var downloadButton: Button
    private lateinit var statusText: TextView
    private var isDownloading = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(32, 40, 32, 32)
            setBackgroundColor(Color.rgb(20, 20, 20))
        }

        val title = TextView(this).apply {
            text = "PlaylistDL"
            textSize = 30f
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
        }

        val subtitle = TextView(this).apply {
            text = "دانلود از لینک‌های پشتیبانی‌شده"
            textSize = 16f
            setTextColor(Color.LTGRAY)
            gravity = Gravity.CENTER
        }

        urlInput = EditText(this).apply {
            hint = "لینک آهنگ یا پلی‌لیست"
            setSingleLine(true)
            setTextColor(Color.WHITE)
            setHintTextColor(Color.GRAY)
        }

        val format = TextView(this).apply {
            text = "خروجی MP3 | هدف کیفیت: 320 kbps\nMusic/PlaylistDL"
            textSize = 14f
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
        }

        downloadButton = Button(this).apply {
            text = "دانلود"
            setOnClickListener { startDownload() }
        }

        statusText = TextView(this).apply {
            text = "آماده"
            textSize = 14f
            setTextColor(Color.LTGRAY)
            gravity = Gravity.CENTER
            setPadding(0, 24, 0, 0)
        }

        layout.addView(title)
        layout.addView(subtitle)
        addSpace(layout, 24)
        layout.addView(urlInput)
        addSpace(layout, 12)
        layout.addView(format)
        addSpace(layout, 12)
        layout.addView(downloadButton)
        layout.addView(statusText)

        val scroll = ScrollView(this)
        scroll.addView(layout, ViewGroup.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ))
        setContentView(scroll)
    }

    private fun addSpace(layout: LinearLayout, height: Int) {
        layout.addView(View(this), LinearLayout.LayoutParams(1, height))
    }

    private fun startDownload() {
        if (isDownloading) return

        val url = urlInput.text.toString().trim()
        if (url.isEmpty()) {
            urlInput.error = "لینک را وارد کن"
            return
        }

        if (!url.startsWith("https://") && !url.startsWith("http://")) {
            urlInput.error = "لینک معتبر وارد کن"
            return
        }

        if (isSpotifyUrl(url)) {
            showStatus(
                "این نسخه هنوز به Spotify API متصل نیست. " +
                "لینک اسپاتیفای برای استخراج آهنگ‌ها پشتیبانی نمی‌شود. " +
                "از لینک منبع صوتی پشتیبانی‌شده استفاده کن."
            )
            return
        }

        val root = getExternalFilesDir(null)
        if (root == null) {
            showStatus("فضای ذخیره‌سازی در دسترس نیست")
            return
        }

        val outputDir = File(root, "PlaylistDL")
        if (!outputDir.exists() && !outputDir.mkdirs()) {
            showStatus("ساخت پوشهٔ موقت ناموفق بود")
            return
        }

        isDownloading = true
        downloadButton.isEnabled = false
        downloadButton.text = "در حال دانلود..."
        showStatus("در حال آماده‌سازی موتور دانلود...")

        Thread {
            try {
                YtDlp.init(applicationContext)

                val template = File(
                    outputDir,
                    "%(playlist_title|Single)s/%(title)s.%(ext)s"
                ).absolutePath

                val request = YtDlpRequest(url)
                    .setOutputTemplate(template)
                    .addOption("-f", "bestaudio/best")
                    .addOption("--yes-playlist")
                    .addOption("--no-mtime")
                    .addOption("--ignore-errors")
                    .addOption("--no-overwrites")

                runOnUiThread {
                    showStatus("در حال دریافت از منبع پشتیبانی‌شده...")
                }

                val result = YtDlp.executeAsync(request) { progress, eta, line ->
                    runOnUiThread {
                        showStatus(
                            "دانلود: ${progress.toInt().coerceIn(0, 100)}%\n" +
                            "زمان باقی‌مانده: ${eta.coerceAtLeast(0)} ثانیه"
                        )
                    }
                }.get(12, TimeUnit.HOURS)

                if (!result.isSuccess) {
                    throw Exception(
                        "دانلود ناموفق بود؛ ممکن است سایت پشتیبانی نشود."
                    )
                }

                val mediaFiles = outputDir.walkTopDown().filter { file ->
                    file.isFile && file.extension.lowercase() in listOf(
                        "mp3", "m4a", "webm", "opus", "aac",
                        "wav", "flac", "ogg", "mp4", "mkv"
                    )
                }.toList()

                if (mediaFiles.isEmpty()) {
                    throw Exception("فایل صوتی پیدا نشد.")
                }

                var saved = 0
                var failed = 0

                for (input in mediaFiles) {
                    val mp3 = if (input.extension.equals("mp3", true)) {
                        input
                    } else {
                        val target = File(
                            input.parentFile,
                            input.nameWithoutExtension + ".mp3"
                        )

                        runOnUiThread {
                            showStatus("در حال تبدیل به MP3:\n${input.name}")
                        }

                        val command = arrayOf(
                            "-y", "-i", input.absolutePath, "-vn",
                            "-codec:a", "libmp3lame",
                            "-b:a", "320k", target.absolutePath
                        )

                        val session = FFmpegKit.executeWithArguments(command)

                        if (session.returnCode.isValueSuccess &&
                            target.exists() && target.length() > 0
                        ) {
                            input.delete()
                            target
                        } else {
                            failed++
                            continue
                        }
                    }

                    try {
                        if (copyToMusic(mp3)) {
                            saved++
                        } else {
                            failed++
                        }
                    } catch (_: Exception) {
                        failed++
                    }
                }

                runOnUiThread {
                    showStatus(
                        "فرایند تمام شد.\nذخیره‌شده در Music: $saved\n" +
                        "ناموفق: $failed\nمسیر: Music/PlaylistDL"
                    )
                }
            } catch (e: Exception) {
                runOnUiThread {
                    showStatus("خطا:\n${e.cause?.message ?: e.message}")
                }
            } finally {
                runOnUiThread {
                    isDownloading = false
                    downloadButton.isEnabled = true
                    downloadButton.text = "دانلود"
                }
            }
        }.start()
    }

    private fun isSpotifyUrl(value: String): Boolean {
        val host = try {
            Uri.parse(value).host?.lowercase()
        } catch (_: Exception) {
            null
        } ?: return false

        return host == "spotify.com" || host.endsWith(".spotify.com")
    }

    private fun copyToMusic(source: File): Boolean {
        val values = ContentValues().apply {
            put(MediaStore.Audio.Media.DISPLAY_NAME, source.name)
            put(MediaStore.Audio.Media.MIME_TYPE, "audio/mpeg")
            put(MediaStore.Audio.Media.RELATIVE_PATH, "Music/PlaylistDL")
            put(MediaStore.Audio.Media.IS_MUSIC, 1)
        }

        val collection = MediaStore.Audio.Media.getContentUri(
            MediaStore.VOLUME_EXTERNAL_PRIMARY
        )
        val uri = contentResolver.insert(collection, values) ?: return false

        return try {
            contentResolver.openOutputStream(uri)?.use { output ->
                FileInputStream(source).use { input ->
                    input.copyTo(output)
                }
            } ?: throw Exception("نوشتن فایل ناموفق بود")
            true
        } catch (e: Exception) {
            contentResolver.delete(uri, null, null)
            throw e
        }
    }

    private fun showStatus(message: String) {
        statusText.text = message
    }
}
