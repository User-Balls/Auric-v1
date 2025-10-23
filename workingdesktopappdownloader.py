# Full replacement program for Pydroid 3
# - Tries ffmpeg for MP3 conversion, falls back to original formats if ffmpeg missing/failed
# - Embeds metadata only into valid MP3s
# - Shows elapsed and remaining times (best-effort using metadata, mutagen, sound length, or estimated from file size)
# - Robust logging to UI and persistent log file
# - Based on earlier versions; hardened for Android/Pydroid environment

import os
import sys
import io
import time
import threading
import shutil
import traceback
import subprocess
from datetime import datetime
from math import floor

# Optional dependencies: import with checks so we can log if missing
HAS_REQUESTS = True
HAS_YTDLP = True
HAS_MUTAGEN = True
HAS_PIL = True

try:
    import requests
except Exception:
    HAS_REQUESTS = False

try:
    import yt_dlp
except Exception:
    HAS_YTDLP = False

try:
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3, APIC, error as MutagenID3Error
    from mutagen.mp3 import MP3
    import mutagen
except Exception:
    HAS_MUTAGEN = False

try:
    from PIL import Image as PILImage, ImageDraw
except Exception:
    HAS_PIL = False

# Kivy / KivyMD imports (we assume Pydroid has these installed; otherwise raise)
try:
    from kivy.clock import Clock
    from kivy.core.image import Image as CoreImage
    from kivy.core.audio import SoundLoader
    from kivy.app import App
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.modalview import ModalView
    from kivy.uix.label import Label
    from kivy.uix.image import Image
    from kivy.uix.button import Button
    from kivy.uix.scrollview import ScrollView
    from kivy.uix.gridlayout import GridLayout
    from kivy.uix.floatlayout import FloatLayout
    from kivy.metrics import dp
    from kivy.graphics import Color, RoundedRectangle
except Exception:
    print("ERROR: Kivy not found. Install Kivy & KivyMD in Pydroid 3 to run the UI.")
    raise

try:
    from kivymd.app import MDApp
    from kivymd.uix.progressbar import MDProgressBar
    from kivymd.uix.button import MDIconButton, MDRaisedButton
    from kivymd.uix.label import MDLabel
    from kivymd.uix.textfield import MDTextField
    from kivymd.uix.card import MDCard
except Exception:
    print("WARNING: KivyMD not found. Some widgets may not render as intended. Install kivymd for best results.")

# --------------------------- Utilities & Paths ---------------------------
def get_writable_directory(subdir_name):
    """
    Return a usable writable directory for Pydroid 3. Tries several likely locations.
    """
    potential_paths = [
        os.path.join('/storage/emulated/0/Android/data/ru.iiec.pydroid3/files', subdir_name),
        os.path.join(os.path.expanduser('~'), subdir_name),
        os.path.join('/tmp', subdir_name),
    ]
    try:
        cwd = os.getcwd()
        if cwd and (not cwd.startswith('/storage/emulated/0') or '/Android/data/' in cwd):
            potential_paths.insert(0, os.path.join(cwd, subdir_name))
    except Exception:
        pass

    for path in potential_paths:
        try:
            os.makedirs(path, exist_ok=True)
            # write test
            test_file = os.path.join(path, ".writetest")
            with open(test_file, "wb") as f:
                f.write(b"ok")
            with open(test_file, "rb") as f:
                if f.read() == b"ok":
                    os.remove(test_file)
                    return os.path.abspath(path)
        except Exception:
            continue

    # fallback
    fallback = os.path.join(os.path.expanduser('~'), subdir_name)
    try:
        os.makedirs(fallback, exist_ok=True)
        return os.path.abspath(fallback)
    except Exception:
        return os.path.abspath('.')

def sanitize_filename(name):
    return "".join(c if c.isalnum() or c in " ._-()" else "_" for c in name).strip()

APP_DIR = get_writable_directory("MusicPlayerApp")
LOG_DIR = get_writable_directory("MusicPlayerApp/logs")
LOG_FILE_PATH = os.path.join(LOG_DIR, "activity.log")

def write_debug_file(msg):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        try:
            print("DEBUGLOG FAIL:", msg)
        except Exception:
            pass

def log_safe(ui_log_func, message):
    """
    Write to persistent log and schedule UI log update if ui_log_func provided.
    """
    try:
        write_debug_file(message)
    except Exception:
        pass

    try:
        if ui_log_func:
            # schedule on main thread
            Clock.schedule_once(lambda dt: ui_log_func(message))
        else:
            print(message)
    except Exception:
        try:
            ui_log_func(message)
        except Exception:
            try:
                print(message)
            except Exception:
                pass

# --------------------------- FFmpeg detection & conversion ---------------------------
def ffmpeg_exists():
    """Return True if ffmpeg binary is callable."""
    try:
        proc = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)
        return proc.returncode == 0 or proc.returncode == 1 or (proc.stdout or proc.stderr)
    except FileNotFoundError:
        return False
    except Exception:
        return False

def convert_to_mp3_with_ffmpeg(source_path, target_path, bitrate="192k", ui_log=None):
    """
    Attempt to convert source_path to MP3 at target_path using ffmpeg.
    Returns True on success, False otherwise.
    """
    log_safe(ui_log, f"🛠️ Attempting ffmpeg conversion: {os.path.basename(source_path)} -> {os.path.basename(target_path)}")
    if not ffmpeg_exists():
        log_safe(ui_log, "⚠️ ffmpeg not available on device.")
        return False

    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", source_path,
            "-vn",
            "-ab", bitrate,
            "-ar", "44100",
            "-f", "mp3",
            target_path
        ]
        # Run and capture output for debug
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if proc.returncode == 0 and os.path.exists(target_path):
            log_safe(ui_log, "✅ ffmpeg conversion succeeded.")
            return True
        else:
            # Write ffmpeg stderr to log for debugging
            errmsg = proc.stderr.decode(errors="ignore") if proc.stderr is not None else "<no stderr>"
            log_safe(ui_log, f"❌ ffmpeg conversion failed (returncode={proc.returncode}). Stderr sample: {errmsg[:300]}")
            write_debug_file(f"ffmpeg stderr: {errmsg}")
            # ensure no half file
            try:
                if os.path.exists(target_path):
                    os.remove(target_path)
            except Exception:
                pass
            return False
    except Exception as e:
        log_safe(ui_log, f"❌ ffmpeg conversion exception: {e}")
        write_debug_file(traceback.format_exc())
        try:
            if os.path.exists(target_path):
                os.remove(target_path)
        except Exception:
            pass
        return False

# --------------------------- Metadata & format helpers ---------------------------
def is_real_mp3(file_path):
    """Return True if file looks like a real MP3 (ID3 header or 0xfffb frame)."""
    try:
        with open(file_path, "rb") as f:
            header = f.read(4)
        if not header:
            return False
        if header.startswith(b"ID3"):
            return True
        if header[:2] == b"\xff\xfb":
            return True
    except Exception:
        pass
    return False

def estimate_duration_from_size_bytes(file_path, assumed_bitrate_kbps=128):
    """
    Estimate duration in seconds from file size and an assumed bitrate (default 128 kbps).
    seconds = (bytes * 8) / (bitrate_kbps * 1000)
    """
    try:
        size = os.path.getsize(file_path)
        if size <= 0:
            return None
        return (size * 8) / (assumed_bitrate_kbps * 1000.0)
    except Exception:
        return None

def get_duration_best_effort(file_path, entry_metadata=None):
    """
    Try multiple ways to get the duration in seconds:
    1) entry_metadata['duration'] (if provided by yt-dlp)
    2) mutagen.File(...).info.length if mutagen present
    3) SoundLoader.length (Kivy)
    4) estimate from file size assuming 128 kbps
    Returns float seconds or None.
    """
    # 1) entry metadata
    if entry_metadata:
        try:
            dur = entry_metadata.get("duration")
            if dur:
                return float(dur)
        except Exception:
            pass

    # 2) mutagen
    if HAS_MUTAGEN:
        try:
            m = mutagen.File(file_path)
            if m and hasattr(m, "info") and getattr(m.info, "length", None):
                return float(m.info.length)
        except Exception:
            pass

    # 3) SoundLoader
    try:
        s = SoundLoader.load(file_path)
        if s and getattr(s, "length", None) and s.length > 0:
            return float(s.length)
    except Exception:
        pass

    # 4) estimate from size
    est = estimate_duration_from_size_bytes(file_path, assumed_bitrate_kbps=128)
    if est:
        return est

    return None

