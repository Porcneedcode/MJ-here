import asyncio
import os
import random
import re
import sys
import datetime
import time
from collections import defaultdict
from typing import Optional, cast
from dotenv import load_dotenv

import discord
from discord import app_commands, VoiceClient, ButtonStyle, Interaction, ui, Embed
from discord.ext import commands
from discord.ui import View
from discord.ext import tasks

import yt_dlp
from concurrent.futures import ProcessPoolExecutor
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

import aiohttp
from io import BytesIO
from PIL import Image

from flask import Flask
from threading import Thread

import io
import json
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseDownload

load_dotenv()

def download_cookie_from_drive():
    """โหลด cookies.txt จาก Google Drive โดยใช้ service account ที่เก็บใน Environment Variable"""
    try:
        service_json_str = os.getenv("mj-need-cookie-58f1c6a3a99e.json")
        file_id = os.getenv("1RARypdufcb5T6z0pG5bsPPMc-AF1Vaoh")

        if not service_json_str or not file_id:
            print("⚠️ GOOGLE_SERVICE_JSON หรือ COOKIE_FILE_ID ไม่ได้ตั้งค่าใน Environment")
            return False

        service_info = json.loads(service_json_str)
        creds = service_account.Credentials.from_service_account_info(
            service_info,
            scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )

        service = build("drive", "v3", credentials=creds)
        request = service.files().get_media(fileId=file_id)
        fh = io.FileIO("cookies.txt", "wb")
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            print(f"Downloading cookies: {int(status.progress() * 100)}%")

        print("✅ Cookie.txt downloaded successfully!")
        return True
    except Exception as e:
        print(f"⚠️ โหลด cookie.txt จาก Drive ไม่สำเร็จ: {e}")
        return False

download_cookie_from_drive()

app = Flask('')

@app.route('/')
def home():
    return "I'm alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

preload_queue = asyncio.Queue()
auto_leave_timers = defaultdict(int)
last_ui_message = None
music_queue = None

def ytdl_extract_info(url, download):
    ytdl_format_options = {
        'format': 'bestaudio/best',
        'merge_output_format': 'm4a',
        'quiet': True,
        'default_search': 'ytsearch',
        'extract_flat': False,
        'source_address': '0.0.0.0',
        'nocheckcertificate': True,
        'geo_bypass': True,
        'cachedir': False,
        'no_warnings': True,
        'extractor_args': {'youtube': {'player_client': ['android', 'web', 'ios']}}
    }

    cookies_path = "cookies.txt"
    env_cookies = os.getenv("YT_COOKIES")
    temp_cookie = False

    print(f"DEBUG: current directory = {os.getcwd()}")
    print(f"DEBUG: cookies.txt exists = {os.path.exists(cookies_path)}")
    print(f"DEBUG: env YT_COOKIES found = {bool(env_cookies)}")

    if os.path.exists(cookies_path):
        ytdl_format_options["cookiefile"] = cookies_path
        print("✅ ใช้ cookies.txt โหลดเพลง")
    elif env_cookies:
        if not env_cookies.strip().startswith("#"):
            env_cookies = "# Netscape HTTP Cookie File\n" + env_cookies.strip()
        with open(cookies_path, "w", encoding="utf-8") as f:
            f.write(env_cookies)
        ytdl_format_options["cookiefile"] = cookies_path
        temp_cookie = True
        print("✅ ใช้ cookies จาก Environment Variable โหลดเพลง")
    else:
        print("⚠️ ไม่พบ cookies → โหลดแบบปกติ (อาจเล่นไม่ได้ถ้าเป็นวิดีโอจำกัดอายุ)")

    with yt_dlp.YoutubeDL(ytdl_format_options) as ytdl:
        info = ytdl.extract_info(url, download=download)

    if temp_cookie and os.path.exists(cookies_path):
        os.remove(cookies_path)
        print("🧹 ลบ cookies.txt ชั่วคราวแล้ว")

    return info

async def connect_voice(channel):
    for attempt in range(3):
        try:
            return await channel.connect()
        except discord.errors.ConnectionClosed as e:
            if e.code == 4006:
                await asyncio.sleep(2)
                continue
            else:
                raise
    return None

