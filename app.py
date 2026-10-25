import os, re, json, glob, shutil, uuid, hashlib, tempfile, threading, zipfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from datetime import datetime
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, send_file
from yt_dlp import YoutubeDL

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except Exception:
    spotipy = None
    SpotifyClientCredentials = None

try:
    from mutagen.id3 import (ID3, TIT2, TPE1, TPE2, TALB, TRCK, TDRC, TCON, USLT, APIC,
                             ID3NoHeaderError)
    from mutagen.mp3 import MP3
except Exception:
    ID3 = TIT2 = TPE1 = TPE2 = TALB = TRCK = TDRC = TCON = USLT = APIC = MP3 = None
    ID3NoHeaderError = Exception

BASE = os.path.expanduser("~/storage/downloads")
if not os.path.isdir(BASE):
    BASE = os.path.expanduser("~/downloads")
if not os.path.isdir(BASE):
    BASE = os.path.expanduser("~/Download")
OUT = os.path.join(BASE, "PlaylistDL")
os.makedirs(OUT, exist_ok=True)
HISTORY_FILE = os.path.join(OUT, ".playlistdl_history.json")
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".playlistdl_settings.json")
JOBS_FILE = os.path.join(os.path.expanduser("~"), ".playlistdl_jobs.json")
DEFAULT_SETTINGS = {
    "output_dir": OUT,
    "theme_intensity": 55,
    "accent_color": "#39ff88",
    "accent_secondary": "#138a4a",
    "background_color": "#050807",
    "concurrency": 1,
    "auto_retries": 3,
    "audio_format": "mp3",
    "audio_quality": "320",
    "theme_preset": "ultramarine",
    "filename_template": "{index:04d} - {title}",
    "fetch_tags": True,
    "album_mode": "original",
    "embed_lyrics": True
}

app = Flask(__name__)
PREVIEWS = {}
JOBS = {}
LOCK = threading.RLock()
_last_job_save = 0.0
_sp = None
PAUSE_EVENTS = {}
CANCEL_EVENTS = {}
JOB_QUEUE = Queue()


def _atomic_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def get_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    settings = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        settings.update({k: v for k, v in raw.items() if k in settings})
    try:
        settings["theme_intensity"] = max(0, min(100, int(settings["theme_intensity"])))
    except (TypeError, ValueError):
        settings["theme_intensity"] = DEFAULT_SETTINGS["theme_intensity"]
    try:
        settings["concurrency"] = max(1, min(3, int(settings["concurrency"])))
    except (TypeError, ValueError):
        settings["concurrency"] = 1
    try:
        settings["auto_retries"] = 3
    except (TypeError, ValueError):
        settings["auto_retries"] = 3
    for key, default in (("accent_color", "#39ff88"), ("accent_secondary", "#138a4a"), ("background_color", "#050807")):
        value = str(settings.get(key, default)).lower()
        if not re.fullmatch(r"#[0-9a-f]{6}", value):
            value = default
        settings[key] = value
    # Fixed download format and quality; these are no longer user-configurable.
    settings["audio_format"] = "mp3"
    settings["audio_quality"] = "320"
    settings["concurrency"] = 1
    settings["fetch_tags"] = True
    settings["embed_lyrics"] = True
    settings["theme_preset"] = str(settings.get("theme_preset", "custom"))[:30]
    settings["output_dir"] = OUT  # Fixed output folder; no user-configurable path.
    settings["auto_retries"] = 3
    settings["filename_template"] = DEFAULT_SETTINGS["filename_template"]  # Fixed four-digit playlist ordering.
    settings["fetch_tags"] = True
    settings["embed_lyrics"] = True
    settings["album_mode"] = "original"  # Always preserve the real album metadata.
    return settings


def save_settings(settings):
    _atomic_json(SETTINGS_FILE, settings)


def persist_jobs(force=False):
    global _last_job_save
    now = time.time()
    if not force and now - _last_job_save < 1.2:
        return
    with LOCK:
        try:
            snapshot = {k: v for k, v in JOBS.items()}
            _atomic_json(JOBS_FILE, snapshot)
            _last_job_save = now
        except Exception:
            pass


def restore_jobs():
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            return
        for jid, job in saved.items():
            if not isinstance(job, dict):
                continue
            if not job.get("finished"):
                job["finished"] = True
                job["state"] = "متوقف‌شده با خاموشی سرور"
                job["finished_at"] = time.time()
                job["error"] = "سرور در زمان دانلود متوقف شده بود؛ برای آهنگ‌های ناموفق تلاش مجدد را بزن."
                source_tracks = job.get("source_tracks", [])
                failed_keys = {x.get("label") for x in job.get("failed", []) if isinstance(x, dict)}
                for i, t in enumerate(job.get("tracks", [])):
                    if t.get("state") in ("در صف", "در حال دانلود"):
                        t["state"] = "نیازمند تلاش مجدد"
                        if i < len(source_tracks) and t.get("label") not in failed_keys:
                            item = source_tracks[i]
                            job.setdefault("failed", []).append({"label": item.get("label"), "target": item.get("target"),
                                "cover": item.get("cover"), "source_url": item.get("source_url"), "error": "دانلود در اثر توقف سرور کامل نشد"})
            JOBS[jid] = job
    except (OSError, ValueError):
        return

restore_jobs()


def get_sp():
    global _sp
    if spotipy is None or SpotifyClientCredentials is None:
        raise RuntimeError("کتابخانه spotipy نصب نیست؛ یا از لینک عمومی استفاده کن یا requirements.txt را نصب کن")
    if _sp is None:
        client_id = os.environ.get("SPOTIFY_ID")
        client_secret = os.environ.get("SPOTIFY_SECRET")
        if not client_id or not client_secret:
            raise RuntimeError("Spotify API credentials تنظیم نشده‌اند")
        _sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
            client_id=client_id, client_secret=client_secret))
    return _sp


def safe(name):
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(name or "")).strip(" .")
    return cleaned[:100] or "playlist"


def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_history(data):
    _atomic_json(HISTORY_FILE, data)


def track_key(track):
    # Exact source key remains compatible with older history files.
    raw = (track.get("target", "") + "\n" + track.get("label", "")).strip().lower()
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


def normalized_label_key(track):
    label = str(track.get("label", "")).casefold()
    label = re.sub(r"\[[^]]*\]|\([^)]*(?:official|audio|video|lyrics?)[^)]*\)", " ", label)
    label = re.sub(r"[^\w\s]", " ", label, flags=re.UNICODE)
    label = re.sub(r"\b(official|audio|video|lyrics?|hd|4k|visualizer|remaster(?:ed)?)\b", " ", label)
    label = re.sub(r"\s+", " ", label).strip()
    return hashlib.sha256(label.encode("utf-8", "ignore")).hexdigest() if label else ""


def make_track(label, target, cover=None, source_url=None, meta=None):
    track = _make_track_base(label, target, cover, source_url)
    clean = {k: v for k, v in (meta or {}).items() if v}
    if clean:
        track["meta"] = clean
    return track


def _make_track_base(label, target, cover=None, source_url=None):
    label = re.sub(r"\s+", " ", str(label or "آهنگ نامشخص")).strip()
    return {"label": label, "target": target, "cover": cover, "source_url": source_url or target,
            "key": hashlib.sha256((str(target) + "\n" + label).lower().encode("utf-8", "ignore")).hexdigest()}


def spotify_api_tracks(url):
    s = get_sp()
    match = re.search(r"/(playlist|album|track)/([A-Za-z0-9]+)", url)
    if not match:
        raise ValueError("لینک Spotify معتبر نیست")
    kind, sid = match.group(1), match.group(2)
    if kind == "playlist":
        entity = s.playlist(sid, fields="name,images")
        name = entity.get("name") or "Spotify playlist"
        images = entity.get("images") or []
        cover = images[0].get("url") if images else None
        res = s.playlist_items(sid)
        entries = []
        while res:
            entries.extend(res.get("items", []))
            res = s.next(res) if res.get("next") else None
        raw_tracks = [x.get("track") for x in entries if x.get("track")]
    elif kind == "album":
        entity = s.album(sid)
        name = entity.get("name") or "Spotify album"
        images = entity.get("images") or []
        cover = images[0].get("url") if images else None
        res = s.album_tracks(sid)
        raw_tracks = []
        while res:
            raw_tracks.extend(res.get("items", []))
            res = s.next(res) if res.get("next") else None
        for t in raw_tracks:
            t.setdefault("album", {"images": images})
    else:
        t = s.track(sid)
        name = t.get("name") or "Spotify track"
        images = ((t.get("album") or {}).get("images") or [])
        cover = images[0].get("url") if images else None
        raw_tracks = [t]
    out = []
    for t in raw_tracks:
        if not t:
            continue
        artists = t.get("artists") or []
        artist = artists[0].get("name", "") if artists else ""
        title = t.get("name", "")
        label = f"{artist} - {title}".strip(" -")
        album_images = ((t.get("album") or {}).get("images") or [])
        track_cover = album_images[0].get("url") if album_images else cover
        alb = t.get("album") or {}
        meta = {"album": alb.get("name"), "year": str(alb.get("release_date") or "") or None,
                "track_no": t.get("track_number")}
        out.append(make_track(label, f"ytsearch1:{label}", track_cover, meta=meta))
    return name, out, cover


def spotify_embed_tracks(url):
    m = re.search(r"/(playlist|album|track)/([A-Za-z0-9]+)", url)
    if not m:
        raise ValueError("لینک عمومی Spotify از نوع playlist، album یا track وارد کن")
    kind, sid = m.group(1), m.group(2)
    response = requests.get(f"https://open.spotify.com/embed/{kind}/{sid}",
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    response.raise_for_status()
    script = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', response.text, re.S)
    if not script:
        raise ValueError("نتونستم اطلاعات Spotify رو بخونم؛ ممکنه ساختار صفحه تغییر کرده یا محتوا محدود باشه")
    try:
        entity = json.loads(script.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError, ValueError):
        raise ValueError("ساختار اطلاعات Spotify تغییر کرده؛ بعداً دوباره امتحان کن")
    name = entity.get("name") or entity.get("title") or "Spotify"
    imgs = entity.get("visualIdentity") or entity.get("images") or []
    cover = imgs[0].get("url") if isinstance(imgs, list) and imgs and isinstance(imgs[0], dict) else None
    entries = entity.get("trackList") or []
    if not entries and entity.get("title"):
        entries = [{"title": entity.get("title"), "subtitle": entity.get("subtitle"), "imageUrl": cover}]
    out = []
    for t in entries:
        label = f'{t.get("subtitle", "")} - {t.get("title", "")}'.strip(" -")
        if label:
            out.append(make_track(label, f"ytsearch1:{label}", t.get("imageUrl") or cover))
    return name, out, cover


def spotify_tracks(url):
    if os.environ.get("SPOTIFY_ID") and os.environ.get("SPOTIFY_SECRET") and spotipy is not None:
        try:
            return spotify_api_tracks(url)
        except Exception:
            pass
    return spotify_embed_tracks(url)


def apple_tracks(url):
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    response.raise_for_status()
    html = response.text
    image_match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, re.I)
    cover = image_match.group(1) if image_match else None
    name_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I)
    name = name_match.group(1) if name_match else "Apple Music playlist"
    out = []
    pattern = r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>'
    for block in re.findall(pattern, html, re.S | re.I):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        entries = data if isinstance(data, list) else [data]
        for item in entries:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or name
            tracks = item.get("track", [])
            if isinstance(tracks, dict):
                tracks = tracks.get("itemListElement", [])
            for t in tracks if isinstance(tracks, list) else []:
                if isinstance(t, dict) and isinstance(t.get("item"), dict):
                    t = t["item"]
                if not isinstance(t, dict):
                    continue
                artist_data = t.get("byArtist")
                if isinstance(artist_data, list):
                    artist_data = artist_data[0] if artist_data else {}
                artist = artist_data.get("name", "") if isinstance(artist_data, dict) else ""
                label = f'{artist} - {t.get("name", "")}'.strip(" -")
                out.append(make_track(label, f"ytsearch1:{label}", t.get("image") or cover))
    # Apple pages often omit track JSON-LD; yt-dlp can still extract public playlist entries.
    if not out:
        try:
            name2, tracks2, cover2 = ytdlp_playlist_tracks(url)
            if tracks2:
                return name2 or name, tracks2, cover2 or cover
        except Exception:
            pass
    return name or "Apple Music playlist", out, cover