def embed_metadata(file_path, metadata, ui_log=None):
    """
    Embed EasyID3 text tags and APIC cover art if file is MP3 and mutagen available.
    If file not MP3, or mutagen not present, skip gracefully.
    """
    log_safe(ui_log, f"🔧 embed_metadata: Starting for {os.path.basename(file_path)}")

    if not HAS_MUTAGEN:
        log_safe(ui_log, "⚠️ mutagen not installed — skipping metadata embedding.")
        return

    if not os.path.exists(file_path):
        log_safe(ui_log, f"⚠️ File does not exist: {file_path}")
        return

    if not is_real_mp3(file_path):
        log_safe(ui_log, "⚠️ File is not a valid MP3 (skipping tag embedding).")
        return

    # First, download cover art if available
    thumb = metadata.get("thumbnail") if isinstance(metadata, dict) else None
    img_data = None
    if thumb and HAS_REQUESTS:
        try:
            log_safe(ui_log, f"📥 Downloading cover art from: {thumb[:50]}...")
            r = requests.get(thumb, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code == 200 and len(r.content) > 0:
                img_data = r.content
                log_safe(ui_log, f"✅ Downloaded {len(img_data)} bytes of cover art")
            else:
                log_safe(ui_log, f"⚠️ Cover download failed: status {r.status_code}")
        except Exception as e:
            log_safe(ui_log, f"⚠️ Error downloading cover: {e}")
            write_debug_file(f"Cover download error: {e}\n{traceback.format_exc()}")
            img_data = None

    # Now embed text metadata and cover art
    try:
        # Load or create ID3 tags
        try:
            id3 = ID3(file_path)
        except MutagenID3Error:
            # No tags exist, create them
            mp3 = MP3(file_path)
            mp3.add_tags()
            mp3.save(file_path, v2_version=3)
            id3 = ID3(file_path)

        # Add text metadata using ID3 directly for better control
        from mutagen.id3 import TIT2, TPE1, TALB

        title = metadata.get("title", os.path.basename(file_path))
        artist = metadata.get("uploader", metadata.get("artist", "Unknown Artist"))
        album = metadata.get("album", "Downloaded")

        id3.delall("TIT2")
        id3.delall("TPE1")
        id3.delall("TALB")

        id3.add(TIT2(encoding=3, text=title))
        id3.add(TPE1(encoding=3, text=artist))
        id3.add(TALB(encoding=3, text=album))

        # Add cover art if we have it
        if img_data:
            id3.delall("APIC")
            id3.add(APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,  # Cover (front)
                desc="Cover",
                data=img_data
            ))
            log_safe(ui_log, "🖼️ Cover art embedded successfully")

        # Save all changes
        id3.save(file_path, v2_version=3)
        log_safe(ui_log, "✅ Metadata and cover art saved to MP3")

    except Exception as e:
        log_safe(ui_log, f"⚠️ Error embedding metadata: {e}")
        write_debug_file(f"Metadata embedding error: {e}\n{traceback.format_exc()}")

# --------------------------- yt-dlp helpers ---------------------------
def get_playlist_entries(url, ui_log=None):
    if not HAS_YTDLP:
        log_safe(ui_log, "⚠️ yt_dlp not installed; cannot extract entries.")
        return []

    # Fix for Pydroid 3: Create proper file-like objects
    class SuppressOutput:
        def write(self, s):
            pass
        def flush(self):
            pass
        def isatty(self):
            return False

    old_stdout = sys.stdout
    old_stderr = sys.stderr
    try:
        sys.stdout = SuppressOutput()
        sys.stderr = SuppressOutput()

        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "noprogress": True,
            "logger": None,
            "retries": 3,
            "fragment_retries": 3,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if isinstance(info, dict):
                if "entries" in info and info["entries"] is not None:
                    return list(info["entries"])
                return [info]
            return []
    except Exception as e:
        log_safe(ui_log, f"❌ yt_dlp extract error: {e}")
        write_debug_file(traceback.format_exc())
        return []
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

# --------------------------- Queue / UI Dialogs ---------------------------
class QueueDialog(ModalView):
    def __init__(self, queue_data, current_index, **kwargs):
        super().__init__(**kwargs)
        self.size_hint = (0.95, 0.85)
        layout = BoxLayout(orientation='vertical', padding=dp(12), spacing=dp(8))
        layout.add_widget(Label(text=f"Queue ({len(queue_data)} items)", size_hint_y=None, height=dp(40)))
        scroll = ScrollView()
        q_layout = GridLayout(cols=1, size_hint_y=None, spacing=dp(8))
        q_layout.bind(minimum_height=q_layout.setter('height'))
        for i, e in enumerate(queue_data):
            txt = f"{'▶' if i==current_index else f'{i+1}.'} {e.get('title','Unknown')}"
            q_layout.add_widget(Label(text=txt, size_hint_y=None, height=dp(40)))
        scroll.add_widget(q_layout)
        layout.add_widget(scroll)
        close = Button(text="Close", size_hint_y=None, height=dp(48))
        close.bind(on_press=lambda x: self.dismiss())
        layout.add_widget(close)
        self.add_widget(layout)

