# MediaVault — Save Instagram / YouTube / Twitter Videos to Your Gallery

Everything runs on your own PC. No third-party apps, no cloud services, no copying from other projects.

```
d:\shortcut\
├── server\
│   ├── server.py          ← Python server (runs on PC)
│   ├── requirements.txt   ← Python dependencies
│   ├── setup.bat          ← First-time setup script
│   └── start_server.bat   ← Start server on subsequent uses
└── shortcut_blueprint.json ← Step-by-step Shortcut recipe
```

---

## How It Works

```
iPhone (iOS Shortcut)
   │  shares URL (Reel / YouTube / Tweet)
   ▼
PC Server (server.py)
   │  yt-dlp downloads best-quality MP4
   │  stores file under ~/Downloads/MediaVault/
   │  logs entry in SQLite DB (vault.db)
   ▼
iPhone (iOS Shortcut)
   │  downloads MP4 from server
   ▼
Photos app — saved to "MediaVault" album
```

---

## Part 1 — PC Setup

### Requirements
- Windows 10 / 11
- Python 3.10 or newer → https://www.python.org/downloads/ (tick **Add Python to PATH**)
- Your PC and iPhone must be on the **same Wi-Fi network**

### First-time setup

1. Open **File Explorer** → navigate to `d:\shortcut\server\`
2. Double-click **`setup.bat`**  
   It will install all dependencies and start the server once.
3. Note the **API Key** printed in the console — you will paste it in the Shortcut.
4. Note your **PC's local IP address**:
   - Press `Win + R` → type `cmd` → press Enter
   - Type `ipconfig` → look for `IPv4 Address` under your Wi-Fi adapter (e.g. `192.168.1.10`)

### Starting the server every session

Double-click **`start_server.bat`** whenever you want to use the Shortcut.  
Keep the window open while downloading.

### Where files are saved

```
C:\Users\<YourName>\Downloads\MediaVault\
   ├── instagram\
   ├── youtube\
   ├── twitter\
   └── vault.db     ← download history (SQLite)
```

---

## Part 2 — iOS Shortcut Setup

Follow these steps inside the **Shortcuts** app on your iPhone.

### Create the Shortcut

1. Open **Shortcuts** → tap **+** (top right)
2. Tap the title at the top and rename it: **MediaVault — Save to Gallery**
3. Tap **Add Action** and add each action below in order.

---

### Action 1 — Receive Share Sheet input

- Search for and add: **"Receive"** (Shortcuts input)
- Settings:
  - Accept: **URLs, Text**
  - If there is no input: **Ask for URL**

---

### Action 2 — Set PC_IP variable

- Add: **Text**
- Enter your PC's local IP, e.g. `192.168.1.10`
- Tap **X** on the right → choose **Add to Variable** → name it **`PC_IP`**

---

### Action 3 — Set PORT variable

- Add: **Text** → enter `8765`
- Add to Variable → name it **`PORT`**

---

### Action 4 — Set API_KEY variable

- Add: **Text** → paste the API key from the server console
- Add to Variable → name it **`API_KEY`**

---

### Action 5 — Build server URL

- Add: **URL**
- Enter:  `http://` then insert variable `PC_IP` then `:` then `PORT` then `/download`
  - Full result: `http://192.168.1.10:8765/download`
- Add to Variable → name it **`SERVER_URL`**

---

### Action 6 — POST the URL to the server

- Add: **Get Contents of URL**
- URL: insert variable **`SERVER_URL`**
- Tap **Show More**:
  - Method: **POST**
  - Headers: tap **+**
    - `Content-Type` = `application/json`
    - `X-API-Key` = insert variable **`API_KEY`**
  - Request Body: **JSON**
  - Fields: tap **+** → Key: `url` → Value: insert variable **`Shortcut Input`**
- Add to Variable → name it **`ServerResponse`**

---

### Action 7 — Get download status

- Add: **Get Dictionary Value**
- Get: **Value** for key `status`
- From: **`ServerResponse`**
- Add to Variable → name it **`DownloadStatus`**

---

### Action 8 — Handle errors

- Add: **If**
  - Input: **`DownloadStatus`**
  - Condition: **is not** `success`
  - Inside the If block:
    - Add **Get Dictionary Value** → key `error` from `ServerResponse` → variable **`ErrorMsg`**
    - Add **Show Alert** → Title: `Download Failed` → Message: `ErrorMsg`
    - Add **Exit Shortcut**
  - Tap **End If** to close the block

---

### Action 9 — Get file path

- Add: **Get Dictionary Value** → key `file_path` from `ServerResponse`
- Add to Variable → name it **`FilePath`**

---

### Action 10 — Build file download URL

- Add: **URL**
- Enter: `http://` + `PC_IP` + `:` + `PORT` + `FilePath` + `?api_key=` + `API_KEY`
- Add to Variable → name it **`FileURL`**

---

### Action 11 — Download the video file

- Add: **Get Contents of URL**
- URL: **`FileURL`**
- Method: **GET**
- Add to Variable → name it **`VideoData`**

---

### Action 12 — Save to Photos

- Add: **Save to Photo Album**
- Input: **`VideoData`**
- Album: type **MediaVault** (it will be created automatically)

---

### Action 13 — Get video title

- Add: **Get Dictionary Value** → key `title` from `ServerResponse`
- Add to Variable → name it **`VideoTitle`**

---

### Action 14 — Show notification

- Add: **Show Notification**
- Title: `Saved to Gallery ✓`
- Body: **`VideoTitle`**

---

### Enable Share Sheet

1. Tap the **settings icon** (top-left of shortcut editor)
2. Turn on **"Show in Share Sheet"**
3. Set accepted types to **URLs**

---

## Part 3 — Using the Shortcut

### From Instagram
1. Open the Reel → tap **Share** (paper plane) → **Copy Link**
2. Open **Shortcuts** → tap **MediaVault** (or tap the share icon in Safari while on the reel URL)

### From YouTube
1. Tap **Share** on any video → **Copy Link**
2. Run the Shortcut — paste when prompted, or use Share Sheet from YouTube app

### From Twitter / X
1. Tap **Share** on a tweet with video → **Copy Link to Tweet**
2. Run the Shortcut

### Quickest method (Share Sheet)
Open the video in Safari or in the app → tap the system **Share** button → scroll down and tap **MediaVault — Save to Gallery**.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Connection refused" | Make sure `start_server.bat` is running on the PC |
| "Invalid or missing API key" | Check the API key matches the one in `api_key.txt` inside the MediaVault folder |
| "DownloadError" for Instagram | Instagram requires you to be logged in. yt-dlp supports cookie import — see yt-dlp docs |
| Video saves but no sound | The server merges audio automatically; make sure ffmpeg is installed (`winget install ffmpeg`) |
| PC IP keeps changing | Set a static local IP in your router's DHCP settings for your PC's MAC address |

### Install ffmpeg (recommended for best quality)

```powershell
winget install --id Gyan.FFmpeg -e
```

---

## Supported Platforms

| Platform | What is saved |
|---|---|
| Instagram | Reels, posts with video, Stories (public) |
| YouTube | Videos, Shorts |
| Twitter / X | Videos embedded in tweets |

---

## Privacy & Security

- The server only accepts requests carrying your personal API key.
- The server binds to `0.0.0.0` (all network interfaces) on port `8765` — only devices on your local Wi-Fi can reach it.
- No data is sent to any external service. yt-dlp contacts the media platform directly from your PC.
- All media and the download history database are stored in `~/Downloads/MediaVault/` on your PC only.
