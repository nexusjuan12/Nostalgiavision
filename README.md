# NostalgicTV — Desktop Port

A Python/web port of the NostalgicTV Android app. Run it on Windows or Linux to get a live TV-style program guide powered by your Plex media server.

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run (opens browser automatically)
python start.py
```

Then open **http://localhost:5000** if the browser doesn't open automatically.

## First-time setup

1. **Settings → Plex Connection** — paste your Plex server URL (e.g. `http://192.168.1.100:32400`) and your [Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
2. **Settings → Library Sync** — select your movie/TV libraries and click **Sync Selected**. This caches your Plex library locally.
3. **Settings → Build Schedule** — generates a 3-day rolling schedule for all 72 channels.
4. Switch to the **Guide** tab to browse what's "on".

## Features

| Feature | Description |
|---------|-------------|
| **72 predefined channels** | Disney, Nickelodeon, HBO, NBC, etc. — content matched from your Plex library |
| **EPG guide** | Scrollable grid showing current + upcoming programs per channel |
| **4 scheduling algorithms** | RANDOM, CYCLIC_SHUFFLE, BLOCK_SHUFFLE, BLOCK_CYCLIC |
| **Channel builder** | Create custom channels filtered by genre, studio, keyword, year |
| **Hide/show channels** | Declutter the guide by hiding channels with no matching content |
| **Local cache** | SQLite database — guide works fast without hitting Plex on every load |
| **Auto-extend schedules** | Only generates new slots; doesn't overwrite existing schedule |

## Project structure

```
nostalgiatv/
├── app.py          # Flask web server + REST API
├── channels.py     # 72 predefined channel configs
├── scheduler.py    # Schedule generation engine (4 algorithms)
├── library_sync.py # Plex → SQLite content sync
├── plex_client.py  # Plex HTTP API client
├── database.py     # SQLite layer (content cache + EPG)
├── start.py        # Entry point (init DB, launch server)
├── config.json     # User config (Plex URL, token, settings)
├── data/           # SQLite database (auto-created)
└── templates/
    └── index.html  # Web UI
```

## Configuration (`config.json`)

```json
{
  "plex_url": "http://192.168.1.100:32400",
  "plex_token": "your-token-here",
  "library_ids": ["1", "2"],
  "schedule_days_ahead": 3,
  "align_to_quarter_hour": true,
  "port": 5000,
  "host": "127.0.0.1"
}
```

## Finding your Plex token

1. Sign in to Plex Web
2. Open any media item → click the ⋮ menu → **Get Info** → **View XML**
3. Copy the `X-Plex-Token` value from the URL

Or visit: https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/

## Channel matching

Channels pull content from your Plex library based on:
- **Genres** — e.g. "Animation", "Horror"
- **Studios/Networks** — e.g. "HBO", "Disney"
- **Keywords** — searched in title + show title
- **Content ratings** — e.g. "TV-PG", "PG-13"
- **Collections** — Plex collection names

Channels with no matching content will show "No programs scheduled" in the guide — use the **Hide** button to clean up the guide.