# --------------------------- Download Manager ---------------------------
class DownloadManager:
    def __init__(self, ui):
        self.ui = ui
        self.download_stop_flag = False
        self.thread = None

    def start_download(self, url):
        self.download_stop_flag = False
        log_safe(self.ui.log, f"🔁 start_download: {url}")
        Clock.schedule_once(lambda dt: self.ui.show_download_progress())
        self.thread = threading.Thread(target=self._worker, args=(url,), daemon=True)
        self.thread.start()

    def cancel_download(self):
        self.download_stop_flag = True
        log_safe(self.ui.log, "⏹ Download cancelled by user.")
        Clock.schedule_once(lambda dt: self.ui.hide_download_progress())

    def _worker(self, url):
        try:
            self._download_audio(url)
            Clock.schedule_once(lambda dt: self.ui.refresh_file_list())
            log_safe(self.ui.log, "✅ Download worker finished.")
        except Exception as e:
            log_safe(self.ui.log, f"❌ Download exception: {e}")
            write_debug_file(traceback.format_exc())
        finally:
            Clock.schedule_once(lambda dt: self.ui.hide_download_progress())

    def _download_audio(self, url):
        log_safe(self.ui.log, f"⬇️ Download request: {url}")
        if not HAS_YTDLP:
            log_safe(self.ui.log, "⚠️ yt_dlp not installed — cannot download.")
            return

        temp_dir = get_writable_directory("temp_download")
        os.makedirs(temp_dir, exist_ok=True)
        out_template = os.path.join(temp_dir, "dl_%(id)s.%(ext)s")

        # Fix for Pydroid 3: Create proper file-like objects for stdout/stderr
        class SuppressOutput:
            def write(self, s):
                pass
            def flush(self):
                pass
            def isatty(self):
                return False

        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "outtmpl": {"default": out_template},
            "noplaylist": False,
            "postprocessors": [],
            "noprogress": True,
            "logger": None,
            "keepvideo": False,
            "overwrites": True,
            "retries": 3,
            "fragment_retries": 3,
        }

        # Fix stdout/stderr for Pydroid 3
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        try:
            sys.stdout = SuppressOutput()
            sys.stderr = SuppressOutput()

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            log_safe(self.ui.log, f"❌ yt_dlp download error: {e}")
            write_debug_file(traceback.format_exc())
            return
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        # If yt-dlp returned playlist/dict, gather entries
        entries = []
        if isinstance(info, dict) and "entries" in info and info["entries"]:
            for e in info["entries"]:
                if e:
                    entries.append(e)
        elif isinstance(info, dict):
            entries.append(info)

        log_safe(self.ui.log, f"🔎 Found {len(entries)} item(s) to process.")

        for i, entry in enumerate(entries):
            if self.download_stop_flag:
                log_safe(self.ui.log, "Download stop flag set - exiting loop.")
                break
            try:
                # Wait a moment for file to be fully written
                time.sleep(0.5)

                # Find the downloaded file, excluding .ytdl temporary files
                candidates = []
                try:
                    for f in os.listdir(temp_dir):
                        fp = os.path.join(temp_dir, f)
                        # Skip .ytdl files (incomplete downloads) and non-files
                        if not os.path.isfile(fp):
                            continue
                        if f.endswith('.ytdl') or f.endswith('.part'):
                            continue
                        if f.startswith("dl_"):
                            candidates.append(fp)
                except Exception as e:
                    log_safe(self.ui.log, f"⚠️ Error listing temp directory: {e}")
                    continue

                # Sort by modification time to get newest
                candidates = sorted(candidates, key=lambda p: os.path.getmtime(p), reverse=True)

                downloaded = None
                for c in candidates:
                    # Make sure file exists and has content
                    try:
                        if os.path.exists(c) and os.path.getsize(c) > 0:
                            downloaded = c
                            break
                    except Exception:
                        continue

                if not downloaded:
                    log_safe(self.ui.log, f"⚠️ Could not find completed download for: {entry.get('title')}")
                    continue

                # Verify file is readable and not empty
                try:
                    size = os.path.getsize(downloaded)
                    if size == 0:
                        log_safe(self.ui.log, f"⚠️ Downloaded file is empty: {downloaded}")
                        continue
                    log_safe(self.ui.log, f"📦 Found downloaded file: {os.path.basename(downloaded)} ({size} bytes)")
                except Exception as e:
                    log_safe(self.ui.log, f"⚠️ Error checking file size: {e}")
                    continue

                # Determine final filename and whether to convert
                title = sanitize_filename(entry.get('title', 'unknown'))
                uploader = sanitize_filename(entry.get('uploader', entry.get('uploader_id', 'unknown')))
                base_name = f"{uploader} - {title}"
                final_base = sanitize_filename(base_name)
                final_mp3 = os.path.join(APP_DIR, final_base + ".mp3")
                final_orig = os.path.join(APP_DIR, final_base + os.path.splitext(downloaded)[1])

                # If downloaded file is already MP3 and valid -> move it
                if is_real_mp3(downloaded):
                    try:
                        shutil.move(downloaded, final_mp3)
                        log_safe(self.ui.log, f"✅ Saved MP3: {final_mp3}")
                        # embed metadata
                        embed_metadata(final_mp3, entry, self.ui.log)
                    except Exception:
                        # fallback: copy
                        try:
                            shutil.copy(downloaded, final_mp3)
                            log_safe(self.ui.log, f"✅ Copied MP3: {final_mp3}")
                            embed_metadata(final_mp3, entry, self.ui.log)
                        except Exception as e:
                            log_safe(self.ui.log, f"❌ Failed to save MP3: {e}")
                            write_debug_file(traceback.format_exc())
                    continue

                # Otherwise, try ffmpeg conversion to MP3
                converted = False
                target_mp3 = final_mp3
                if ffmpeg_exists():
                    # produce temp mp3 before moving
                    temp_mp3 = os.path.join(temp_dir, f"conv_{int(time.time())}.mp3")
                    converted = convert_to_mp3_with_ffmpeg(downloaded, temp_mp3, ui_log=self.ui.log)
                    if converted and os.path.exists(temp_mp3):
                        try:
                            shutil.move(temp_mp3, target_mp3)
                            log_safe(self.ui.log, f"✅ Converted & saved MP3: {target_mp3}")
                            embed_metadata(target_mp3, entry, self.ui.log)
                            # remove original downloaded file
                            try:
                                if os.path.exists(downloaded):
                                    os.remove(downloaded)
                            except Exception:
                                pass
                            continue
                        except Exception as e:
                            log_safe(self.ui.log, f"❌ Error moving converted MP3: {e}")
                            write_debug_file(traceback.format_exc())
                else:
                    log_safe(self.ui.log, "ℹ️ ffmpeg not available — will keep original format.")

                # If conversion failed or not attempted: keep original extension and move to APP_DIR
                try:
                    # avoid overwriting if already exists
                    dest = final_orig
                    if os.path.exists(dest):
                        # add timestamp
                        dest = os.path.join(APP_DIR, final_base + "_" + datetime.utcnow().strftime("%Y%m%d%H%M%S") + os.path.splitext(downloaded)[1])
                    shutil.move(downloaded, dest)
                    log_safe(self.ui.log, f"✅ Saved original file (no mp3): {dest}")
                    # Attempt embedding metadata only if mutagen supports container (mutagen can handle many types)
                    try:
                        if HAS_MUTAGEN:
                            # embed metadata only for formats that mutagen supports for writing (MP3 best supported)
                            if dest.lower().endswith(".mp3"):
                                embed_metadata(dest, entry, self.ui.log)
                            else:
                                log_safe(self.ui.log, f"ℹ️ Skipping metadata embed for {os.path.splitext(dest)[1]} (non-MP3).")
                    except Exception:
                        pass
                except Exception as e:
                    log_safe(self.ui.log, f"❌ Failed to save downloaded file: {e}")
                    write_debug_file(traceback.format_exc())

            except Exception as e:
                log_safe(self.ui.log, f"❌ Error processing entry: {e}")
                write_debug_file(traceback.format_exc())

        # cleanup temp files
        try:
            for f in os.listdir(temp_dir):
                try:
                    os.remove(os.path.join(temp_dir, f))
                except Exception:
                    pass
        except Exception:
            pass