class MusicQueue:
    def __init__(self):
        self._queue = []
        self.now_playing = None
        self.lock = asyncio.Lock()
        self.skip_requested = asyncio.Event()
        self.playing_lock = asyncio.Lock()
        self.leave_after_current = False
        self.preloaded_streams = {}
        self.stopping = False

    async def add(self, items):
        async with self.lock:
            self.stopping = False
            for item in items:
                if isinstance(item, dict) and 'url' in item and 'title' in item:
                    self._queue.append({
                        "url": item.get("url") or item.get("webpage_url"),
                        "webpage_url": item.get("webpage_url"),
                        "title": item.get("title", "เพลงไม่มีชื่อ 🤷‍♂️"),
                        "duration": item.get("duration")
                    })
                    continue

                url = str(item)
                loop = asyncio.get_event_loop()
                try:
                    data = await loop.run_in_executor(None, lambda: ytdl_extract_info(url, download=False))
                except Exception as e:
                    print(f"[YTDL Error] {e}")
                    continue

                if not data:
                    continue

                if 'entries' in data:
                    for entry in data['entries']:
                        if not entry:
                            continue
                        self._queue.append({
                            "url": entry.get("url") or entry.get("webpage_url"),
                            "webpage_url": entry.get("webpage_url"),
                            "title": entry.get("title", "เพลงไม่มีชื่อ 🤷‍♂️"),
                            "duration": entry.get("duration")
                        })
                else:
                    self._queue.append({
                        "url": data.get("url") or data.get("webpage_url"),
                        "webpage_url": data.get("webpage_url"),
                        "title": data.get("title", "เพลงไม่มีชื่อ 🤷‍♂️"),
                        "thumbnail": data.get("thumbnail"),
                        "duration": data.get("duration")
                    })

            asyncio.create_task(self._preload_streams())

    async def _preload_streams(self):
        async with self.lock:
            to_preload = self._queue[:3]

            for item in to_preload:
                title = item["title"]

                cached_url = self.preloaded_streams.get(title)

                if cached_url:
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.head(cached_url, timeout=5) as resp:
                                if resp.status != 200:
                                    print(f"[preload] cached stream_url for '{title}' expired (status {resp.status})")
                                    cached_url = None
                    except Exception as e:
                        print(f"[preload] Error checking cached URL for '{title}': {e}")
                        cached_url = None

                if not cached_url:
                    stream_url = await get_stream_url(item)
                    if stream_url:
                        self.preloaded_streams[title] = stream_url
                        print(f"[preload] Preloaded: {title}")

    async def get_next(self):
        async with self.lock:
            if self.stopping:
                return None
            if not self._queue:
                return None
            self.now_playing = self._queue.pop(0)
            title = self.now_playing["title"]
            if title in self.preloaded_streams:
                self.now_playing["stream_url"] = self.preloaded_streams.pop(title)
            else:
                self.now_playing["stream_url"] = await get_stream_url(self.now_playing)
            asyncio.create_task(self._preload_streams())
            return self.now_playing

    async def clear(self):
        async with self.lock:
            self._queue.clear()
            self.preloaded_streams.clear()
            self.skip_requested.set()
            self.stopping = True

    async def skip(self):
        self.skip_requested.set()

    async def request_leave_after_current(self):
        self.leave_after_current = True

    def reset_leave_request(self):
        self.leave_after_current = False

    def should_leave_after_current(self) -> bool:
        return self.leave_after_current
    
    def is_empty(self):
        return len(self._queue) == 0 and self.now_playing is None

    @property
    def queue(self):
        return self._queue

def extract_info(url):
    return ytdl_extract_info(url, download=False)

class YTDLSource:
    yt_dlp_executor = ProcessPoolExecutor()

    @classmethod
    async def from_url(cls, url: str, loop: Optional[asyncio.AbstractEventLoop] = None):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(
            cls.yt_dlp_executor,
            extract_info, url
        )

        if 'entries' in data:
            return data['entries']
        else:
            return [data]

