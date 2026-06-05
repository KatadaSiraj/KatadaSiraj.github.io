import os
import re
import uuid
import json
import zipfile
import tempfile
import threading
import time
from pathlib import Path
from flask import Flask, jsonify, request, send_file, Response
from flask_cors import CORS
import yt_dlp
import musicbrainzngs
import requests
from mutagen.id3 import (
    ID3, TIT2, TPE1, TALB, TRCK, TDRC, TCON, TPE2,
    APIC, TPOS, COMM, ID3NoHeaderError
)
from mutagen.mp3 import MP3

app = Flask(__name__)
CORS(app)

# ── MusicBrainz setup ──────────────────────────────────────────────────────
musicbrainzngs.set_useragent("PlaylistDownloader", "1.0", "https://github.com/KatadaSiraj/playlist-downloader")

# ── In-memory job store ────────────────────────────────────────────────────
jobs: dict[str, dict] = {}   # job_id → {status, progress, tracks, error, zip_path}

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "playlist_downloader"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ── Cleanup old jobs every 30 min ─────────────────────────────────────────
def _cleanup():
    while True:
        time.sleep(1200)
        cutoff = time.time() - 2400
        for jid in list(jobs):
            if jobs[jid].get("created_at", 0) < cutoff:
                zp = jobs[jid].get("zip_path")
                if zp and os.path.exists(zp):
                    os.remove(zp)
                del jobs[jid]

threading.Thread(target=_cleanup, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def extract_playlist_info(url: str) -> list[dict]:
    """Return list of {title, url, channel} from a YouTube playlist."""
    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "skip_download": True,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or "entries" not in info:
        raise ValueError("Could not extract playlist. Check the URL.")
    tracks = []
    for entry in info["entries"]:
        if entry:
            tracks.append({
                "id": entry.get("id", ""),
                "title": entry.get("title", "Unknown"),
                "url": f"https://www.youtube.com/watch?v={entry.get('id', '')}",
                "channel": entry.get("channel") or entry.get("uploader", ""),
                "duration": entry.get("duration"),
                "thumbnail": entry.get("thumbnail") or (
                    f"https://img.youtube.com/vi/{entry.get('id','')}/mqdefault.jpg"
                ),
            })
    return tracks


def search_musicbrainz(title: str, artist: str = "") -> dict:
    """Query MusicBrainz for accurate metadata. Returns best-match dict."""
    query = f'recording:"{title}"'
    if artist:
        query += f' AND artist:"{artist}"'
    try:
        result = musicbrainzngs.search_recordings(
            query=query, limit=1, includes=["artists", "releases"]
        )
        recordings = result.get("recording-list", [])
        if not recordings:
            return {}
        rec = recordings[0]
        meta = {
            "title": rec.get("title", title),
        }
        # Artist
        artist_credits = rec.get("artist-credit", [])
        if artist_credits:
            meta["artist"] = artist_credits[0].get("artist", {}).get("name", artist)
        # Release info
        releases = rec.get("release-list", [])
        if releases:
            rel = releases[0]
            meta["album"] = rel.get("title", "")
            meta["date"] = rel.get("date", "")[:4]  # year only
            meta["track_number"] = rel.get("medium-list", [{}])[0]\
                .get("track-list", [{}])[0].get("number", "")
        # Genre (MusicBrainz tags)
        tags = rec.get("tag-list", [])
        if tags:
            meta["genre"] = tags[0].get("name", "")
        return meta
    except Exception:
        return {}


def fetch_cover_art(mbid: str) -> bytes | None:
    """Fetch cover art from Cover Art Archive."""
    try:
        url = f"https://coverartarchive.org/release/{mbid}/front-250"
        r = requests.get(url, timeout=10, allow_redirects=True)
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return None


def download_audio(video_url: str, out_dir: Path) -> Path:
    """Download best audio as MP3 via yt-dlp. Returns path to file."""
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }],
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        vid_id = info["id"]
    mp3_path = out_dir / f"{vid_id}.mp3"
    if not mp3_path.exists():
        # fallback if extension differs
        for f in out_dir.glob(f"{vid_id}.*"):
            if f.suffix == ".mp3":
                mp3_path = f
                break
    return mp3_path


def write_tags(mp3_path: Path, meta: dict, options: dict, cover_bytes: bytes | None):
    """Write ID3 tags to MP3 based on user-selected options."""
    try:
        tags = ID3(str(mp3_path))
    except ID3NoHeaderError:
        tags = ID3()

    def set_tag(flag_key, tag_class, value):
        if options.get(flag_key) and value:
            tags.add(tag_class(encoding=3, text=str(value)))

    set_tag("title",        TIT2, meta.get("title"))
    set_tag("artist",       TPE1, meta.get("artist"))
    set_tag("album",        TALB, meta.get("album"))
    set_tag("album_artist", TPE2, meta.get("artist"))  # album artist = artist
    set_tag("track_number", TRCK, meta.get("track_number"))
    set_tag("disc_number",  TPOS, meta.get("disc_number"))
    set_tag("year",         TDRC, meta.get("date"))
    set_tag("genre",        TCON, meta.get("genre"))

    if options.get("cover_art") and cover_bytes:
        tags.add(APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,      # front cover
            desc="Cover",
            data=cover_bytes,
        ))

    tags.save(str(mp3_path), v2_version=3)


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