# --------------------------- Stream Player ---------------------------
class StreamPlayer:
    def __init__(self, ui):
        self.ui = ui
        self.sound = None
        self.current_file = None
        self.current_entry = None
        self.queue = []
        self.current_index = 0
        self.temp_dir = get_writable_directory("stream_cache")
        os.makedirs(self.temp_dir, exist_ok=True)
        self.next_thread = None
        self.stop_flag = False
        self.pause_flag = False
        self.skip_flag = False
        self.progress_event = None
        self.play_start_time = 0.0
        self.total_paused_time = 0.0
        self.last_pause_time = 0.0
        self.pause_position = 0.0
        self.played_files = set()

    def cleanup_temp(self):
        try:
            for fname in os.listdir(self.temp_dir):
                fp = os.path.join(self.temp_dir, fname)
                try:
                    if self.current_file and os.path.abspath(fp) == os.path.abspath(self.current_file):
                        continue
                    os.remove(fp)
                except Exception:
                    pass
        except Exception:
            pass

    def stream_playlist(self, url):
        self.stop_flag = False
        self.pause_flag = False
        self.skip_flag = False
        self.current_index = 0
        self.played_files.clear()
        self.cleanup_temp()

        entries = get_playlist_entries(url, ui_log=self.ui.log)
        if not entries:
            log_safe(self.ui.log, "❌ No entries found to stream.")
            return

        self.queue = entries
        for i, entry in enumerate(entries):
            if self.stop_flag:
                break
            self.current_index = i
            try:
                Clock.schedule_once(lambda dt: self.ui.show_stream_progress())
                Clock.schedule_once(lambda dt, e=entry: self.ui.update_stream_progress(0, f"Preparing: {e.get('title','')[:30]}"))
                filename = self._prepare_for_stream(entry)
                Clock.schedule_once(lambda dt: self.ui.hide_stream_progress())
                if not filename:
                    log_safe(self.ui.log, f"⚠️ Could not prepare entry: {entry.get('title')}")
                    continue
                # Pre-download next entry in background
                if i + 1 < len(entries):
                    next_entry = entries[i + 1]
                    self.next_thread = threading.Thread(target=self._prepare_for_stream, args=(next_entry,), daemon=True)
                    self.next_thread.start()
                Clock.schedule_once(lambda dt, q=self.queue, idx=self.current_index: self.ui.update_queue_display(q, idx))
                success = self.play_song(filename, entry)
                # after playback, try remove cached file if it's in temp_dir
                try:
                    if filename and filename.startswith(self.temp_dir) and os.path.exists(filename):
                        os.remove(filename)
                        log_safe(self.ui.log, f"🧹 Deleted cached stream file: {os.path.basename(filename)}")
                except Exception:
                    pass
                if not success and not self.stop_flag:
                    log_safe(self.ui.log, f"⏭️ Skipping: {entry.get('title')}")
            except Exception as e:
                log_safe(self.ui.log, f"❌ stream loop error: {e}")
                write_debug_file(traceback.format_exc())

        if not self.stop_flag:
            log_safe(self.ui.log, "✅ Stream finished.")
        Clock.schedule_once(lambda dt: self.ui.clear_queue_display())
        self.cleanup_temp()

    def _prepare_for_stream(self, entry):
        """
        Download an entry into temp_dir and attempt to convert to mp3 via ffmpeg.
        Return path to a playable file (mp3 or original) or None.
        """
        if not HAS_YTDLP:
            log_safe(self.ui.log, "⚠️ yt_dlp missing — cannot stream/download.")
            return None

        url = entry.get("webpage_url") or entry.get("url")
        if not url:
            return None

        safe_title = sanitize_filename(entry.get("title", "stream_item"))
        out_template = os.path.join(self.temp_dir, f"{safe_title}.%(ext)s")

        def progress(d):
            try:
                status = d.get("status")
                if status == "downloading":
                    pct = 0.0
                    if "_percent_str" in d:
                        try:
                            pct = float(d.get("_percent_str", "0%").strip().strip("%"))
                        except Exception:
                            pct = 0.0
                    elif "percent" in d:
                        try:
                            pct = float(d.get("percent", 0.0))
                        except Exception:
                            pct = 0.0
                    speed_str = d.get("_speed_str", "N/A")
                    total_str = d.get("_total_bytes_str", "N/A")
                    Clock.schedule_once(lambda dt, p=pct, s=f"{speed_str} - {total_str}": self.ui.update_stream_progress(p, s))
                elif status == "finished":
                    Clock.schedule_once(lambda dt: self.ui.update_stream_progress(100, "Processing..."))
            except Exception:
                pass

        # Fix for Pydroid 3: Create proper file-like objects
        class SuppressOutput:
            def write(self, s):
                pass
            def flush(self):
                pass
            def isatty(self):
                return False

        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "outtmpl": {"default": out_template},
            "noplaylist": True,
            "progress_hooks": [progress],
            "noprogress": True,
            "logger": None,
            "keepvideo": False,
            "overwrites": True,
            "retries": 3,
            "fragment_retries": 3,
        }

        old_stdout = sys.stdout
        old_stderr = sys.stderr
        try:
            sys.stdout = SuppressOutput()
            sys.stderr = SuppressOutput()

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            log_safe(self.ui.log, f"❌ yt_dlp stream download error: {e}")
            write_debug_file(traceback.format_exc())
            return None
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        # Wait a moment for file to be fully written
        time.sleep(0.5)

        # Find downloaded file in temp_dir, excluding incomplete files
        downloaded_path = None
        try:
            for f in os.listdir(self.temp_dir):
                # Skip incomplete downloads
                if f.endswith('.ytdl') or f.endswith('.part'):
                    continue
                if f.startswith(safe_title):
                    fp = os.path.join(self.temp_dir, f)
                    if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                        downloaded_path = fp
                        # prefer mp3 if already exists
                        if fp.lower().endswith(".mp3"):
                            break
        except Exception as e:
            log_safe(self.ui.log, f"⚠️ Error finding downloaded file: {e}")
            write_debug_file(traceback.format_exc())

        if not downloaded_path:
            log_safe(self.ui.log, "⚠️ No completed file found after download for streaming.")
            return None

        # Verify file has content
        try:
            size = os.path.getsize(downloaded_path)
            if size == 0:
                log_safe(self.ui.log, f"⚠️ Downloaded file is empty: {downloaded_path}")
                return None
            log_safe(self.ui.log, f"📦 Stream file ready: {os.path.basename(downloaded_path)} ({size} bytes)")
        except Exception as e:
            log_safe(self.ui.log, f"⚠️ Error verifying file: {e}")
            return None

        # If it's a real mp3, use directly
        if is_real_mp3(downloaded_path):
            log_safe(self.ui.log, f"🎧 Stream ready (MP3): {os.path.basename(downloaded_path)}")
            return downloaded_path

        # Attempt ffmpeg conversion to mp3 into temp file
        if ffmpeg_exists():
            temp_mp3 = os.path.join(self.temp_dir, f"{safe_title}_conv.mp3")
            converted = convert_to_mp3_with_ffmpeg(downloaded_path, temp_mp3, ui_log=self.ui.log)
            if converted and os.path.exists(temp_mp3):
                log_safe(self.ui.log, f"🎧 Stream ready (converted MP3): {os.path.basename(temp_mp3)}")
                # keep converted file for playing
                return temp_mp3
            else:
                log_safe(self.ui.log, "⚠️ ffmpeg conversion failed for stream; will attempt to play original format.")
        else:
            log_safe(self.ui.log, "ℹ️ ffmpeg not available; using original file format for streaming.")

        # Use original file (m4a/webm/opus/etc.)
        if os.path.exists(downloaded_path):
            log_safe(self.ui.log, f"🎧 Stream ready (original format): {os.path.basename(downloaded_path)}")
            return downloaded_path

        return None

    def play_song(self, filepath, entry_metadata=None):
        """
        Play a local audio file and provide periodic progress updates.
        Returns True on normal completion, False on error/skip/stop.
        """
        if not filepath or not os.path.exists(filepath):
            log_safe(self.ui.log, "❌ play_song called but file missing.")
            return False

        # Stop previous sound if any
        try:
            if self.sound:
                try:
                    self.sound.stop()
                    self.sound.unload()
                except Exception:
                    pass
                self.sound = None
            self.current_file = filepath
            self.current_entry = entry_metadata
        except Exception:
            pass

        # Load sound
        try:
            self.sound = SoundLoader.load(filepath)
            if not self.sound:
                log_safe(self.ui.log, f"❌ Could not load audio for playing: {os.path.basename(filepath)}")
                return False
            # Some backends may not have length; we'll compute best-effort
        except Exception as e:
            log_safe(self.ui.log, f"❌ Error loading sound: {e}")
            write_debug_file(traceback.format_exc())
            return False

        # Update UI track info
        try:
            md = entry_metadata if isinstance(entry_metadata, dict) else {}
            self.ui.update_current_track(md, filepath)
        except Exception:
            pass

        # Start playback
        try:
            self.sound.play()
            log_safe(self.ui.log, f"▶️ Playing: {os.path.basename(filepath)}")
        except Exception as e:
            log_safe(self.ui.log, f"❌ Error during play(): {e}")
            write_debug_file(traceback.format_exc())
            return False

        # Setup timing
        self.play_start_time = time.time()
        self.total_paused_time = 0.0
        self.last_pause_time = 0.0
        self.pause_position = 0.0
        self.pause_flag = False
        self.skip_flag = False

        # Start periodic progress updates
        self.start_progress_updates()

        # Determine duration best-effort
        duration = get_duration_best_effort(filepath, entry_metadata)
        if duration is None:
            # fall back to 5 minutes as upper limit to avoid infinite waits (but UI will show estimate)
            duration = 300.0

        # Playback loop: wait until playback stops or skip/stop triggered
        try:
            start_check = time.time()
            timeout = max(10.0, duration + 30.0)  # give a cushion
            while (not self.stop_flag) and (not self.skip_flag):
                # Check whether sound is still playing
                state = getattr(self.sound, "state", None)
                # If backend changes state to 'stop' or 'stop' after done
                if state != "play" and not self.pause_flag:
                    # Usually this indicates playback ended
                    break
                # sleep small
                time.sleep(0.3)
                # Safety: break after timeout
                if (time.time() - start_check) > timeout:
                    log_safe(self.ui.log, "⚠️ Playback timeout reached, stopping playback loop.")
                    break
        except Exception as e:
            log_safe(self.ui.log, f"❌ Playback runtime exception: {e}")
            write_debug_file(traceback.format_exc())

        # cleanup after playback
        self.stop_progress_updates()
        try:
            if self.sound:
                try:
                    self.sound.stop()
                    self.sound.unload()
                except Exception:
                    pass
                self.sound = None
        except Exception:
            pass

        # mark file as played
        try:
            self.played_files.add(os.path.basename(filepath))
        except Exception:
            pass

        if self.skip_flag:
            log_safe(self.ui.log, "⏩ Song skipped by user.")
            self.skip_flag = False

        return not self.stop_flag

    def start_progress_updates(self):
        self.stop_progress_updates()
        try:
            self.progress_event = Clock.schedule_interval(self._progress_tick, 0.5)
        except Exception:
            self.progress_event = None

    def stop_progress_updates(self):
        if self.progress_event:
            try:
                self.progress_event.cancel()
            except Exception:
                pass
            self.progress_event = None

    def _progress_tick(self, dt):
        try:
            if not self.current_file:
                return
            # compute elapsed
            elapsed = time.time() - self.play_start_time - self.total_paused_time
            # if paused, adjust elapsed accordingly
            if self.pause_flag and self.last_pause_time > 0:
                elapsed = self.last_pause_time - self.play_start_time - self.total_paused_time

            # get best possible duration
            duration = get_duration_best_effort(self.current_file, self.current_entry)
            if duration and duration > 0:
                remaining = max(0.0, duration - elapsed)
                progress_pct = min(100.0, (elapsed / duration) * 100.0)
                # UI update
                Clock.schedule_once(lambda dt, p=progress_pct, e=elapsed, r=remaining, d=duration: self.ui.update_playback_progress(p, e, d))
            else:
                # Unknown duration: estimate from file size
                estimated = estimate_duration_from_size_bytes(self.current_file, assumed_bitrate_kbps=128)
                if estimated:
                    remaining = max(0.0, estimated - elapsed)
                    prog = min(100.0, (elapsed / estimated) * 100.0) if estimated > 0 else 0.0
                    Clock.schedule_once(lambda dt, p=prog, e=elapsed, r=remaining, d=estimated: self.ui.update_playback_progress(p, e, d))
                else:
                    # Show elapsed with unknown remaining: pass duration=0 to indicate unknown
                    Clock.schedule_once(lambda dt, p=0.0, e=elapsed, d=0.0: self.ui.update_playback_progress(p, e, d))
        except Exception:
            pass

    def pause(self):
        if self.sound and getattr(self.sound, "state", None) == "play":
            try:
                # try to get pos if available
                try:
                    pos = self.sound.get_pos()
                    if pos is not None:
                        self.pause_position = pos
                except Exception:
                    self.pause_position = time.time() - self.play_start_time - self.total_paused_time
                try:
                    self.sound.stop()
                except Exception:
                    pass
                self.pause_flag = True
                if self.last_pause_time == 0:
                    self.last_pause_time = time.time()
                Clock.schedule_once(lambda dt: self.ui.update_playback_state("Paused"))
                log_safe(self.ui.log, "⏸️ Playback paused.")
            except Exception as e:
                log_safe(self.ui.log, f"⚠️ Pause error: {e}")
                write_debug_file(traceback.format_exc())

    def resume(self):
        if self.sound and self.pause_flag:
            try:
                if self.last_pause_time > 0:
                    self.total_paused_time += time.time() - self.last_pause_time
                    self.last_pause_time = 0
                self.sound.play()
                # try to seek if backend supports
                try:
                    if self.pause_position and hasattr(self.sound, "seek"):
                        self.sound.seek(self.pause_position)
                        self.pause_position = 0
                except Exception:
                    # fallback: adjust start time so UI progress remains consistent
                    self.play_start_time = time.time() - (self.pause_position + self.total_paused_time)
                    self.pause_position = 0
                self.pause_flag = False
                Clock.schedule_once(lambda dt: self.ui.update_playback_state("Playing"))
                log_safe(self.ui.log, "▶️ Playback resumed.")
            except Exception as e:
                log_safe(self.ui.log, f"⚠️ Resume error: {e}")
                write_debug_file(traceback.format_exc())

    def toggle_pause(self):
        if self.pause_flag:
            self.resume()
        else:
            self.pause()

    def skip(self):
        self.skip_flag = True
        try:
            if self.sound:
                self.sound.stop()
        except Exception:
            pass

    def show_queue(self):
        if self.queue:
            Clock.schedule_once(lambda dt: QueueDialog(self.queue, self.current_index).open())
        else:
            log_safe(self.ui.log, "📜 No queue active.")

    def stop(self):
        self.stop_flag = True
        self.pause_flag = False
        self.skip_flag = False
        self.stop_progress_updates()
        try:
            if self.sound:
                self.sound.stop()
                self.sound.unload()
        except Exception:
            pass
        self.sound = None
        self.cleanup_temp()