class MusicControlView(View):
    def __init__(self, interaction: Interaction):
        super().__init__(timeout=None)
        self.interaction = interaction
        self.bot = bot

    @ui.button(label="⏸️ หยุดชั่วคราว = ถาวร", style=ButtonStyle.grey)
    async def pause_button(self, interaction: Interaction, _):
        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            await interaction.followup.send("สงสัยปุ่มหลุด QC มาแน่ๆ เลย 😥", ephemeral=True)
            return

        vc_raw = interaction.guild.voice_client
        vc = cast(Optional[VoiceClient], vc_raw)
        if vc and vc.is_playing():
            vc.pause()
            await interaction.followup.send("พักคอแปปนึง 😖", ephemeral=True)
        else:
            await interaction.followup.send("เราหยุดแล้ว แล้วท่านล่ะหยุดกดเล่นได้ยัง 😌✨", ephemeral=True)

    @ui.button(label="▶️ เล่นต่อดิ รอไร", style=ButtonStyle.green)
    async def resume_button(self, interaction: Interaction, _):
        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            await interaction.followup.send("กดปุ่มนี้ได้ไงเนี่ย 🤯", ephemeral=True)
            return

        vc_raw = interaction.guild.voice_client
        vc = cast(Optional[VoiceClient], vc_raw)
        if vc and vc.is_paused():
            vc.resume()
            await interaction.followup.send("จะร้องต่อแล้วนะ 🎤", ephemeral=True)
        else:
            await interaction.followup.send("เพลงไม่ได้หยุดนิ 🫤", ephemeral=True)

    @ui.button(label="⏭️ บิด! ข้ามเพลง", style=ButtonStyle.blurple)
    async def skip(self, interaction: Interaction, _):
        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            await interaction.followup.send("Bro กดปุ่มนี้ได้ไงเนี่ย ‼️", ephemeral=True)
            return

        self.bot.loop.create_task(self._handle_skip(interaction))

    async def _handle_skip(self, interaction: Interaction):
        vc_raw = interaction.guild.voice_client
        vc = cast(Optional[VoiceClient], vc_raw)
        global music_queue

        if vc and music_queue is not None:
            if len(music_queue._queue) > 0:
                await music_queue.skip()
                vc.stop()
                await interaction.followup.send("เบื่อเพลงแล้วหรอ เปลี่ยนเพลงก็ได้ 🫨", ephemeral=True)
            else:
                await interaction.followup.send("จะกดเล่นหาพระแสงไรรึ 😠", ephemeral=True)
        else:
            await interaction.followup.send("หู้ย! กดได้ไงทั้งๆ ที่มันเป็นไปไม่ได้ 😦", ephemeral=True)

    @ui.button(label="⏹️ หยุดเพลง & yoink!", style=ButtonStyle.red)
    async def stop_button(self, interaction: Interaction, _):
        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            await interaction.followup.send("สงสัยปุ่มควบคุมเพลงกาก ไปโทษคนสร้างฉันโน้น 👉", ephemeral=True)
            return

        self.bot.loop.create_task(self._handle_stop(interaction))

    async def _handle_stop(self, interaction: Interaction):
        vc_raw = interaction.guild.voice_client
        vc = cast(Optional[VoiceClient], vc_raw)
        global music_queue

        if vc:
            if music_queue is not None:
                await music_queue.clear()
            vc.stop()
            await interaction.followup.send("ขี้เกียจร้องล่ะ เลิกดีกว่า 🤐", ephemeral=True)
        else:
            await interaction.followup.send("ยังไม่มีเพลงใน list เลยจะเล่นได้ไงเล่า 📃", ephemeral=True)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SFX_JOIN_FOLDER = os.path.join(BASE_DIR, "sfx")
SFX_COMMAND_FOLDER = os.path.join(BASE_DIR, "sfxlol") 

async def play_sfx(vc: VoiceClient):
    if os.path.exists(SFX_JOIN_FOLDER):
        sfx_files = [f for f in os.listdir(SFX_JOIN_FOLDER) if f.endswith((".mp3", ".wav", ".ogg"))]
        if sfx_files:
            chosen_file = os.path.join(SFX_JOIN_FOLDER, random.choice(sfx_files))
            source = discord.FFmpegPCMAudio(chosen_file)
            if not vc.is_playing():
                vc.play(source)
    
async def get_stream_url(item):
    if "stream_url" in item and item["stream_url"]:
        return item["stream_url"]
    
    if "url" in item and item["url"]:
        item["stream_url"] = item["url"]
        return item["url"]

    url = item.get("webpage_url", item.get("url"))
    if not url:
        print("[get_stream_url] ไม่มี URL ให้โหลด 😥")
        return None

    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(
            None,
            lambda: ytdl_extract_info(url, download=False)
        )
    except Exception as e:
        print(f"[get_stream_url] Error: {e}")
        return None

    if not data:
        print("[get_stream_url] ไม่ได้ข้อมูลจาก yt_dlp")
        return None

    audio_formats = [
        f for f in data.get("formats", [])
        if f.get("acodec") != "none" and f.get("vcodec") == "none"
    ]
    best_audio = max(audio_formats, key=lambda f: f.get("abr", 0), default=None)

    stream_url = best_audio["url"] if best_audio else data.get("url")
    if not stream_url:
        print("[get_stream_url] ไม่มี stream_url เลย 😶")
        return None

    if "thumbnail" not in item:
        if data.get("thumbnail"):
            item["thumbnail"] = data["thumbnail"]
        elif data.get("thumbnails"):
            item["thumbnail"] = data["thumbnails"][-1]["url"]

    item["stream_url"] = stream_url
    return stream_url

