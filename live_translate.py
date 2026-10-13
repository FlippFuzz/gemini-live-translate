import asyncio
import collections
import os
import re
import sys
from datetime import datetime
from typing import Any, cast

import httpx
import static_ffmpeg
import yt_dlp
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load environment configuration
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
YOUTUBE_URL = os.getenv("YOUTUBE_URL")
TARGET_LANGUAGE = os.getenv("TARGET_LANGUAGE", "en")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.5-live-translate-preview").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None

try:
    CHUNK_SECONDS = float(os.getenv("CHUNK_SECONDS", "0.1"))
    if CHUNK_SECONDS <= 0:
        raise ValueError
except ValueError:
    CHUNK_SECONDS = 0.1

static_ffmpeg.add_paths()


class DiscordNotifier:
    """Unified logging and subtitle dispatcher to stdout and Discord."""

    def __init__(self, webhook_url: str | None = None) -> None:
        self.webhook_url = webhook_url
        self.enabled = bool(webhook_url and webhook_url.strip())
        self._queue: collections.deque[str] = collections.deque()
        self._event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None
        self._running = False

    async def start(self) -> None:
        if not self.enabled or not self.webhook_url:
            return
        self._running = True
        self._client = httpx.AsyncClient(timeout=10.0)
        self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if not self.enabled:
            return
        self._running = False
        self._event.set()
        if self._worker_task:
            try:
                # Allow grace period to flush pending messages
                await asyncio.wait_for(self._worker_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass
        if self._client:
            await self._client.aclose()

    def log(self, message: str) -> None:
        """Single unified logging call for both stdout and Discord."""
        print(message, flush=True)

        if not self.enabled or not self.webhook_url:
            return

        clean_msg = message.strip()
        if not clean_msg:
            return
        if len(clean_msg) > 1900:
            clean_msg = clean_msg[:1897] + "..."
        if len(self._queue) > 500:
            self._queue.popleft()
        self._queue.append(clean_msg)
        self._event.set()

    def log_translation(self, timestamp_dt: datetime, raw_og: str, raw_en: str) -> None:
        """Unified logging for subtitle translations using Discord timestamp syntax."""
        cleaned_og = clean_transcript(raw_og) if raw_og else ""
        time_str = timestamp_dt.strftime("%H:%M:%S")
        epoch_ts = int(timestamp_dt.timestamp())

        # Stdout output (HH:MM:SS)
        print(f"[{time_str}] OG: {cleaned_og}")
        print(f"           EN: {raw_en}\n", flush=True)

        if not self.enabled or not self.webhook_url:
            return

        # Discord output: <t:TIMESTAMP:T> formats as localized Long Time (e.g. 11:10:25 AM)
        parts: list[str] = [f"<t:{epoch_ts}:T>"]
        if cleaned_og:
            parts.append(f"**OG:** {cleaned_og}")
        if raw_en:
            parts.append(f"**EN:** {raw_en}")

        msg = "\n".join(parts)
        if len(self._queue) > 500:
            self._queue.popleft()
        self._queue.append(msg)
        self._event.set()

    async def _send_with_retry(self, text: str, max_retries: int = 5) -> bool:
        payload = {"content": text}
        retries = 0

        while retries < max_retries:
            if not self.enabled or not self.webhook_url or not self._client:
                return False

            try:
                response = await self._client.post(self.webhook_url, json=payload)

                # HTTP 429 Too Many Requests (Rate limit hit)
                if response.status_code == 429:
                    retry_after = 1.0
                    try:
                        data = response.json()
                        if isinstance(data, dict) and "retry_after" in data:
                            retry_after = float(data["retry_after"])
                    except (ValueError, TypeError):
                        pass

                    retry_header = response.headers.get("Retry-After")
                    if retry_header:
                        try:
                            retry_after = max(retry_after, float(retry_header))
                        except ValueError:
                            pass

                    retry_after = max(retry_after, 0.1) + 0.1
                    print(
                        f"[Discord] Rate limit reached. Backing off for {retry_after:.2f}s...",
                        flush=True,
                    )
                    await asyncio.sleep(retry_after)
                    retries += 1
                    continue

                # HTTP 200 OK or 204 No Content
                if response.status_code in (200, 204):
                    remaining = response.headers.get("X-RateLimit-Remaining")
                    reset_after = response.headers.get("X-RateLimit-Reset-After")
                    if remaining == "0" and reset_after:
                        try:
                            wait_seconds = float(reset_after)
                            if wait_seconds > 0:
                                await asyncio.sleep(wait_seconds + 0.05)
                        except ValueError:
                            pass
                    return True

                # Webhook URL missing, deleted, or unauthorized
                if response.status_code in (401, 403, 404):
                    print(
                        f"[Discord] Webhook target error (HTTP {response.status_code}). Disabling Discord integration.",
                        flush=True,
                    )
                    self.enabled = False
                    return False

                # Malformed request
                if response.status_code == 400:
                    print(
                        f"[Discord] Bad Request (HTTP 400): {response.text}. Skipping batch.",
                        flush=True,
                    )
                    return False

                # 5xx Discord server errors
                if 500 <= response.status_code < 600:
                    retries += 1
                    wait_time = min(2.0 * (2 ** (retries - 1)), 15.0)
                    print(
                        f"[Discord] Server error (HTTP {response.status_code}). Retrying in {wait_time:.1f}s...",
                        flush=True,
                    )
                    await asyncio.sleep(wait_time)
                    continue

                print(
                    f"[Discord] Unexpected response (HTTP {response.status_code}): {response.text}",
                    flush=True,
                )
                return False

            except httpx.RequestError as exc:
                retries += 1
                wait_time = min(2.0 * (2 ** (retries - 1)), 15.0)
                print(
                    f"[Discord] Network error ({exc}). Retrying in {wait_time:.1f}s...",
                    flush=True,
                )
                await asyncio.sleep(wait_time)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"[Discord] Unexpected error: {exc}", flush=True)
                return False

        print("[Discord] Max retries reached for message batch.", flush=True)
        return False

    async def _worker(self) -> None:
        while self._running or self._queue:
            if not self._queue:
                self._event.clear()
                try:
                    await self._event.wait()
                except asyncio.CancelledError:
                    break
                if not self._running and not self._queue:
                    break

            batch: list[str] = []
            current_len = 0
            while self._queue:
                next_item = self._queue[0]
                if len(next_item) > 1900:
                    next_item = next_item[:1897] + "..."
                    self._queue[0] = next_item

                additional_len = len(next_item) + (2 if batch else 0)
                if batch and (current_len + additional_len > 1900):
                    break

                batch.append(self._queue.popleft())
                current_len += additional_len
                if len(batch) >= 10:
                    break

            if not batch:
                continue

            payload_text = "\n\n".join(batch)
            await self._send_with_retry(payload_text)

            if self._queue:
                await asyncio.sleep(0.2)