# --------------------------- UI (DownloaderUI) ---------------------------
class DownloaderUI(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", spacing=0, padding=0, **kwargs)
        self.streamer = StreamPlayer(self)
        self.downloader = DownloadManager(self)
        self.current_sound = None
        self.local_is_paused = False
        self.local_play_start_time = 0.0
        self.local_total_paused_time = 0.0
        self.local_last_pause_time = 0.0
        self.local_progress_event = None

        with self.canvas.before:
            Color(0.07, 0.07, 0.07, 1)
            self.rect = RoundedRectangle(size=self.size, pos=self.pos)
        self.bind(size=self._update_rect, pos=self._update_rect)

        self.build_ui()
        self.set_default_cover()
        Clock.schedule_once(lambda dt: self.refresh_file_list(), 0.5)

    def _update_rect(self, instance, value):
        try:
            self.rect.pos = self.pos
            self.rect.size = self.size
        except Exception:
            pass

    def build_ui(self):
        self.clear_widgets()
        # Top area: URL input + buttons
        top = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(140), spacing=dp(12), padding=dp(12))
        self.url_input = MDTextField(hint_text="Enter YouTube/SoundCloud link...", mode="rectangle", size_hint=(1, None), height=dp(64), font_size=dp(18))
        btn_row = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(8))
        self.stream_btn = MDRaisedButton(text="Stream", size_hint=(1,1), on_press=self.start_stream)
        self.download_btn = MDRaisedButton(text="Download", size_hint=(1,1), on_press=self.start_download)
        btn_row.add_widget(self.stream_btn)
        btn_row.add_widget(self.download_btn)
        top.add_widget(self.url_input)
        top.add_widget(btn_row)
        self.add_widget(top)

        # Download progress (hidden by default)
        self.download_section = BoxLayout(orientation="vertical", size_hint_y=None, height=0)
        self.download_section.opacity = 0
        self.download_title = MDLabel(text="Download", size_hint_y=None, height=dp(28))
        self.download_progress = MDProgressBar(value=0, size_hint_y=None, height=dp(6))
        self.download_status = MDLabel(text="", size_hint_y=None, height=dp(24))
        self.download_section.add_widget(self.download_title)
        self.download_section.add_widget(self.download_progress)
        self.download_section.add_widget(self.download_status)
        self.add_widget(self.download_section)

        # Main content
        main_content = BoxLayout(orientation="vertical", spacing=dp(12), padding=[dp(12),dp(8),dp(12),dp(12)], size_hint_y=None)
        main_content.bind(minimum_height=main_content.setter("height"))

        # Now playing card
        now_card = MDCard(size_hint_y=None, height=dp(420), md_bg_color=[0.09,0.09,0.09,1], radius=[dp(12)], padding=dp(12))
        card_content = BoxLayout(orientation="vertical", spacing=dp(8))
        self.cover_art = Image(size_hint=(1, None), height=dp(220), allow_stretch=True, keep_ratio=True)
        card_content.add_widget(self.cover_art)
        self.track_title = MDLabel(text="No track playing", font_style="H5", halign="center")
        self.track_artist = MDLabel(text="", font_style="Body1", halign="center")
        self.track_album = MDLabel(text="", font_style="Body2", halign="center")
        self.track_duration = MDLabel(text="", font_style="Caption", halign="center")
        self.queue_info = MDLabel(text="Queue: 0 songs", font_style="Body2", halign="center")
        card_content.add_widget(self.track_title)
        card_content.add_widget(self.track_artist)
        card_content.add_widget(self.track_album)
        card_content.add_widget(self.track_duration)
        card_content.add_widget(self.queue_info)
        self.progress_bar = MDProgressBar(value=0, size_hint_y=None, height=dp(6))
        card_content.add_widget(self.progress_bar)
        self.time_label = MDLabel(text="00:00 / 00:00", font_style="Body2", halign="center")
        card_content.add_widget(self.time_label)
        now_card.add_widget(card_content)
        main_content.add_widget(now_card)

        # Stream download progress (hidden)
        self.stream_section = BoxLayout(orientation="vertical", size_hint_y=None, height=0)
        self.stream_section.opacity = 0
        self.stream_title = MDLabel(text="Stream Download", size_hint_y=None, height=dp(28))
        self.stream_progress = MDProgressBar(value=0, size_hint_y=None, height=dp(6))
        self.stream_status = MDLabel(text="", size_hint_y=None, height=dp(24))
        self.stream_section.add_widget(self.stream_title)
        self.stream_section.add_widget(self.stream_progress)
        self.stream_section.add_widget(self.stream_status)
        main_content.add_widget(self.stream_section)

        # File list
        file_header = MDLabel(text="Downloaded Songs", font_style="H6", size_hint_y=None, height=dp(36))
        main_content.add_widget(file_header)
        self.file_list_layout = GridLayout(cols=1, spacing=dp(8), size_hint_y=None)
        self.file_list_layout.bind(minimum_height=self.file_list_layout.setter("height"))
        scroll_files = ScrollView(size_hint=(1, None), size=(self.width, dp(220)))
        scroll_files.add_widget(self.file_list_layout)
        main_content.add_widget(scroll_files)

        # Activity log
        self.log_label = MDLabel(text="", size_hint_y=None, markup=False)
        self.log_label.bind(texture_size=self._update_log_height)
        log_scroll = ScrollView(size_hint=(1, None), height=dp(140))
        log_scroll.add_widget(self.log_label)
        main_content.add_widget(MDLabel(text="Activity Log", size_hint_y=None, height=dp(28)))
        main_content.add_widget(log_scroll)

        # wrap main_content in scroll
        main_scroll = ScrollView(scroll_type=['bars', 'content'], bar_width=dp(10), effect_cls='ScrollEffect')
        main_scroll.add_widget(main_content)
        self.add_widget(main_scroll)

        # Bottom controls
        bottom = FloatLayout(size_hint_y=None, height=dp(96))
        with bottom.canvas.before:
            Color(0.09,0.09,0.09,1)
            self.bottom_rect = RoundedRectangle(size=(0,0), pos=(0,0))
        bottom.bind(size=lambda i,v: setattr(self.bottom_rect, 'size', v), pos=lambda i,v: setattr(self.bottom_rect, 'pos', v))

        controls = BoxLayout(size_hint=(None,None), width=dp(360), height=dp(72), spacing=dp(12), pos_hint={'center_x':0.5,'center_y':0.5})
        self.stop_btn = MDIconButton(icon="stop", icon_size=dp(36), on_press=self.stop_playback)
        self.pause_btn = MDIconButton(icon="pause", icon_size=dp(36), on_press=self.toggle_pause)
        self.skip_btn = MDIconButton(icon="skip-next", icon_size=dp(36), on_press=self.skip_song)
        self.queue_btn = MDIconButton(icon="playlist-music", icon_size=dp(36), on_press=self.show_queue)
        controls.add_widget(self.stop_btn)
        controls.add_widget(self.pause_btn)
        controls.add_widget(self.skip_btn)
        controls.add_widget(self.queue_btn)
        bottom.add_widget(controls)
        self.add_widget(bottom)

    # Download UI helpers
    def show_download_progress(self):
        self.download_section.height = dp(70)
        self.download_section.opacity = 1

    def hide_download_progress(self):
        self.download_section.height = 0
        self.download_section.opacity = 0
        try:
            self.download_progress.value = 0
            self.download_status.text = ""
        except Exception:
            pass

    def update_download_progress(self, v, status):
        try:
            self.download_progress.value = v
            self.download_status.text = status
        except Exception:
            pass

    def update_download_title(self, title):
        try:
            self.download_title.text = title
        except Exception:
            pass

    # Stream UI helpers
    def show_stream_progress(self):
        self.stream_section.height = dp(50)
        self.stream_section.opacity = 1

    def hide_stream_progress(self):
        self.stream_section.height = 0
        self.stream_section.opacity = 0
        try:
            self.stream_progress.value = 0
            self.stream_status.text = ""
        except Exception:
            pass

    def update_stream_progress(self, v, status):
        try:
            self.stream_progress.value = v
            self.stream_status.text = status
        except Exception:
            pass

    # Cover art and track display
    def set_default_cover(self):
        try:
            default_icon_path = os.path.join(APP_DIR, "default_cover.png")
            log_safe(self.log, f"🖼️ set_default_cover: Looking for default at {default_icon_path}")

            if not os.path.exists(default_icon_path) and HAS_PIL:
                log_safe(self.log, "🖼️ Creating default cover icon...")
                created = self.create_default_cover_icon(default_icon_path)
                if created:
                    log_safe(self.log, f"✅ Default cover icon created at {default_icon_path}")
                else:
                    log_safe(self.log, "⚠️ Failed to create default cover icon")

            if os.path.exists(default_icon_path):
                try:
                    log_safe(self.log, f"🖼️ Loading default cover from {default_icon_path}")
                    core_img = CoreImage(default_icon_path)
                    if core_img and core_img.texture:
                        self.cover_art.texture = core_img.texture
                        self.cover_art.color = [1, 1, 1, 1]
                        log_safe(self.log, "✅ Default cover loaded successfully")
                        return
                    else:
                        log_safe(self.log, "⚠️ CoreImage loaded but texture is None")
                except Exception as e:
                    log_safe(self.log, f"❌ Failed to load default cover texture: {e}")
                    write_debug_file(f"Failed to load default cover: {e}\n{traceback.format_exc()}")
            else:
                log_safe(self.log, f"⚠️ Default cover file does not exist at {default_icon_path}")

            log_safe(self.log, "🖼️ Using gray placeholder color for cover art")
            self.cover_art.color = [0.3, 0.3, 0.3, 1]
        except Exception as e:
            log_safe(self.log, f"❌ Error in set_default_cover: {e}")
            write_debug_file(f"Error in set_default_cover: {e}\n{traceback.format_exc()}")
            self.cover_art.color = [0.3, 0.3, 0.3, 1]

    def create_default_cover_icon(self, path):
        if not HAS_PIL:
            return False
        try:
            img = PILImage.new('RGB', (512, 512), color=(50, 50, 50))
            draw = ImageDraw.Draw(img)
            draw.rectangle([128, 128, 384, 384], fill=(80, 80, 80))
            draw.polygon([(180, 280), (180, 200), (260, 240)], fill=(150, 150, 150))
            draw.ellipse([300, 200, 360, 260], fill=(150, 150, 150))
            img.save(path)
            return True
        except Exception as e:
            write_debug_file(f"Failed to create default cover icon: {e}")
            return False

    def update_cover_art(self, file_path=None, thumbnail_url=None):
        log_safe(self.log, f"🖼️ update_cover_art called: file_path={file_path}, thumbnail_url={thumbnail_url}")
        cover = None

        if file_path and os.path.exists(file_path):
            log_safe(self.log, f"🖼️ Attempting to extract cover from file: {os.path.basename(file_path)}")
            cover = extract_cover_from_file(file_path)
            if cover:
                log_safe(self.log, f"✅ Cover extracted from file: {cover}")
            else:
                log_safe(self.log, "⚠️ No cover found in file")

        if not cover and thumbnail_url:
            log_safe(self.log, f"🖼️ Attempting to download cover from URL: {thumbnail_url[:50]}...")
            cover = download_cover_art(thumbnail_url)
            if cover:
                log_safe(self.log, f"✅ Cover downloaded from URL: {cover}")
            else:
                log_safe(self.log, "⚠️ Failed to download cover from URL")

        if cover and os.path.exists(cover):
            try:
                log_safe(self.log, f"🖼️ Loading cover image texture from: {cover}")
                core_img = CoreImage(cover)
                if core_img and core_img.texture:
                    self.cover_art.texture = core_img.texture
                    self.cover_art.color = [1, 1, 1, 1]
                    log_safe(self.log, "✅ Cover art texture loaded and applied successfully")
                else:
                    log_safe(self.log, "⚠️ CoreImage loaded but texture is None")
                    self.set_default_cover()
            except Exception as e:
                log_safe(self.log, f"❌ Failed to load cover image texture: {e}")
                write_debug_file(f"Failed to load cover texture: {e}\n{traceback.format_exc()}")
                self.set_default_cover()
        else:
            log_safe(self.log, f"⚠️ No valid cover found, using default (cover={cover}, exists={os.path.exists(cover) if cover else False})")
            self.set_default_cover()

    def update_current_track(self, metadata, file_path=None):
        try:
            title = metadata.get("title", os.path.basename(file_path) if file_path else "Unknown")
            artist = metadata.get("uploader", metadata.get("artist", "Unknown Artist"))
            album = metadata.get("album", "Unknown Album")
            self.track_title.text = title
            self.track_artist.text = f"Artist: {artist}"
            self.track_album.text = f"Album: {album}"
            # duration label will be updated by progress ticks
        except Exception:
            pass
        # attempt to update cover
        try:
            thumb = metadata.get("thumbnail") if isinstance(metadata, dict) else None
            self.update_cover_art(file_path, thumb)
        except Exception:
            pass

    def update_queue_display(self, queue, current_index):
        try:
            remaining = max(0, len(queue) - current_index - 1)
            self.queue_info.text = f"Queue: {remaining} songs remaining"
        except Exception:
            pass

    def clear_queue_display(self):
        try:
            self.queue_info.text = "Queue: 0 songs"
        except Exception:
            pass

    # Playback progress update called by StreamPlayer
    def update_playback_progress(self, progress_percent, elapsed_seconds, total_seconds):
        try:
            if total_seconds and total_seconds > 0:
                self.progress_bar.value = min(100.0, progress_percent)
                left = max(0.0, total_seconds - elapsed_seconds)
                self.time_label.text = f"{format_time(elapsed_seconds)} / {format_time(total_seconds)}"
                self.track_duration.text = f"Duration: {format_time(total_seconds)}   Left: {format_time(left)}"
            else:
                # Unknown total: show elapsed only and unknown total (represented as '--:--')
                self.progress_bar.value = min(100.0, progress_percent) if progress_percent else 0
                total_display = "--:--"
                self.time_label.text = f"{format_time(elapsed_seconds)} / {total_display}"
                self.track_duration.text = f"Duration: Unknown"
        except Exception:
            pass

    def update_playback_state(self, state):
        try:
            if state == "Playing":
                self.pause_btn.icon = "pause"
            else:
                self.pause_btn.icon = "play"
        except Exception:
            pass

    def toggle_pause(self, _=None):
        try:
            if self.streamer.sound:
                self.streamer.toggle_pause()
            elif self.current_sound:
                self.toggle_local_pause()
        except Exception as e:
            log_safe(self.log, f"⚠️ toggle_pause error: {e}")
            write_debug_file(traceback.format_exc())

    # Local file playback (from downloaded songs)
    def play_audio(self, filepath):
        try:
            if self.current_sound:
                try:
                    self.current_sound.stop()
                    self.current_sound.unload()
                except Exception:
                    pass
            self.current_sound = SoundLoader.load(filepath)
            if self.current_sound:
                self.local_play_start_time = time.time()
                self.local_total_paused_time = 0.0
                self.local_last_pause_time = 0.0
                self.local_is_paused = False
                try:
                    self.current_sound.play()
                except Exception:
                    pass
                metadata = get_metadata(filepath)
                self.update_current_track(metadata, filepath)
                self.start_local_progress_updates(filepath, metadata)
                self.log(f"🎧 Now Playing: {os.path.basename(filepath)}")
            else:
                self.log("⚠️ Failed to load audio.")
        except Exception as e:
            log_safe(self.log, f"❌ play_audio error: {e}")
            write_debug_file(traceback.format_exc())

    def start_local_progress_updates(self, filepath, metadata):
        self.stop_local_progress_updates()
        try:
            self.local_progress_event = Clock.schedule_interval(lambda dt: self.update_local_progress(filepath, metadata), 0.5)
        except Exception:
            self.local_progress_event = None

    def stop_local_progress_updates(self):
        if hasattr(self, "local_progress_event") and self.local_progress_event:
            try:
                self.local_progress_event.cancel()
            except Exception:
                pass
            self.local_progress_event = None

    def update_local_progress(self, filepath, metadata):
        try:
            if not self.current_sound:
                return
            if getattr(self.current_sound, "state", None) != "play" and not self.local_is_paused:
                return
            elapsed = time.time() - self.local_play_start_time - self.local_total_paused_time
            # duration best-effort
            duration = get_duration_best_effort(filepath, metadata)
            if duration and duration > 0:
                progress = (elapsed / duration) * 100.0
                self.progress_bar.value = min(100.0, progress)
                self.time_label.text = f"{format_time(elapsed)} / {format_time(duration)}"
                self.track_duration.text = f"Duration: {format_time(duration)}   Left: {format_time(max(0.0, duration - elapsed))}"
            else:
                # unknown duration
                self.time_label.text = f"{format_time(elapsed)} / --:--"
                self.track_duration.text = "Duration: Unknown"
        except Exception:
            pass

    def toggle_local_pause(self):
        if self.local_is_paused:
            try:
                self.current_sound.play()
            except Exception:
                pass
            self.local_is_paused = False
            try:
                self.pause_btn.icon = "pause"
            except Exception:
                pass
            if self.local_last_pause_time > 0:
                self.local_total_paused_time += time.time() - self.local_last_pause_time
                self.local_last_pause_time = 0
        else:
            try:
                self.current_sound.stop()
            except Exception:
                pass
            self.local_is_paused = True
            try:
                self.pause_btn.icon = "play"
            except Exception:
                pass
            self.local_last_pause_time = time.time()

    def skip_song(self, _=None):
        if self.streamer.sound and getattr(self.streamer.sound, "state", None) == "play":
            self.streamer.skip()
        else:
            self.log("⚠️ No active stream to skip.")

    def show_queue(self, _=None):
        self.streamer.show_queue()

    def stop_playback(self, _=None):
        try:
            self.streamer.stop()
            if self.current_sound:
                try:
                    self.current_sound.stop()
                    self.current_sound.unload()
                except Exception:
                    pass
                self.current_sound = None
            self.log("⏹ Playback stopped.")
            self.progress_bar.value = 0
            self.time_label.text = "00:00 / 00:00"
            self.local_is_paused = False
            self.local_total_paused_time = 0
            self.local_last_pause_time = 0
            self.set_default_cover()
        except Exception as e:
            log_safe(self.log, f"⚠️ stop_playback error: {e}")
            write_debug_file(traceback.format_exc())

    # Activity log (UI + persistent)
    def log(self, message):
        try:
            lines = self.log_label.text.split('\n') if self.log_label.text else []
            if len(lines) > 300:
                lines = lines[-200:]
            lines.append(message)
            self.log_label.text = '\n'.join(lines)
        except Exception:
            pass
        try:
            write_debug_file(message)
        except Exception:
            pass

    def _update_log_height(self, instance, size):
        try:
            self.log_label.height = max(size[1], self.log_label.height)
            self.log_label.text_size = (self.log_label.width, None)
        except Exception:
            pass

    # File list management
    def refresh_file_list(self):
        self.file_list_layout.clear_widgets()
        audio_extensions = (
            ".mp3", ".m4a", ".webm", ".opus", ".ogg", ".wav", ".flac",
            ".aac", ".wma", ".mp4", ".m4b", ".oga", ".spx", ".ape",
            ".wv", ".mpc", ".aiff", ".aif", ".au", ".mid", ".midi"
        )
        try:
            files = sorted([f for f in os.listdir(APP_DIR) if f.lower().endswith(audio_extensions)])
        except Exception:
            files = []
        for f in files:
            song_card = MDCard(size_hint_y=None, height=dp(88), md_bg_color=[0.09,0.09,0.09,1], radius=[dp(8)], padding=dp(8))
            card_layout = BoxLayout(spacing=dp(12))
            play_btn = MDIconButton(icon="play-circle", icon_size=dp(36), size_hint_x=None, width=dp(56))
            play_btn.bind(on_press=lambda inst, file=f: self.play_audio(os.path.join(APP_DIR, file)))
            info_layout = BoxLayout(orientation="vertical")
            label = MDLabel(text=f[:35] + "..." if len(f) > 35 else f, font_style="Body1")
            info_layout.add_widget(label)
            del_btn = MDIconButton(icon="delete", icon_size=dp(30), size_hint_x=None, width=dp(56))
            del_btn.bind(on_press=lambda inst, file=f: self.delete_audio(os.path.join(APP_DIR, file)))
            card_layout.add_widget(play_btn)
            card_layout.add_widget(info_layout)
            card_layout.add_widget(del_btn)
            song_card.add_widget(card_layout)
            self.file_list_layout.add_widget(song_card)
        try:
            self.file_list_layout.height = len(self.file_list_layout.children) * dp(100)
        except Exception:
            pass

    def show_metadata(self, file_path):
        metadata = get_metadata(file_path)
        dialog = ModalView(size_hint=(0.8, 0.7))
        layout = BoxLayout(orientation="vertical", padding=10, spacing=10)
        layout.add_widget(Label(text="[b]Track Metadata[/b]", size_hint_y=0.1, markup=True))
        cover = extract_cover_from_file(file_path)
        if cover and os.path.exists(cover):
            layout.add_widget(Image(source=cover, size_hint_y=0.3))
        details = BoxLayout(orientation="vertical", size_hint_y=0.4)
        details.add_widget(Label(text=f"Title: {metadata.get('title','Unknown')}", size_hint_y=0.25))
        details.add_widget(Label(text=f"Artist: {metadata.get('artist','Unknown')}", size_hint_y=0.25))
        details.add_widget(Label(text=f"Album: {metadata.get('album','Unknown')}", size_hint_y=0.25))
        details.add_widget(Label(text=f"File: {file_path}", size_hint_y=0.25))
        layout.add_widget(details)
        close = Button(text="Close", size_hint_y=0.1)
        close.bind(on_press=lambda x: dialog.dismiss())
        layout.add_widget(close)
        dialog.add_widget(layout)
        dialog.open()

    def delete_audio(self, file_path):
        try:
            coverpath = extract_cover_from_file(file_path)
            if coverpath and os.path.exists(coverpath):
                try:
                    os.remove(coverpath)
                except Exception:
                    pass
            os.remove(file_path)
            self.log(f"🗑️ Deleted: {os.path.basename(file_path)}")
            self.refresh_file_list()
        except Exception as e:
            self.log(f"❌ Error deleting {file_path}: {e}")
            write_debug_file(traceback.format_exc())

    # UI button bindings
    def start_download(self, _=None):
        url = self.url_input.text.strip()
        if not url:
            self.log("⚠️ Please enter a link to download.")
            return
        self.downloader.start_download(url)

    def start_stream(self, _=None):
        url = self.url_input.text.strip()
        if not url:
            self.log("⚠️ Please enter a playlist/URL to stream.")
            return
        self.log("📡 Starting stream...")
        threading.Thread(target=self.streamer.stream_playlist, args=(url,), daemon=True).start()