# ═══════════════════════════════════════════════════════════════════════════
# BACKGROUND WORKER
# ═══════════════════════════════════════════════════════════════════════════

def process_job(job_id: str, tracks: list[dict], options: dict):
    job = jobs[job_id]
    job["status"] = "downloading"
    total = len(tracks)
    work_dir = DOWNLOAD_DIR / job_id
    work_dir.mkdir(exist_ok=True)

    downloaded_files = []

    for idx, track in enumerate(tracks):
        if job.get("cancelled"):
            job["status"] = "cancelled"
            return

        # Update per-track status
        job["current_track"] = track["title"]
        job["progress"] = idx / total

        try:
            # 1. Download audio
            job["tracks"][idx]["status"] = "downloading"
            mp3_path = download_audio(track["url"], work_dir)

            # 2. MusicBrainz lookup
            job["tracks"][idx]["status"] = "tagging"
            artist_hint = track.get("channel", "")
            mb_meta = search_musicbrainz(track["title"], artist_hint)
            # Fallback to YouTube title/channel if MB misses
            meta = {
                "title":        mb_meta.get("title")        or track["title"],
                "artist":       mb_meta.get("artist")       or artist_hint,
                "album":        mb_meta.get("album")        or "",
                "track_number": mb_meta.get("track_number") or str(idx + 1),
                "disc_number":  mb_meta.get("disc_number")  or "1",
                "date":         mb_meta.get("date")         or "",
                "genre":        mb_meta.get("genre")        or "",
            }

            # 3. Cover art
            cover_bytes = None
            if options.get("cover_art"):
                releases = mb_meta.get("releases", [])
                mbid = releases[0].get("id") if releases else None
                if mbid:
                    cover_bytes = fetch_cover_art(mbid)

            # 4. Write tags
            write_tags(mp3_path, meta, options, cover_bytes)

            # 5. Rename to clean filename
            clean_name = safe_filename(f"{meta['artist']} - {meta['title']}.mp3" if meta["artist"] else f"{meta['title']}.mp3")
            final_path = work_dir / clean_name
            mp3_path.rename(final_path)
            downloaded_files.append(final_path)

            job["tracks"][idx]["status"] = "done"
            job["tracks"][idx]["meta"] = meta

        except Exception as e:
            job["tracks"][idx]["status"] = "error"
            job["tracks"][idx]["error"] = str(e)
            print(f"[ERROR] Track {idx} ({track['title']}): {e}")

        job["progress"] = (idx + 1) / total

    # 6. Zip everything
    job["status"] = "zipping"
    zip_path = DOWNLOAD_DIR / f"{job_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in downloaded_files:
            zf.write(fp, fp.name)

    # 7. Cleanup work dir
    import shutil
    shutil.rmtree(work_dir, ignore_errors=True)

    job["status"] = "done"
    job["zip_path"] = str(zip_path)
    job["progress"] = 1.0


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/playlist", methods=["POST"])
def get_playlist():
    """Preview a playlist before downloading."""
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    try:
        tracks = extract_playlist_info(url)
        return jsonify({"tracks": tracks, "count": len(tracks)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    """Kick off a background download job."""
    data = request.json or {}
    url = data.get("url", "").strip()
    options = data.get("options", {})
    tracks = data.get("tracks")   # optionally pass pre-fetched track list

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = str(uuid.uuid4())
    try:
        if not tracks:
            tracks = extract_playlist_info(url)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    track_states = [
        {"title": t["title"], "url": t["url"], "status": "queued", "thumbnail": t.get("thumbnail")}
        for t in tracks
    ]

    jobs[job_id] = {
        "status": "queued",
        "progress": 0.0,
        "tracks": track_states,
        "current_track": "",
        "zip_path": None,
        "error": None,
        "created_at": time.time(),
    }

    thread = threading.Thread(
        target=process_job,
        args=(job_id, tracks, options),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id, "track_count": len(tracks)})


@app.route("/api/status/<job_id>")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status":        job["status"],
        "progress":      job["progress"],
        "current_track": job.get("current_track", ""),
        "tracks":        job["tracks"],
        "error":         job.get("error"),
    })


@app.route("/api/download/<job_id>")
def download_zip(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "done":
        return jsonify({"error": "Not ready yet"}), 202
    zip_path = job.get("zip_path")
    if not zip_path or not os.path.exists(zip_path):
        return jsonify({"error": "File missing"}), 404
    return send_file(
        zip_path,
        as_attachment=True,
        download_name="playlist.zip",
        mimetype="application/zip",
    )


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job["cancelled"] = True
    return jsonify({"ok": True})


@app.route("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
