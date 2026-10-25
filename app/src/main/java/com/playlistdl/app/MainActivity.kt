
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
import android.widget.Toast
import java.io.File
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
            text = "دانلود موسیقی و پلی‌لیست"
            textSize = 16f
            setTextColor(Color.LTGRAY)
            gravity = Gravity.CENTER
        }

        urlInput = EditText(this).apply {
            hint = "لینک آهنگ یا پلی‌لیست"
            setSingleLine(true)
            setTextColor(Color.WHITE)
            setHintTextColor(Color.GRAY)
            setPadding(20, 16, 20, 16)
        }

        val format = TextView(this).apply {
            text = "فرمت خروجی: MP3 | کیفیت هدف: 320 kbps"
            textSize = 14f
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
        }

        downloadButton = Button(this).apply {
            text = "دانلود"
            setOnClickListener {
                startDownload()
            }
        }

        statusText = TextView(this).apply {
            text = "آمادهٔ دانلود"
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

        val scrollView = ScrollView(this)
        scrollView.addView(
            layout,
            ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            )
        )

        setContentView(scrollView)
    }

    private fun addSpace(layout: LinearLayout, height: Int) {
        layout.addView(
            View(this),
            LinearLayout.LayoutParams(
                1,
                height
            )
        )
    }

    private fun startDownload() {
        if (isDownloading) return

        val url = urlInput.text.toString().trim()

        if (url.isEmpty()) {
            urlInput.error = "لطفاً لینک را وارد کن"
            return
        }

        if (!url.startsWith("https://") &&
            !url.startsWith("http://")) {
            urlInput.error = "لینک معتبر وارد کن"
            return
        }

        val root = getExternalFilesDir(null)
        if (root == null) {
            showStatus("فضای ذخیره‌سازی در دسترس نیست")
            return
        }

        val outputDir = File(root, "PlaylistDL")
        if (!outputDir.exists() && !outputDir.mkdirs()) {
            showStatus("ساخت پوشهٔ دانلود ناموفق بود")
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

                runOnUiThread {
                    showStatus("در حال دانلود آهنگ‌ها...")
                }

                val future = YtDlp.executeAsync(request) {
                        progress, eta, line ->
                    runOnUiThread {
                        val percent = progress.toInt().coerceIn(0, 100)
                        showStatus(
                            "دانلود: $percent%\n" +
                            "زمان باقی‌مانده: ${eta.coerceAtLeast(0)} ثانیه"
                        )
                    }
                }

                val result = future.get(12, TimeUnit.HOURS)

                if (!result.isSuccess) {
                    throw Exception("موتور دانلود با خطا متوقف شد")
                }

                runOnUiThread {
                    showStatus("دانلود تمام شد؛ بررسی فایل‌ها...")
                }

                val mediaFiles = outputDir.walkTopDown()
                    .filter { file ->
                        file.isFile &&
                        file.extension.lowercase() in listOf(
                            "m4a", "webm", "opus", "aac",
                            "wav", "flac", "ogg", "mp4", "mkv"
                        )
                    }
                    .toList()

                if (mediaFiles.isEmpty()) {
                    throw Exception(
                        "فایل صوتی پیدا نشد؛ ممکن است سایت از دانلود پشتیبانی نکند"
                    )
                }

                var converted = 0
                var failed = 0

                for (input in mediaFiles) {
                    val output = File(
                        input.parentFile,
                        input.nameWithoutExtension + ".mp3"
                    )

                    runOnUiThread {
                        showStatus("در حال تبدیل به MP3:\n${input.name}")
                    }

                    val command = arrayOf(
                        "-y",
                        "-i", input.absolutePath,
                        "-vn",
                        "-codec:a", "libmp3lame",
                        "-b:a", "320k",
                        output.absolutePath
                    )

                    val session = FFmpegKit.executeWithArguments(command)

                    if (session.returnCode.isValueSuccess &&
                        output.exists() &&
                        output.length() > 0
                    ) {
                        converted++
                        input.delete()
                    } else {
                        failed++
                    }
                }

                runOnUiThread {
                    showStatus(
                        "فرایند تمام شد.\n" +
                        "تبدیل موفق: $converted\n" +
                        "تبدیل ناموفق: $failed\n\n" +
                        "محل فایل‌ها:\n${outputDir.absolutePath}"
                    )
                }

            } catch (e: Exception) {
                runOnUiThread {
                    showStatus(
                        "خطا:\n${e.cause?.message ?: e.message ?: "خطای ناشناخته"}"
                    )
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

    private fun showStatus(message: String) {
        statusText.text = message
    }
}
