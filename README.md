# gemini-live-translate

> ⚠️ **Project Status: Proof of Concept (Inactive)**  
> This repository was built as an experimental Proof of Concept (PoC) to test real-time simultaneous translation using Google's Gemini Live API. There is currently **no intention to continue active development or add new features**.

Real-time, continuous speech translation and live subtitling for YouTube livestreams and videos using Google's **Gemini 3.5 Live Translate API** (`gemini-3.5-live-translate-preview`).

---

## ⚖️ Trade-offs & Quality Note

If you are looking for high-quality, polished subtitles, tools like [**ai-sub**](https://github.com/FlippFuzz/ai-sub) produce significantly better results:

- **[ai-sub](https://github.com/FlippFuzz/ai-sub)** processes both **audio and video** multimodal frames in batches/offline, allowing the model to understand on-screen visual context, character dialogue, and full surrounding sentences.
- **This project (`gemini-live-translate`)** processes **audio-only streaming** via simultaneous interpretation.

**The main trade-off:**

- **Quality**: Much lower than `ai-sub`. Because it translates eager, sub-second audio clauses in real time without visual or cross-sentence discourse context, translations can be literal, fragmented, or miss dropped pronouns.
- **Advantage**: It is **live**. Translations appear with minimal delay as the streamer speaks.

---

## ✨ Features

- **⚡ Live Streaming Translation**: Continuous streaming speech-to-text translation over a persistent, bidirectional WebSocket connection.
- **🌐 Dual-Language Output**: Simultaneously streams the speaker's original language (`OG:`) and the translated English text (`EN:`) with timestamps.
- **💸 Single WebSocket Economy**: Runs within a single persistent WebSocket connection. Does **not** issue per-sentence REST API requests, keeping free-tier rate limits intact.
- **🎙️ Vocal Bandpass Pre-Processing**: Applies an FFmpeg audio filter (`highpass=f=150,lowpass=f=4000`) to isolate human vocal frequencies and reduce background music and sound effect interference.
- **📦 Zero-Config FFmpeg**: Bundles static FFmpeg binaries automatically via `static-ffmpeg`—no manual system installations or PATH adjustments needed.
- **🔄 Live & VOD Compatibility**: Handles live streams natively and automatically paces archived videos (VODs) at 1x real-time speed for testing.
- **🧹 Punctuation Normalization**: Cleans speech-to-text spacing artifacts around CJK and full-width punctuation marks.

---

## 🏗️ How It Works

```
┌─────────────────────────────────┐
│     YouTube Video / Stream      │
└────────────────┬────────────────┘
                 │ HLS / Media stream URL
                 ▼
┌─────────────────────────────────┐
│    Stream Resolver (yt-dlp)     │
└────────────────┬────────────────┘
                 │ Raw audio piped to
                 ▼
┌─────────────────────────────────┐
│     FFmpeg Transcoder           │
│   - Vocal bandpass (150-4000Hz) │
│   - Transcoded to 16kHz PCM     │
└────────────────┬────────────────┘
                 │ 100ms PCM chunks (3,200 bytes)
                 ▼
┌─────────────────────────────────┐
│   Gemini Live API WebSocket     │
│ (gemini-3.5-live-translate-preview)
└────────────────┬────────────────┘
                 │ Streaming clauses
                 ▼
┌─────────────────────────────────┐
│     Sentence Aggregator         │
│   - Pairs OG & EN clauses       │
│   - Formats & strips artifacts  │
└────────────────┬────────────────┘
                 │
                 ▼
         Console Subtitles
   [10:46:12] OG: 何が来るかな？
              EN: What's going to happen?
```

---

## 📋 Prerequisites

- **Python 3.10+**
- A **Gemini API Key** from [Google AI Studio](https://aistudio.google.com/)

---

## 🚀 Installation & Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/your-username/gemini-live-translate.git
cd gemini-live-translate
```

### 2. Set Up Virtual Environment

```bash
# Create virtual environment
python -m venv .venv

# Activate on Linux/macOS:
source .venv/bin/activate

# Activate on Windows:
.venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure `.env`

Create a `.env` file in the project root:

```env
# Required: Your Gemini API Key from Google AI Studio
GEMINI_API_KEY=your_gemini_api_key_here

# Required: YouTube livestream or VOD URL
YOUTUBE_URL=https://www.youtube.com/watch?v=YOUR_VIDEO_ID

# Optional Settings
TARGET_LANGUAGE=en
MODEL_NAME=gemini-3.5-live-translate-preview
CHUNK_SECONDS=0.1
```

### 5. Run the Script

```bash
python live_translate.py
```

---

## ⚙️ Configuration Reference

| Variable          | Required |               Default               | Description                                        |
| :---------------- | :------: | :---------------------------------: | :------------------------------------------------- |
| `GEMINI_API_KEY`  | **Yes**  |                  —                  | Google AI Studio API key                           |
| `YOUTUBE_URL`     | **Yes**  |                  —                  | YouTube livestream or video URL                    |
| `TARGET_LANGUAGE` |    No    |                `en`                 | Target translation language code (BCP-47)          |
| `MODEL_NAME`      |    No    | `gemini-3.5-live-translate-preview` | Dedicated speech-to-speech translation model       |
| `CHUNK_SECONDS`   |    No    |                `0.1`                | Audio frame payload size in seconds (0.1s = 100ms) |

---

## 🖥️ Example Output

```text
[Stream] Resolving fresh media stream for: https://www.youtube.com/watch?v=Uv-Kc2uBKlM
[Stream] Title: 【 #ホロ鯖ARK 】緊急参戦‼️新衣装おうち3Dも初お披露目✨とりま裸で乗り込んでみます！【博衣こより/ホロライブ】 2026-10-12 12:10
[Stream] Mode: LIVE
[Gemini] Connecting to gemini-3.5-live-translate-preview...
[Gemini] Connected and listening!
[Stream] Audio streaming actively to Gemini...
==================================================
[11:10:25] OG: 笑いの両開きのドアでね。
           EN: The double doors of laughter So yeah.

[11:10:29] OG: ドア。
           EN: The door.

[11:10:35] OG: このドアで全部補えるのか。
           EN: This door is enough for everything?

[11:10:40] OG: うわ。
           EN: Whoa.

[11:10:44] OG: あ、補えそう。
           EN: Oh, it looks like it's enough.

[11:10:46] OG: トシくんこれならいける?
           EN: Toshi, can you do it with this?

[11:10:53] OG: 兄人間も恐竜も両開きのドアでどうにかする感じね。
           EN: Brother, humans and dinosaurs can both use the double doors, right?

[11:10:55] OG: え?
           EN: Huh?

[11:10:56] OG: ちょっと
           EN: Wait a minute.

[11:11:03] OG: トシくんもしかして足が短い。
           EN: Toshi, maybe your legs It's short.

[11:11:03] OG: え?
           EN: Huh?
```

---

## 💡 Technical Notes

### Audio Filtering

To maintain transcription quality in streams with heavy background music or noise, FFmpeg applies a bandpass filter:

```bash
-af "highpass=f=150,lowpass=f=4000"
```

This restricts the audio passed to Gemini to human vocal frequencies, helping reduce false detections caused by low-frequency bass or high-frequency game effects.

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