def clean_transcript(text: str) -> str:
    """Removes ASR space artifacts before punctuation in Japanese/CJK text."""
    text = re.sub(r"\s+([。、！？!?,.])", r"\1", text)
    return text.strip()


def extract_stream_info(url: str) -> tuple[str, bool, str]:
    """Resolves YouTube URL to direct audio stream URL and checks if live."""
    ydl_opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
        extracted = ydl.extract_info(url, download=False)
        if not extracted:
            raise RuntimeError(f"Failed to extract info for URL: {url}")

        info: dict[str, Any] = cast(dict[str, Any], extracted)
        entries = info.get("entries")
        if entries:
            entries_list = list(entries)
            if entries_list and isinstance(entries_list[0], dict):
                info = cast(dict[str, Any], entries_list[0])

        stream_url: str | None = info.get("url")
        if not stream_url:
            formats = info.get("formats")
            if isinstance(formats, list) and formats:
                last_format = formats[-1]
                if isinstance(last_format, dict):
                    stream_url = last_format.get("url")

        if not stream_url:
            raise RuntimeError(f"Could not extract valid stream URL for: {url}")

        is_live = bool(info.get("is_live", False))
        title = str(info.get("title", "Unknown Stream"))
        return stream_url, is_live, title


async def launch_ffmpeg(stream_url: str, is_live: bool) -> asyncio.subprocess.Process:
    """Spawns FFmpeg with speech bandpass filtering to suppress loud BGM."""
    cmd = [
        "ffmpeg",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
    ]

    if not is_live:
        cmd.append("-re")

    cmd.extend(
        [
            "-i",
            stream_url,
            "-vn",
            "-af",
            "highpass=f=150,lowpass=f=4000",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "pipe:1",
        ]
    )

    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    return process


async def audio_reader_worker(
    proc: asyncio.subprocess.Process,
    audio_queue: asyncio.Queue[bytes],
    stream_done: asyncio.Event,
    chunk_size: int,
    notifier: DiscordNotifier,
) -> None:
    if proc.stdout is None:
        stream_done.set()
        return

    try:
        while not stream_done.is_set():
            chunk = await proc.stdout.readexactly(chunk_size)
            await audio_queue.put(chunk)
    except asyncio.IncompleteReadError:
        notifier.log("ℹ️ [Stream] FFmpeg audio stream reached EOF.")
    except asyncio.CancelledError:
        pass
    finally:
        stream_done.set()


async def audio_sender_worker(
    session: Any,
    audio_queue: asyncio.Queue[bytes],
    stream_done: asyncio.Event,
    notifier: DiscordNotifier,
) -> None:
    chunks_sent = 0
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.5)
            except TimeoutError:
                if stream_done.is_set() and audio_queue.empty():
                    break
                continue

            try:
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                )
                chunks_sent += 1
                if chunks_sent == 10:
                    notifier.log("🎙️ [Stream] Audio streaming actively to Gemini...")
            finally:
                audio_queue.task_done()
    except asyncio.CancelledError:
        pass


