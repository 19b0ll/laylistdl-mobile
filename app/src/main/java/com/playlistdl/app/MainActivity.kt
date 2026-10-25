
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
import android.content.Intent
import android.net.Uri
import java.net.URLEncoder

class MainActivity : Activity() {

    private lateinit var trackInput: EditText
    private lateinit var statusText: TextView

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
            text = "جست‌وجوی آهنگ‌ها در YouTube"
            textSize = 16f
            setTextColor(Color.LTGRAY)
            gravity = Gravity.CENTER
        }

        trackInput = EditText(this).apply {
            hint = "هر آهنگ را در یک خط وارد کن"
            setSingleLine(false)
            minLines = 4
            maxLines = 10
            gravity = Gravity.TOP
            setTextColor(Color.WHITE)
            setHintTextColor(Color.GRAY)
        }

        val help = TextView(this).apply {
            text = "اسم آهنگ و خواننده را وارد کن؛ برای هر خط جست‌وجوی YouTube ساخته می‌شود."
            textSize = 14f
            setTextColor(Color.LTGRAY)
            gravity = Gravity.CENTER
        }

        val searchButton = Button(this).apply {
            text = "پیدا کردن در YouTube"
            setOnClickListener { searchTracks() }
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
        addSpace(layout, 20)
        layout.addView(trackInput)
        addSpace(layout, 12)
        layout.addView(help)
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

    private fun searchTracks() {
        val tracks = trackInput.text.toString()
            .lines()
            .map { it.trim() }
            .filter { it.isNotEmpty() }
            .distinct()
            .take(100)

        if (tracks.isEmpty()) {
            trackInput.error = "اسم آهنگ‌ها را وارد کن"
            return
        }

        statusText.text =
            "${tracks.size} آهنگ پیدا شد. برای جست‌وجو انتخاب کن:"

        android.app.AlertDialog.Builder(this)
            .setTitle("آهنگ‌ها")
            .setItems(tracks.toTypedArray()) { _, index ->
                val query = URLEncoder.encode(
                    tracks[index],
                    "UTF-8"
                )

                val url =
                    "https://www.youtube.com/results?search_query=$query"

                startActivity(
                    Intent(
                        Intent.ACTION_VIEW,
                        Uri.parse(url)
                    )
                )
            }
            .setPositiveButton("بستن", null)
            .show()
    }
}
