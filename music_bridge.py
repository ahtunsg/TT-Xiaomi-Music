"""Local music and story playback for the speaker.

Two levels of granularity:

  a single file      -> say the keyword plus a title fragment
  a folder as a set  -> every folder under MUSIC_DIR is a series; saying the
                        folder name plays it from the start and then keeps
                        going episode after episode

Playback progress is written to music_state.json after each track, so
"继续" picks the series up where it stopped even after a reboot.

Standalone use:
    python music_bridge.py list
    python music_bridge.py play 晴天
    python music_bridge.py play 小鸡多多
    python music_bridge.py resume
    python music_bridge.py stop

xiaogpt imports handle_command() through patch_xiaogpt.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import socket
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

# All site-specific values come from the environment; nothing personal is
# baked into the source. See README.md for how to obtain each one.
MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "./music")).resolve()
PORT = int(os.environ.get("MUSIC_PORT", "18899"))
DEVICE_ID = os.environ.get("MUSIC_DEVICE_ID", "")
STATE_FILE = Path(os.environ.get("MUSIC_STATE", "./music_state.json"))
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus"}

# How long before the end we call a track finished. Tracks often report a
# slightly shorter real length than the container says.
TAIL_GRACE = 2.0
# Ignore "暂停"/"继续" from the voice channel this long after our own last action,
# so a native Xiaomi playlist is not hijacked once we are out of the picture.
INTERCEPT_WINDOW = 1800.0


@dataclass
class Track:
    path: Path
    title: str
    artist: str
    series: str
    _key: str = field(default="", repr=False)

    @property
    def label(self) -> str:
        return f"{self.series}：{self.title}" if self.series else self.title


# ---------------------------------------------------------------- text helpers


_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def cn_to_int(text: str) -> int | None:
    """Parse 12 / 十二 / 二十三 / 一百零五. Returns None when unparsable."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if not text:
        return None
    total = 0
    num = 0
    for ch in text:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            total += (num or 1) * _CN_UNITS[ch]
            num = 0
        else:
            return None
    return total + num


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[\(\)\[\]【】\-_.,，。、!！?？'\"\s]+", "", text)
    return text


def _natural_key(text: str) -> list:
    """So 第2集 sorts before 第10集 instead of after it."""
    parts = re.split(r"(\d+)", text.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def _read_id3(path: Path) -> tuple[str, str]:
    """Minimal ID3v2.3/2.4 TIT2/TPE1 reader. Returns ("", "") when absent."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(10)
            if len(head) < 10 or head[:3] != b"ID3":
                return "", ""
            size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
            body = fh.read(min(size, 1_000_000))
    except OSError:
        return "", ""

    title = artist = ""
    pos = 0
    while pos + 10 <= len(body):
        frame_id = body[pos : pos + 4]
        if not frame_id.strip(b"\x00"):
            break
        frame_size = int.from_bytes(body[pos + 4 : pos + 8], "big")
        if frame_size <= 0 or pos + 10 + frame_size > len(body):
            break
        payload = body[pos + 10 : pos + 10 + frame_size]
        if payload:
            encoding = payload[0]
            raw = payload[1:]
            try:
                if encoding == 0:
                    text = raw.decode("latin-1", "ignore")
                elif encoding == 1:
                    text = raw.decode("utf-16", "ignore")
                elif encoding == 2:
                    text = raw.decode("utf-16-be", "ignore")
                else:
                    text = raw.decode("utf-8", "ignore")
            except Exception:
                text = ""
            text = text.replace("\x00", "").strip()
            if frame_id == b"TIT2":
                title = text
            elif frame_id == b"TPE1":
                artist = text
        pos += 10 + frame_size
    return title, artist


def _duration(path: Path) -> float:
    """Track length in seconds. 300s fallback keeps a failed probe harmless."""
    try:
        from mutagen import File as MutagenFile

        info = MutagenFile(str(path))
        if info is not None and info.info and info.info.length:
            return float(info.info.length)
    except Exception:
        pass
    return 300.0


# ---------------------------------------------------------------- library scan

# Reading ID3 tags is the slow part of a scan (roughly 10 ms per file, so about
# six seconds across a 600-track library). Every voice command scans, and the
# scan runs inside xiaogpt's event loop, which would stall polling and make the
# speaker feel laggy. So the result is cached in memory and mirrored to a small
# JSON index keyed by (mtime, size): unchanged files are never re-read, and the
# index survives restarts.
INDEX_FILE = Path(os.environ.get("MUSIC_INDEX", "./music_index.json"))

_index: dict[str, dict] = {}
_index_loaded = False
_tracks: list[Track] = []
_cache_stamp: tuple = ()


def _tree_stamp() -> tuple:
    """Fingerprint from directory mtimes only.

    Adding, deleting or moving a file bumps its parent folder's mtime, which is
    enough to notice a change without stat-ing every file on every command.
    """
    stamp = []
    for root, _dirs, _files in os.walk(MUSIC_DIR):
        try:
            stamp.append((root, Path(root).stat().st_mtime_ns))
        except OSError:
            pass
    return tuple(stamp)


def _load_index() -> None:
    global _index, _index_loaded
    if _index_loaded:
        return
    _index_loaded = True
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _index = {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        _index = {}


def _save_index() -> None:
    try:
        INDEX_FILE.write_text(
            json.dumps(_index, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def _tags(path: Path, st: os.stat_result) -> tuple[str, str]:
    """Cached ID3 lookup: re-read only when the file really changed."""
    _load_index()
    key = str(path)
    hit = _index.get(key)
    if hit and hit.get("mtime") == st.st_mtime_ns and hit.get("size") == st.st_size:
        return hit.get("title", ""), hit.get("artist", "")
    title, artist = _read_id3(path)
    _index[key] = {
        "mtime": st.st_mtime_ns,
        "size": st.st_size,
        "title": title,
        "artist": artist,
    }
    return title, artist


def all_tracks() -> list[Track]:
    """Every audio file, series folders first, loose files after."""
    global _tracks, _cache_stamp
    if not MUSIC_DIR.is_dir():
        return []
    stamp = _tree_stamp()
    if _tracks and stamp == _cache_stamp:
        return list(_tracks)

    files = [p for p in MUSIC_DIR.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXT]
    files.sort(key=lambda p: [_natural_key(str(p.relative_to(MUSIC_DIR)))])

    tracks: list[Track] = []
    seen: set[str] = set()
    for path in files:
        rel = path.relative_to(MUSIC_DIR)
        series = str(rel.parent).replace("\\", "/") if rel.parent.parts != (".",) else ""
        if series == ".":
            series = ""
        try:
            st = path.stat()
        except OSError:
            continue
        seen.add(str(path))
        id3_title, id3_artist = _tags(path, st)
        title = id3_title or path.stem
        tracks.append(
            Track(
                path=path,
                title=title,
                artist=id3_artist,
                series=series,
                _key=_normalize(title + id3_artist + path.stem),
            )
        )
    # Drop index rows for files that are gone, otherwise the JSON only grows.
    if _index_loaded:
        for stale in [k for k in _index if k not in seen]:
            _index.pop(stale, None)
        _save_index()

    _tracks = tracks
    _cache_stamp = stamp
    return list(tracks)


def series_names(tracks: list[Track]) -> list[str]:
    names = {t.series for t in tracks if t.series}
    return sorted(names, key=_natural_key)


def tracks_in_series(name: str, tracks: list[Track] | None = None) -> list[Track]:
    tracks = tracks if tracks is not None else all_tracks()
    key = _normalize(name)
    hit = [s for s in {t.series for t in tracks if t.series} if _normalize(s) == key]
    if not hit:
        hit = [s for s in {t.series for t in tracks if t.series} if key and key in _normalize(s)]
    if not hit:
        return []
    target = sorted(hit)[0]
    return [t for t in tracks if t.series == target]


def search(keyword: str, tracks: list[Track] | None = None) -> list[Track]:
    tracks = tracks if tracks is not None else all_tracks()
    key = _normalize(keyword)
    if not key:
        return []
    exact = [t for t in tracks if _normalize(t.title) == key]
    if exact:
        return exact
    starts = [t for t in tracks if _normalize(t.title).startswith(key)]
    contains = [t for t in tracks if key in t._key]
    ordered: list[Track] = []
    for group in (starts, contains):
        for t in group:
            if t not in ordered:
                ordered.append(t)
    return ordered


# ---------------------------------------------------------------------- state


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(series: str, index: int, title: str = "", mode: str = "series") -> None:
    payload = {
        "series": series,
        "index": int(index),
        "title": title,
        "mode": mode,
        "updated": int(time.time()),
    }
    try:
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------- spawn


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def track_url(track: Track) -> str:
    """/f/ takes the path itself, so playback order is ours to choose."""
    rel = str(track.path.relative_to(MUSIC_DIR)).replace("\\", "/")
    return f"http://{local_ip()}:{PORT}/f/{urllib.parse.quote(rel)}"


async def _mina():
    from miservice.miaccount import MiAccount
    from miservice.minaservice import MiNAService

    if not DEVICE_ID:
        raise RuntimeError(
            "set MUSIC_DEVICE_ID to your speaker's deviceId (see README)"
        )
    token = Path.home() / ".mi.token"
    if not token.exists():
        raise RuntimeError("no ~/.mi.token; log in with miservice first")
    account = MiAccount(None, "", "", str(token))
    return MiNAService(account)


# --------------------------------------------------------------------- player


class Player:
    """Owns one playback chain: current list, position, auto-advance loop."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.series = ""
        self.mode = "series"
        self.tracks: list[Track] = []
        self.pos = 0
        self.manual_stop = False
        self.last_active = 0.0

    def cancel(self) -> None:
        if self.task and not self.task.done():
            self.manual_stop = True
            self.task.cancel()
        self.task = None

    async def play_list(
        self, series: str, tracks: list[Track], start: int = 0, mode: str = "series"
    ) -> str:
        if not tracks:
            return "没有可播放的内容"
        start = max(0, min(start, len(tracks) - 1))
        self.cancel()
        self.manual_stop = False
        self.series = series
        self.mode = mode
        self.tracks = tracks
        self.pos = start
        self.last_active = time.time()
        self.task = asyncio.create_task(self._run())
        return self.describe(start)

    def describe(self, index: int) -> str:
        track = self.tracks[index]
        total = len(self.tracks)
        if self.mode == "shuffle":
            return f"随机播放{track.title}"
        if self.series and total > 1:
            return f"{self.series} 第{index + 1}集，共{total}集"
        return f"正在播放{track.title}"

    async def _run(self) -> None:
        mina = await _mina()
        index = self.pos
        while index < len(self.tracks):
            track = self.tracks[index]
            self.pos = index
            self.last_active = time.time()
            save_state(self.series, index, track.title, self.mode)
            print(f"play [{index + 1}/{len(self.tracks)}] {track.path.name}", flush=True)
            try:
                await mina.play_by_url(DEVICE_ID, track_url(track))
            except Exception as exc:  # noqa: BLE001
                print(f"play failed: {exc!r}", flush=True)
                return
            if not await self._wait_done(_duration(track.path)):
                return
            index += 1
        self.pos = len(self.tracks) - 1
        # The device loops a single URL by itself (loop_type=1), so without this
        # the last episode would replay forever once the series runs out.
        try:
            await mina.player_stop(DEVICE_ID)
        except Exception:
            pass
        print("series finished", flush=True)

    async def _wait_done(self, duration: float) -> bool:
        """True = track ran its full length. False = user stopped or cancelled.

        The status endpoint on this hardware reports a constant value, so the
        container's own length is the only reliable end-of-track signal.
        """
        try:
            await asyncio.sleep(max(1.0, duration + TAIL_GRACE))
        except asyncio.CancelledError:
            return False
        return True


player = Player()


# ------------------------------------------------------------------- commands


_STOP_WORDS = {
    "停止", "停止播放", "暂停", "暂停播放", "暂停一下", "停", "停下", "停一下",
    "停一停", "别放了", "别放", "别播了", "不听了", "关掉", "关闭", "关",
}
_NEXT_WORDS = {
    "下一集", "下一个", "下一首", "下一首歌", "下集", "下一部", "下一曲",
    "换一首", "换首歌", "换一个", "切歌",
}
_PREV_WORDS = {
    "上一集", "上一个", "上一首", "上一首歌", "上集", "上一曲", "前一首",
}
_RESUME_WORDS = {
    "继续", "接着", "接着放", "继续播放", "继续放", "接着播", "继续播",
    "接着听", "继续听", "放吧",
}
_INFO_WORDS = {"列表", "有几首", "多少首", "有几个", "有什么"}
# "just play something" — no title, no folder, so the whole library is shuffled.
_SHUFFLE_WORDS = {
    "音乐", "歌", "歌曲", "听歌", "放歌", "点音乐", "些音乐", "来点音乐",
    "放点音乐", "随便", "随便放", "随便听听", "随便来点", "随便播", "随机",
    "随机播放", "随机放", "一首歌", "首歌", "来首歌", "放首歌", "点歌",
    "听首歌", "来首歌听", "歌吧",
}

_EPISODE_RE = re.compile(r"第\s*([0-9零〇一二两三四五六七八九十百]+)\s*[集话回章节期]")

_ALL_WORDS = (
    _STOP_WORDS | _NEXT_WORDS | _PREV_WORDS | _RESUME_WORDS
    | _INFO_WORDS | _SHUFFLE_WORDS
)

# Speech recognition rarely returns our word list verbatim: 暂停一下, 停了吧,
# 说得客气一点都会多出尾巴。Stripping them means one entry covers the family.
# No 一首 in this list: 下一首 / 上一首 / 换一首 all end with it.
_TAILS = (
    "一下", "一遍", "吧", "啊", "呀", "了", "嘛", "呢", "哦",
    "好吗", "行吗", "谢谢", "啦",
)


def _clean_cmd(text: str) -> str:
    text = (text or "").strip().strip("。！？!?.,，、~ ")
    changed = True
    while changed:
        changed = False
        for tail in _TAILS:
            if len(text) > len(tail) and text.endswith(tail):
                text = text[: -len(tail)].strip()
                changed = True
    return text


def _strip_verb(text: str) -> str:
    for verb in (
        "继续播放", "播放", "放一首", "来一首", "我要听", "我想听", "听一首",
        "我想", "放到", "放", "播", "听",
    ):
        if text.startswith(verb):
            return text[len(verb) :].strip()
    return text


def _resolve_cmd(text: str) -> str:
    """Normalise a spoken command into a word-list entry when possible.

    The untouched phrase is checked first: 继续播放 is both a control word and
    a verb, and 放吧 would lose its 吧 before matching. Only then is the verb
    prefix dropped, which is what 播放晴天 needs.
    """
    raw = (text or "").strip().strip("。！？!?.,，、~ ")
    if raw in _ALL_WORDS:
        return raw
    cleaned = _clean_cmd(raw)
    if cleaned in _ALL_WORDS:
        return cleaned
    return _clean_cmd(_strip_verb(cleaned))


async def stop_playback(speak: bool = True) -> tuple[bool, str]:
    player.cancel()
    player.manual_stop = True
    if player.tracks:
        save_state(player.series, player.pos, player.tracks[player.pos].title, player.mode)
    try:
        mina = await _mina()
        await mina.player_pause(DEVICE_ID)
    except Exception as exc:  # noqa: BLE001
        print(f"pause failed: {exc!r}", flush=True)
    return speak, "已停止"


async def _resume() -> tuple[bool, str]:
    state = load_state()
    if state.get("mode") == "shuffle":
        return await _shuffle_all()
    series = state.get("series") or ""
    index = int(state.get("index") or 0)
    if not series:
        loose = [t for t in all_tracks() if not t.series]
        if not loose:
            return True, "还没有播放记录"
        # Position inside the loose pool is remembered by title, so 继续 comes
        # back to the song that was playing rather than to the top of the list.
        title = state.get("title") or ""
        start = next((i for i, t in enumerate(loose) if t.title == title), 0)
        return False, await player.play_list("", loose, start)
    tracks = tracks_in_series(series)
    if not tracks:
        return True, f"找不到系列{series}了"
    return False, await player.play_list(series, tracks, index)


async def _step(delta: int) -> tuple[bool, str]:
    if not player.tracks:
        return await _resume()
    target = player.pos + delta
    if target < 0:
        target = 0
    if target >= len(player.tracks):
        return True, "已经是最后一集了" if player.series else "已经是最后一首了"
    player.cancel()
    player.manual_stop = False
    player.pos = target
    player.task = asyncio.create_task(player._run())
    return False, player.describe(target)


async def _jump(episode: int) -> tuple[bool, str]:
    if not player.tracks:
        return True, "先说播放哪个系列"
    if not 1 <= episode <= len(player.tracks):
        return True, f"只有{len(player.tracks)}集"
    return await _step(episode - 1 - player.pos)


async def _start_series(name: str, start: int = 0) -> tuple[bool, str]:
    tracks = tracks_in_series(name)
    if not tracks:
        return True, f"本地没有找到{name}"
    return False, await player.play_list(name, tracks, start)


async def _shuffle_all() -> tuple[bool, str]:
    """Random order over the loose files in the music root.

    Sub-folders are series and stay out of the shuffle. A story folder can hold
    a hundred episodes, and having episode 37 land between two songs is never
    what "play some music" means. Series only play when asked for by name.
    """
    tracks = [t for t in all_tracks() if not t.series]
    if not tracks:
        return True, "音乐文件夹里还没有单曲，只有系列文件夹"
    queue = tracks[:]
    random.shuffle(queue)
    return False, await player.play_list("", queue, 0, mode="shuffle")


async def handle_command(text: str) -> tuple[bool, str]:
    """Turn a voice command (keyword already stripped) into (speak?, reply).

    speak=False means stay quiet: for playback the audio itself is the feedback,
    and a TTS line would talk over the first seconds.
    """
    cmd = _resolve_cmd(text)

    if not cmd:
        tracks = all_tracks()
        names = series_names(tracks)
        if not tracks:
            return True, "音乐文件夹里还没有内容"
        if names:
            return True, f"本地有{len(names)}个系列，{len(tracks)}个音频"
        return True, f"本地共{len(tracks)}首"

    if cmd in _STOP_WORDS:
        return await stop_playback()

    if cmd in _RESUME_WORDS:
        return await _resume()

    if cmd in _NEXT_WORDS:
        return await _step(1)

    if cmd in _PREV_WORDS:
        return await _step(-1)

    if cmd in _INFO_WORDS:
        tracks = all_tracks()
        names = series_names(tracks)
        if names:
            return True, f"本地有{len(names)}个系列，共{len(tracks)}个音频"
        return True, f"本地共{len(tracks)}首"

    # A folder whose name is exactly this word outranks the generic
    # "play something" phrases below, so 音乐 as a folder stays reachable.
    library = all_tracks()
    exact_series = [
        s for s in series_names(library) if _normalize(s) == _normalize(cmd)
    ]
    if exact_series:
        return await _start_series(exact_series[0])

    if cmd in _SHUFFLE_WORDS:
        return await _shuffle_all()

    # "第N集" alone means: jump inside whatever is currently loaded.
    only_episode = _EPISODE_RE.fullmatch(cmd)
    if only_episode:
        number = cn_to_int(only_episode.group(1))
        if number:
            return await _jump(number)

    # "小鸡多多第3集" / "小鸡多多 第三集"
    match = _EPISODE_RE.search(cmd)
    if match:
        number = cn_to_int(match.group(1))
        name = cmd[: match.start()].strip()
        if name and number:
            return await _start_series(name, number - 1)

    # Folder name wins over track title: a series is the bigger intent.
    if tracks_in_series(cmd):
        return await _start_series(cmd)

    tracks = all_tracks()
    hits = search(cmd, tracks)
    if not hits:
        return True, f"本地没有找到{cmd}"
    first = hits[0]
    if first.series:
        series_tracks = tracks_in_series(first.series, tracks)
        start = series_tracks.index(first) if first in series_tracks else 0
        return False, await player.play_list(first.series, series_tracks, start)
    # A single song plays from inside the whole loose pool instead of alone.
    # Naming one track is how people start listening; the very next thing they
    # say is 下一首, and a one-track queue answers that with "already the last".
    pool = [t for t in tracks if not t.series]
    if not pool:
        return False, await player.play_list("", [first], 0)
    start = pool.index(first) if first in pool else 0
    return False, await player.play_list("", pool, start)


_INTERCEPT_WORDS = _STOP_WORDS | _RESUME_WORDS | _NEXT_WORDS | _PREV_WORDS


def intercept_native(text: str) -> bool:
    """Claim a plain "暂停"/"继续" while our own playback is running.

    The device loops whatever URL it was given, so a native pause would be
    overridden by the next episode a few seconds later. Handled here so the
    chain stops at the same moment the user asked it to.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if not player.tracks or not player.last_active:
        return False
    if time.time() - player.last_active > INTERCEPT_WINDOW:
        return False
    cmd = _resolve_cmd(text)
    if cmd not in _INTERCEPT_WORDS:
        return False
    loop.create_task(handle_command(cmd))
    return True


# ---------------------------------------------------------------------- shell

async def _cli() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    action = args[0]
    if action == "list":
        tracks = all_tracks()
        print(f"{len(tracks)} files under {MUSIC_DIR}")
        for name in series_names(tracks):
            group = tracks_in_series(name, tracks)
            print(f"  [{name}]  {len(group)} 集")
            for t in group[:3]:
                print(f"      - {t.path.name}")
            if len(group) > 3:
                print(f"      ... 共{len(group)}集")
        loose = [t for t in tracks if not t.series]
        if loose:
            print(f"  [单曲]  {len(loose)} 首")
            for t in loose:
                print(f"      - {t.path.name}")
    elif action == "play" and len(args) > 1:
        speak, reply = await handle_command(" ".join(args[1:]))
        print(reply)
        if not speak:
            await asyncio.sleep(3600)
    elif action == "resume":
        speak, reply = await _resume()
        print(reply)
        await asyncio.sleep(3600)
    elif action == "stop":
        print((await stop_playback())[1])
    elif action == "state":
        print(json.dumps(load_state(), ensure_ascii=False))
    else:
        print(__doc__)


if __name__ == "__main__":
    os.environ.setdefault(
        "SSL_CERT_FILE", r"C:\xiaogpt\.venv\Lib\site-packages\certifi\cacert.pem"
    )
    try:
        asyncio.run(_cli())
    except KeyboardInterrupt:
        pass