async def play_next(interaction: discord.Interaction, thinking_msg: Optional[discord.Message] = None):
    global last_ui_message, music_queue

    if music_queue is None:
        if not interaction.response.is_done():
            await interaction.response.send_message("hmmm เพลงไม่มี 🗿", ephemeral=True)
        else:
            await interaction.followup.send("hmmm เพลงไม่มี 🗿", ephemeral=True)
        return
    
    vc_raw = interaction.guild.voice_client if interaction.guild else None
    vc = cast(Optional[VoiceClient], vc_raw)

    local_music_queue = music_queue
    source = await local_music_queue.get_next()
    
    if source is None:
        if local_music_queue.should_leave_after_current():
            local_music_queue.reset_leave_request()
            if vc:
                await vc.disconnect()

        if thinking_msg:
            try:
                await thinking_msg.delete()
            except Exception as e:
                print(f"[play_next] ลบ thinking_msg ไม่ได้: {e}")

        if not interaction.response.is_done():
            await interaction.response.send_message("เล่นเพลงจบแล้ว 😄", ephemeral=True)
        else:
            await interaction.followup.send("เล่นเพลงจบแล้ว 😄", ephemeral=True)
        return
    
    if vc is None:
        if thinking_msg:
            try:
                await thinking_msg.delete()
            except Exception as e:
                print(f"[play_next] ลบ thinking_msg ไม่ได้: {e}")

        if not interaction.response.is_done():
            await interaction.response.send_message("ไปอยู่ในห้องก่อนเดี๋ยวตามไปถ้าเรียกอะ 🗣️", ephemeral=True)
        else:
            await interaction.followup.send("ไปอยู่ในห้องก่อนเดี๋ยวตามไปถ้าเรียกอะ 🗣️", ephemeral=True)
        return
    
    stream_url = await get_stream_url(source)
    if not stream_url:
        if not interaction.response.is_done():
            await interaction.response.send_message("เพลงไรไม่รู้ 😢", ephemeral=True)
        else:
            await interaction.followup.send("เพลงไรไม่รู้ 😢", ephemeral=True)
        return

    await asyncio.sleep(0.5)

    play_future = asyncio.get_running_loop().create_future()

    def after_playing(error):
        if error:
            print(f"Error playing audio: {error}")
        
        local_music_queue.now_playing = None 
        
        if music_queue and getattr(music_queue, "leave_after_current", False):
            music_queue.leave_after_current = False

            async def send_and_disconnect():
                if interaction.channel:
                    try:
                        await interaction.channel.send("ไปดีกว่า Adios ✌️😶‍🌫️")
                    except Exception as e:
                        print(f"ส่งข้อความลาไม่สำเร็จ: {e}")
                if vc:
                    await vc.disconnect()

                try:
                    await interaction.client.change_presence(
                        activity=discord.Activity(type=discord.ActivityType.playing, name="เล่นบ้าบออะไรละหมอ ถ้าอยากจะเล่นให้ /mj มา 😜")
                    )
                except Exception as e:
                    print(f"เปลี่ยน status ไม่สำเร็จ: {e}")

            asyncio.run_coroutine_threadsafe(send_and_disconnect(), interaction.client.loop)

        if local_music_queue.should_leave_after_current():
            local_music_queue.reset_leave_request()
            
            async def send_and_disconnect_empty():
                if interaction.channel:
                    try:
                        await interaction.channel.send("ไปดีกว่า Adios ✌️😶‍🌫️")
                    except Exception as e:
                        print(f"ส่งข้อความลาไม่สำเร็จ: {e}")
                if vc:
                    await vc.disconnect()

            asyncio.run_coroutine_threadsafe(send_and_disconnect_empty(), interaction.client.loop)

        if not play_future.done():
            play_future.set_result(None)

    ffmpeg_options = {
        'before_options': (
            '-reconnect 1 '
            '-reconnect_streamed 1 '
            '-reconnect_delay_max 5 '
            '-nostdin '
            '-probesize 156M '
            '-analyzeduration 156M '
            '-thread_queue_size 10000 '
            '-nostats -hide_banner '
            '-loglevel warning '
        ),
        'options': (
            '-vn '
            '-ac 2 '
            '-ar 48000 '
            '-bufsize 300M '
            '-b:a 256k '
            '-rtbufsize 650M '
            '-af aresample=async=1400:min_hard_comp=0.100:first_pts=0,'
            'dynaudnorm=f=150:g=15,volume=1.0 '
            '-use_wallclock_as_timestamps 1 '
        )
    }

    audio = discord.FFmpegOpusAudio(stream_url, **ffmpeg_options)
    await asyncio.sleep(0.2)
    vc.play(audio, after=lambda e: interaction.client.loop.call_soon_threadsafe(after_playing, e))
    vc.source.start_time = time.time() 

    await interaction.client.change_presence(
        activity=discord.Activity(type=discord.ActivityType.playing, name=f"{source['title']}")
    )
    
    if thinking_msg:
        try:
            await thinking_msg.delete()
        except Exception as e:
            print(f"[play_next] ลบ thinking_msg ไม่ได้: {e}")
        thinking_msg = None

    if last_ui_message:
        try:
            await last_ui_message.delete()
        except Exception as e:
            print(f"Failed to delete last_ui_message: {e}")

    view = MusicControlView(interaction)
    if interaction.channel:
        last_ui_message = await interaction.channel.send(f"🎶 กำลังร้องเพลง: **{source['title']}**", view=view)

    await play_future

    vc = interaction.guild.voice_client if interaction.guild else None
    if vc is None or not vc.is_connected():
        print("บอทออกไปแล้วหลังเพลงจบ ไม่อัปเดตสถานะอะไรต่อ")
        return

    async with local_music_queue.lock:
        queue_empty = not bool(local_music_queue._queue)

    if queue_empty:
        if music_queue:
            music_queue.has_added_once = False

        await interaction.client.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="รอดูว่าจะมีเพลงต่อไปมั้ย 🙃")
        )

        if last_ui_message:
            try:
                await last_ui_message.delete()
                view.stop()
            except Exception as e:
                print(f"Failed to cleanup UI message: {e}")
            last_ui_message = None

        if not interaction.response.is_done():
            await interaction.response.send_message("เล่นเพลงจบแล้ว 😄", ephemeral=True)
        else:
            await interaction.followup.send("เล่นเพลงจบแล้ว 😄", ephemeral=True)
    else:
        await play_next(interaction)