# --------------------------- Small helpers for cover extraction & metadata fallback ---------------------------
def extract_cover_from_file(file_path, cache_dir=None):
    """Try to extract embedded cover from file using mutagen (returns a cached image path)"""
    if cache_dir is None:
        cache_dir = get_writable_directory("cover_cache")
    os.makedirs(cache_dir, exist_ok=True)

    try:
        if not HAS_MUTAGEN:
            write_debug_file(f"extract_cover: mutagen not available")
            return None

        if not os.path.exists(file_path):
            write_debug_file(f"extract_cover: file does not exist: {file_path}")
            return None

        write_debug_file(f"extract_cover: Processing {os.path.basename(file_path)}")
        audio = None

        try:
            audio = ID3(file_path)
            write_debug_file(f"extract_cover: Loaded as ID3")
        except Exception as e:
            write_debug_file(f"extract_cover: Not ID3, trying mutagen.File: {e}")
            try:
                audio = mutagen.File(file_path)
                write_debug_file(f"extract_cover: Loaded with mutagen.File")
            except Exception as e2:
                write_debug_file(f"extract_cover: Failed to load with mutagen.File: {e2}")
                audio = None

        if audio is None:
            write_debug_file(f"extract_cover: No audio metadata found")
            return None

        # For MP3 with ID3 tags
        try:
            if isinstance(audio, ID3) or hasattr(audio, 'getall'):
                write_debug_file(f"extract_cover: Checking ID3 tags for images")
                for tag in audio.values():
                    if hasattr(tag, "data") and hasattr(tag, "mime"):
                        imgdata = getattr(tag, "data", None)
                        if imgdata:
                            fname = os.path.join(cache_dir, f"cover_{abs(hash(file_path))}.jpg")
                            with open(fname, "wb") as f:
                                f.write(imgdata)
                            write_debug_file(f"extract_cover: ✅ Extracted ID3 cover to {fname} ({len(imgdata)} bytes)")
                            return fname
                write_debug_file(f"extract_cover: No image data found in ID3 tags")
        except Exception as e:
            write_debug_file(f"extract_cover: Error checking ID3 tags: {e}\n{traceback.format_exc()}")

        # For other containers (FLAC, OGG, etc)
        try:
            pics = getattr(audio, "pictures", None)
            if pics:
                write_debug_file(f"extract_cover: Found {len(pics)} pictures in audio.pictures")
                for i, p in enumerate(pics):
                    try:
                        fname = os.path.join(cache_dir, f"cover_{abs(hash(file_path))}.jpg")
                        with open(fname, "wb") as f:
                            f.write(p.data)
                        write_debug_file(f"extract_cover: ✅ Extracted picture {i} to {fname} ({len(p.data)} bytes)")
                        return fname
                    except Exception as e:
                        write_debug_file(f"extract_cover: Error extracting picture {i}: {e}")
            else:
                write_debug_file(f"extract_cover: No pictures attribute found")
        except Exception as e:
            write_debug_file(f"extract_cover: Error checking pictures: {e}\n{traceback.format_exc()}")

        write_debug_file(f"extract_cover: No cover art found in {os.path.basename(file_path)}")
    except Exception as e:
        write_debug_file(f"extract_cover: Unexpected error: {e}\n{traceback.format_exc()}")

    return None

