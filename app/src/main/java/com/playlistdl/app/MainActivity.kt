package com.playlistdl.app

import android.app.Activity
import android.app.AlertDialog
import android.os.Bundle
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
            setOnClickListener {
                loadPlaylist()
            }
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
        layout.addView(
            View(this),
            LinearLayout.LayoutParams(1, height)
        )
    }

    private fun loadPlaylist() {
        val input = playlistInput.text.toString().trim()

        val id = Regex(
            """(?:playlist/|playlist:)([A-Za-z0-9]+)"""
        ).find(input)?.groupValues?.get(1)

        if (
            id == null ||
            !input.contains("spotify", ignoreCase = true)
        ) {
            playlistInput.error =
                "لینک معتبر پلی‌لیست Spotify وارد کن"
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
                        "استخراج ناموفق بود. دسترسی عمومی Spotify را بررسی کن."
                    return@runOnUiThread
                }

                statusText.text =
                    "${tracks.size} آهنگ استخراج شد"

                AlertDialog.Builder(this)
                    .setTitle("آهنگ‌های پلی‌لیست")
                    .setItems(tracks.toTypedArray()) { _, index ->
                        openYouTube(tracks[index])
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

        val connection =
            url.openConnection() as HttpURLConnection

        connection.connectTimeout = 15000
        connection.readTimeout = 15000
        connection.requestMethod = "GET"
        connection.setRequestProperty(
            "User-Agent",
            "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36"
        )

        try {
            val responseCode = connection.responseCode

            if (responseCode !in 200..299) {
                return emptyList()
            }

            val html = connection.inputStream
                .bufferedReader()
                .use { it.readText() }

            val results = linkedSetOf<String>()

            // تلاش برای خواندن عنوان آهنگ‌ها از داده‌های embed
            val patterns = listOf(
                """"trackList"\s*:\s*(\[[\s\S]*?\])""",
                """"tracks"\s*:\s*(\[[\s\S]*?\])"""
            )

            for (pattern in patterns) {
                val matcher = Pattern.compile(pattern).matcher(html)

                while (matcher.find() && results.size < 100) {
                    val section = matcher.group(1) ?: continue

                    val titleMatcher = Pattern.compile(
                        """"title"\s*:\s*"((?:\\.|[^"\\])*)""""
                    ).matcher(section)

                    while (titleMatcher.find() && results.size < 100) {
                        val raw = titleMatcher.group(1) ?: continue

                        val decoded = try {
                            JSONObject()
                                .put("value", raw)
                                .toString()
                                .let {
                                    JSONObject(it).getString("value")
                                }
                        } catch (_: Exception) {
                            raw
                        }

                        if (
                            decoded.isNotBlank() &&
                            !decoded.equals("undefined", true)
                        ) {
                            results.add(decoded)
                        }
                    }
                }

                if (results.isNotEmpty()) break
            }

            return results.toList()
        } finally {
            connection.disconnect()
        }
    }

    private fun openYouTube(track: String) {
        val query = URLEncoder.encode(track, "UTF-8")

        val url =
            "https://www.youtube.com/results?search_query=$query"

        startActivity(
            Intent(Intent.ACTION_VIEW, Uri.parse(url))
        )
    }
}
