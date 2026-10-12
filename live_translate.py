import asyncio
import os
import re
import sys
from datetime import datetime
from typing import Any, cast

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

try:
    CHUNK_SECONDS = float(os.getenv("CHUNK_SECONDS", "0.1"))
    if CHUNK_SECONDS <= 0:
        raise ValueError
except ValueError:
    CHUNK_SECONDS = 0.1

static_ffmpeg.add_paths()


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
    print(f"[Stream] Resolving fresh media stream for: {url}")
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
            # Audio filter: vocal bandpass (150Hz - 4000Hz) to reduce BGM/SFX interference
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
) -> None:
    if proc.stdout is None:
        stream_done.set()
        return

    try:
        while not stream_done.is_set():
            chunk = await proc.stdout.readexactly(chunk_size)
            await audio_queue.put(chunk)
    except asyncio.IncompleteReadError:
        print("\n[Stream] FFmpeg audio stream reached EOF.")
    except asyncio.CancelledError:
        pass
    finally:
        stream_done.set()


async def audio_sender_worker(
    session: Any,
    audio_queue: asyncio.Queue[bytes],
    stream_done: asyncio.Event,
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
                    print(
                        "[Stream] Audio streaming actively to Gemini...\n" + "=" * 50,
                        flush=True,
                    )
            finally:
                audio_queue.task_done()
    except asyncio.CancelledError:
        pass


async def gemini_receiver_worker(session: Any) -> None:
    """Aggregates streaming phrases into coherent full sentences."""
    og_buffer: list[str] = []
    en_buffer: list[str] = []

    def flush_subtitles() -> None:
        nonlocal og_buffer, en_buffer
        raw_og = "".join(og_buffer).strip()
        raw_en = "".join(en_buffer).strip()

        if raw_og or raw_en:
            timestamp = datetime.now().strftime("%H:%M:%S")
            cleaned_og = clean_transcript(raw_og)

            print(f"[{timestamp}] OG: {cleaned_og}")
            print(f"           EN: {raw_en}\n", flush=True)

        og_buffer = []
        en_buffer = []

    try:
        async for response in session.receive():
            server_content = response.server_content
            if not server_content:
                continue

            # 1. Source language phrase
            if (
                server_content.input_transcription
                and server_content.input_transcription.text
            ):
                text = server_content.input_transcription.text.strip()
                if text:
                    # If previous buffer ended with sentence-ending punctuation, flush before starting new sentence
                    if og_buffer and og_buffer[-1].rstrip().endswith(
                        ("。", "！", "？", "!", "?", ".")
                    ):
                        flush_subtitles()
                    og_buffer.append(text)

            # 2. Translated language phrase
            if (
                server_content.output_transcription
                and server_content.output_transcription.text
            ):
                chunk = server_content.output_transcription.text
                en_buffer.append(chunk)

                # Flush on true sentence-ending punctuation (excluding mid-clause commas)
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
) -> None:
    sample_rate = 16000
    bytes_per_sample = 2
    chunk_size = max(
        bytes_per_sample, int(chunk_seconds * sample_rate) * bytes_per_sample
    )
    queue_maxsize = max(10, int(5.0 / chunk_seconds))

    # Resolve stream
    stream_url, is_live, title = extract_stream_info(youtube_url)
    print(f"[Stream] Title: {title}")
    print(f"[Stream] Mode: {'LIVE' if is_live else 'VOD/Archive'}")

    ffmpeg_proc = await launch_ffmpeg(stream_url, is_live)
    audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_maxsize)
    stream_done = asyncio.Event()

    reader_task = asyncio.create_task(
        audio_reader_worker(ffmpeg_proc, audio_queue, stream_done, chunk_size)
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

    print(f"[Gemini] Connecting to {model_name}...")

    try:
        while not stream_done.is_set() or not audio_queue.empty():
            try:
                async with client.aio.live.connect(
                    model=model_name, config=config
                ) as session:
                    print("[Gemini] Connected and listening!")

                    sender_task = asyncio.create_task(
                        audio_sender_worker(session, audio_queue, stream_done)
                    )
                    receiver_task = asyncio.create_task(gemini_receiver_worker(session))

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
                print(f"\n[Gemini] Connection dropped ({err}). Reconnecting in 2s...")
                await asyncio.sleep(2)

    finally:
        stream_done.set()
        reader_task.cancel()
        if ffmpeg_proc.returncode is None:
            ffmpeg_proc.terminate()
            await ffmpeg_proc.wait()
        print("[System] Stream ended and resources cleaned up.")


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
                youtube_url, api_key, TARGET_LANGUAGE, MODEL_NAME, CHUNK_SECONDS
            )
        )
    except KeyboardInterrupt:
        print("\n[System] Interrupted by user. Exiting...")


if __name__ == "__main__":
    main()
