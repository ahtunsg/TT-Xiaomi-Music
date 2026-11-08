# TT-Xiaomi-Music

[English](README.md) | [简体中文](README.zh-CN.md)

Local music and story playback for XiaoAI speakers, built on top of [xiaogpt](https://github.com/yihong0618/xiaogpt). It turns a folder of MP3s on your PC into a library the speaker can play by name, while keeping the original 「小爱同学」 wake word and every native feature intact. No firmware flashing, no firmware modification, no wake-word replacement.

Tested on an LX06 (XiaoAI Speaker Pro) with a 620-track library: title lookup, shuffled continuous playback, folder-based story sets played episode by episode, resume after stopping, and voice stop / track skip.

## Why this exists

xiaogpt is an LLM side-channel: it polls Xiaomi cloud conversation records, and when a keyword matches it sends the question to an LLM and speaks the answer through the speaker's native TTS. This project reuses that side-channel without the LLM. When the music prefix is detected, it queries a local library and pushes a local file to the speaker over DLNA.

```
Say 「小爱同学 小爱音乐 播放晴天」
  -> wake word triggers; ASR result is written to Xiaomi cloud conversation records
  -> xiaogpt polls the new record and sees the 「小爱音乐」 prefix
  -> native answer is cut off; control goes to music_bridge.handle_command()
  -> library lookup / queue calculation / progress saved to disk
  -> miservice.play_by_url() hands the speaker a URL on this PC
  -> the speaker fetches the file directly from music_server.py (Range / non-ASCII names / subfolders)
```

Voice commands are Chinese because the speaker's ASR is Chinese. The phrases in the tables below are the literal words to say.

## Library layout

The library root is set by `MUSIC_DIR`:

- Loose audio files directly under the root form the music pool, used for shuffle and title lookup
- Each subfolder under the root is a story series; the folder name is the series name, and episodes play in natural sort order
- Looking up a single track queues the entire loose-file pool starting at the matched track, so 「下一首」 keeps going
- Shuffle only shuffles loose files; episode 37 of a story set will never land between two songs

Supported formats: mp3 / m4a / aac / flac / wav / ogg / opus. Title and artist come from ID3 tags, falling back to the file name. Scan results are cached in memory and in `music_index.json`: about 1 ms on the hot path for 620 tracks, about 60 ms for the first scan in a new process.

## Setup

Requirements: a Windows PC running 24/7 on the same LAN as the speaker, Python 3.11+, xiaogpt 3.24, and `~/.mi.token` (obtained by signing in to a Xiaomi account through miservice).

1. Clone the repository, create a venv and install dependencies:

   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```

2. Copy the samples from `examples/` to the repository root and fill them in:

   - `music_env.bat`: at minimum `set "MUSIC_DIR=path\to\your\library"`
   - `secrets.bat`: Xiaomi credentials (first login only), `MI_DID`, and the LLM API key
   - `xiao_config.yaml`: xiaogpt model and hardware settings

3. Get the speaker's `deviceId` (a UUID) and set `MUSIC_DEVICE_ID` in `music_env.bat`. Use `miservice mina` to list devices, or read it from the UPnP description.

4. Apply the patch and start:

   ```
   patch_xiaogpt.py is applied automatically by scripts\start_xiaogpt.bat
   scripts\run_service.bat
   ```

   `run_service.bat` starts both the file server and xiaogpt. The patch is idempotent; running it again will not duplicate code.

5. For autostart, put a shortcut to `scripts\start_hidden.vbs` in the Windows Startup folder.

`patch_xiaogpt.py` does four things: removes the hard-coded waiting tone, skips redundant playback-status calls, shortens the poll interval from 1 s to 0.4 s, and hooks the music prefix plus short in-playback commands into the main loop.

## Voice commands

Playback commands need the prefix, 「小爱音乐」 by default; change it with `XIAOGPT_MUSIC_PREFIX`. The wake word 「小爱同学」 is mandatory: the speaker only starts listening after the wake word, so anything said before it never reaches ASR. This is a device limitation.

| Say | Effect |
| --- | --- |
| 小爱音乐 播放 海阔天空 | Look up a single track by title fragment |
| 小爱音乐 播放 小鸡多多 | Play a whole series by folder name |
| 小爱音乐 小鸡多多第5集 | Start at episode 5 (Chinese numerals also work) |
| 小爱音乐 第5集 | Jump to episode 5 in the current queue |
| 小爱音乐 播放音乐 / 随机 / 随便听听 / 来点音乐 | Shuffle the root-level music pool and play continuously |
| 小爱音乐 继续 | Resume: by episode for a series, by title for a single track |
| 小爱音乐 停止 | Stop and save progress |
| 小爱音乐 有几首 / 列表 | Report the library size |

Control commands need no prefix — just say 「小爱同学 停止」 — as long as playback was started by this program and there was activity within the last 30 minutes. Accepted phrases:

- Stop: 停止、停止播放、暂停、暂停一下、停、停下、停一下、别放了、关掉、关闭
- Resume: 继续、继续播放、继续播、接着、接着放、接着听
- Next: 下一首、下一集、下一曲、换一首、换一个、切歌
- Previous: 上一首、上一集、上一曲、前一首

## Known limitations

On the LX06, `player_get_status` returns `status=1` whether the speaker is playing or idle. There is no playback position and no way to tell if audio is running, so episode changes are driven by a timer based on file duration. Two consequences:

- Given a single URL the speaker loops it on its own, so `player_stop` must be called when the queue ends; otherwise the last episode repeats forever
- Pausing from the phone app or with the physical buttons is invisible to this program, so the next episode still starts on schedule. Saying 「暂停」 is intercepted and really stops playback

Voice commands also inherit a one-to-two-second delay because they depend on Xiaomi cloud conversation records. That is the cost of a side-channel architecture. Removing it requires a firmware-level approach such as Open-XiaoAI, which this project deliberately avoids.

## Security and privacy

The repository contains no real credentials, device identifiers, LAN addresses, or audio files. Everything below is supplied through environment variables or local files and is excluded by `.gitignore`:

- `secrets.bat` (API key, Xiaomi account, `MI_DID`)
- `~/.mi.token` (Xiaomi login session)
- `music_state.json`, `music_index.json` (playback progress and library index)
- Logs and the audio files themselves

Before publishing, double-check that `MUSIC_DEVICE_ID` is not hard-coded in the code or in the sample configs.

## Credits

The side-channel architecture comes from [xiaogpt](https://github.com/yihong0618/xiaogpt); Xiaomi cloud access comes from `miservice`. This project only adds a local library layer on top.

Released under the MIT License. See LICENSE in the repository root.