async def gemini_receiver_worker(
    session: Any,
    notifier: DiscordNotifier,
) -> None:
    """Aggregates streaming phrases into coherent full sentences."""
    og_buffer: list[str] = []
    en_buffer: list[str] = []

    def flush_subtitles() -> None:
        nonlocal og_buffer, en_buffer
        raw_og = "".join(og_buffer).strip()
        raw_en = "".join(en_buffer).strip()

        if raw_og or raw_en:
            now = datetime.now()
            notifier.log_translation(now, raw_og, raw_en)

        og_buffer = []
        en_buffer = []

    try:
        async for response in session.receive():
            server_content = response.server_content
            if not server_content:
                continue

            if (
                server_content.input_transcription
                and server_content.input_transcription.text
            ):
                text = server_content.input_transcription.text.strip()
                if text:
                    if og_buffer and og_buffer[-1].rstrip().endswith(
                        ("。", "！", "？", "!", "?", ".")
                    ):
                        flush_subtitles()
                    og_buffer.append(text)

            if (
                server_content.output_transcription
                and server_content.output_transcription.text
            ):
                chunk = server_content.output_transcription.text
                en_buffer.append(chunk)

                if chunk.rstrip().endswith((".", "!", "?", "。", "！", "？")):
                    flush_subtitles()

    except asyncio.CancelledError:
        flush_subtitles()
        raise


async def run_pipeline(
    youtube_url: str,
    api_key: str,
    target_lang: str,
    model_name: str,
    chunk_seconds: float,
    discord_webhook_url: str | None = None,
) -> None:
    sample_rate = 16000
    bytes_per_sample = 2
    chunk_size = max(
        bytes_per_sample, int(chunk_seconds * sample_rate) * bytes_per_sample
    )
    queue_maxsize = max(10, int(5.0 / chunk_seconds))

    notifier = DiscordNotifier(discord_webhook_url)
    await notifier.start()

    notifier.log(f"🔍 [Stream] Resolving media stream for: {youtube_url}")

    stream_url, is_live, title = extract_stream_info(youtube_url)
    mode_str = "LIVE" if is_live else "VOD/Archive"
    notifier.log(
        f"📢 [Stream] Title: {title}\n"
        f"   Mode: {mode_str} | Target: {target_lang} | Model: {model_name}"
    )

    ffmpeg_proc = await launch_ffmpeg(stream_url, is_live)
    audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_maxsize)
    stream_done = asyncio.Event()

    reader_task = asyncio.create_task(
        audio_reader_worker(ffmpeg_proc, audio_queue, stream_done, chunk_size, notifier)
    )

    client = genai.Client(api_key=api_key)

    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        translation_config=types.TranslationConfig(
            target_language_code=target_lang,
            echo_target_language=False,
        ),
    )

    notifier.log(f"🔄 [Gemini] Connecting to {model_name}...")

    interrupted = False
    try:
        while not stream_done.is_set() or not audio_queue.empty():
            try:
                async with client.aio.live.connect(
                    model=model_name, config=config
                ) as session:
                    notifier.log("✅ [Gemini] Connected and listening!")

                    sender_task = asyncio.create_task(
                        audio_sender_worker(session, audio_queue, stream_done, notifier)
                    )
                    receiver_task = asyncio.create_task(
                        gemini_receiver_worker(session, notifier)
                    )

                    done, pending = await asyncio.wait(
                        [sender_task, receiver_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                    for task in done:
                        exc = task.exception()
                        if exc:
                            raise exc

            except Exception as err:  # noqa: BLE001
                if stream_done.is_set() and audio_queue.empty():
                    break
                notifier.log(
                    f"⚠️ [Gemini] Connection dropped ({err}). Reconnecting in 2s..."
                )
                await asyncio.sleep(2)

    except (KeyboardInterrupt, asyncio.CancelledError):
        interrupted = True
        raise
    finally:
        stream_done.set()
        reader_task.cancel()
        if ffmpeg_proc.returncode is None:
            ffmpeg_proc.terminate()
            try:
                await asyncio.shield(ffmpeg_proc.wait())
            except asyncio.CancelledError:
                print("[System] FFmpeg shutdown was cancelled.", flush=True)
            except Exception as err:  # noqa: BLE001
                print(f"[System] Error waiting for FFmpeg shutdown: {err}", flush=True)

        if interrupted:
            notifier.log("⏹️ [System] Process interrupted by user (Ctrl+C). Exiting...")
        else:
            notifier.log("🛑 [System] Stream ended and resources cleaned up.")

        try:
            await asyncio.shield(notifier.stop())
        except asyncio.CancelledError:
            print("[System] Discord notifier shutdown was cancelled.", flush=True)
        except Exception as err:  # noqa: BLE001
            print(f"[System] Error shutting down Discord notifier: {err}", flush=True)


def main() -> None:
    if not GEMINI_API_KEY:
        print("Error: GEMINI_API_KEY not found in environment or .env file.")
        sys.exit(1)

    if not YOUTUBE_URL:
        print("Error: YOUTUBE_URL not found in environment or .env file.")
        sys.exit(1)

    api_key: str = GEMINI_API_KEY
    youtube_url: str = YOUTUBE_URL

    try:
        asyncio.run(
            run_pipeline(
                youtube_url,
                api_key,
                TARGET_LANGUAGE,
                MODEL_NAME,
                CHUNK_SECONDS,
                DISCORD_WEBHOOK_URL,
            )
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
