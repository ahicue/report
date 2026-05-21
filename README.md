# Economics Paper Push Robot

A scheduled Discord robot that recommends economics journal papers, with a preference for environmental, labor, and urban topics using input-output table methods.

## What it pushes

Each Discord message includes:

- Paper title and link
- Journal grade from the local `journal_tiers` config
- Research background
- Research purpose
- Originality
- Method
- Findings summary
- Journal, publisher/platform, authors, and publication date

The default source is OpenAlex metadata. OpenAlex indexes papers across publishers and platforms such as Elsevier/ScienceDirect, Springer Nature, Wiley, Taylor & Francis, Oxford, Cambridge, JSTOR, NBER, and many university repositories. This keeps the robot stable without scraping publisher pages.

## Quick Start

1. Copy the templates:

```bash
cp .env.example .env
cp config.example.json config.json
```

2. Edit `.env`:

```bash
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
OPENALEX_MAILTO=your-email@example.com
```

`OPENALEX_MAILTO` is optional but recommended by OpenAlex for polite API use.

3. Edit `config.json` if you want different keywords or journal grades.

4. Test without posting to Discord:

```bash
python3 paper_robot.py --config config.json --dry-run
```

5. Post one paper:

```bash
python3 paper_robot.py --config config.json
```

## Discord "Continue" Command

The webhook can only send messages. To make the robot react when someone types `继续` in Discord, create a Discord Bot and run the listener:

1. Install the listener dependency:

```bash
python3 -m pip install -r requirements.txt
```

2. In the Discord Developer Portal, create a bot and enable `MESSAGE CONTENT INTENT`.

3. Invite the bot to your server with permissions to read messages, send messages, and add reactions.

4. Add these values to `.env`:

```bash
DISCORD_BOT_TOKEN=your-bot-token
DISCORD_COMMAND_CHANNEL_ID=optional-channel-id
```

`DISCORD_COMMAND_CHANNEL_ID` is optional. If it is blank, the bot will accept `继续` in any channel it can read.

5. Start the listener:

```bash
python3 discord_listener.py --config config.json
```

When someone sends exactly `继续`, the listener pushes the next paper through the existing Discord webhook and replies with a short status message.

## Discord Forum Channel

To post papers into a Discord Forum channel, create or move the webhook so it belongs to the Forum channel, then set:

```bash
DISCORD_FORUM_POSTS=true
```

Each paper will create a new forum post. The forum post title is generated from the paper title.

If the Forum channel requires tags, add the tag IDs as a comma-separated list:

```bash
DISCORD_FORUM_TAG_IDS=123456789012345678,234567890123456789
```

Discord requires `thread_name` or `thread_id` when executing a webhook in a Forum or Media channel. This robot uses `thread_name` to create one new forum thread per paper.

## Schedule

The GitHub Actions workflow runs every 30 minutes from 08:00 to 20:00 Asia/Tokyo, pushing one paper per run.

For local cron:

```cron
0,30 8-20 * * * cd "/Users/kongziqing/Documents/stamp robot" && /usr/bin/python3 paper_robot.py --config config.json
```

## Journal Grades

Journal grade is not universal. The robot reads grades from `journal_tiers` in `config.json`.

The default file includes a small starter list such as:

- Top 5 economics journals
- Journal of Environmental Economics and Management
- Journal of Labor Economics
- Journal of Urban Economics
- Economic Systems Research
- Energy Economics
- Ecological Economics

Replace or expand this with your preferred ranking source, for example ABS/AJG, ABDC, JCR quartile, Scimago, or your university's internal list.

## About School Login and Paywalled Sources

Publisher sites such as ScienceDirect, SpringerLink, and Wiley Online Library are best handled through one of these routes:

- Metadata-first: use OpenAlex/Crossref/Semantic Scholar to discover papers, then link to the DOI or publisher page. This is the current implementation.
- API-first: add official APIs such as Elsevier APIs, Springer Nature APIs, Wiley TDM, Crossref, OpenAlex, Semantic Scholar, RePEc, NBER, SSRN, and EconBiz where keys are available.
- Institution access: run the robot on your machine while connected to your university VPN/proxy, or add a later browser-based full-text fetcher using a local logged-in browser session.

Do not put your school username/password directly into this project. If we add authenticated full-text access later, use a local session, VPN/proxy, or official API token rather than saving account credentials in code.
