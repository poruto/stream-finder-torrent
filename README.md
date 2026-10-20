# StreamFinder

A self-hosted streaming front end: browse movies and TV shows, press **Play**, and
StreamFinder picks the best torrent, streams it through
[TorrServer](https://github.com/YouROK/TorrServer) and loads matching subtitles.
English and Czech UI.

## What it does

- **Browse like a streaming service**: trending rows, genres, decade, original
  language, IMDb rating, live search (titles, people, IMDb links, "title year").
- **One-click playback**: torrent indexes are searched in parallel (YTS, EZTV,
  The Pirate Bay, Knaben, Torrents-CSV, and the Czech/Slovak tracker SkTorrent
  with a free account). Releases are parsed and ranked
  by title/episode match, quality, availability, size per hour of video and whether
  a browser can play them. Cinema recordings, AI upscales, commentary tracks and
  oversized files are never picked by default.
- **Czech versions**: with "Czech first" dubbed and Czech-subtitled releases are
  searched by the Czech title too and played first, and the Czech audio track of a
  file is picked; versions are grouped by language and any audio track can be
  switched in the player.
- **Right file, right episode**: season packs play the requested episode; the player
  has an episode picker, autoplay of the next episode and resume.
- **Plays what browsers can't**: AC3/DTS/TrueHD audio is converted to AAC on the
  fly with ffmpeg (video is only copied, so a Raspberry Pi copes). Matroska files
  are remuxed for Firefox/Safari.
- **Subtitles that fit**: bundled subtitles from the torrent, OpenSubtitles
  (exact match by file hash; OpenSubtitles.org first, the .com API when .org
  refuses), Czech and Slovak encoding detection, and alignment to the speech - only
  when certain: with AI subtitles on, subtitle text is matched word by word with a
  transcript; otherwise speech pauses are matched and confirmed on a second stretch
  of the film (fixes offsets and 25 ↔ 23.976 fps drift).
- **Machine translation**: subtitles in another language translated into yours
  (Google Translate, free, no key; or AI with a Groq key).
- **AI subtitles (experimental)**: written live from the audio by Whisper, in any
  language, optionally translated - with a local whisper.cpp server (offline, free;
  installed by the setup scripts) or a free Groq key. Line starts come from the
  timing of the spoken words. The admin switches them on in Settings.
- **Self-healing playback**: a dead or very slow source is replaced automatically by
  a faster version, at the same position.

## Run it

You need a free [TMDB API key](https://www.themoviedb.org/settings/api). Optional:
an [OpenSubtitles API key](https://www.opensubtitles.com/consumers) (and account for
20 downloads a day), a [SkTorrent](https://sktorrent.eu) account, a
[Groq key](https://console.groq.com/keys) for AI subtitles. The installers ask for
whatever is missing - also on a manual update - and everything can be entered later
under **Settings → Server settings** (admin login, `admin` / `admin` until changed).

### Docker (Raspberry Pi, any Linux server)

```sh
curl -fsSL https://raw.githubusercontent.com/poruto/stream-finder-torrent/master/install.sh | bash
```

or, in a clone: `cp .env.example .env`, fill in the keys, `docker compose up -d --build`.
Open `http://<host>:5000` (pages: `/movies`, `/shows`, `/search`, `/settings`, `/status`).

`install.sh` also adds a cron job (`autoupdate.sh`, every 5 minutes) that rebuilds and
restarts the app whenever GitHub `master` has a new commit; the log is in
`~/.local/state/streamfinder/autoupdate.log`. Update by hand with `./update.sh`.
On 64-bit machines both also set up local Whisper (`install-whisper.sh`, a systemd
service on port 8178); a Raspberry Pi 3 is too slow for it - use a Groq key there.

### Android (Termux)

Install [Termux from F-Droid](https://f-droid.org/packages/com.termux/), then:

```sh
curl -fsSL https://raw.githubusercontent.com/poruto/stream-finder-torrent/master/termux-install.sh | bash
```

It runs as an always-on service: restarted if it crashes, and started again when the
phone boots if [Termux:Boot](https://f-droid.org/packages/com.termux.boot/) is installed.
It checks GitHub every 30 minutes and installs new versions by itself, and sets up
local Whisper for AI subtitles (whisper.cpp, built once, about 5-10 minutes; opt out
with `touch ~/stream-finder-torrent/cache/.no-whisper`).
Control it with `streamfinder start | stop | restart | status | update | logs`;
`streamfinder update` also asks for keys and logins that are still missing.

### Development

```sh
pip install -r requirements.txt   # on Windows/macOS this also brings ffmpeg
cp .env.example .env          # TMDB_API_KEY at least
./torr/TorrServer-windows-amd64.exe   # or any TorrServer on :8090
python app.py                 # http://127.0.0.1:5000
python -m pytest tests
```

## Configuration

All settings live in `.env` (see `.env.example`): API keys, preferred subtitle
languages, TorrServer URLs, torrent index mirrors and ffmpeg paths. Viewers choose
their quality, subtitle languages and playback options under **Settings**; those are
stored in the browser.

## Layout

```
app.py                     entry point (python app.py / gunicorn app:app)
streamfinder/
  views.py, api.py         pages and JSON API
  matching.py              release-name parsing and scoring
  torrents/                index providers (sktorrent.py: Czech tracker), ranking, picks
  torrserver.py            TorrServer client, file selection
  transcode.py             ffmpeg probe / audio conversion / speech detection
  subtitles/               sources, decoding, WebVTT conversion, speech alignment,
                           AI subtitles (transcribe.py), translation, Whisper models
  tmdb.py, imdb.py         metadata and IMDb ratings
  i18n.py                  English and Czech strings
  templates/, static/      UI
tests/                     unit tests
setup-keys.sh              asks for missing keys/logins (installers, manual update)
install-whisper.sh         local Whisper server for AI subtitles
```

Only use it for content you are allowed to watch.
