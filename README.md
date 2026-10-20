# StreamFinder

A self-hosted streaming front end: browse movies and TV shows, press **Play**, and
StreamFinder picks the best torrent, streams it through
[TorrServer](https://github.com/YouROK/TorrServer) and loads matching subtitles.
English and Czech UI.

## What it does

- **Browse like a streaming service**: trending rows, genres, decade, original
  language, IMDb rating, live search (titles, people, IMDb links, "title year").
- **One-click playback**: five torrent indexes are searched in parallel
  (YTS, EZTV, The Pirate Bay, Knaben, Torrents-CSV). Releases are parsed and ranked
  by title/episode match, quality, availability, size per hour of video and whether
  a browser can play them. Cinema recordings, AI upscales, commentary tracks and
  oversized files are never picked by default.
- **Right file, right episode**: season packs play the requested episode; the player
  has an episode picker, autoplay of the next episode and resume.
- **Plays what browsers can't**: AC3/DTS/TrueHD audio is converted to AAC on the
  fly with ffmpeg (video is only copied, so a Raspberry Pi copes). Matroska files
  are remuxed for Firefox/Safari.
- **Subtitles that fit**: bundled subtitles from the torrent, OpenSubtitles
  (exact match by file hash), quota-free downloads from OpenSubtitles.org, Czech and
  Slovak encoding detection, and automatic alignment to the speech in the audio
  (fixes constant offsets and 25 ↔ 23.976 fps drift).
- **Self-healing playback**: a dead or very slow source is replaced automatically by
  a faster version, at the same position.

## Run it

You need a free [TMDB API key](https://www.themoviedb.org/settings/api). An
[OpenSubtitles API key](https://www.opensubtitles.com/consumers) is optional.

### Docker (Raspberry Pi, any Linux server)

```sh
curl -fsSL https://raw.githubusercontent.com/poruto/stream-finder-torrent/master/install.sh | bash
```

or, in a clone: `cp .env.example .env`, fill in the keys, `docker compose up -d --build`.
Open `http://<host>:5000` (pages: `/movies`, `/shows`, `/search`, `/settings`, `/status`).

`install.sh` also adds a cron job (`autoupdate.sh`, every 5 minutes) that rebuilds and
restarts the app whenever GitHub `master` has a new commit; the log is in
`~/.local/state/streamfinder/autoupdate.log`. Update by hand with `./update.sh`.

### Android (Termux)

Install [Termux from F-Droid](https://f-droid.org/packages/com.termux/), then:

```sh
curl -fsSL https://raw.githubusercontent.com/poruto/stream-finder-torrent/master/termux-install.sh | bash
```

It runs as an always-on service: restarted if it crashes, and started again when the
phone boots if [Termux:Boot](https://f-droid.org/packages/com.termux.boot/) is installed.
It checks GitHub every 30 minutes and installs new versions by itself.
Control it with `streamfinder start | stop | restart | status | update | logs`.

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
  torrents/                index providers, ranking and picks
  torrserver.py            TorrServer client, file selection
  transcode.py             ffmpeg probe / audio conversion / speech detection
  subtitles/               sources, decoding, WebVTT conversion, speech alignment
  tmdb.py, imdb.py         metadata and IMDb ratings
  i18n.py                  English and Czech strings
  templates/, static/      UI
tests/                     unit tests
```

Only use it for content you are allowed to watch.
