"""HTTP file server for the local music folder.

Only serves files under MUSIC_DIR. Supports HEAD, GET and Range so the speaker
can seek. Runs on 18899; the speaker fetches from here, which is why this port
needs an inbound firewall exception (see allow_firewall.bat).

Two URL forms:
    /f/<url-encoded relative path>   direct file
    /i/<n>                           n-th file in sorted order, avoids URL
                                     encoding issues with Chinese filenames
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import socket
import sys
import urllib.parse
from pathlib import Path

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "./music")).resolve()
PORT = int(os.environ.get("MUSIC_PORT", "18899"))

MIME = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
}


def all_tracks() -> list[Path]:
    """Same ordering as music_bridge.py, so /i/<n> stays consistent."""
    files = [
        p
        for p in MUSIC_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in MIME
    ]
    files.sort(key=lambda p: str(p.relative_to(MUSIC_DIR)).lower())
    return files


def resolve_path(raw: str) -> Path | None:
    path = urllib.parse.unquote(raw)
    if path.startswith("/f/"):
        path = path[3:]
    else:
        path = path.lstrip("/")
    candidate = (MUSIC_DIR / path.replace("/", os.sep)).resolve()
    if not str(candidate).startswith(str(MUSIC_DIR)):
        return None
    return candidate if candidate.is_file() else None


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        writer.close()
        return

    lines = head.decode("latin-1").split("\r\n")
    try:
        method, raw_path, _ = lines[0].split(" ", 2)
    except ValueError:
        writer.close()
        return

    range_header = ""
    for line in lines[1:]:
        if line.lower().startswith("range:"):
            range_header = line.split(":", 1)[1].strip()

    peer = writer.get_extra_info("peername")
    peer_ip = peer[0] if peer else "?"

    if raw_path.startswith("/i/"):
        try:
            idx = int(raw_path[3:].split("?")[0])
            tracks = all_tracks()
            target = tracks[idx] if 0 <= idx < len(tracks) else None
        except ValueError:
            target = None
    else:
        target = resolve_path(raw_path)

    if target is None:
        body = b"not found"
        writer.write(
            b"HTTP/1.1 404 Not Found\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
        )
        writer.write(body)
        await writer.drain()
        writer.close()
        print(f"404 {peer_ip} {method} {raw_path}", flush=True)
        return

    size = target.stat().st_size
    start, end = 0, size - 1
    partial = False
    if range_header.startswith("bytes="):
        spec = range_header[6:].split(",")[0]
        first, _, last = spec.partition("-")
        if first:
            start = int(first)
            if last:
                end = min(int(last), size - 1)
        elif last:
            start = max(0, size - int(last))
        partial = True
        if end < start:
            end = size - 1

    length = end - start + 1
    ctype = MIME.get(target.suffix.lower()) or "application/octet-stream"

    status = "206 Partial Content" if partial else "200 OK"
    header = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {ctype}\r\n"
        f"Content-Length: {length}\r\n"
        "Accept-Ranges: bytes\r\n"
    )
    if partial:
        header += f"Content-Range: bytes {start}-{end}/{size}\r\n"
    header += "Connection: close\r\n\r\n"
    writer.write(header.encode("ascii"))

    written = 0
    try:
        if method != "HEAD":
            with open(target, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        break
                    writer.write(chunk)
                    remaining -= len(chunk)
                    written += len(chunk)
                    await writer.drain()
        else:
            await writer.drain()
    except (ConnectionError, BrokenPipeError) as exc:
        print(f"aborted {peer_ip} {target.name}: {type(exc).__name__}", flush=True)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        print(
            f"{status.split()[0]} {peer_ip} {method} {target.name} "
            f"range={range_header or '-'} sent={written}",
            flush=True,
        )


def port_in_use(port: int) -> bool:
    """True when something already accepts connections on localhost:port.

    Windows lets a second bind sneak through in some cases, so probing with a
    real connect is more reliable than trusting bind() to fail.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.4)
    try:
        return probe.connect_ex(("127.0.0.1", port)) == 0
    finally:
        probe.close()


async def main() -> None:
    if not MUSIC_DIR.is_dir():
        print(f"music dir missing: {MUSIC_DIR}", flush=True)
        sys.exit(1)
    if port_in_use(PORT):
        print(f"port {PORT} already in use, another instance is serving. exiting.", flush=True)
        return
    # reuse_address must stay off: on Windows SO_REUSEADDR lets a second copy of
    # this script bind the same port, and then requests land on either process at
    # random. Refusing the second bind is the behaviour we want.
    server = await asyncio.start_server(
        handle, "0.0.0.0", PORT, reuse_address=False
    )
    count = len(all_tracks())
    print(f"music server on 0.0.0.0:{PORT} root={MUSIC_DIR} tracks={count}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
