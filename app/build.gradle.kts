plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.playlistdl.app"
    compileSdk = 35

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    defaultConfig {
        applicationId = "com.playlistdl.app"
        minSdk = 26
        targetSdk = 35
        versionCode = 2
        versionName = "1.1"
    }
}

dependencies {
    implementation("dev.ffmpegkit-maintained:yt-dlp-android:2.0.2")
}