async def auto_leave_check_loop(bot: commands.Bot):
    while True:
        for guild in bot.guilds:
            vc_raw = guild.voice_client
            vc = cast(Optional[VoiceClient], vc_raw)

            global music_queue
            if vc and music_queue and music_queue.should_leave_after_current():
                if not vc.is_playing() and not vc.is_paused():
                    music_queue.reset_leave_request()
                    await vc.disconnect()
                    auto_leave_timers[guild.id] = 0
                    continue

            if vc and not vc.is_playing() and not vc.is_paused():
                if len(vc.channel.members) <= 1:
                    auto_leave_timers[guild.id] += 60
                    if auto_leave_timers[guild.id] > 420:
                        await vc.disconnect()
                        auto_leave_timers[guild.id] = 0
                else:
                    auto_leave_timers[guild.id] = 0
            elif vc:
                auto_leave_timers[guild.id] = 0

        await asyncio.sleep(60)

def is_spotify_url(url: str) -> bool:
    return "open.spotify.com/track" in url

def get_spotify_track_info(url: str, sp: spotipy.Spotify) -> Optional[str]:
    match = re.search(r"track/([a-zA-Z0-9]+)", url)
    if not match:
        return None
    track_id = match.group(1)
    try:
        track = sp.track(track_id)
        return f"{track['name']} {track['artists'][0]['name']}"
    except Exception as e:
        print(f"Spotify track lookup error: {e}")
        return None

def is_spotify_playlist(url: str) -> bool:
    return "open.spotify.com/playlist" in url

def is_youtube_playlist(url: str) -> bool:
    return "playlist" in url and "youtube.com" in url

