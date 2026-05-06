# Steam Signal Bot — Phase 3

Professional Steam gaming signal bot for Railway.

## What it does

- Discovers Steam games from popular new releases and top sellers
- Pulls real Steam store data
- Pulls recent Steam reviews
- Pulls current player counts
- Detects repeated player sentiment patterns
- Detects hype type: organic, streamer, nostalgia, social, risk
- Detects community overlap from review language
- Scores the game using real signals
- Sends clean Discord embeds
- Adds short Arabic echo lines under key insights
- Uses anti-spam cooldown memory

## Railway Setup

1. Upload these files to GitHub.
2. Go to Railway.
3. Create New Project.
4. Choose GitHub Repository.
5. Select this repo.
6. Add Railway variable:

```env
DISCORD_WEBHOOK_URL=your_discord_webhook_url
```

Optional:

```env
INSTANT_GAMING_ENABLED=false
```

7. Railway should run:

```bash
python main.py
```

If not, set Start Command manually to:

```bash
python main.py
```

## Tune Spam Level

In `config.yaml`:

```yaml
alert_threshold: 72
cooldown_hours: 36
scan_minutes: 60
```

Less spam:

```yaml
alert_threshold: 82
cooldown_hours: 48
scan_minutes: 120
```

More alerts:

```yaml
alert_threshold: 65
cooldown_hours: 24
scan_minutes: 45
```

## Important

The bot does not fake certainty. It uses real public Steam signals and labels uncertain things as watch points.