def ytdlp_playlist_tracks(url):
    opts = {"extract_flat": True, "quiet": True, "no_warnings": True, "ignoreerrors": True,
            "skip_download": True, "playlistend": 500}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise ValueError("سرویس نتونست اطلاعات لینک رو بخونه")
    title = info.get("title") or info.get("playlist_title") or "Playlist"
    cover = info.get("thumbnail")
    entries = info.get("entries")
    if entries is None:
        label = info.get("title") or url
        return title, [make_track(label, url, info.get("thumbnail"), url)], cover
    out = []
    for entry in entries:
        if not entry:
            continue
        entry_url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
        if entry.get("id") and not (entry_url and entry_url.startswith("http")):
            extractor = (entry.get("ie_key") or info.get("extractor_key") or "").lower()
            if "soundcloud" in extractor:
                entry_url = f'https://soundcloud.com/{entry.get("uploader_id", "")}/{entry.get("id")}'
            elif "youtube" in extractor or "youtube" in str(entry.get("url", "")):
                entry_url = f'https://www.youtube.com/watch?v={entry["id"]}'
        entry_url = entry_url or url
        label = entry.get("title") or entry_url
        out.append(make_track(label, entry_url, entry.get("thumbnail") or cover, entry_url))
    return title, out, cover or (out[0].get("cover") if out else None)


def get_tracks(text):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        raise ValueError("لینک یا نام آهنگ وارد نشده")
    if len(lines) > 1 or not lines[0].lower().startswith(("http://", "https://")):
        name = "لیست آهنگ‌ها"
        return name, [make_track(line, f"ytsearch1:{line}") for line in lines], None
    url = lines[0]
    host = (urlparse(url).hostname or "").lower()
    if host == "open.spotify.com" and re.search(r"/(playlist|album|track)/", url):
        return spotify_tracks(url)
    if host == "music.apple.com":
        return apple_tracks(url)
    if host == "soundcloud.com" or host.endswith(".soundcloud.com"):
        return ytdlp_playlist_tracks(url)
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be") or host.endswith("youtube.com"):
        return ytdlp_playlist_tracks(url)
    raise ValueError("این لینک پشتیبانی نمی‌شه؛ لینک عمومی Spotify، Apple Music، YouTube یا SoundCloud رو وارد کن")


def is_supported(url):
    host = (urlparse(url).hostname or "").lower()
    return (host == "open.spotify.com" or host == "music.apple.com" or host.endswith("youtube.com")
            or host == "youtu.be" or host == "soundcloud.com" or host.endswith(".soundcloud.com"))


UA = {"User-Agent": "PlaylistDL/1.0 (personal use)"}
_NET_LOCK = threading.Lock()
_LAST_CALL = {}
_META_CACHE = {}
_LYRICS_CACHE = {}


def _throttle(name, gap):
    """Space out calls to each free web service so we stay within its rate limits."""
    with _NET_LOCK:
        wait = gap - (time.time() - _LAST_CALL.get(name, 0.0))
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[name] = time.time()


def split_label(label):
    label = str(label or "").strip()
    if " - " in label:
        artist, title = label.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", label


def _norm(text):
    text = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", str(text or "").casefold())
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _contains(needle, haystack):
    """True when the normalized phrase `needle` appears as whole words inside `haystack`."""
    return bool(needle) and re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack) is not None


def _same_track(q_artist, q_title, c_artist, c_title):
    """Accept a web result only when it clearly is the same song (never guess)."""
    qt, ct = _norm(q_title), _norm(c_title)
    if not qt or not ct:
        return False
    if not (qt == ct or _contains(ct, qt) or _contains(qt, ct)):
        return False
    qa, ca = _norm(q_artist), _norm(c_artist)
    if qa:
        q_tokens = {w for w in qa.split() if len(w) > 1}
        c_tokens = {w for w in ca.split() if len(w) > 1}
        return bool(q_tokens & c_tokens) or _contains(qa, ca) or _contains(ca, qa)
    return qt == ct or (_contains(ca, qt) and _contains(ct, qt))


def _itunes_meta(artist, title):
    _throttle("itunes", 3.0)
    resp = requests.get("https://itunes.apple.com/search", headers=UA, timeout=15,
                        params={"term": f"{artist} {title}".strip(), "media": "music",
                                "entity": "song", "limit": 8})
    resp.raise_for_status()
    for item in resp.json().get("results", []):
        if not _same_track(artist, title, item.get("artistName"), item.get("trackName")):
            continue
        art = item.get("artworkUrl100") or ""
        return {"artist": item.get("artistName"), "album": item.get("collectionName"),
                "year": str(item.get("releaseDate") or "")[:10] or None,
                "genre": item.get("primaryGenreName"),
                "track_no": item.get("trackNumber"), "track_count": item.get("trackCount"),
                "cover": re.sub(r"/\d+x\d+bb", "/600x600bb", art) if art else None}
    return {}


def _musicbrainz_meta(artist, title):
    def esc(value):
        return str(value).replace("\\", "\\\\").replace('"', '\\"')
    query = f'recording:"{esc(title)}"' + (f' AND artist:"{esc(artist)}"' if artist else "")
    _throttle("musicbrainz", 1.2)
    resp = requests.get("https://musicbrainz.org/ws/2/recording", headers=UA, timeout=15,
                        params={"query": query, "fmt": "json", "limit": 5})
    resp.raise_for_status()
    for rec in resp.json().get("recordings", []):
        credit = " ".join(c.get("name", "") for c in rec.get("artist-credit", []) if isinstance(c, dict)).strip()
        if not _same_track(artist, title, credit, rec.get("title")):
            continue
        rel = (rec.get("releases") or [{}])[0]
        date = str(rel.get("date") or rec.get("first-release-date") or "")
        tags = sorted(rec.get("tags") or [], key=lambda t: -int(t.get("count", 0) or 0))
        return {"artist": credit or None, "album": rel.get("title"), "year": date[:10] or None,
                "genre": str(tags[0].get("name", "")).title() if tags else None}
    return {}


def fetch_online_meta(artist, title):
    """Release date / genre / album from iTunes, with MusicBrainz as a fallback."""
    key = (_norm(artist), _norm(title))
    if key in _META_CACHE:
        return _META_CACHE[key]
    merged, had_error = {}, False
    for lookup in (_itunes_meta, _musicbrainz_meta):
        try:
            found = lookup(artist, title) or {}
        except Exception:
            found, had_error = {}, True
        for k, v in found.items():
            if v and not merged.get(k):
                merged[k] = v
        if merged.get("album") and merged.get("year") and merged.get("genre"):
            break
    if merged or not had_error:
        _META_CACHE[key] = merged
    return merged


def _plain_from_synced(text):
    lines = [re.sub(r"^(?:\[[^\]]*\]\s*)+", "", ln).strip() for ln in str(text or "").splitlines()]
    return "\n".join(ln for ln in lines if ln)


def fetch_lyrics(artist, title, duration=None):
    """Best-effort lyrics lookup; returns (plain lyrics, synced LRC), either may be None."""
    key = (_norm(artist), _norm(title))
    if key in _LYRICS_CACHE:
        return _LYRICS_CACHE[key]
    queries = []
    if artist:
        queries.append({"track_name": title, "artist_name": artist})
    queries.append({"track_name": title})
    queries.append({"q": f"{artist} {title}".strip()})
    records_by_id = {}
    for params in queries:
        for attempt in range(2):
            try:
                _throttle("lrclib", 1.0)
                resp = requests.get("https://lrclib.net/api/search", params=params, headers=UA, timeout=20)
                if (resp.status_code == 429 or resp.status_code >= 500) and attempt == 0:
                    try:
                        delay = min(5.0, max(1.0, float(resp.headers.get("Retry-After", "1"))))
                    except (TypeError, ValueError):
                        delay = 1.0
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            records_by_id[item.get("id", (item.get("artistName"), item.get("trackName")))] = item
                break
            except Exception:
                if attempt == 0:
                    time.sleep(1.0)
                else:
                    break
    best_plain = best_synced = None
    for item in records_by_id.values():
        if item.get("instrumental"):
            continue
        if not _same_track(artist, title, item.get("artistName"), item.get("trackName")):
            continue
        diff = 0.0
        if duration and item.get("duration"):
            try:
                diff = abs(float(item["duration"]) - float(duration))
            except (TypeError, ValueError):
                diff = 0.0
            if diff > 25:
                continue
        synced = str(item.get("syncedLyrics") or "").strip()
        plain = str(item.get("plainLyrics") or "").strip() or _plain_from_synced(synced)
        if plain and (best_plain is None or diff < best_plain[0]):
            best_plain = (diff, plain)
        if synced and (best_synced is None or diff < best_synced[0]):
            best_synced = (diff, synced)
    result = (best_plain[1] if best_plain else None, best_synced[1] if best_synced else None)
    _LYRICS_CACHE[key] = result
    return result


