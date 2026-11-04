# Hayamimi Live - User Manual

[日本語マニュアル](MANUAL.md) / [README](README.en.md)

This manual details the system requirements, startup procedures, OBS Studio integration, subtitle customization, multilingual translation, speaker profile customization, and troubleshooting for Hayamimi Live.

---

## Table of Contents
1. [System Requirements](#1-system-requirements)
2. [Startup Procedures](#2-startup-procedures)
3. [OBS Studio Integration](#3-obs-studio-integration)
4. [Subtitle Styling & Customization](#4-subtitle-styling--customization)
   - [Unified Mode](#unified-mode)
   - [Split Custom Mode](#split-custom-mode)
   - [Available Fonts](#available-fonts)
   - [Text Effects](#text-effects)
5. [Multilingual Simultaneous Translation](#5-multilingual-simultaneous-translation)
6. [Desktop Overlay](#6-desktop-overlay)
7. [Speaker Profile Studio (Dictionary & Replacement Rules)](#7-speaker-profile-studio-dictionary--replacement-rules)
   - [Automated Dictionary Generation from YouTube Archives](#automated-dictionary-generation-from-youtube-archives)
   - [Manual Replacement Rules & Profile Migration](#manual-replacement-rules--profile-migration)
8. [Troubleshooting & FAQ](#8-troubleshooting--faq)
9. [CLI Options & HTTP APIs](#9-cli-options--http-apis)

---

## 1. System Requirements

The default CPU mode runs efficiently on standard personal computer CPUs without a dedicated GPU. Supported PCs can also use NPU mode, and NVIDIA GPUs can use the experimental GPU mode.

| Component | Recommended Specs | Minimum Requirements |
| :--- | :--- | :--- |
| **OS** | Windows 11 / Windows 10 (64-bit) | Windows 10 (Build 19044 or later) |
| **CPU** | Intel Core i5 (8th gen+) / AMD Ryzen 5+ | Quad-core x86_64 CPU |
| **GPU** | Not required (GPU mode needs an NVIDIA GPU) | None |
| **RAM** | 8 GB or higher | 4 GB |
| **Disk** | 5 GB free storage (for model weights); GPU mode needs about 10 GB more | 2 GB |
| **Audio** | Standard USB microphone or headset | Audio input device |

> [!NOTE]
> **Privacy & Network Architecture**:  
> Live speech-to-text and real-time translation run locally and never send microphone audio off your PC.  
> *Note: Only speaker profile generation uses the network: it downloads YouTube videos (via yt-dlp) and sends their audio to the Google Gemini API. See section 7 for how that data is handled. API keys are stored locally.*

---

## 2. Startup Procedures

### Standard Launch
1. Double-click `start.bat` in the project root directory.
2. The modern desktop launcher GUI will launch.
3. Choose the runtime and inference mode, then click the start button (**"アプリ起動 & Webを開く"**).
4. The status switches to `稼働中 (8833 / 8766)`, and your default browser opens the Web Mic page at `http://localhost:8833/mic`.

### Choosing the Runtime and Inference Mode
Select these options in the launcher:
- **Runtime: Python venv (recommended)**: Runs directly in the local virtual environment (`.venv`).
- **Runtime: WSL Containers**: Runs inside an isolated Microsoft WSL 3.0 container.
- **Inference: CPU**: The default mode, running ReazonSpeech on the CPU.
- **Inference: NPU**: Available on PCs with a supported NPU.
- **Inference: GPU (experimental / LoRA)**: Runs Whisper on an NVIDIA GPU. The first launch creates a dedicated environment (`.venv_gpu`) and downloads about 3 GB, including PyTorch.

If Windows Smart App Control (SAC) is on, the launcher automatically starts through `start.bat`.

---

## 3. OBS Studio Integration

To overlay transparent live subtitles on OBS Studio:

### 1. Add Browser Source
1. In OBS Studio, click the `+` button in the **Sources** dock.
2. Choose **Browser** and name the source (e.g., `Hayamimi Subtitles`).

### 2. Configure Properties
- **URL**: `http://localhost:8833/`
- **Width**: `1920` (match your canvas resolution)
- **Height**: `1080`
- **Custom CSS**: Leave blank (background is automatically transparent).
- **Shutdown source when not visible**: Uncheck (recommended).

Click **OK** to place the subtitle overlay in your scene.

---

## 4. Subtitle Styling & Customization

Open `http://localhost:8833/mic` to adjust styling under the **OBS Subtitle Styling** card.

### Unified Mode
Applies a single set of font, size, color, outline, and effect parameters across all subtitle lines. Translation lines automatically scale to approximately 75% of the primary font size.

### Split Custom Mode
Allows independent styling across three tabs:
1. **Main Subtitles (Voice)**: Font, size, color, stroke color/width, and effect for spoken speech.
2. **Translation Subtitles (General)**: Shared font, size, color, stroke, and effect for translations.
3. **Language Color Overrides**: Specify distinct text colors per target language (e.g., English in cyan, Chinese in orange, Korean in mint).

### Available Fonts
Pre-loaded with open-source Google Fonts:
- **Japanese & General**: Zen Kaku Gothic New, M PLUS Rounded 1c, Dela Gothic One, Hachi Maru Pop, DotGothic16, Reggae One, Rampart One, Kiwi Maru, Yuji Boku, Shippori Mincho B1, LINE Seed JP.
- **English / Latin**: Montserrat, Bungee, Fredoka, Oswald, Righteous.
- **Chinese**: ZCOOL KuaiLe, Ma Shan Zheng, Noto Sans SC, Noto Sans TC.
- **Korean**: Do Hyeon, Jua, Black Han Sans, LINE Seed KR, Noto Sans KR.

### Text Effects
- **Stroke**: Crisp outline around characters for clean readability.
- **Double Outline**: Inner black border with an outer colored outline for broadcast-style text.
- **Glow**: Soft neon blur for a cyber, luminous effect.

---

## 5. Multilingual Simultaneous Translation

Translates recognized speech into selected target languages in real time:
- English (`en`): FuguMT (CTranslate2 INT8)
- Chinese Simplified (`zh`): Meta M2M-100
- Chinese Traditional / Taiwan (`zh-tw`): M2M-100 + OpenCC
- Chinese Traditional / Hong Kong (`zh-hk`): M2M-100 + OpenCC
- Korean (`ko`): M2M-100
- Spanish (`es`), French (`fr`), German (`de`): M2M-100

Target languages can be toggled on or off on the Web Mic page without restarting the server.

---

## 6. Desktop Overlay

A floating, topmost, transparent subtitle window for on-screen monitoring without OBS.
- Click **"Desktop Overlay"** on the launcher GUI.
- Drag borders to move and resize.
- Press `Ctrl + Shift + H` or toggle via system tray to enable **click-through (mouse transparency)**.

---

## 7. Speaker Profile Studio (Dictionary & Replacement Rules)

Manage specialized terminology, proper nouns, and creator-specific pronunciations at `http://localhost:8833/profile`.

### Automated Dictionary Generation from YouTube Archives
Automatically extracts proper nouns and pronunciation patterns from past streams:
1. **Gemini API Key**: Enter your key in the header configuration card (stored locally and masked).
2. **Input Archive URL**: Enter a YouTube video URL, playlist URL (`playlist?list=...`), or channel handle (`@ChannelName`), then click **"Generate Profile"**.
3. **Review & Apply**: The system extracts speech segments, transcribes them with local ASR, detects errors and proper nouns, and generates phonetic replacement pairs. Click **"Apply Profile"** to activate.
4. **Automated Maintenance**: If linked to a channel URL, new uploads are checked when the launcher starts and exits to update dictionaries incrementally.

> [!IMPORTANT]
> **Data sent to Google**: Generating or growing a profile sends audio from the selected videos (up to 12 minutes per video) to the Gemini API. With a free-tier key, Google may use that content to improve its products and human reviewers may read it. Paid keys are not used for product improvement. Handling varies by region, so check the [terms](https://aistudio.google.com/docs/terms). Make sure you are comfortable sending your voice and streams before using this feature.

### Speaker LoRA (GPU mode, experimental)
GPU mode also trains a per-speaker LoRA in addition to the dictionary. Subtitles are not guaranteed to improve, so this is an experimental feature.

- **Requirements**: A registered voiceprint, and the speaker's own voice in at least 50% of the video. Videos below that are treated as collaborations and skipped.
- **Training data**: The speaker's segments are cut out by voiceprint and transcribed by Gemini. This sends more audio, roughly doubling Gemini usage per video.
- **Training**: Labels from up to 6 videos are kept, and each run trains from scratch. Training waits until 25 segments are available.
- **On / off**: A trained LoRA is on by default. If subtitles get worse, turn it off on the profile card at `/profile`. Changes apply on the next launch.
- **Disk use**: About 1.5 GB per speaker.
- **Voiceprints**: You can also record one manually at `/profile`. If videos keep being skipped, register the voiceprint again.

### Priority Tags & Video Title Hashtag Integration
For creators streaming multiple games or themes, "Priority Tags" let you tag individual vocabulary items and selectively activate them.

1. **Automatic Hashtag Extraction from YouTube Titles & Descriptions**:
   - When generating or growing a profile from a YouTube video URL or channel, hashtags in both the video title and description (e.g. `【#ApexLegends】Ranked Grind` and description `#GTA RP` ──▶ `#ApexLegends`, `#GTARP`) are automatically detected and assigned to extracted terms.
   - General speaking quirks and non-hashtag terms are assigned the "`共通`" (Common) tag.
2. **Multi-Game Vocabulary Growth**:
   - When training on additional videos from different games, existing words retain previous tags while newly detected tags are merged without duplication (e.g., "ウルト" can organically belong to both `#ApexLegends` and `#OW2`).
3. **Tag Multi-Selection & Hot-Reload on Web Mic Page (`/mic`)**:
   - **Web Mic Page (`/mic`)**: Selecting a speaker profile dynamically reveals priority tag chips (e.g., `[✓ Common]` `[✓ #Apex]` `[  #GTARP]`). Click chips to multi-select tags and immediately hot-reload the ASR replacement dictionary without typing.
   - **🔄 Reload Button**: Located beside the profile selector to instantly refresh profile lists and rule updates without restarting the server or launcher.
   - **Management Page (`/profile`)**: Inspect tags on cards and edit individual rule tags via modal.
4. **SudachiPy Morphological Correction**:
   - Fast morphological analysis (~0.02ms / 0.02% CPU) prevents improper splitting of compound gaming terms and applies natural normalization during both live in-progress drafts and final confirmed subtitles.
5. **Automatic Profile Migration**:
   - Older profiles (schema 1.1.0 and earlier) are automatically migrated to `1.2.0` on startup or maintenance, with all existing rules initialized with the "`共通`" tag.

### Manual Replacement Rules & Profile Migration
- **Replacement Rules**: Manually add, edit, or remove word replacement pairs with regex support.
- **Profile Import & Export**: Export speaker profiles as JSON files for easy migration and backup.

---

## 8. Troubleshooting & FAQ

### Microphone Audio Not Picked Up
1. Verify browser microphone permissions at `http://localhost:8833/mic`.
2. Ensure the correct input device is selected in the dropdown.
3. Check that the visual input level meter responds in green when speaking.

### Subtitles Not Appearing in OBS
1. Verify launcher status is `RUNNING`.
2. Confirm the Browser Source URL is set to `http://localhost:8833/` (port 8833).
3. Right-click the source in OBS and select **Refresh cache of current page**.

---

## 9. CLI Options & HTTP APIs

```bash
python scripts/realtime_transcribe.py \
  --input ws \
  --serve 8833 \
  --ws-host 0.0.0.0 \
  --ws-port 8766 \
  --translate en,zh,ko \
  --punct-model 4class
```

### Endpoints
- `GET /`: OBS transparent overlay HTML
- `GET /mic`: Web microphone and live controller
- `GET /dashboard`: Event log and live transcript dashboard
- `GET /profile`: Speaker profile and dictionary studio
- `GET /events`: Server-Sent Events stream for speech, translation, and style updates
- `GET /overlay-style` / `POST /overlay-style`: Subtitle style configuration
- `GET /config` / `POST /config`: Dynamic runtime settings

---

## License

Released under the [MIT License](LICENSE).
