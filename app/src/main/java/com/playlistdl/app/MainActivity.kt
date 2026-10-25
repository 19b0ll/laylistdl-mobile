package com.playlistdl.app

import android.app.Activity
import android.os.Bundle
import android.os.StrictMode
import android.graphics.Color
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.*
import android.content.Intent
import android.net.Uri
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.regex.Pattern

class MainActivity : Activity() {

    private lateinit var playlistInput: EditText
    private lateinit var statusText: TextView
    private lateinit var searchButton: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        StrictMode.setThreadPolicy(
            StrictMode.ThreadPolicy.Builder()
                .permitAll()
                .build()
        )

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
            text = "آهنگ‌های پلی‌لیست Spotify"
            textSize = 16f
            setTextColor(Color.LTGRAY)
            gravity = Gravity.CENTER
        }

        playlistInput = EditText(this).apply {
            hint = "لینک پلی‌لیست Spotify را وارد کن"
            setSingleLine(true)
            setTextColor(Color.WHITE)
            setHintTextColor(Color.GRAY)
        }

        searchButton = Button(this).apply {
            text = "استخراج آهنگ‌ها"
            setOnClickListener { loadPlaylist() }
        }

        statusText = TextView(this).apply {
            text = "آماده"
            textSize = 14f
            setTextColor(Color.LTGRAY)
            gravity = Gravity.CENTER
            setPadding(0, 24, 0, 0)
        }

        layout.addView(title)
        addSpace(layout, 16)
        layout.addView(subtitle)
        addSpace(layout, 20)
        layout.addView(playlistInput)
        addSpace(layout, 12)
        layout.addView(searchButton)
        layout.addView(statusText)

        val scroll = ScrollView(this)
        scroll.addView(
            layout,
            ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            )
        )
        setContentView(scroll)
    }

    private fun addSpace(layout: LinearLayout, height: Int) {
        layout.addView(View(this), LinearLayout.LayoutParams(1, height))
    }

    private fun loadPlaylist() {
        val input = playlistInput.text.toString().trim()

        val id = Regex(
            """(?:playlist/|playlist:)([A-Za-z0-9]+)"""
        ).find(input)?.groupValues?.get(1)

        if (id == null || !input.contains("spotify", true)) {
            playlistInput.error = "لینک معتبر پلی‌لیست Spotify وارد کن"
            return
        }

        searchButton.isEnabled = false
        statusText.text = "در حال دریافت آهنگ‌ها..."

        Thread {
            val tracks = try {
                fetchTracks(id)
            } catch (e: Exception) {
                emptyList()
            }

            runOnUiThread {
                searchButton.isEnabled = true

                if (tracks.isEmpty()) {
                    statusText.text =
                        "استخراج ناموفق بود. ممکن است Spotify دسترسی عمومی ندهد."
                    return@runOnUiThread
                }

                statusText.text = "${tracks.size} آهنگ استخراج شد"

                val names = tracks.toTypedArray()

                AlertDialog.Builder(this)
                    .setTitle("آهنگ‌های پلی‌لیست")
                    .setItems(names) { _, index ->
                        val query = URLEncoder.encode(
                            tracks[index],
                            "UTF-8"
                        )

                        val url =
                            "https://www.youtube.com/results?search_query=$query"

                        startActivity(
                            Intent(Intent.ACTION_VIEW, Uri.parse(url))
                        )
                    }
                    .setPositiveButton("بستن", null)
                    .show()
            }
        }.start()
    }

    private fun fetchTracks(playlistId: String): List<String> {
        val url = URL(
            "https://open.spotify.com/embed/playlist/$playlistId"
        )

        val connection = url.openConnection() as HttpURLConnection
        connection.connectTimeout = 15000
        connection.readTimeout = 15000
        connection.setRequestProperty(
            "User-Agent",
            "Mozilla/5.0"
        )

        try {
            val html = connection.inputStream
                .bufferedReader()
                .use { it.readText() }

            val results = linkedSetOf<String>()

            // Spotify embed may expose track metadata as JSON.
            val marker = "\"trackList\""
            val start = html.indexOf(marker)

            if (start >= 0) {
                val end = html.indexOf("\"tracks\"", start + marker.length)
                    .takeIf { it > start }
                    ?: minOf(html.length, start + 500000)

                val section = html.substring(start, end)

                val titlePattern = Pattern.compile(
                    "\"title\"\\s*:\\s*\"((?:\\\\.|[^\"\\\\])*)\""
                )

                val matcher = titlePattern.matcher(section)

                while (matcher.find() && results.size < 100) {
                    val raw = matcher.group(1) ?: continue

                    val decoded = try {
                        JSONObject("{\"v\":\"$raw\"}").getString("v")
                    } catch (_: Exception) {
                        raw
                    }

                    if (decoded.isNotBlank() &&
                        !decoded.equals("undefined", true)
                    ) {
                        results.add(decoded)
                    }
                }
            }

            return results.toList()
        } finally {
            connection.disconnect()
        }
    }
}