def write_lrc(audio_path, synced_text):
    """Write synced lyrics beside an audio file using the same basename."""
    text = str(synced_text or "").strip()
    if not text:
        return False
    # Accept only timestamped lines so plain lyrics are never mislabeled as synced.
    timestamped = [line for line in text.splitlines() if re.search(r"\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]", line)]
    if not timestamped:
        return False
    lrc_path = os.path.splitext(audio_path)[0] + ".lrc"
    with open(lrc_path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write("\n".join(timestamped).strip() + "\n")
    return True


def lyrics_for(track, audio_path, fallback_artist=""):
    artist, title = split_label(track.get("label", ""))
    length = None
    if MP3 is not None:
        try:
            length = MP3(audio_path).info.length
        except Exception:
            pass
    return fetch_lyrics(artist or fallback_artist or "", title or track.get("label", ""), length)


def embed_metadata(mp3_path, track, playlist_name, index, settings=None):
    if ID3 is None:
        return False
    settings = settings or {}
    settings["fetch_tags"] = True
    settings["embed_lyrics"] = True
    try:
        label = track.get("label", "")
        artist, title = split_label(label)
        meta = dict(track.get("meta") or {})
        if settings.get("fetch_tags", True):
            for k, v in fetch_online_meta(artist, title).items():
                if v and not meta.get(k):
                    meta[k] = v
        try:
            tags = ID3(mp3_path)
        except ID3NoHeaderError:
            tags = ID3()
        for frame_id in ("TIT2", "TPE1", "TPE2", "TALB", "TRCK", "TDRC", "TCON", "USLT", "APIC"):
            tags.delall(frame_id)
        # Always write useful baseline tags, even when online metadata providers fail.
        # Use explicit fallbacks instead of leaving artist/album/genre blank in Android players.
        clean_title = (title or label or os.path.splitext(os.path.basename(mp3_path))[0]).strip() or "Unknown Title"
        main_artist = str(meta.get("artist") or artist or track.get("artist") or "Unknown Artist").strip() or "Unknown Artist"
        tags.add(TIT2(encoding=3, text=clean_title))
        tags.add(TPE1(encoding=3, text=main_artist))
        tags.add(TPE2(encoding=3, text=main_artist))
        if settings.get("album_mode", "original") == "original" and meta.get("album"):
            album = str(meta["album"]).strip()
            number, count = meta.get("track_no"), meta.get("track_count")
            track_text = f"{number}/{count}" if number and count else (str(number) if number else str(index))
        else:
            album = str(playlist_name or "PlaylistDL").strip()
            track_text = str(index)
        tags.add(TALB(encoding=3, text=album or clean_title))
        tags.add(TRCK(encoding=3, text=track_text))
        if meta.get("year"):
            tags.add(TDRC(encoding=3, text=str(meta["year"])))
        # Never invent a genre. Use the provider value, otherwise mark it as unknown.
        genre = str(meta.get("genre") or track.get("genre") or "Unknown").strip() or "Unknown"
        tags.add(TCON(encoding=3, text=genre))
        cover_url = track.get("cover") or meta.get("cover")
        if cover_url:
            try:
                resp = requests.get(cover_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                if content_type in ("image/jpeg", "image/png", "image/webp"):
                    tags.add(APIC(encoding=3, mime=content_type, type=3, desc="Cover", data=resp.content))
            except Exception:
                pass
        # Embed plain lyrics in the MP3 only; never create separate .lrc files.
        plain, _synced = lyrics_for(track, mp3_path, main_artist)
        if plain:
            tags.add(USLT(encoding=3, lang="XXX", desc="", text=plain))
        tags.save(mp3_path, v2_version=3)
        return True
    except Exception:
        return False


def _update_aggregate(job):
    total = max(int(job.get("total") or 1), 1)
    progress = job.get("track_progress", [])
    done = int(job.get("done") or 0)
    active_fraction = sum(max(0.0, min(0.999, float(x or 0))) for x in progress)
    job["overall_percent"] = min(100, round((done + active_fraction) * 100 / total))
    job["speed"] = int(sum(job.get("track_speeds", []) or []))
    etas = [int(x) for x in (job.get("track_etas", []) or []) if isinstance(x, (int, float)) and x >= 0]
    job["eta"] = max(etas) if etas else None


class DownloadCancelled(Exception):
    """Raised by a yt-dlp progress hook when the user cancels a job."""


def wait_for_job(jid):
    """Wait through a pause without making cancellation wait for a resume."""
    pause_event = PAUSE_EVENTS.get(jid)
    cancel_event = CANCEL_EVENTS.get(jid)
    while pause_event is not None and not pause_event.is_set():
        if cancel_event is not None and cancel_event.is_set():
            return False
        time.sleep(0.2)
    return not (cancel_event is not None and cancel_event.is_set())


def download_one(track, folder, job, index, total):
    os.makedirs(folder, exist_ok=True)
    def hook(data):
        cancel_event = CANCEL_EVENTS.get(job.get("id"))
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("دانلود به درخواست کاربر لغو شد")
        pause_event = PAUSE_EVENTS.get(job.get("id"))
        while pause_event is not None and not pause_event.is_set():
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("دانلود به درخواست کاربر لغو شد")
            time.sleep(0.2)
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("دانلود به درخواست کاربر لغو شد")
        status = data.get("status")
        with LOCK:
            if status == "downloading":
                downloaded = data.get("downloaded_bytes") or 0
                size = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                frac = min(0.999, downloaded / size) if size else 0
                job["track_progress"][index - 1] = frac
                job["track_speeds"][index - 1] = int(data.get("speed") or 0)
                job["track_etas"][index - 1] = data.get("eta")
                job["tracks"][index - 1]["state"] = "در حال دانلود"
                job["current"] = job["tracks"][index - 1]["label"]
            elif status == "finished":
                job["track_progress"][index - 1] = 0.999
            _update_aggregate(job)
        persist_jobs()
    settings = get_settings()
    audio_format = settings.get("audio_format", "mp3")
    audio_quality = settings.get("audio_quality", "192")
    opts = {
        "format": "bestaudio/best", "outtmpl": os.path.join(folder, "source.%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True, "ignoreerrors": False,
        "progress_hooks": [hook], "writethumbnail": False,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": audio_format, "preferredquality": audio_quality}],
        "retries": 3, "fragment_retries": 3, "extractor_retries": 3, "file_access_retries": 3,
        "socket_timeout": 25, "continuedl": True, "overwrites": True,
    }
    with YoutubeDL(opts) as ydl:
        result = ydl.download([track["target"]])
    files = [p for p in glob.glob(os.path.join(folder, "*")) if p.lower().endswith("." + audio_format) and os.path.isfile(p)]
    if result not in (None, 0) or not files:
        raise RuntimeError(f"فایل {audio_format.upper()} کامل ساخته نشد؛ اتصال، FFmpeg یا دسترسی منبع رو بررسی کن")
    mp3_path = max(files, key=os.path.getmtime)
    if os.path.getsize(mp3_path) < 1024:
        raise RuntimeError("فایل خروجی ناقص یا بیش از حد کوچک است؛ به‌عنوان دانلود موفق ثبت نشد")
    if audio_format == "mp3":
        embed_metadata(mp3_path, track, job.get("playlist", "PlaylistDL"), index, settings)
        # Automatically create a matching sidecar only when synced lyrics are available.
        # No LRC setting is exposed in the UI; the MP3 itself remains the only audio file.
        try:
            _plain, synced = lyrics_for(track, mp3_path)
            if synced:
                write_lrc(mp3_path, synced)
        except Exception:
            pass
    return mp3_path


def output_filename(track, playlist, index, template, extension="mp3"):
    label = str(track.get("label") or "آهنگ")
    if " - " in label:
        artist, title = label.split(" - ", 1)
    else:
        artist, title = "", label
    try:
        name = template.format(index=index, artist=artist.strip(), title=title.strip(), playlist=playlist)
    except (KeyError, ValueError, IndexError):
        name = f"{index:04d} - {title}"
    name = re.sub(r"\s-\s{2,}-\s", " - ", name)
    return safe(name) + "." + (extension if extension in ("mp3", "m4a") else "mp3")


def run_job(jid, tracks, playlist, output_mode, retry_mode=False):
    job = JOBS[jid]
    settings = get_settings()
    folder = os.path.join(settings["output_dir"], safe(playlist))
    os.makedirs(folder, exist_ok=True)
    with LOCK:
        job.update(playlist=playlist, folder=folder, output_mode=output_mode, total=len(tracks), done=0,
                   saved=0, failed=[], finished=False, state="در حال دانلود", started_at=time.time(), finished_at=None,
                   current="", speed=0, eta=None, track_percent=0,
                   overall_percent=0, tracks=[{"label": t["label"], "cover": t.get("cover"), "state": "در صف"} for t in tracks],
                   source_tracks=tracks, saved_paths=[], track_progress=[0.0] * len(tracks),
                   track_speeds=[0] * len(tracks), track_etas=[None] * len(tracks), zip_path=None)
    persist_jobs(force=True)

    def one(index, track):
        label = track.get("label", "آهنگ")
        cancel_event = CANCEL_EVENTS.get(jid)
        if cancel_event is not None and cancel_event.is_set():
            with LOCK:
                job["tracks"][index - 1]["state"] = "لغو شد"
                job["done"] += 1
            return
        if not wait_for_job(jid):
            with LOCK:
                job["tracks"][index - 1]["state"] = "لغو شد"
                job["done"] += 1
            return
        history = load_history()
        previous = history.get(track_key(track))
        if not previous:
            nkey = normalized_label_key(track)
            previous = next((v for v in history.values() if nkey and normalized_label_key({"label": v.get("label", "")}) == nkey), None)
        if previous and previous.get("path") and os.path.isfile(previous["path"]):
            with LOCK:
                job["saved"] += 1
                job["saved_paths"].append(previous["path"])
                job["tracks"][index - 1]["state"] = "تکراری؛ از کتابخانه استفاده شد"
                job["done"] += 1
                job["track_progress"][index - 1] = 0
                _update_aggregate(job)
            persist_jobs()
            return
        max_attempts = 1 + settings["auto_retries"]
        last_error = None
        for attempt in range(1, max_attempts + 1):
            temp = tempfile.mkdtemp(prefix="playlistdl_track_")
            try:
                if not wait_for_job(jid):
                    last_error = None
                    with LOCK:
                        job["tracks"][index - 1]["state"] = "لغو شد"
                    break
                with LOCK:
                    job["tracks"][index - 1]["state"] = "در حال دانلود" if attempt == 1 else f"تلاش مجدد {attempt - 1}"
                    job["current"] = label
                persist_jobs()
                mp3 = download_one(track, temp, job, index, len(tracks))
                dest_name = output_filename(track, playlist, index, settings["filename_template"], settings.get("audio_format", "mp3"))
                dest = os.path.join(folder, dest_name)
                counter = 2
                base, ext = os.path.splitext(dest_name)
                while os.path.exists(dest):
                    dest = os.path.join(folder, f"{base} ({counter}){ext}")
                    counter += 1
                shutil.move(mp3, dest)
                # Keep the synchronized lyrics paired with the final MP3 basename.
                temp_lrc = os.path.splitext(mp3)[0] + ".lrc"
                dest_lrc = os.path.splitext(dest)[0] + ".lrc"
                if os.path.isfile(temp_lrc):
                    try:
                        shutil.move(temp_lrc, dest_lrc)
                    except OSError:
                        pass
                update_history(track, dest, playlist)
                with LOCK:
                    job["saved"] += 1
                    job["saved_paths"].append(dest)
                    job["tracks"][index - 1]["state"] = "دانلود شد"
                    job["track_progress"][index - 1] = 0
                last_error = None
                break
            except Exception as exc:
                cancel_event = CANCEL_EVENTS.get(jid)
                if isinstance(exc, DownloadCancelled) or (cancel_event is not None and cancel_event.is_set()):
                    last_error = None
                    with LOCK:
                        job["tracks"][index - 1]["state"] = "لغو شد"
                        job["track_progress"][index - 1] = 0
                        job["track_speeds"][index - 1] = 0
                        job["track_etas"][index - 1] = None
                    persist_jobs()
                    break
                last_error = str(exc)[:220]
                with LOCK:
                    job["tracks"][index - 1]["state"] = f"خطا؛ تلاش {attempt}/{max_attempts}"
                    job["track_progress"][index - 1] = 0
                    job["track_speeds"][index - 1] = 0
                    job["track_etas"][index - 1] = None
                persist_jobs()
            finally:
                shutil.rmtree(temp, ignore_errors=True)
            if last_error and attempt < max_attempts:
                time.sleep(min(attempt * 1.5, 5))
        with LOCK:
            cancel_event = CANCEL_EVENTS.get(jid)
            was_cancelled = cancel_event is not None and cancel_event.is_set()
            if last_error and not was_cancelled:
                job["failed"].append({"label": label, "target": track.get("target"), "cover": track.get("cover"),
                                      "source_url": track.get("source_url"), "error": last_error})
                job["tracks"][index - 1]["state"] = "ناموفق؛ تلاش مجدد دستی ممکن است"
            elif was_cancelled:
                job["tracks"][index - 1]["state"] = "لغو شد"
            job["done"] += 1
            job["track_progress"][index - 1] = 0
            job["track_speeds"][index - 1] = 0
            job["track_etas"][index - 1] = None
            _update_aggregate(job)
        persist_jobs()

    with ThreadPoolExecutor(max_workers=settings["concurrency"]) as pool:
        futures = [pool.submit(one, i, track) for i, track in enumerate(tracks, 1)]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                with LOCK:
                    job.setdefault("error", str(exc)[:220])

    if output_mode == "zip" and job["saved_paths"]:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = os.path.join(settings["output_dir"], f"{safe(playlist)}_{stamp}.zip")
        temp_zip = zip_path + ".part"
        try:
            with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in job["saved_paths"]:
                    if os.path.isfile(path) and os.path.getsize(path) > 0:
                        zf.write(path, arcname=os.path.basename(path))
            if os.path.getsize(temp_zip) < 22:
                raise RuntimeError("فایل ZIP معتبر ساخته نشد")
            os.replace(temp_zip, zip_path)
            job["zip_path"] = zip_path
        except Exception as exc:
            try: os.remove(temp_zip)
            except OSError: pass
            job["error"] = f"آهنگ‌ها ذخیره شدند اما ZIP ساخته نشد: {exc}"
    with LOCK:
        cancelled = bool(job.get("cancel_requested") or (CANCEL_EVENTS.get(jid) and CANCEL_EVENTS[jid].is_set()))
        job["finished"] = True
        job["paused"] = False
        job["current"] = ""
        job["speed"] = 0
        job["eta"] = None
        job["cancelled"] = cancelled
        job["state"] = "لغو شد" if cancelled else ("پایان با خطا" if job.get("failed") else "تمام‌شده")
        job["finished_at"] = time.time()
        if cancelled:
            job["overall_percent"] = int(job.get("cancelled_percent", job.get("overall_percent", 0)) or 0)
            job["error"] = "دانلود به درخواست کاربر لغو شد"
        else:
            job["overall_percent"] = 100 if not job.get("failed") else round(job["done"] * 100 / max(job["total"], 1))
    persist_jobs(force=True)


def _job_queue_worker():
    while True:
        item = JOB_QUEUE.get()
        try:
            jid, tracks, playlist, output_mode, retry_mode = item
            job = JOBS.get(jid)
            cancel_event = CANCEL_EVENTS.get(jid)
            if not job or job.get("finished") or (cancel_event is not None and cancel_event.is_set()):
                if job:
                    with LOCK:
                        job["finished"] = True
                        job["cancelled"] = True
                        job["cancel_requested"] = False
                        job["state"] = "لغو شد"
                        job["paused"] = False
                        job["finished_at"] = time.time()
                        job["overall_percent"] = int(job.get("cancelled_percent", 0) or 0)
                    persist_jobs(force=True)
                continue
            with LOCK:
                job["state"] = "در حال دانلود"
                job["started_at"] = time.time()
            persist_jobs(force=True)
            try:
                run_job(jid, tracks, playlist, output_mode, retry_mode)
            except Exception as exc:
                with LOCK:
                    job["finished"] = True
                    job["state"] = "پایان با خطا"
                    job["finished_at"] = time.time()
                    job["error"] = str(exc)[:220]
                    job["paused"] = False
                persist_jobs(force=True)
        finally:
            JOB_QUEUE.task_done()


# One FIFO worker serializes playlist jobs; each job may still use its configured track concurrency.
threading.Thread(target=_job_queue_worker, name="PlaylistDLQueue", daemon=True).start()


def update_history(track, path, playlist):
    with LOCK:
        history = load_history()
        history[track_key(track)] = {"label": track.get("label", ""), "path": path,
                                     "normalized_key": normalized_label_key(track),
                                     "source_url": track.get("source_url"), "target": track.get("target"),
                                     "playlist": playlist, "saved_at": datetime.now().isoformat(timespec="seconds")}
        save_history(history)


@app.route("/")
def index():
    return PAGE


@app.route("/preview", methods=["POST"])
def preview():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    try:
        name, tracks, cover = get_tracks(text)
    except Exception as exc:
        return jsonify(error=str(exc) or "خواندن پلی‌لیست ناموفق بود"), 400
    if not tracks:
        return jsonify(error="آهنگی پیدا نشد؛ ممکنه پلی‌لیست خصوصی باشه یا سرویس اطلاعاتش رو نده"), 400
    pid = uuid.uuid4().hex[:12]
    PREVIEWS[pid] = {"playlist": name, "tracks": tracks, "cover": cover, "created": time.time()}
    return jsonify(id=pid, playlist=name, cover=cover, total=len(tracks),
                   tracks=[{"index": i, "label": t["label"], "cover": t.get("cover")} for i, t in enumerate(tracks)])


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json(silent=True) or {}
    pid = data.get("preview_id")
    if not pid or pid not in PREVIEWS:
        return jsonify(error="اول پیش‌نمایش پلی‌لیست رو بگیر و بعد دانلود رو شروع کن"), 400
    preview_data = PREVIEWS[pid]
    selected = data.get("selected")
    if not isinstance(selected, list):
        selected = list(range(len(preview_data["tracks"])))
    indexes = []
    for value in selected:
        try:
            idx = int(value)
            if 0 <= idx < len(preview_data["tracks"]) and idx not in indexes:
                indexes.append(idx)
        except (TypeError, ValueError):
            pass
    if not indexes:
        return jsonify(error="حداقل یک آهنگ رو انتخاب کن"), 400
    output_mode = data.get("output", "individual")
    if output_mode not in ("individual", "zip"):
        output_mode = "individual"
    tracks = [preview_data["tracks"][i] for i in indexes]
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"id": jid, "playlist": preview_data["playlist"], "cover": preview_data.get("cover"),
                 "total": len(tracks), "done": 0, "saved": 0, "current": "", "failed": [],
                 "finished": False, "cancelled": False, "cancel_requested": False,
                 "state": "در صف", "created_at": time.time(), "started_at": None, "finished_at": None,
                 "error": None, "folder": "", "tracks": [], "output_mode": output_mode,
                 "speed": 0, "eta": None, "track_percent": 0, "overall_percent": 0, "zip_path": None}
    PAUSE_EVENTS[jid] = threading.Event()
    PAUSE_EVENTS[jid].set()
    CANCEL_EVENTS[jid] = threading.Event()
    JOBS[jid]["paused"] = False
    persist_jobs(force=True)
    JOB_QUEUE.put((jid, tracks, preview_data["playlist"], output_mode, False))
    return jsonify(id=jid, queued=True)


@app.route("/retry/<jid>", methods=["POST"])
def retry(jid):
    old = JOBS.get(jid)
    if not old or not old.get("failed"):
        return jsonify(error="آهنگ ناموفقی برای تلاش مجدد وجود نداره"), 400
    tracks = [make_track(x.get("label"), x.get("target"), x.get("cover"), x.get("source_url")) for x in old["failed"]]
    new_id = uuid.uuid4().hex[:10]
    JOBS[new_id] = {"id": new_id, "playlist": old.get("playlist", "Retry"), "total": len(tracks), "done": 0, "saved": 0, "failed": [], "finished": False,
                    "cancelled": False, "cancel_requested": False, "state": "در صف", "created_at": time.time(),
                    "started_at": None, "finished_at": None, "error": None, "tracks": [], "zip_path": None,
                    "speed": 0, "eta": None, "track_percent": 0, "overall_percent": 0, "source_tracks": tracks}
    PAUSE_EVENTS[new_id] = threading.Event()
    PAUSE_EVENTS[new_id].set()
    CANCEL_EVENTS[new_id] = threading.Event()
    JOBS[new_id]["paused"] = False
    persist_jobs(force=True)
    JOB_QUEUE.put((new_id, tracks, old.get("playlist", "Retry"), old.get("output_mode", "individual"), True))
    return jsonify(id=new_id, queued=True)


@app.route("/jobs")
def list_jobs():
    # The queue is for pending/active work only; completed historical downloads belong in Library.
    with LOCK:
        items = []
        for job in JOBS.values():
            if job.get("finished"):
                continue
            items.append({"id": job.get("id"), "playlist": job.get("playlist", "دانلود"),
                          "total": job.get("total", 0), "done": job.get("done", 0),
                          "saved": job.get("saved", 0), "failed_count": len(job.get("failed", [])),
                          "finished": False, "paused": bool(job.get("paused")),
                          "cancelled": bool(job.get("cancelled")), "cancel_requested": bool(job.get("cancel_requested")),
                          "overall_percent": int(job.get("overall_percent", 0) or 0),
                          "state": job.get("state", "در صف"), "created_at": job.get("created_at", 0),
                          "current": job.get("current", ""), "output_mode": job.get("output_mode", "individual")})
    items.sort(key=lambda x: float(x.get("created_at") or 0))
    return jsonify(jobs=items[:30])


@app.route("/cancel/<jid>", methods=["POST"])
def cancel_job(jid):
    with LOCK:
        job = JOBS.get(jid)
        if not job:
            return jsonify(error="صف پیدا نشد"), 404
        if job.get("finished"):
            return jsonify(error="این صف قبلاً تمام شده"), 400
        event = CANCEL_EVENTS.setdefault(jid, threading.Event())
        event.set()
        PAUSE_EVENTS.setdefault(jid, threading.Event()).set()
        job["cancel_requested"] = True
        job["cancelled_percent"] = int(job.get("overall_percent", 0) or 0)
        job["paused"] = False
        job["current"] = "در حال لغو دانلود…"
        if job.get("state") == "در صف":
            job["finished"] = True
            job["cancelled"] = True
            job["cancel_requested"] = False
            job["state"] = "لغو شد"
            job["finished_at"] = time.time()
            job["error"] = "صف قبل از شروع به درخواست کاربر لغو شد"
    persist_jobs(force=True)
    return jsonify(ok=True, cancelled=True)


@app.route("/status/<jid>")
def status(jid):
    with LOCK:
        job = JOBS.get(jid)
        if not job:
            return jsonify(error="کار پیدا نشد", finished=True), 404
        # Return a snapshot so JSON serialization doesn't race with worker updates.
        return jsonify(json.loads(json.dumps(job, ensure_ascii=False)))


@app.route("/library")
def library():
    history = load_history()
    items = [v for v in history.values() if v.get("path") and os.path.isfile(v["path"])]
    items.sort(key=lambda x: x.get("saved_at", ""), reverse=True)
    return jsonify(items=items, count=len(items), output=get_settings()["output_dir"])


@app.route("/stats")
def stats():
    history = load_history()
    existing = [v for v in history.values() if v.get("path") and os.path.isfile(v["path"])]
    total_bytes = 0
    for item in existing:
        try: total_bytes += os.path.getsize(item["path"])
        except OSError: pass
    jobs = list(JOBS.values())
    return jsonify(library_count=len(existing), library_bytes=total_bytes,
                   jobs_count=len(jobs), finished_jobs=sum(1 for j in jobs if j.get("finished")),
                   failed_tracks=sum(len(j.get("failed", [])) for j in jobs),
                   active_jobs=sum(1 for j in jobs if not j.get("finished")))


@app.route("/pause/<jid>", methods=["POST"])
def pause_job(jid):
    job = JOBS.get(jid)
    if not job or job.get("finished") or job.get("cancel_requested"):
        return jsonify(error="این صف فعال نیست یا در حال لغو است"), 400
    event = PAUSE_EVENTS.setdefault(jid, threading.Event())
    event.clear()
    with LOCK:
        job["paused"] = True
        job["current"] = "مکث شده؛ برای ادامه، دکمه ادامه را بزن"
    persist_jobs(force=True)
    return jsonify(ok=True, paused=True)


@app.route("/resume/<jid>", methods=["POST"])
def resume_job(jid):
    job = JOBS.get(jid)
    if not job or job.get("finished") or job.get("cancel_requested"):
        return jsonify(error="این صف فعال نیست یا در حال لغو است"), 400
    PAUSE_EVENTS.setdefault(jid, threading.Event()).set()
    with LOCK:
        job["paused"] = False
    persist_jobs(force=True)
    return jsonify(ok=True, paused=False)


@app.route("/manifest.webmanifest")
def manifest():
    return jsonify({"name":"PlaylistDL Music Manager","short_name":"PlaylistDL","start_url":"/","display":"standalone","background_color":"#050807","theme_color":get_settings().get("accent_color", "#a970f5"),"description":"مدیریت و دانلود پلی‌لیست موسیقی"})


@app.route("/sw.js")
def service_worker():
    response = app.response_class("""const CACHE='playlistdl-shell-v1';self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/','/manifest.webmanifest']))));self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;const u=new URL(e.request.url);if(u.pathname!=='/'&&u.pathname!=='/manifest.webmanifest')return;e.respondWith(fetch(e.request).then(r=>{if(r.ok){const copy=r.clone();caches.open(CACHE).then(c=>c.put(e.request,copy));}return r;}).catch(()=>caches.match(e.request)));});""", mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.route("/settings", methods=["GET", "POST"])
def settings_route():
    if request.method == "GET":
        settings = get_settings()
        return jsonify(settings)
    data = request.get_json(silent=True) or {}
    settings = get_settings()
    try:
        intensity = int(data.get("theme_intensity", settings["theme_intensity"]))
        retries = 3
        concurrency = 1
    except (TypeError, ValueError):
        return jsonify(error="مقادیر شدت تم، هم‌زمانی و تلاش مجدد باید عدد باشند"), 400
    if not 0 <= intensity <= 100:
        return jsonify(error="شدت رنگ باید بین ۰ تا ۱۰۰ باشد"), 400
    # Theme preset drives colors; individual color controls are removed.
    presets = {
        "ultramarine": ("#6E92F2", "#1A3BB0", "#071039"),
        "ember": ("#FFA586", "#B51A28", "#16162F"),
        "electric": ("#136CFC", "#C3F84A", "#152641"),
        "slate": ("#9BA8AB", "#4A5C6A", "#06141B"),
        "midnight": ("#F0E7D5", "#212842", "#11172D"),
        "bordo": ("#F5DABF", "#6C151E", "#0F3D3A"),
        "citrus": ("#C3F84A", "#136CFC", "#152641"),
        "cobalt": ("#2E5DE6", "#1A3BB0", "#071039"),
        "sunset": ("#FF6D29", "#453027", "#161316"),
        "cream": ("#E6EDF9", "#6E92F2", "#172447"),
        "forest": ("#8BC7A0", "#0F5A46", "#081711"),
        "plum": ("#D6A0FF", "#6A36A8", "#170D24"),
        "rosewood": ("#F2A6A0", "#8E3048", "#1C1018"),
        "ice": ("#AFC5F8", "#2E5DE6", "#101A36"),
        "copper": ("#E6B27E", "#92552F", "#1A120D"),
        "mono": ("#CCD0CF", "#4A5C6A", "#06141B"),
    }
    preset = str(data.get("theme_preset", settings.get("theme_preset", "ultramarine")))
    if preset not in presets:
        preset = "ultramarine"
    accent_color, accent_secondary, background_color = presets[preset]
    audio_format = "mp3"
    audio_quality = "320"
    outdir = OUT
    template = "{index:04d} - {title}"
    fetch_tags = True
    embed_lyrics = True
    album_mode = "original"
    os.makedirs(outdir, exist_ok=True)
    settings.update(output_dir=outdir, theme_intensity=intensity, concurrency=1,
                    auto_retries=3, filename_template=template, audio_format="mp3",
                    audio_quality="320", fetch_tags=True, album_mode=album_mode, embed_lyrics=True,
                    theme_preset=preset, accent_color=accent_color, accent_secondary=accent_secondary,
                    background_color=background_color)
    save_settings(settings)
    return jsonify(ok=True, settings=settings)


@app.route("/download-zip/<jid>")
def download_zip(jid):
    job = JOBS.get(jid)
    path = job.get("zip_path") if job else None
    if not path or not os.path.isfile(path):
        return jsonify(error="فایل ZIP آماده نیست"), 404
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


PAGE = r'''<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="theme-color" content="#050807"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes"><link rel="manifest" href="/manifest.webmanifest"><title>PlaylistDL — دانلود و مدیریت موسیقی</title>
<style>
:root{color-scheme:dark;--bg:#0b0a0e;--panel:#15121b;--panel2:#1d1727;--line:#342940;--purple:#a970f5;--purple2:#7540ba;--text:#faf7ff;--muted:#aaa0b5;--good:#c5a0ff;--bad:#ffaaa9;--glow:0.30}*{box-sizing:border-box}body{margin:0;min-height:100vh;padding:22px 13px 45px;background:radial-gradient(ellipse at 50% -10%,rgba(var(--glow-rgb,116,62,170),var(--glow)) 0,#17101f 30%,var(--bg) 66%);font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--text)}.wrap{max-width:760px;margin:auto}.brand{display:flex;align-items:center;gap:12px;margin:3px 0 22px}.logo{width:52px;height:52px;border-radius:17px;display:grid;place-items:center;background:linear-gradient(145deg,var(--purple),var(--purple2));font-size:29px;color:#100b17;box-shadow:0 8px 30px #9d65ed30}.brand h1{font-size:22px;margin:0;letter-spacing:-.5px}.brand p{margin:3px 0 0;color:var(--muted);font-size:12px}.tabs{display:flex;gap:8px;margin-bottom:13px}.tab{border:1px solid var(--line);background:#15111b;color:var(--muted);padding:10px 14px;border-radius:999px;font-weight:700;cursor:pointer}.tab.active{background:#30203f;color:#f6eaff;border-color:#8156ac}.hero,.panel{background:linear-gradient(150deg,#1b1623f2,#100e14f5);border:1px solid #392c45;border-radius:26px;padding:20px;box-shadow:0 18px 55px #0005}.eyebrow{font-size:11px;color:var(--purple);font-weight:900;letter-spacing:1.1px}.hero h2{font-size:27px;line-height:1.45;margin:8px 0}.sub{font-size:14px;line-height:1.9;color:var(--muted);margin:0 0 17px}textarea{display:block;width:100%;min-height:125px;resize:vertical;border:1px solid #403448;border-radius:17px;background:#0c0a10;color:var(--text);padding:15px;font:14px/1.8 system-ui;direction:ltr;text-align:left;outline:none}textarea:focus{border-color:var(--purple);box-shadow:0 0 0 3px #a970f522}.label{display:block;font-size:13px;font-weight:800;margin:18px 0 9px}.choices{display:grid;grid-template-columns:1fr 1fr;gap:10px}.choice{border:1px solid #3b3045;border-radius:15px;padding:13px;background:#120f17;display:flex;align-items:center;gap:10px;cursor:pointer}.choice:has(input:checked){border-color:var(--purple);background:#281a38}.choice input{accent-color:var(--purple);width:17px;height:17px;margin:0}.choice b{display:block;font-size:13px}.choice small{display:block;color:var(--muted);font-size:11px;margin-top:4px}.btn{border:1px solid transparent;border-radius:15px;background:linear-gradient(130deg,var(--purple),var(--purple2));color:#140c1d;font-size:14px;font-weight:900;padding:14px 17px;cursor:pointer}.btn:disabled{opacity:.5;cursor:wait}.primary{width:100%;margin-top:16px;font-size:16px;padding:16px}.secondary{background:#21192b;color:#e9d8ff;border-color:#4a365d}.hint{font-size:11px;color:var(--muted);line-height:1.8;margin:12px 2px 0}.panel{margin-top:15px}.hidden{display:none!important}.playlist-head{display:flex;align-items:center;gap:13px}.cover{width:78px;height:78px;flex-shrink:0;border-radius:16px;object-fit:cover;background:#2a2034}.fallback{display:grid;place-items:center;font-size:27px;color:#d6b7ff}.plname{font-size:18px;font-weight:900;overflow-wrap:anywhere}.plmeta{font-size:12px;color:var(--muted);margin-top:5px}.selection-actions{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:17px 0 9px}.selection-actions button{border:0;background:none;color:#cba6ff;font-size:12px;font-weight:800;cursor:pointer}.tracklist{display:grid;gap:8px;margin-top:12px}.track{display:flex;align-items:center;gap:10px;background:#100d15;border:1px solid #30253a;padding:9px;border-radius:14px;min-width:0}.track input{accent-color:var(--purple);width:17px;height:17px;flex-shrink:0}.track img{width:47px;height:47px;border-radius:10px;object-fit:cover;background:#2a2034;flex-shrink:0}.trackname{font-size:12px;font-weight:750;overflow-wrap:anywhere;flex:1}.trackstate{font-size:10px;color:var(--muted);margin-top:4px}.badge{font-size:10px;background:#30223f;border-radius:999px;padding:5px 8px;color:#e3caff;white-space:nowrap}.progressrow{display:flex;justify-content:space-between;gap:10px;margin:19px 0 8px;font-size:12px}.bar{height:9px;background:#352a3f;border-radius:99px;overflow:hidden}.bar div{height:100%;width:0;background:linear-gradient(90deg,var(--purple2),var(--purple));border-radius:99px;transition:width .3s}.status{font-size:12px;line-height:1.8;overflow-wrap:anywhere;margin-top:13px;color:#ddd2e9}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:13px}.metric{background:#100d15;border:1px solid #30253a;border-radius:13px;padding:10px}.metric small{display:block;color:var(--muted);font-size:10px}.metric b{display:block;font-size:12px;margin-top:5px}.fails{font-size:12px;line-height:1.9;color:var(--bad);padding-right:20px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:13px}.actions .btn{font-size:12px;padding:10px 12px}.library-item{display:flex;justify-content:space-between;align-items:start;gap:10px;padding:12px 0;border-bottom:1px solid #33283d}.library-item:last-child{border-bottom:0}.queue-item{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 0;border-bottom:1px solid #33283d}.queue-item:last-child{border-bottom:0}.queue-copy{min-width:0;flex:1}.queue-copy b{display:block;font-size:12px;overflow-wrap:anywhere}.queue-copy small{display:block;color:var(--muted);font-size:10px;line-height:1.7;margin-top:4px;overflow-wrap:anywhere}.queue-item .btn{flex-shrink:0}.library-item b{font-size:12px}.library-item small{display:block;color:var(--muted);font-size:10px;margin-top:5px;overflow-wrap:anywhere}.foot{text-align:center;color:#7e7488;font-size:11px;margin:20px 0}.btn,.panel,.hero,.track{transition:background .25s,border-color .25s,transform .2s,box-shadow .25s}.btn:active{transform:scale(.985)}.logo{animation:logoGlow 4s ease-in-out infinite alternate}@keyframes logoGlow{from{box-shadow:0 8px 25px #9d65ed20}to{box-shadow:0 8px 35px var(--purple)55}}@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}.color-pick{display:flex;align-items:center;gap:10px;flex-wrap:wrap;border:1px solid #403448;background:#100d15;border-radius:13px;padding:10px}.color-pick input[type=color]{width:48px;height:38px;border:0;background:transparent;padding:0;cursor:pointer}.color-pick span{flex:1;color:var(--muted);font-size:11px}.color-pick code{font-size:11px;direction:ltr}@media(min-width:600px){body{padding-top:36px}.hero,.panel{padding:26px}.hero h2{font-size:30px}}@media(max-width:400px){.hero,.panel{padding:15px}.choices{grid-template-columns:1fr}.metrics{grid-template-columns:1fr 1fr 1fr}.cover{width:65px;height:65px}}
.theme-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.theme-card{min-width:0;text-align:right;border:1px solid var(--line);border-radius:15px;padding:10px;background:var(--panel);color:var(--text);cursor:pointer;transition:.2s}.theme-card.selected{border-color:var(--purple);box-shadow:0 0 0 2px color-mix(in srgb,var(--purple) 25%,transparent);background:var(--panel2)}.theme-card b{display:block;font-size:12px;margin-top:9px}.swatches{display:flex;height:25px;border-radius:8px;overflow:hidden}.swatches i{display:block;flex:1;min-width:0}.theme-card:active{transform:scale(.98)}@media(min-width:520px){.theme-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}

/* PlaylistDL AMOLED Neon Green visual refresh — presentation only */
:root{color-scheme:dark;--bg:#050807;--panel:#0b1410;--panel2:#101c15;--line:#1b3b2a;--purple:#39ff88;--purple2:#138a4a;--text:#effff5;--muted:#8da999;--good:#39ff88;--bad:#ff9b9b;--glow:.24}
body{background:radial-gradient(ellipse at 50% -10%,rgba(var(--glow-rgb,57,255,136),var(--glow)) 0,#09130d 28%,var(--bg) 68%)}
.logo{color:#04140a;box-shadow:0 0 28px #39ff8830}
.tab{background:#09100c;border-color:#1b3b2a}.tab.active{background:#10301d;color:#dffff0;border-color:#39ff88;box-shadow:0 0 18px #39ff8815}
.hero,.panel{background:linear-gradient(145deg,rgba(13,25,17,.97),rgba(6,12,8,.98));border-color:#1b3b2a;box-shadow:0 18px 55px #0008}
.eyebrow{color:#39ff88}.sub,.hint{color:#9ab5a4}
textarea,.track,.metric,.color-pick{background:#070d09;border-color:#1b3b2a}
textarea:focus{border-color:#39ff88;box-shadow:0 0 0 3px #39ff8820}
.choice{background:#09110c;border-color:#1b3b2a}.choice:has(input:checked){border-color:#39ff88;background:#102719}.choice input,.track input{accent-color:#39ff88}
.btn{background:linear-gradient(130deg,#39ff88,#16b85b);color:#04140a;box-shadow:0 5px 20px #39ff8816}.secondary{background:#102117;color:#caffdd;border-color:#28543b;box-shadow:none}
.track{border-radius:15px}.track:hover{border-color:#28633e}.cover,.track img{background:#102117}.fallback{color:#39ff88}
.selection-actions button{color:#70ffa8}.bar{background:#15271b}.bar div{background:linear-gradient(90deg,#138a4a,#39ff88);box-shadow:0 0 12px #39ff8850}
.badge{background:#10301d;color:#baffd0}.status{color:#c7dbce}.foot{color:#607c69}
.color-pick input[type=color]{accent-color:#39ff88}
.btn:focus-visible,.tab:focus-visible,textarea:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid #39ff88;outline-offset:2px}
@media(hover:hover){.btn:hover{filter:brightness(1.06);box-shadow:0 7px 26px #39ff8828}.panel:hover,.hero:hover{border-color:#255239}}


/* Premium polish pass — visual only; existing routes and handlers unchanged */
:root{--bg:#030604;--panel:#0a110c;--panel2:#101a13;--line:#1b3827;--purple:#39ff88;--purple2:#138a4a;--text:#f1fff6;--muted:#91a99a;--good:#39ff88;--glow:.2}
html{scroll-behavior:smooth;scrollbar-color:#245638 #050806}
body{padding-top:clamp(18px,4vh,38px);background:radial-gradient(ellipse at 50% -12%,rgba(57,255,136,.12),transparent 38%),#030604;letter-spacing:.05px}
.wrap{max-width:790px}
.brand{gap:14px;margin-bottom:25px}
.logo{width:56px;height:56px;border-radius:18px;background:linear-gradient(145deg,#53ffa0,#13934d);box-shadow:0 0 0 1px #39ff8830,0 8px 32px #39ff8820;font-weight:900}
.brand h1{font-size:24px;letter-spacing:-.7px}.brand p{font-size:12px;line-height:1.7}
.tabs{gap:9px;margin-bottom:16px;position:sticky;top:8px;z-index:5;padding:5px;background:#050806d9;border:1px solid #163020;border-radius:999px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.tab{flex:1;padding:11px 12px;background:transparent;border-color:transparent;transition:all .2s ease}.tab.active{background:linear-gradient(135deg,#143321,#0c1c12);border-color:#2b7548;box-shadow:inset 0 0 0 1px #39ff8810,0 4px 18px #0005}
.hero,.panel{border-radius:25px;padding:clamp(17px,4vw,27px);background:linear-gradient(145deg,#0c1710f5,#070c08fa);border:1px solid #1a3a27;box-shadow:0 22px 65px #0007,inset 0 1px #ffffff05}
.hero{position:relative;overflow:hidden}.hero:before{content:"";position:absolute;inset:0 0 auto;height:2px;background:linear-gradient(90deg,transparent,#39ff8870,transparent);pointer-events:none}
.eyebrow{letter-spacing:1.6px;font-size:10px}.hero h2{font-size:clamp(24px,5vw,31px);letter-spacing:-.6px;margin:10px 0}.sub{line-height:1.95}
textarea{min-height:135px;background:#040805;border-color:#1a3524;border-radius:17px;line-height:1.9;transition:border-color .2s,box-shadow .2s}
textarea::placeholder{color:#617868}textarea:focus{border-color:#39ff88;box-shadow:0 0 0 3px #39ff8818,0 0 24px #39ff8809}
.choices{gap:11px}.choice{border-radius:16px;background:#080e0a;border-color:#1a3524;transition:transform .18s,border-color .18s,background .18s}.choice:active{transform:scale(.99)}.choice:has(input:checked){background:linear-gradient(135deg,#102719,#09150d);box-shadow:inset 0 0 0 1px #39ff8810}
.btn{border-radius:15px;min-height:45px;transition:transform .16s,filter .16s,box-shadow .16s}.primary{min-height:52px;letter-spacing:.1px;box-shadow:0 8px 24px #39ff8814}.secondary{background:#0d1b12;border-color:#28543a;color:#d9ffe6}
.btn:active{transform:scale(.985)}.btn:disabled{filter:saturate(.45)}
.playlist-head{align-items:center}.cover{border-radius:17px;border:1px solid #24452f;box-shadow:0 8px 22px #0005}.tracklist{gap:9px}.track{background:linear-gradient(110deg,#080e0a,#070b08);border-color:#172f20;border-radius:16px;padding:10px;min-height:66px}.track img{border-radius:12px}.trackname{line-height:1.65}.trackstate{line-height:1.6;color:#9bb5a2}.track:hover{border-color:#2a6540;transform:translateY(-1px)}
.progressrow{font-weight:750;margin-top:21px}.bar{height:10px;background:#132219;border:1px solid #1c3b28}.bar div{background:linear-gradient(90deg,#128947,#39ff88);box-shadow:0 0 15px #39ff8840}
.metrics{gap:9px}.metric{border-radius:15px;background:linear-gradient(145deg,#0a120c,#070b08);border-color:#193423;padding:12px}.metric b{font-variant-numeric:tabular-nums}.badge{background:#10301d;color:#c8ffda;border:1px solid #1d4b30}
.status{line-height:1.9}.actions{gap:9px}.library-item{padding:14px 0;border-color:#193222}.color-pick{border-color:#193b27;background:#080e0a}.foot{padding-top:5px;color:#627d6b}
button,input,textarea,select{font:inherit}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid #39ff88;outline-offset:3px}
@media(max-width:480px){body{padding-left:10px;padding-right:10px}.brand h1{font-size:22px}.logo{width:50px;height:50px}.tabs{gap:4px;padding:4px}.tab{font-size:12px;padding:10px 5px}.hero,.panel{border-radius:21px}.metrics{gap:6px}.metric{padding:9px}.metric b{font-size:11px}.track{gap:8px}.track img{width:43px;height:43px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}.track,.btn,.tab,.choice{transition:none!important}}
/* User-customizable colors: all major UI surfaces derive from the selected palette. */
:root{--panel:color-mix(in srgb,var(--bg) 82%,var(--purple) 18%);--panel2:color-mix(in srgb,var(--bg) 70%,var(--purple2) 30%);--line:color-mix(in srgb,var(--bg) 55%,var(--purple) 45%);--good:var(--purple);}
body{background:radial-gradient(ellipse at 50% -12%,color-mix(in srgb,var(--purple) 18%,transparent),transparent 42%),var(--bg)}
.logo{background:linear-gradient(145deg,var(--purple),var(--purple2));box-shadow:0 0 0 1px color-mix(in srgb,var(--purple) 30%,transparent),0 8px 32px color-mix(in srgb,var(--purple) 18%,transparent)}
.tabs{border-color:color-mix(in srgb,var(--purple) 22%,var(--bg));background:color-mix(in srgb,var(--bg) 88%,black)}
.tab.active{background:linear-gradient(135deg,color-mix(in srgb,var(--purple) 23%,var(--bg)),color-mix(in srgb,var(--purple2) 18%,var(--bg)));border-color:color-mix(in srgb,var(--purple) 55%,var(--bg));color:var(--text)}
.hero,.panel{background:linear-gradient(145deg,color-mix(in srgb,var(--bg) 78%,var(--purple2) 22%),color-mix(in srgb,var(--bg) 92%,black));border-color:color-mix(in srgb,var(--purple) 25%,var(--bg))}
.hero:before{background:linear-gradient(90deg,transparent,color-mix(in srgb,var(--purple) 65%,transparent),transparent)}
textarea,.choice,.track,.metric,.color-pick{background:color-mix(in srgb,var(--bg) 88%,var(--purple2) 12%);border-color:color-mix(in srgb,var(--purple) 20%,var(--bg))}
textarea:focus{border-color:var(--purple);box-shadow:0 0 0 3px color-mix(in srgb,var(--purple) 12%,transparent)}
.choice:has(input:checked){border-color:var(--purple);background:color-mix(in srgb,var(--bg) 78%,var(--purple) 22%)}
.choice input,.track input,.color-pick input[type=color]{accent-color:var(--purple)}
.btn{background:linear-gradient(130deg,var(--purple),var(--purple2));color:var(--bg);box-shadow:0 5px 20px color-mix(in srgb,var(--purple) 10%,transparent)}
.secondary{background:color-mix(in srgb,var(--bg) 78%,var(--purple2) 22%);color:var(--text);border-color:color-mix(in srgb,var(--purple) 30%,var(--bg))}
.selection-actions button,.eyebrow{color:var(--purple)}
.bar{background:color-mix(in srgb,var(--bg) 75%,var(--purple2) 25%);border-color:color-mix(in srgb,var(--purple) 20%,var(--bg))}.bar div{background:linear-gradient(90deg,var(--purple2),var(--purple));box-shadow:0 0 15px color-mix(in srgb,var(--purple) 25%,transparent)}
.badge{background:color-mix(in srgb,var(--bg) 75%,var(--purple2) 25%);color:var(--text);border-color:color-mix(in srgb,var(--purple) 28%,var(--bg))}
.track:hover,.panel:hover,.hero:hover{border-color:color-mix(in srgb,var(--purple) 45%,var(--bg))}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline-color:var(--purple)}

/* Motion pack: balanced, lightweight, and color-theme aware */
@keyframes softRise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
@keyframes tabReveal{from{opacity:.55;transform:translateY(5px) scale(.995)}to{opacity:1;transform:translateY(0) scale(1)}}
@keyframes tapPulse{0%{transform:scale(1)}45%{transform:scale(.975)}100%{transform:scale(1)}}
@keyframes progressSheen{0%{transform:translateX(-130%)}100%{transform:translateX(230%)}}
.brand{animation:softRise .55s cubic-bezier(.2,.7,.2,1) both}
.tabs{animation:softRise .55s .06s cubic-bezier(.2,.7,.2,1) both}
.hero{animation:softRise .6s .1s cubic-bezier(.2,.7,.2,1) both}
.panel:not(.hidden){animation:tabReveal .28s cubic-bezier(.2,.7,.2,1) both}
.tab-enter{animation:tabReveal .28s cubic-bezier(.2,.7,.2,1) both}
.tab{transition:background .22s ease,border-color .22s ease,color .22s ease,transform .22s ease,box-shadow .22s ease}
.tab:active{transform:scale(.97)}
.btn{position:relative;overflow:hidden;transform:translateZ(0);transition:transform .18s ease,filter .2s ease,box-shadow .2s ease}
.btn:active{transform:scale(.975);filter:brightness(1.08)}
.btn.tap-feedback{animation:tapPulse .28s ease-out}
.choice,.track,.metric,.library-item{transition:transform .2s ease,border-color .22s ease,background .22s ease,box-shadow .22s ease}
.choice:active{transform:scale(.99)}
.track:hover{transform:translateY(-1px)}
.bar div{position:relative;overflow:hidden;transition:width .55s cubic-bezier(.2,.7,.2,1)}
.bar div::after{content:"";position:absolute;inset:0 auto 0 0;width:42%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.35),transparent);transform:translateX(-130%);animation:progressSheen 2.2s ease-in-out infinite;pointer-events:none}
.bar div[style*="width: 0%"]::after,.bar div[style*="width:0%"]::after{animation:none;opacity:0}
@media(prefers-reduced-motion:reduce){.brand,.tabs,.hero,.panel:not(.hidden),.tab-enter{animation:none!important}.bar div::after{animation:none!important}.btn.tap-feedback{animation:none!important}}

</style></head><body><main class="wrap">
<header class="brand"><div class="logo">♫</div><div><h1>PlaylistDL</h1></div></header>
<nav class="tabs"><button class="tab active" id="tabDownload" onclick="showTab('download')">دانلود موسیقی</button><button class="tab" id="tabLibrary" onclick="showTab('library');loadLibrary()">کتابخانه</button><button class="tab" id="tabSettings" onclick="showTab('settings');loadSettings()">تنظیمات</button></nav>
<section id="downloadView"><section class="hero"><div class="eyebrow">دانلود</div><h2>لینک آهنگ یا پلی‌لیست</h2><p class="sub">لینک YouTube، Spotify یا SoundCloud را وارد کن؛ یا نام هر آهنگ را در یک خط بنویس. برای لینک‌های Spotify، نسخهٔ قابل‌دسترسی در YouTube پیدا می‌شود.</p>
<textarea id="inputText" placeholder="لینک آهنگ یا پلی‌لیست را وارد کن…&#10;یا&#10;نام خواننده - نام آهنگ"></textarea>
<span class="label">روش ذخیره‌سازی</span><div class="choices"><label class="choice"><input type="radio" name="output" value="individual" checked><span><b>فایل جدا برای هر آهنگ</b><small>هر آهنگ به‌صورت یک فایل مستقل</small></span></label><label class="choice"><input type="radio" name="output" value="zip"><span><b>همهٔ آهنگ‌ها در یک فایل ZIP</b><small>مناسب برای انتقال یک‌جای فایل‌ها</small></span></label></div>
<button class="btn primary" id="previewBtn" onclick="previewPlaylist()">بررسی آهنگ‌ها</button><p class="hint">برای تبدیل صدا، FFmpeg باید نصب باشد. فقط محتوایی را دانلود کن که اجازهٔ ذخیره‌کردنش را داری. دسترسی به SoundCloud به عمومی‌بودن محتوا و پشتیبانی سرویس بستگی دارد.</p></section>
<section id="previewPanel" class="panel hidden"><div class="playlist-head"><img id="previewCover" class="cover hidden" alt="کاور پلی‌لیست"><div id="previewFallback" class="cover fallback">♫</div><div><div class="eyebrow">بررسی فهرست</div><div class="plname" id="previewName">پلی‌لیست</div><div class="plmeta" id="previewMeta"></div></div></div><div class="selection-actions"><b id="selectedCount">۰ آهنگ انتخاب شده</b><span><button onclick="selectAll(true)">انتخاب همه</button><button onclick="selectAll(false)">لغو انتخاب</button></span></div><div class="tracklist" id="previewTracks"></div><button class="btn primary" id="downloadBtn" onclick="startDownload()">دانلود انتخاب‌شده‌ها</button></section>
<section id="jobPanel" class="panel hidden"><div class="playlist-head"><img id="jobCover" class="cover hidden" alt="کاور"><div id="jobFallback" class="cover fallback">♫</div><div><div class="eyebrow">وضعیت دانلود</div><div class="plname" id="jobName">دانلود در حال اجراست…</div><div class="plmeta" id="jobMeta"></div></div></div><div class="progressrow"><span id="jobStatus">در حال آماده‌سازی دانلود…</span><b id="jobPercent">0%</b></div><div class="bar"><div id="jobFill"></div></div><div class="metrics"><div class="metric"><small>سرعت دانلود</small><b id="speed">—</b></div><div class="metric"><small>زمان باقی‌مانده</small><b id="eta">—</b></div><div class="metric"><small>دانلودشده</small><b id="saved">۰</b></div></div><div class="status" id="jobCurrent"></div><div class="actions" id="jobActions"><button class="btn secondary" id="pauseBtn" onclick="togglePause()">توقف موقت</button><button class="btn secondary" id="cancelBtn" onclick="cancelJob(activeJob)">لغو دانلود</button></div><ul class="fails" id="jobFails"></ul><div class="tracklist" id="jobTracks"></div></section><section id="queuePanel" class="panel hidden"><div class="eyebrow">صف دانلود</div><h2 style="font-size:20px;margin:8px 0">صف دانلود</h2><p class="sub">دانلودها به‌ترتیب اجرا می‌شوند. امکان لغو دانلودهای منتظر و فعال وجود دارد.</p><div id="queueItems" class="status"></div></section></section>
<section id="settingsView" class="panel hidden"><div class="eyebrow">تنظیمات برنامه</div><h2 style="font-size:22px;margin:8px 0">تنظیمات</h2><p class="sub">تغییرات روی همین گوشی ذخیره می‌شوند.</p>
<label class="label">ترکیب‌رنگ‌های آماده</label><p class="hint" style="margin:0 0 12px">یکی از پالت‌های الهام‌گرفته از آبی‌های عمیق، رنگ‌های نئونی، خاکی و ترکیب‌های گرم را انتخاب کن.</p><div id="themeGrid" class="theme-grid"></div><input id="themePreset" type="hidden" value="ultramarine">
<input id="accentColor" type="hidden" value="#a970f5"><input id="secondaryColor" type="hidden" value="#7540ba"><input id="backgroundColor" type="hidden" value="#0b0a0e"><input id="audioFormat" type="hidden" value="mp3"><input id="audioQuality" type="hidden" value="320"><input id="concurrency" type="hidden" value="1"><input id="fetchTags" type="hidden" value="1"><input id="embedLyrics" type="hidden" value="1">
<div class="status">فرمت: MP3 · کیفیت ثابت: 320 kbps</div>
<div class="status">تلاش مجدد خودکار: ۳ بار</div>
<div class="status">اطلاعات آهنگ و متن ترانه همیشه به‌صورت خودکار جست‌وجو می‌شوند؛ در صورت نبودن اطلاعات در منابع، امکان ثبت آن‌ها وجود ندارد.</div><div class="status">نام آلبوم واقعی حفظ می‌شود؛ نام فایل‌ها با شمارهٔ چهاررقمی بر اساس ترتیب پلی‌لیست ذخیره می‌شوند.</div>
<button class="btn primary" onclick="saveSettings()">ذخیرهٔ تغییرات</button><div id="settingsMessage" class="status"></div></section>
<section id="libraryView" class="panel hidden"><div class="eyebrow">کتابخانه</div><h2 style="font-size:22px;margin:8px 0">دانلودهای اخیر</h2><p class="sub">فهرست فایل‌های صوتی ذخیره‌شده در این برنامه.</p><div id="statsItems" class="metrics"></div><div id="libraryItems" class="status">در حال بارگذاری فهرست…</div></section>

<div class="foot">PlaylistDL</div></main>
<script>
const $=id=>document.getElementById(id);let previewId=null,previewTracks=[],activeJob=null,pollTimer=null,queueTimer=null;
function showTab(which){const lib=which==='library',settings=which==='settings';$('downloadView').classList.toggle('hidden',lib||settings);$('libraryView').classList.toggle('hidden',!lib);$('settingsView').classList.toggle('hidden',!settings);$('tabDownload').classList.toggle('active',!lib&&!settings);$('tabLibrary').classList.toggle('active',lib);$('tabSettings').classList.toggle('active',settings);const active=lib?$('libraryView'):settings?$('settingsView'):$('downloadView');active.classList.remove('tab-enter');void active.offsetWidth;active.classList.add('tab-enter')}
function hexToRgb(hex){const h=String(hex||'#39ff88').replace('#','');return parseInt(h.slice(0,2),16)+','+parseInt(h.slice(2,4),16)+','+parseInt(h.slice(4,6),16)}
const palettes={
 ultramarine:{label:'اولترامارین',colors:['#071039','#0F2270','#1A3BB0','#2E5DE6','#6E92F2','#AFC5F8','#E6EDF9'],main:['#6E92F2','#1A3BB0','#071039']},
 ember:{label:'اخگر',colors:['#16162F','#242F49','#384358','#FFA586','#B51A28','#541A2E'],main:['#FFA586','#B51A28','#16162F']},
 electric:{label:'آبی الکتریکی',colors:['#136CFC','#152641','#C3F84A','#E8F4FC'],main:['#C3F84A','#136CFC','#152641']},
 slate:{label:'اسلیت',colors:['#06141B','#11212D','#253745','#4A5C6A','#9BA8AB','#CCD0CF'],main:['#9BA8AB','#4A5C6A','#06141B']},
 midnight:{label:'نیمه‌شب و کرم',colors:['#212842','#F0E7D5','#A9A18F','#11172D'],main:['#F0E7D5','#212842','#11172D']},
 bordo:{label:'بردو و سبز',colors:['#6C151E','#0F3D3A','#F5DABF'],main:['#F5DABF','#6C151E','#0F3D3A']},
 citrus:{label:'مرکبات نئونی',colors:['#136CFC','#152641','#C3F84A'],main:['#C3F84A','#136CFC','#152641']},
 cobalt:{label:'کوبالت',colors:['#071039','#1A3BB0','#2E5DE6','#6E92F2'],main:['#2E5DE6','#1A3BB0','#071039']},
 sunset:{label:'غروب نارنجی',colors:['#FF6D29','#453027','#161316','#BABABA','#FFFFFF'],main:['#FF6D29','#453027','#161316']},
 cream:{label:'یخی روشن',colors:['#172447','#6E92F2','#AFC5F8','#E6EDF9'],main:['#E6EDF9','#6E92F2','#172447']},
 forest:{label:'جنگل',colors:['#081711','#0F5A46','#246B53','#8BC7A0','#E6E8D8'],main:['#8BC7A0','#0F5A46','#081711']},
 plum:{label:'آلویی',colors:['#170D24','#3A1D5A','#6A36A8','#D6A0FF','#F3E9FF'],main:['#D6A0FF','#6A36A8','#170D24']},
 rosewood:{label:'رز و چوب',colors:['#1C1018','#512033','#8E3048','#F2A6A0','#F8E6D8'],main:['#F2A6A0','#8E3048','#1C1018']},
 ice:{label:'یخ آبی',colors:['#101A36','#2E5DE6','#6E92F2','#AFC5F8','#E6EDF9'],main:['#AFC5F8','#2E5DE6','#101A36']},
 copper:{label:'مسی',colors:['#1A120D','#453027','#92552F','#E6B27E','#F5E6D5'],main:['#E6B27E','#92552F','#1A120D']},
 mono:{label:'خاکستری سرد',colors:['#06141B','#253745','#4A5C6A','#9BA8AB','#CCD0CF'],main:['#CCD0CF','#4A5C6A','#06141B']}
};
function buildThemeGrid(){const grid=$('themeGrid');if(!grid)return;grid.innerHTML='';Object.entries(palettes).forEach(([key,p])=>{const b=document.createElement('button');b.type='button';b.className='theme-card';b.dataset.theme=key;b.innerHTML='<span class="swatches">'+p.colors.map(c=>'<i style="background:'+c+'"></i>').join('')+'</span><b>'+p.label+'</b>';b.onclick=()=>applyPreset(key);grid.append(b)})}
function applyPreset(name){if(!palettes[name])name='ultramarine';$('themePreset').value=name;const [a,s,b]=palettes[name].main;[$('accentColor').value,$('secondaryColor').value,$('backgroundColor').value]=[a,s,b];document.querySelectorAll('.theme-card').forEach(el=>el.classList.toggle('selected',el.dataset.theme===name));applyColors()}
function applyColors(){const root=document.documentElement;const a=$('accentColor').value,s=$('secondaryColor').value,b=$('backgroundColor').value;root.style.setProperty('--purple',a);root.style.setProperty('--purple2',s);root.style.setProperty('--bg',b);root.style.setProperty('--glow-rgb',hexToRgb(a));root.style.setProperty('--glow','0.14')}
async function loadSettings(){try{const r=await fetch('/settings');const s=await r.json();buildThemeGrid();applyPreset(s.theme_preset||'ultramarine');$('audioFormat').value='mp3';$('audioQuality').value='320';$('concurrency').value='1';$('fetchTags').value='1';$('embedLyrics').value='1'}catch(e){$('settingsMessage').textContent='خواندن تنظیمات ناموفق بود'}}
async function saveSettings(){const data={theme_preset:$('themePreset').value,audio_format:$('audioFormat').value,audio_quality:$('audioQuality').value,accent_color:$('accentColor').value,accent_secondary:$('secondaryColor').value,background_color:$('backgroundColor').value,concurrency:Number($('concurrency').value),auto_retries:3,filename_template:'{index:04d} - {title}',fetch_tags:$('fetchTags').value==='1',album_mode:'original',embed_lyrics:$('embedLyrics').value==='1'};$('settingsMessage').textContent='در حال ذخیره…';try{const r=await fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const d=await r.json();if(!r.ok)throw new Error(d.error||'ذخیره ناموفق بود');$('settingsMessage').textContent='تنظیمات ذخیره شدند.';applyColors()}catch(e){$('settingsMessage').textContent=e.message}}
function fmtNum(n){return Number(n||0).toLocaleString('fa-IR')}
function fmtTime(s){if(s===null||s===undefined||!Number.isFinite(Number(s)))return '—';s=Math.max(0,Math.round(Number(s)));return s<60?fmtNum(s)+' ثانیه':s<3600?fmtNum(Math.floor(s/60))+' دقیقه '+fmtNum(s%60)+' ثانیه':fmtNum(Math.floor(s/3600))+' ساعت '+fmtNum(Math.floor(s%3600/60))+' دقیقه'}
function fmtSpeed(n){if(!n)return '—';return n>1048576?(n/1048576).toFixed(1)+' MB/s':(n/1024).toFixed(0)+' KB/s'}
function setCover(imgId,fallId,url){const im=$(imgId),fb=$(fallId);if(url){im.src=url;im.classList.remove('hidden');fb.classList.add('hidden');im.onerror=()=>{im.classList.add('hidden');fb.classList.remove('hidden')}}else{im.classList.add('hidden');fb.classList.remove('hidden')}}
async function previewPlaylist(){const text=$('inputText').value.trim();if(!text){$('inputText').focus();return} $('previewBtn').disabled=true;$('previewBtn').textContent='در حال بررسی فهرست…';$('previewPanel').classList.remove('hidden');$('previewTracks').innerHTML='';$('previewName').textContent='در حال دریافت اطلاعات…';$('previewMeta').textContent='';try{const r=await fetch('/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});const d=await r.json();if(!r.ok)throw new Error(d.error||'دریافت اطلاعات ناموفق بود');previewId=d.id;previewTracks=d.tracks||[];$('previewName').textContent=d.playlist||'پلی‌لیست';$('previewMeta').textContent=fmtNum(d.total)+' آهنگ';setCover('previewCover','previewFallback',d.cover);renderPreviewTracks();$('downloadBtn').disabled=false}catch(e){$('previewName').textContent=e.message;$('previewMeta').textContent='لینک و اتصال اینترنت رو بررسی کن'}finally{$('previewBtn').disabled=false;$('previewBtn').textContent='بررسی دوباره'}}
function renderPreviewTracks(){const box=$('previewTracks');box.innerHTML='';previewTracks.forEach((t,i)=>{const row=document.createElement('label');row.className='track';const cb=document.createElement('input');cb.type='checkbox';cb.checked=true;cb.dataset.index=i;cb.addEventListener('change',updateSelected);const img=document.createElement('img');img.alt='';img.loading='lazy';img.src=t.cover||'';img.onerror=()=>img.style.visibility='hidden';const info=document.createElement('div');info.className='trackname';info.textContent=t.label;row.append(cb,img,info);box.append(row)});updateSelected()}
function updateSelected(){const count=[...$('previewTracks').querySelectorAll('input:checked')].length;$('selectedCount').textContent=fmtNum(count)+' آهنگ انتخاب‌شده';$('downloadBtn').disabled=count===0}
function selectAll(value){$('previewTracks').querySelectorAll('input').forEach(cb=>cb.checked=value);updateSelected()}
async function startDownload(){if(!previewId)return;const selected=[...$('previewTracks').querySelectorAll('input:checked')].map(cb=>Number(cb.dataset.index));const output=document.querySelector('input[name="output"]:checked').value;$('downloadBtn').disabled=true;$('downloadBtn').textContent='در حال آماده‌سازی دانلود…';try{const r=await fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({preview_id:previewId,selected,output})});const d=await r.json();if(!r.ok)throw new Error(d.error||'شروع دانلود انجام نشد.');activeJob=d.id;localStorage.setItem('playlistdl_job',activeJob);$('jobPanel').classList.remove('hidden');$('jobName').textContent=$('previewName').textContent;setCover('jobCover','jobFallback',$('previewCover').classList.contains('hidden')?'':$('previewCover').src);$('jobTracks').innerHTML='';showJob(d.id);loadQueue()}catch(e){$('selectedCount').textContent=e.message}finally{$('downloadBtn').disabled=false;$('downloadBtn').textContent='دانلود انتخاب‌شده‌ها'}}
function showJob(id){clearInterval(pollTimer);const tick=async()=>{try{const r=await fetch('/status/'+id);const s=await r.json();if(!r.ok)throw new Error(s.error);renderJob(s);if(s.finished){clearInterval(pollTimer);pollTimer=null}}catch(e){$('jobStatus').textContent='ارتباط با سرور قطع شد'}};tick();pollTimer=setInterval(tick,1200)}
function renderJob(s){$('jobName').textContent=s.playlist||'دانلود';$('jobMeta').textContent=fmtNum(s.total)+' آهنگ · '+(s.output_mode==='zip'?'خروجی ZIP':'فایل جدا برای هر آهنگ');setCover('jobCover','jobFallback',s.cover);const pct=s.overall_percent||0;$('jobFill').style.width=pct+'%';$('jobPercent').textContent=fmtNum(pct)+'%';$('jobStatus').textContent=s.cancel_requested?'در حال لغو دانلود…':(s.cancelled?'دانلود لغو شد':(s.finished?(s.failed&&s.failed.length?'پایان با خطا':'عملیات تمام شد'):(s.paused?'صف مکث شده':(s.state==='در صف'?'در صف انتظار':fmtNum(s.done)+' از '+fmtNum(s.total)+' آهنگ'))));if($('pauseBtn'))$('pauseBtn').textContent=s.paused?'ادامه دانلود':'توقف موقت';if($('pauseBtn'))$('pauseBtn').disabled=!!s.finished||!!s.cancel_requested||s.state==='در صف';if($('cancelBtn'))$('cancelBtn').disabled=!!s.finished||!!s.cancel_requested;$('speed').textContent=fmtSpeed(s.speed);$('eta').textContent=fmtTime(s.eta);$('saved').textContent=fmtNum(s.saved);$('jobCurrent').textContent=s.current||s.folder||'';$('jobFails').innerHTML='';(s.failed||[]).forEach(f=>{const li=document.createElement('li');li.textContent=(typeof f==='string'?f:(f.label+' — '+(f.error||'دانلود ناموفق')));$('jobFails').append(li)});if(s.tracks&&s.tracks.length){const box=$('jobTracks');if(box.children.length!==s.tracks.length){box.innerHTML='';s.tracks.forEach(t=>{const row=document.createElement('div');row.className='track';const img=document.createElement('img');img.src=t.cover||'';img.onerror=()=>img.style.visibility='hidden';const info=document.createElement('div');info.className='trackname';const title=document.createElement('div');title.textContent=t.label;const state=document.createElement('div');state.className='trackstate';state.textContent=t.state;info.append(title,state);row.append(img,info);box.append(row)})}else [...box.children].forEach((row,i)=>{const t=s.tracks[i];if(t)row.querySelector('.trackstate').textContent=t.state})}if(s.finished){$('jobActions').innerHTML='';if(s.zip_path){const a=document.createElement('a');a.className='btn';a.href='/download-zip/'+s.id;a.textContent='دریافت فایل ZIP';$('jobActions').append(a)}if(s.failed&&s.failed.length){const b=document.createElement('button');b.className='btn secondary';b.textContent='تلاش مجدد برای ناموفق‌ها';b.onclick=()=>retryFailed(s.id);$('jobActions').append(b)}const msg=document.createElement('span');msg.className='status';msg.textContent=s.cancelled?'صف لغو شد · فایل‌های کامل‌شده حفظ شده‌اند':'مسیر ذخیره: '+(s.folder||'پوشه PlaylistDL');$('jobActions').append(msg)}loadQueue()}
async function togglePause(){if(!activeJob)return;const paused=$('pauseBtn').textContent.includes('ادامه');try{const r=await fetch('/'+(paused?'resume/':'pause/')+activeJob,{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.error);showJob(activeJob)}catch(e){$('jobStatus').textContent=e.message}}
async function retryFailed(id){try{const r=await fetch('/retry/'+id,{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.error);activeJob=d.id;showJob(d.id)}catch(e){alert(e.message)}}
async function loadQueue(){const panel=$('queuePanel'),box=$('queueItems');if(!panel||!box)return;try{const r=await fetch('/jobs');const d=await r.json();const items=(d.jobs||[]).filter(item=>!item.finished);if(!items.length){box.innerHTML='';panel.classList.add('hidden');return}panel.classList.remove('hidden');box.innerHTML='';items.forEach(item=>{const row=document.createElement('div');row.className='queue-item';const copy=document.createElement('div');copy.className='queue-copy';const name=document.createElement('b');name.textContent=item.playlist||'دانلود';const meta=document.createElement('small');const state=item.cancel_requested?'در حال لغو…':(item.cancelled?'لغو شد':(item.finished?(item.failed_count?'پایان با خطا':'تمام‌شده'):(item.paused?'مکث‌شده':(item.state==='در صف'?'در صف انتظار':'در حال دانلود'))));meta.textContent=state+' · '+fmtNum(item.overall_percent)+'٪ · '+fmtNum(item.saved)+' فایل دانلودشده · '+fmtNum(item.total)+' آهنگ';copy.append(name,meta);row.append(copy);if(!item.finished&&!item.cancel_requested){const b=document.createElement('button');b.className='btn secondary';b.textContent='لغو';b.onclick=()=>cancelJob(item.id);row.append(b)}box.append(row)});}catch(e){/* Queue indicator is optional; the active job panel still works. */}}
async function cancelJob(id){if(!id)return;try{const r=await fetch('/cancel/'+id,{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.error||'لغو ناموفق بود');await loadQueue();if(id===activeJob)showJob(id)}catch(e){$('jobStatus').textContent=e.message}}
async function loadLibrary(){const box=$('libraryItems');box.textContent='در حال بارگذاری فهرست…';try{const sr=await fetch('/stats');const st=await sr.json();$('statsItems').innerHTML='';[['آهنگ‌های کتابخانه',fmtNum(st.library_count)],['حجم کتابخانه',(st.library_bytes/1048576).toFixed(1)+' MB'],['صف‌های فعال',fmtNum(st.active_jobs)],['آهنگ‌های ناموفق',fmtNum(st.failed_tracks)]].forEach(x=>{const el=document.createElement('div');el.className='metric';const sm=document.createElement('small');sm.textContent=x[0];const b=document.createElement('b');b.textContent=x[1];el.append(sm,b);$('statsItems').append(el)});const r=await fetch('/library');const d=await r.json();box.innerHTML='';if(!d.items||!d.items.length){box.textContent='هنوز فایلی دانلود نشده است.';return}d.items.forEach(item=>{const row=document.createElement('div');row.className='library-item';const info=document.createElement('div');const title=document.createElement('b');title.textContent=item.label||'آهنگ';const meta=document.createElement('small');meta.textContent=(item.playlist||'')+' · '+(item.saved_at||'');info.append(title,meta);const path=document.createElement('small');path.textContent=item.path;row.append(info,path);box.append(row)})}catch(e){box.textContent='بارگذاری فهرست انجام نشد.'}}
document.addEventListener('click',e=>{const btn=e.target.closest('button.btn');if(!btn||btn.disabled)return;btn.classList.remove('tap-feedback');void btn.offsetWidth;btn.classList.add('tap-feedback');setTimeout(()=>btn.classList.remove('tap-feedback'),320)});if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});loadSettings();loadQueue();queueTimer=setInterval(loadQueue,2200);const old=localStorage.getItem('playlistdl_job');if(old){$('jobPanel').classList.remove('hidden');showJob(old)}
</script></body></html>'''

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
