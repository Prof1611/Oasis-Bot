# Oasis Bot

Oasis Bot is a Discord bot tailored for Oasis communities, with lightweight utility commands and music-first features:

- `/track` Song.link lookups with platform buttons
- `/droptrack` mini-game management (Drop The Track)
- `/help` command discovery
- `/uptime` runtime status checks

## Getting started

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```
2. **Create your Discord application**
   - Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications).
   - Enable intents as needed (the bot currently starts with full intents).
   - Set your bot token as environment variable `TOKEN`.
3. **Invite the bot**
   - Use OAuth2 URL Generator with `bot` and `applications.commands` scopes.
   - Grant permissions your server needs (Manage Threads, Send Messages, Add Reactions, Manage Webhooks, etc.).

## Configuration (`config.yaml`)

Only actively used keys are included:

- `bot.statuses` – rotating listening statuses.
- `features.drop_the_track` – defaults used when a guild first initializes Drop The Track settings.
- `features.songlink` – API options for `/track`.
- `appearance.colours` – embed colours used across cogs.

After updating `config.yaml`, restart the bot.

## Run locally

```bash
python main.py
```

The bot auto-loads all cogs from `cogs/`.
