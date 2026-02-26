# Oasis Discord Bot

Oasis Bot keeps your Oasis Discord community running smoothly with welcome posts, live show updates, giveaways, and music-friendly utilities like Songlink lookups. Use this repository to configure and run the bot for your server.

## Getting started

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```
2. **Create the Discord application**
   - Set up a bot user in the [Discord Developer Portal](https://discord.com/developers/applications) and enable the privileged intents you need (at minimum Server Members for welcomes and autorole).
   - Add the bot token to your environment or hosting platform as `TOKEN`.
3. **Invite the bot to your Oasis server**
   - Use the OAuth2 URL generator to create an invite link with `bot` and `applications.commands` scopes.
   - Choose the permissions your staff team requires (Manage Roles, Manage Events, Moderate Members, etc.) and place the bot role above any roles it should assign.

## Configure `config.yaml`

Fill in the server IDs and options for your Oasis community:

- **`bot.statuses`** – Rotating presence lines themed around Oasis updates, shows, and announcements.
- **`channels`** – IDs for welcome, moderation log, live show forum, and optional introductions channels.
- **`features`** – Toggle modules and provide their options:
  - `welcome_messages` – Enable/disable welcome embeds.
  - `autorole` – Role ID to grant on join and whether to include bots.
  - `member_stats` – Category and naming format for the member count voice channel.
  - `live_events.page_url` – Source URL for the live show scraper.
  - `giveaways` – Defaults, labels, and manager role IDs for the giveaway cog (`default_winner_count`, `default_duration`, `default_max_entries_per_user`).
  - `songlink` – API configuration for the `/track` command.
- **`appearance.colours`** – Hex colours used across the embeds.

After updating the configuration, restart the bot to reload the settings.

## Run the bot locally

```bash
python main.py
```

The bot loads every cog in `cogs/` and logs configuration warnings in the console for anything missing.