def get_spotify_playlist_tracks(url: str, sp: spotipy.Spotify) -> list[str]:
    match = re.search(r"playlist/([a-zA-Z0-9]+)", url)
    if not match:
        return []
    playlist_id = match.group(1)
    try:
        results = sp.playlist_tracks(playlist_id)
        tracks = results['items']
        return [
            f"{item['track']['name']} {item['track']['artists'][0]['name']}"
            for item in tracks if item['track']
        ]
    except Exception as e:
        print(f"Spotify playlist lookup error: {e}")
        return []

async def get_dominant_color_from_url(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                img = Image.open(BytesIO(data))
                img = img.resize((50, 50))
                result = img.convert('P', palette=Image.ADAPTIVE, colors=1)
                dominant_color = result.getpalette()[0:3]
                return dominant_color
    except Exception as e:
        print(f"Error getting dominant color: {e}")
        return None

async def ensure_disconnect_before_connect(guild):
    vc = guild.voice_client
    if vc and vc.is_connected():
        try:
            await vc.disconnect(force=True)
            print("Disconnect voice client before connect new one.")
        except Exception as e:
            print(f"Error disconnecting voice client: {e}")
    
class MJ(app_commands.Group):
    def __init__(self, bot: commands.Bot, sp: spotipy.Spotify):
        super().__init__(name="mj", description="MJ Music Bot 🎵")
        self.bot = bot                   
        self.sp = sp
        self.pending_sfx_task = None  
        self.block_leave = False

    @app_commands.command(name="join", description="ต้องการเรียกฉันเข้าห้องหรอ hee hee🕺")
    async def join(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("ไปอยู่ในห้องก่อนเดี๋ยวตามไปถ้าเรียกอะ 🗣️", ephemeral=True)
            return

        channel = interaction.user.voice.channel
        vc_raw = interaction.guild.voice_client if interaction.guild else None
        vc = cast(Optional[VoiceClient], vc_raw)

        if vc:
            if vc.channel == channel:
                await interaction.response.send_message("ฉันก็อยู่ในห้องอยู่แล้วนี่ 😒", ephemeral=True)
                return
            else:
                await vc.move_to(channel)
        else:
            await ensure_disconnect_before_connect(interaction.guild)

            try:
                vc = await channel.connect()
            except Exception as e:
                await interaction.response.send_message(f"บอทไม่อยากเข้าห้อง ขี้เกียจ 🦥: {e}", ephemeral=True)
                return

        await play_sfx(vc)

        await interaction.client.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="รอดูว่าจะให้ร้องเพลงอะไร 😋")
        )

        await interaction.response.send_message("ว่าไง hee hee! 🙋‍♂️")

    @app_commands.command(name="play", description="ใส่ลิงก์เพลงมาเลยเดี๋ยวจัดให้ 😏")
    @app_commands.describe(url="ขอลิงก์ YouTube ▶︎ / Spotify ᯤ")
    async def play(self, interaction: discord.Interaction, url: str):
        vc = interaction.guild.voice_client
        already_in_channel = vc is not None and vc.is_connected()

        if not already_in_channel:
            await interaction.response.defer(thinking=True)
        else:
            await interaction.response.defer(thinking=True)

        thinking_msg = await interaction.followup.send("กำลังคิดอยู่ hmmmm 🤔")

        vc = interaction.guild.voice_client
        if not vc:
            if interaction.user.voice is None or interaction.user.voice.channel is None:
                await thinking_msg.delete()
                await interaction.followup.send("แล้วฉันจะให้ร้องเพลงให้ใครฟังล่ะ ผีหรอ 👻", ephemeral=True)
                return
            try:
                if interaction.guild.voice_client:
                    await interaction.guild.voice_client.disconnect(force=True)
                vc = await interaction.user.voice.channel.connect()
            except discord.ClientException:
                await thinking_msg.delete()
                await interaction.followup.send("ขี้เกียจเข้าอะ 🦥", ephemeral=True)
                return

        global music_queue
        if music_queue is None:
            music_queue = MusicQueue()

        if music_queue.should_leave_after_current():
            await thinking_msg.delete()
            await interaction.followup.send(
                "เฮ้! ฉันจะออกจากห้องแล้วนะ ไว้เพิ่มเพลงที่หลังละกัน 😉", ephemeral=True
            )
            return

        songs = []
        try:
            if is_spotify_playlist(url):
                queries = get_spotify_playlist_tracks(url, self.sp)
                for q in queries:
                    songs.extend(await YTDLSource.from_url(f"ytsearch:{q}"))
            elif is_youtube_playlist(url):
                songs = await YTDLSource.from_url(url)
            elif is_spotify_url(url):
                query = get_spotify_track_info(url, self.sp)
                if query:
                    songs = await YTDLSource.from_url(f"ytsearch:{query}")
                else:
                    await thinking_msg.delete()
                    await interaction.followup.send("เพลงนี้ร้องไม่เป็น 🗿", ephemeral=True)
                    return
            else:
                songs = await YTDLSource.from_url(url)
        except Exception as e:
            await thinking_msg.delete()
            await interaction.followup.send("ลิงก์มั่วชันปะนี่ 😑", ephemeral=True)
            return

        if not songs:
            await thinking_msg.delete()
            await interaction.followup.send("เพลงนี้ร้องไม่เป็น 🗿", ephemeral=True)
            return

        queue_was_empty = music_queue.is_empty()
        await music_queue.add(songs)
        await thinking_msg.delete()

        if getattr(music_queue, "has_added_once", False) and not queue_was_empty:
            await interaction.followup.send(
                "เพิ่มเพลงละ เดี๋ยวจะร้องหลังเพลงจบหรือข้ามให้เลย 🙂", ephemeral=True
            )
        else:
            music_queue.has_added_once = True

        if not vc.is_playing() and not vc.is_paused() and not getattr(self, "pending_sfx", False):
            async with music_queue.playing_lock:
                await play_next(interaction)

    @app_commands.command(name="status", description="อยากจะเช็คว่าฉันทำอะไรอยู่หรอ well well well ก็ได้ 😋")
    async def status(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client

        if vc and music_queue and music_queue.now_playing and vc.is_playing():
            now_playing = music_queue.now_playing
            title = now_playing.get("title", "เพลงชื่อไรไม่รู้อ่านไม่ออก 🤷‍♂️")
            duration_seconds = now_playing.get("duration")
            channel_name = vc.channel.name if vc.channel else "้ห้องไหนเนี่ย 😩"
            video_url = now_playing.get("webpage_url", "")

            elapsed = 0
            if hasattr(vc.source, 'start_time'):
                elapsed = time.time() - vc.source.start_time

            elapsed_mins, elapsed_secs = divmod(int(elapsed), 60)
            total_mins, total_secs = (0, 0)
            if duration_seconds:
                total_mins, total_secs = divmod(duration_seconds, 60)

            duration_str = f"{elapsed_mins}:{elapsed_secs:02d} / {total_mins}:{total_secs:02d}"

            video_id_match = re.search(r"v=([a-zA-Z0-9_-]{11})", video_url)
            thumbnail_url = None
            if video_id_match:
                video_id = video_id_match.group(1)
                thumbnail_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"

            dominant_color = None
            if thumbnail_url:
                dominant_color = await get_dominant_color_from_url(thumbnail_url)

            embed_color = discord.Color.green()
            if dominant_color:
                embed_color = discord.Color.from_rgb(*dominant_color)

            embed = discord.Embed(
                title="สถานะเพลงที่กำลังร้องอยู่ตอนนี้ 🤓☝️",
                color=embed_color
            )
            embed.add_field(
                name="ร้องเพลง 🎤🎶",
                value=f"[{title}]({video_url})\nกำลังเล่น ⏳: `{duration_str}`",
                inline=True
            )
            embed.add_field(name="จัดคอนเสิร์ต 👇", value=f"`{channel_name}`", inline=True)

            if thumbnail_url:
                embed.set_image(url=thumbnail_url)

            if len(music_queue._queue) > 0:
                next_song = music_queue._queue[0]
                embed.add_field(name="เพลงถัดไปที่จะร้อง 🎤", value=f"*{next_song['title']}*", inline=False)

            await interaction.response.send_message(embed=embed, ephemeral=True)

        else:
            await interaction.response.send_message("เพลงไม่ได้เล่นโคตรน่าเบื่อเลย 🥱", ephemeral=True)

    @app_commands.command(name="sfx", description="เล่น sfx แบบ random be like 🎲🗣️")
    async def sfx(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client

        if not vc or not vc.is_connected():
            if interaction.user.voice and interaction.user.voice.channel:
                try:
                    vc = await interaction.user.voice.channel.connect()
                except discord.ClientException:
                    vc = interaction.guild.voice_client
            else:
                await interaction.response.send_message("เข้าห้องก่อนดิ เล่น sfx คนเดียวมันเหงา 😭", ephemeral=True)
                return

        if vc.is_playing() or vc.is_paused() or (music_queue and not music_queue.is_empty()):
            await interaction.response.send_message("เฮ้! ยังไม่ใช่เวลาเล่น sfx ลองใหม่ครั้งหน้าละกัน 😣", ephemeral=True)
            return

        if self.pending_sfx_task and not self.pending_sfx_task.done():
            self.pending_sfx_task.cancel()

        self.pending_sfx = True
        self.last_interaction = interaction  

        await interaction.response.send_message("รับไปซะ 🤪🫵", ephemeral=True)

        sfx_files = [f for f in os.listdir(SFX_COMMAND_FOLDER) if f.endswith(('.mp3', '.wav', '.ogg'))]
        if not sfx_files:
            await interaction.followup.send("ไม่มี SFX ให้เล่นเลย 😥")
            self.pending_sfx = False
            return

        unique_sfx_files = list(set(sfx_files))
        random.shuffle(unique_sfx_files)         
        random_sfx = random.choice(unique_sfx_files)

        sfx_path = os.path.join(SFX_COMMAND_FOLDER, random_sfx)

        def after_sfx_playing(error):
            self.pending_sfx = False
            self.block_leave = False
            if error:
                print(f"SFX play error: {error}")
            else:
                if music_queue and not music_queue.is_empty() and getattr(self, "last_interaction", None):
                    asyncio.run_coroutine_threadsafe(
                        play_next(self.last_interaction),
                        self.last_interaction.client.loop
                    )

        vc.play(discord.FFmpegPCMAudio(sfx_path), after=after_sfx_playing)

    @app_commands.command(name="leave", description="Bruh จะไล่ฉันออกจากห้องรึ ok ก็ได้ 😑")
    async def leave(self, interaction: discord.Interaction):
        global music_queue

        if getattr(self, 'block_leave', False):
            await interaction.response.send_message("เล่น sfx อยู่ ไม่ว่างใช้อีกทีตอน sfx จบละกัน 😑👍", ephemeral=True)
            return
        
        voice_client = interaction.guild.voice_client
        if voice_client is None or not voice_client.is_connected():
            await interaction.response.send_message("ฉันไม่ออกเพราะฉันไม่ได้อยู่ห้อง 😡", ephemeral=True)
            return

        if voice_client.is_playing() or voice_client.is_paused():
            await interaction.response.send_message("เพลงกำลังมันเลย เดี๋ยวออกเองได้ถ้าเพลงจบอะ 😐🫥", ephemeral=True)

            if music_queue:
                music_queue.leave_after_current = True
        else:
            await self._leave_voice(interaction)
            
    async def _leave_voice(self, interaction: discord.Interaction):
        global music_queue
        voice_client = interaction.guild.voice_client

        if not interaction.response.is_done():
            await interaction.response.send_message("ไปดีกว่า Adios ✌️😶‍🌫️")
        else:
            await interaction.followup.send("ไปดีกว่า Adios ✌️😶‍🌫️")

        if music_queue:
            await music_queue.clear()
            music_queue = None
        if voice_client:
            await voice_client.disconnect(force=True)

        await interaction.client.change_presence(
            activity=discord.Activity(type=discord.ActivityType.playing, name="เล่นบ้าบออะไรละหมอ ถ้าอยากจะเล่นให้ /mj มา 😜")
        )
        
intents = discord.Intents.default()
intents.message_content = False
intents.voice_states = True

music_queue = MusicQueue()

spotify_client_id = os.environ.get("SPOTIPY_CLIENT_ID")
spotify_client_secret = os.environ.get("SPOTIPY_CLIENT_SECRET")
sp = spotipy.Spotify(
    client_credentials_manager=NoCacheSpotifyClientCredentials(
        client_id=spotify_client_id, client_secret=spotify_client_secret
    )
)

mj_group = MJ(None, sp)

class MyBot(commands.Bot):
    async def setup_hook(self):
        mj_group.bot = self
        self.tree.add_command(mj_group)
        try:
            await self.tree.sync()
            print("🔄 Synced app commands successfully.")
        except Exception as e:
            print(f"Error syncing commands: {e}")
        self.loop.create_task(auto_leave_check_loop(self))

bot = MyBot(command_prefix="!", intents=intents, help_command=None)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    await bot.change_presence(
        activity=discord.Game(name="เล่นบ้าบออะไรละหมอ ถ้าอยากจะเล่นให้ /mj มา 😜")
    )

if __name__ == "__main__":
    keep_alive()
    DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
    bot.run(DISCORD_TOKEN)
