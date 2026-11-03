# Japan Fare Watch 🇯🇵✈️

A Discord bot that watches flight-deal sites and pings your server when a good
**LAX → Tokyo (HND/NRT) or Osaka (KIX)** fare shows up. Possible error fares get a
loud `@everyone` ping so your group can book before the airline fixes it.

## How it works

1. Every 10 minutes it reads the RSS feeds of two curated deal sites
   (Secret Flying and The Flight Deal's LAX page).
2. It keeps a post only if:
   - it goes **to** Tokyo/Haneda/Narita/Osaka/Kansai,
   - it leaves **from** Los Angeles, and
   - the price is under your limit.
3. Each match is posted to Discord as a color-coded card:

   | Card | Meaning |
   |------|---------|
   | 🚨 red | The post mentions an error, mistake, or glitch fare. Comes with `@everyone`. |
   | 🔥 orange | Great price (≤ $550 round trip / $325 one-way) |
   | ✅ green | Good price (≤ $750 round trip / $450 one-way) |
   | ✈️ blue | A matching deal, but the bot couldn't read the price |

4. It remembers what it already posted in `seen.json`, so you never get the same deal twice.

## Setup (about 15 minutes)

### 1. Make a Discord webhook

In your server, open the channel you want alerts in → **Edit Channel → Integrations → Webhooks → New Webhook** → **Copy Webhook URL**.

Treat that URL like a password. Anyone who has it can post in your channel.

### 2. Try it on your computer

```bash
pip install -r requirements.txt

# See what currently matches (posts nothing):
python bot.py --dry-run

# Check the webhook works. Sends a fake error-fare alert:
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."   # Windows: set DISCORD_WEBHOOK_URL=...
python bot.py --test
```

### 3. Host it for free on GitHub Actions

You don't need to leave your laptop on. GitHub runs the bot for you on a schedule.

1. Create a new GitHub repo and push this folder to it, including `.github/workflows/deal-alerts.yml`.
2. In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**.
   Name it `DISCORD_WEBHOOK_URL` and paste your webhook URL.
3. Go to the **Actions** tab, open **deal-alerts**, and click **Run workflow** once to start it.

On its very first run, the bot just memorizes what's already in the feeds and
posts nothing, so you don't get flooded with old deals. After that, only new
deals get posted.

A few notes on the schedule:

- **Public vs. private repo.** Public repos run Actions for free. Private repos
  have a monthly cap on minutes, so if you make the repo private, change the
  cron line to `*/30 * * * *`. Your webhook stays private either way, because
  it lives in Secrets, not in the code.
- **Delays.** GitHub's scheduled runs can be delayed by a few minutes when
  their servers are busy.
- **Auto-disable.** GitHub can turn off schedules on inactive public repos.
  The bot's own `seen.json` commits normally keep it active, but if alerts
  ever stop, check the Actions tab.

**Prefer to run it on your own machine instead?**
Use `python bot.py --loop 10`, which checks every 10 minutes until you stop it.

## Customizing

All the settings are in `config.py`:

- **Add more home airports.** Uncomment SNA or LGB in `ORIGINS`.
- **Add more destinations.** For example, `"FUK": ["fukuoka", "fuk"]` or `"MNL": ["manila", "mnl"]`.
- **Change what counts as a deal.** Edit `GREAT_PRICE` and `MAX_PRICE`.
- **Ping a role instead of everyone.** Set `ERROR_FARE_MENTION = "<@&ROLE_ID>"`.
  To get the role ID, turn on Developer Mode in Discord, then right-click the role → Copy Role ID.

After changing the filter, run the tests to make sure it still behaves:
`python -m pytest -q`

## Limitations

- **It's only as fast as the sources.** The bot relays what deal sites find;
  it doesn't search airline prices itself. That's the realistic free approach,
  since flight price APIs cost money per search.
- **The same sale can show up twice** if both sites post it.
- **Always confirm the price** on Google Flights or the airline's own site
  before booking. Deal sites sometimes link through travel agencies.

## If you catch an error fare

- **Book right away.** Most error fares are gone within hours.
- **Don't call or message the airline about the price.** That just gets it fixed faster.
- **Don't book anything nonrefundable** (hotels, connecting flights) until the
  airline has issued your ticket and it's been a few days. Some error fares get canceled.