def download_cover_art(url, cache_dir=None, filename=None):
    """Download cover art from URL and cache it locally."""
    if not HAS_REQUESTS or not url:
        return None
    if cache_dir is None:
        cache_dir = get_writable_directory("cover_cache")
    os.makedirs(cache_dir, exist_ok=True)
    if filename is None:
        filename = f"cover_{abs(hash(url))}.jpg"
    cache_file = os.path.join(cache_dir, filename)
    if os.path.exists(cache_file):
        return cache_file
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            with open(cache_file, "wb") as f:
                f.write(r.content)
            return cache_file
    except Exception:
        pass
    return None

def get_metadata(file_path):
    """Return a dict with title/artist/album best-effort via mutagen, otherwise filename fallback."""
    if HAS_MUTAGEN:
        try:
            f = mutagen.File(file_path, easy=True)
            if f:
                title = f.get("title", [None])[0]
                artist = f.get("artist", [None])[0]
                album = f.get("album", [None])[0]
                return {
                    "title": title or os.path.basename(file_path),
                    "artist": artist or "Unknown Artist",
                    "album": album or "Unknown Album",
                }
        except Exception:
            pass
    return {
        "title": os.path.basename(file_path),
        "artist": "Unknown Artist",
        "album": "Unknown Album",
    }

# --------------------------- Utilities for formatting ---------------------------
def format_time(seconds):
    try:
        s = int(round(seconds))
        m = s // 60
        ss = s % 60
        return f"{m:02d}:{ss:02d}"
    except Exception:
        return "00:00"

# --------------------------- App Entry ---------------------------
class AudioApp(MDApp):
    def build(self):
        self.title = "🎵 Music Player"
        try:
            self.theme_cls.theme_style = "Dark"
            self.theme_cls.primary_palette = "Green"
        except Exception:
            pass
        # Ensure app dir exists
        try:
            os.makedirs(APP_DIR, exist_ok=True)
        except Exception:
            pass
        return DownloaderUI()

if __name__ == "__main__":
    try:
        AudioApp().run()
    except Exception as e:
        write_debug_file(f"Application crashed: {e}")
        write_debug_file(traceback.format_exc())
        try:
            print("Application error:", e)
        except Exception:
            pass
