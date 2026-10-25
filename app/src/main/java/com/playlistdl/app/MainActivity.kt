package com.playlistdl.app

import android.app.Activity
import android.os.Bundle
import android.graphics.Color
import android.view.Gravity
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.EditText
import android.widget.Button

class MainActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(32, 32, 32, 32)
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

        val urlInput = EditText(this).apply {
            hint = "لینک آهنگ یا پلی‌لیست"
            setSingleLine(true)
            setTextColor(Color.WHITE)
            setHintTextColor(Color.GRAY)
        }

        val format = TextView(this).apply {
            text = "فرمت: MP3 • کیفیت: 320 kbps"
            textSize = 15f
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
        }

        val downloadButton = Button(this).apply {
            text = "دانلود"
            setOnClickListener {
                android.widget.Toast.makeText(
                    this@MainActivity,
                    "قابلیت دانلود هنوز در حال آماده‌سازی است",
                    android.widget.Toast.LENGTH_LONG
                ).show()
            }
        }

        layout.addView(title)
        layout.addView(subtitle)
        layout.addView(urlInput)
        layout.addView(format)
        layout.addView(downloadButton)

        setContentView(layout)
    }
}
