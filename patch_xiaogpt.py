"""Re-apply latency patches to xiaogpt. Idempotent, safe to run repeatedly.

Run this after any `pip install -U xiaogpt`. Each patch is independent and
guarded by its own env var, so any single one can be switched off without
touching the others. Exit codes: 0 ok, 1 target missing, 2 an upstream line
changed so that patch was skipped.

P1 XIAOGPT_NO_HINT       skip the spoken "正在问xxx请耐心等待" hint.
                         Costs one cloud TTS round trip plus playback seconds.

P2 XIAOGPT_POLL_INTERVAL polling cycle length in seconds (upstream: 1).
                         This is the floor on "user stops talking -> we notice".

P3 XIAOGPT_FAST_STOP     skip the second playback check in the main loop.
                         The poll loop already paused XiaoAI microseconds
                         earlier for the same record; measured 250-930 ms per
                         status call, so this is time saved before the LLM runs.

P4 XIAOGPT_SKIP_PLAY_CHECK  send pause without asking for playback status first.
                         Saves one more cloud round trip and cuts the window in
                         which the native answer is audible.
"""

from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path(r"C:\xiaogpt\.venv\Lib\site-packages\xiaogpt\xiaogpt.py")

PATCHES: list[tuple[str, str, str]] = [
    (
        "XIAOGPT_NO_HINT",
        '            await self.do_tts(f"正在问{self.chatbot.name}请耐心等待")',
        '            if not os.environ.get("XIAOGPT_NO_HINT"):  # patched: skip_hint\n'
        '                await self.do_tts(f"正在问{self.chatbot.name}请耐心等待")',
    ),
    (
        "XIAOGPT_POLL_INTERVAL",
        "                if (d := time.perf_counter() - start) < 1:",
        "                _iv = float(os.environ.get(\"XIAOGPT_POLL_INTERVAL\", \"1\"))  # patched: poll_interval\n"
        "                if (d := time.perf_counter() - start) < _iv:",
    ),
    (
        "await asyncio.sleep(_iv - d)",
        "                    await asyncio.sleep(1 - d)",
        "                    await asyncio.sleep(_iv - d)  # patched: poll_interval",
    ),
    (
        "XIAOGPT_FAST_STOP",
        "            if self.config.mute_xiaoai:\n"
        "                await self.stop_if_xiaoai_is_playing()",
        '            if self.config.mute_xiaoai:\n'
        '                if not os.environ.get("XIAOGPT_FAST_STOP"):  # patched: fast_stop\n'
        "                    await self.stop_if_xiaoai_is_playing()",
    ),
    (
        "XIAOGPT_SKIP_PLAY_CHECK",
        "        is_playing = await self.get_if_xiaoai_is_playing()",
        '        if os.environ.get("XIAOGPT_SKIP_PLAY_CHECK"):  # patched: skip_play_check\n'
        "            is_playing = True\n"
        "        else:\n"
        "            is_playing = await self.get_if_xiaoai_is_playing()",
    ),
    (
        "def _intercept(",
        'EOF = object()\nPASS_TOKEN_KEYS = ("deviceId", "userId", "passToken")\n',
        'EOF = object()\nPASS_TOKEN_KEYS = ("deviceId", "userId", "passToken")\n'
        "\n"
        "\n"
        "def _intercept(text: str) -> bool:  # patched: music\n"
        '    """Let the local library claim plain 暂停/继续 while it is the thing playing."""\n'
        "    try:\n"
        "        from music_bridge import intercept_native\n"
        "    except Exception:\n"
        "        return False\n"
        "    return intercept_native(text)\n",
    ),
    (
        "XIAOGPT_MUSIC_PREFIX",
        "            # we can change prompt\n"
        "            if self.need_change_prompt(new_record):",
        '            _mp = os.environ.get("XIAOGPT_MUSIC_PREFIX", "小爱音乐")  # patched: music\n'
        "            if _mp and query.startswith(_mp):\n"
        "                await self.handle_music(query[len(_mp):])\n"
        "                continue\n"
        "            if _intercept(query):  # patched: music\n"
        "                continue\n"
        "            # we can change prompt\n"
        "            if self.need_change_prompt(new_record):",
    ),
    (
        "async def handle_music(self, text: str)",
        "    async def speak(self, text_stream: AsyncIterator[str]) -> None:",
        '    async def handle_music(self, text: str) -> None:  # patched: music\n'
        "        from music_bridge import handle_command\n"
        "\n"
        "        if self.config.mute_xiaoai:\n"
        "            await self.stop_if_xiaoai_is_playing()\n"
        "        try:\n"
        "            speak, reply = await handle_command(text)\n"
        "        except Exception as exc:\n"
        '            print(f"music error: {exc}")\n'
        "            speak, reply = True, \"本地音乐服务出错\"\n"
        '        print("音乐：" + reply)\n'
        "        if speak:\n"
        "            await self.do_tts(reply)\n"
        "\n"
        "    async def speak(self, text_stream: AsyncIterator[str]) -> None:",
    ),
]


def main() -> int:
    if not TARGET.is_file():
        print(f"target not found: {TARGET}")
        return 1

    with open(TARGET, encoding="utf-8", newline="") as fh:
        src = fh.read()

    original = src
    skipped: list[str] = []
    for marker, old, new in PATCHES:
        if marker in src:
            print(f"already patched: {marker}")
            continue
        if old not in src:
            print(f"original line not found, skipped: {marker}")
            skipped.append(marker)
            continue
        src = src.replace(old, new, 1)
        print(f"patched: {marker}")

    if src != original:
        with open(TARGET, "w", encoding="utf-8", newline="") as fh:
            fh.write(src)

    return 2 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
