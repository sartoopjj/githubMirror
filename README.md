# GitHub Mirror — thefeed release publisher

A small Telegram bot that watches the
[thefeed](https://github.com/sartoopjj/thefeed) GitHub repository for new
releases and posts the **client** binaries to the
[**@thefeedfile**](https://t.me/thefeedfile) channel.

A scheduled GitHub Action (see `.github/workflows/`) runs the bot on a
cadence; when a new tag is published on `thefeed`, this bot:

1. Pulls the release metadata from the GitHub API.
2. Filters the release assets down to client binaries only
   (server binaries, checksum files, and release notes are skipped).
3. Downloads each client and re-uploads it to `@thefeedfile` via
   Telethon (MTProto, so the 50 MB bot-API limit does not apply).
4. Attaches the release's `README.md` to the introduction message.
5. Posts a final `#گزارش` summary listing every tracked project and
   its latest version, with quick-jump buttons to the thefeed channels.

## Upload order

Files are sent in a fixed platform order so users see the most relevant
binary first:

```
openbsd  →  termux  →  darwin  →  linux  →  windows  →  android  →  ios
```

Within each platform, 64-bit and `universal` variants are surfaced
before 32-bit / niche variants. Windows ships in three architectures —
`amd64`, `arm64` (Surface / Snapdragon) and `386` (32-bit) — each with
its own caption; the arch checks run before the plain 64-bit fallback,
or every Windows build would be labelled "۶۴ بیتی".

## Per-file captions

Every uploaded file gets:

- The version tag.
- The exact filename.
- A short Persian one-line description (e.g. *مناسب همه گوشی‌های اندروید*
  for the universal APK, *کلاینت مک (Apple Silicon — M1/M2/M3)* for
  `darwin-arm64`, etc.).
- The SHA-256 of the binary as uploaded.
- A single inline button: **📥 Download from Github**, pointing at the
  original release asset.

## Summary message

After all files are sent, the bot posts a status report containing:

- Every tracked project and its latest version.
- A quoted block listing each file uploaded in this run — its Persian
  description and a direct `t.me` link to that message in the channel,
  so a release post can link straight to an individual binary. The
  description and URL sit on separate lines because a Persian label and
  an LTR URL on one line get scrambled by bidi reordering. The link base
  is resolved from the channel entity (public `t.me/<username>/<id>`,
  otherwise the private `t.me/c/<id>/<id>` form), not from
  `channel_username` in config — that key names the announcement
  channel, not necessarily the upload target.

The report is sent as HTML (the quoted block needs a real
`<blockquote>`, which markdown cannot express) and carries three
quick-jump buttons:

- 📢 کانال اصلی دفید — [@networkti](https://t.me/networkti)
- 📦 کانال فایل‌های باینری/نصبی دفید — [@thefeedfile](https://t.me/thefeedfile)
- ⚙ کانال کانفیگ‌های دفید — [@thefeedconfig](https://t.me/thefeedconfig)

The previous summary message is deleted before a new one is posted, so
the channel only ever has one report pinned to the top of recent
history.

## Configuration

`config.json` holds the target channel and the list of tracked
repositories:

```json
{
  "telegram": {
    "channel_id": "-100…",
    "channel_username": "@thefeedfile"
  },
  "repositories": [
    {
      "name": "theFeed",
      "github_url": "https://github.com/sartoopjj/thefeed"
    }
  ]
}
```

Telegram credentials come from environment variables (or a local
`.env` file):

```
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_BOT_TOKEN=...
```

State is kept in `processed_releases.json` so an already-published
release is not re-uploaded on the next run.

## Running locally

```bash
pip install -r requirements.txt
python bot.py
```

Or via Docker:

```bash
docker build -t githubmirror .
docker run --rm --env-file .env -v "$PWD/processed_releases.json":/app/processed_releases.json githubmirror
```
