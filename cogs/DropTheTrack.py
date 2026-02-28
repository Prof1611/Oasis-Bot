from __future__ import annotations

from typing import Optional, List, Dict, Tuple
import asyncio
import datetime
import logging
import random
import re
import sqlite3

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config_helpers import get_embed_colours, load_config

# ============================================================
# Database setup (sqlite)
# ============================================================
conn = sqlite3.connect("database.db", check_same_thread=False)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS drop_track_settings (
        guild_id INTEGER PRIMARY KEY,
        channel_id INTEGER,
        ping_role_id INTEGER,
        duration_seconds INTEGER NOT NULL DEFAULT 600,
        daily_enabled INTEGER NOT NULL DEFAULT 0,
        daily_hhmm_utc TEXT DEFAULT '20:00',
        daily_random_date_utc TEXT,
        webhook_url TEXT,
        webhook_name TEXT DEFAULT 'Drop The Track',
        webhook_avatar_url TEXT,
        allow_domains TEXT DEFAULT 'youtube.com,youtu.be,open.spotify.com,music.apple.com,soundcloud.com'
    )
    """
)

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS drop_track_rounds (
        round_id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        thread_id INTEGER NOT NULL,
        start_time INTEGER NOT NULL,
        end_time INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'running', -- running | ended | cancelled
        prompt_text TEXT,
        prompt_message_id INTEGER, -- webhook message id (best-effort)
        winners_message_id INTEGER, -- webhook message id (best-effort)
        winner_user_id INTEGER,
        winner_message_id INTEGER,
        winner_score INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    )
    """
)

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS drop_track_submissions (
        round_id INTEGER NOT NULL,
        guild_id INTEGER NOT NULL,
        thread_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        submitted_at INTEGER NOT NULL,
        url TEXT NOT NULL,
        PRIMARY KEY (round_id, message_id)
    )
    """
)

conn.commit()

# Lightweight migration for older DBs
try:
    cursor.execute(
        "ALTER TABLE drop_track_settings ADD COLUMN daily_random_date_utc TEXT"
    )
except sqlite3.OperationalError:
    pass


def unix_now() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp())


def utc_today_yyyymmdd() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def parse_hhmm(s: str) -> Optional[Tuple[int, int]]:
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    return hh, mm


def random_daily_hhmm_utc() -> str:
    minute_of_day = random.randint(8 * 60, 19 * 60)
    hh, mm = divmod(minute_of_day, 60)
    return f"{hh:02d}:{mm:02d}"


def humanize_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    mins, rem = divmod(seconds, 60)
    if mins < 60:
        return f"{mins} min" if rem == 0 else f"{mins} min {rem}s"
    hrs, mins = divmod(mins, 60)
    if hrs < 24:
        return f"{hrs}h {mins}m" if mins else f"{hrs}h"
    days, hrs = divmod(hrs, 24)
    return f"{days}d {hrs}h" if hrs else f"{days}d"


URL_RE = re.compile(r"(https?://[^\s<>()]+)", re.IGNORECASE)


def extract_first_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = URL_RE.search(text)
    if not m:
        return None
    return m.group(1).strip()


def domain_from_url(url: str) -> str:
    # Very lightweight parse (good enough for allowlist checks)
    try:
        u = url.lower()
        u = re.sub(r"^https?://", "", u)
        u = u.split("/", 1)[0]
        u = u.split("?", 1)[0]
        u = u.split("#", 1)[0]
        u = u.split(":", 1)[0]
        return u
    except Exception:
        return ""


def is_domain_allowed(url: str, allow_domains_csv: str) -> bool:
    d = domain_from_url(url)
    if not d:
        return False
    allowed = [
        x.strip().lower() for x in (allow_domains_csv or "").split(",") if x.strip()
    ]
    # Allow subdomains too
    return any(d == a or d.endswith("." + a) for a in allowed)


# ============================================================
# Drop The Track Cog
# ============================================================
class DropTheTrack(commands.Cog):
    """
    A “Drop The Track” style daily mini-game.

    - Bot creates a dated thread in a configured channel.
    - Webhook posts the prompt into the thread and later posts the winner announcement.
    - Users submit a single music link (configurable domains).
    - Voting via 🔥 reactions.
    - At end: compute top 🔥, announce winner, post closing message, lock the thread.

    All game-facing messages are sent via webhook. Bot only performs actions.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        colours = get_embed_colours()
        self.success_colour = colours["success"]
        self.info_colour = colours["info"]
        self.error_colour = colours["error"]

        # Defaults (overridable by config.yaml and DB)
        self.default_prompt = "🎵 **What’s stuck in your head?**"
        self.default_duration_seconds = 600  # 10 min
        self.placeholder_webhook_name = "Drop The Track"
        self.post_round_lock_delay_seconds = 3600  # 1 hour
        self.drop_message_variants = [
            "Time to spill your queue while it’s hot, clock’s ticking, {duration}",
            "Drop your current obsession before the timer runs dry — {duration}",
            "Queue confession booth is open for {duration}; make it count",
            "Hit send with your hottest track — only {duration} on the clock",
            "This is your sign to share that repeat-worthy tune ({duration})",
            "Your aux moment starts now: post a track within {duration}",
            "No gatekeeping — reveal the song looped in your brain in {duration}",
            "Quick fire round: drop one song before {duration} disappears",
            "The floor is yours for {duration}; show us what you’re playing",
            "Current mood in audio form, please. Deadline: {duration}",
            "Pass the aux and flex a track before the {duration} timer ends",
            "Music dump window is open for {duration}; go go go",
            "What track defines your vibe right now? You’ve got {duration}",
            "Post your banger of the moment while the {duration} countdown rolls",
            "Speedrun your best recommendation — submission window: {duration}",
            "One link, big energy. Drop it in the next {duration}",
            "We need your song of the day, and we need it in {duration}",
            "Thread’s live. Bring your top pick before {duration} is up",
            "Got a heater? Prove it. Timer says {duration}",
            "Now playing challenge: share one track in {duration}",
            "Tell us what’s in your headphones before {duration} expires",
            "Queue roulette starts now — submit your entry in {duration}",
            "Your soundtrack check-in starts now: {duration}",
            "Drop one song that deserves the spotlight. Time left: {duration}",
            "Playlist architects, assemble. Submission timer: {duration}",
            "The beat clock is running — share your pick within {duration}",
            "What song should everyone hear next? You get {duration}",
            "Today’s sonic flex round lasts {duration}; drop your link",
            "Incoming track battle: submit before {duration} is gone",
            "You’ve got {duration} to post the song you can’t stop replaying",
            "Let the music speak — one submission, {duration} max",
            "Need fresh tunes. Add your best one in {duration}",
            "Call your shot with one track before {duration} wraps",
            "Drop a tune that deserves 🔥 reactions in the next {duration}",
            "The queue is hungry — feed it within {duration}",
            "Share your latest earworm while the {duration} window is open",
            "This round is live for {duration}; bring your strongest link",
            "Turn your current vibe into a URL and post in {duration}",
            "Aux cord draft is open for {duration}; claim your spot",
            "Moment of truth: what’s your track pick? Timer: {duration}",
        ]
        self.default_allow_domains = (
            "youtube.com,youtu.be,open.spotify.com,music.apple.com,soundcloud.com"
        )

        # Optional config.yaml overrides
        self.config = {}
        try:
            self.config = load_config()
        except Exception as e:
            logging.warning(
                f"DropTheTrack: failed to load config.yaml, using defaults. {e}"
            )

        # Use a background loop to (a) start daily rounds and (b) end overdue rounds
        self._tick_loop.start()

    def cog_unload(self) -> None:
        try:
            self._tick_loop.cancel()
        except Exception:
            pass

    # --------------------------------------------------------
    # DB helpers
    # --------------------------------------------------------
    def _get_settings(self, guild_id: int) -> sqlite3.Row:
        cursor.execute(
            "SELECT * FROM drop_track_settings WHERE guild_id = ?", (guild_id,)
        )
        row = cursor.fetchone()
        if row:
            return row

        # Insert defaults
        cfg = (self.config.get("features", {}) or {}).get("drop_the_track", {}) or {}
        # Daily starts are randomized each UTC day (08:00-19:00), not configured.
        default_time = "08:00"
        default_dur = int(cfg.get("duration_seconds", self.default_duration_seconds))
        default_domains = str(cfg.get("allow_domains", self.default_allow_domains))
        default_webhook_url = str(cfg.get("webhook_url", "")).strip() or None
        default_channel_id = int(cfg["channel_id"]) if cfg.get("channel_id") else None
        default_ping_role_id = (
            int(cfg["ping_role_id"]) if cfg.get("ping_role_id") else None
        )
        daily_enabled = 1 if bool(cfg.get("daily_enabled", False)) else 0

        cursor.execute(
            """
            INSERT INTO drop_track_settings
                (guild_id, channel_id, ping_role_id, duration_seconds, daily_enabled, daily_hhmm_utc,
                 daily_random_date_utc, webhook_url, webhook_name, webhook_avatar_url, allow_domains)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                default_channel_id,
                default_ping_role_id,
                max(30, default_dur),
                daily_enabled,
                default_time,
                None,
                default_webhook_url,
                None,
                None,
                default_domains,
            ),
        )
        conn.commit()
        cursor.execute(
            "SELECT * FROM drop_track_settings WHERE guild_id = ?", (guild_id,)
        )
        return cursor.fetchone()

    def _update_settings(self, guild_id: int, **kwargs) -> None:
        keys = []
        vals = []
        for k, v in kwargs.items():
            keys.append(f"{k} = ?")
            vals.append(v)
        if not keys:
            return
        vals.append(guild_id)
        cursor.execute(
            f"UPDATE drop_track_settings SET {', '.join(keys)} WHERE guild_id = ?",
            tuple(vals),
        )
        conn.commit()

    def _get_running_round(self, guild_id: int) -> Optional[sqlite3.Row]:
        now = unix_now()
        cursor.execute(
            """
            SELECT * FROM drop_track_rounds
            WHERE guild_id = ? AND status = 'running'
            ORDER BY end_time ASC
            LIMIT 1
            """,
            (guild_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        # If it's somehow stale, still return it and let end logic handle
        if row["end_time"] <= now:
            return row
        return row

    def _round_already_started_today(self, guild_id: int) -> bool:
        # Prevent duplicate daily start: check any round created today (UTC)
        today = utc_today_yyyymmdd()
        cursor.execute(
            """
            SELECT 1 FROM drop_track_rounds
            WHERE guild_id = ? AND DATE(datetime(created_at, 'unixepoch')) = DATE(?)
            LIMIT 1
            """,
            (guild_id, today),
        )
        return cursor.fetchone() is not None

    def _fetch_round(self, round_id: int) -> Optional[sqlite3.Row]:
        cursor.execute(
            "SELECT * FROM drop_track_rounds WHERE round_id = ?", (round_id,)
        )
        return cursor.fetchone()

    def _get_today_daily_hhmm(self, settings: sqlite3.Row) -> str:
        today = utc_today_yyyymmdd()
        scheduled = str(settings["daily_hhmm_utc"] or "").strip()
        schedule_day = str(settings["daily_random_date_utc"] or "").strip()

        if schedule_day != today or not parse_hhmm(scheduled):
            scheduled = random_daily_hhmm_utc()
            self._update_settings(
                int(settings["guild_id"]),
                daily_hhmm_utc=scheduled,
                daily_random_date_utc=today,
            )

        return scheduled

    def _store_submission(
        self, round_row: sqlite3.Row, message: discord.Message, url: str
    ) -> None:
        cursor.execute(
            """
            INSERT OR IGNORE INTO drop_track_submissions
                (round_id, guild_id, thread_id, message_id, user_id, submitted_at, url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(round_row["round_id"]),
                int(round_row["guild_id"]),
                int(round_row["thread_id"]),
                int(message.id),
                int(message.author.id),
                unix_now(),
                url,
            ),
        )
        conn.commit()

    def _has_user_submitted(self, round_id: int, user_id: int) -> bool:
        cursor.execute(
            "SELECT 1 FROM drop_track_submissions WHERE round_id = ? AND user_id = ? LIMIT 1",
            (round_id, user_id),
        )
        return cursor.fetchone() is not None

    def _get_submissions(self, round_id: int) -> List[sqlite3.Row]:
        cursor.execute(
            "SELECT * FROM drop_track_submissions WHERE round_id = ? ORDER BY submitted_at ASC",
            (round_id,),
        )
        return cursor.fetchall()

    # --------------------------------------------------------
    # Webhook helpers
    # --------------------------------------------------------
    def _resolve_webhook_url(self, guild_id: int) -> str:
        settings = self._get_settings(guild_id)
        db_url = str(settings["webhook_url"] or "").strip()
        if db_url:
            return db_url

        cfg = (self.config.get("features", {}) or {}).get("drop_the_track", {}) or {}
        cfg_url = str(cfg.get("webhook_url", "")).strip()
        if cfg_url:
            self._update_settings(guild_id, webhook_url=cfg_url)
            return cfg_url

        return ""

    async def _webhook_send(
        self,
        webhook_url: str,
        *,
        content: Optional[str] = None,
        embed: Optional[discord.Embed] = None,
        thread: Optional[discord.Thread] = None,
        allowed_mentions: Optional[discord.AllowedMentions] = None,
    ) -> Optional[discord.WebhookMessage]:
        """
        Sends a message via webhook URL. Supports posting into a thread.
        Returns the webhook message if wait=True succeeds, else None.
        """
        if not webhook_url:
            return None

        async with aiohttp.ClientSession() as session:
            wh = discord.Webhook.from_url(webhook_url, session=session)
            try:
                send_kwargs = {
                    "content": content,
                    "embed": embed,
                    "allowed_mentions": allowed_mentions
                    or discord.AllowedMentions.none(),
                    "wait": True,
                }
                if thread is not None:
                    send_kwargs["thread"] = thread

                msg = await wh.send(**send_kwargs)
                return msg
            except Exception as e:
                logging.warning(f"DropTheTrack: webhook send failed: {e}")
                return None

    # --------------------------------------------------------
    # Game logic
    # --------------------------------------------------------
    async def _start_round(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        prompt_text: Optional[str],
        duration_seconds: int,
        ping_role_id: Optional[int],
    ) -> Optional[int]:
        """
        Creates the thread + posts prompt via webhook.
        Returns round_id on success.
        """
        webhook_url = self._resolve_webhook_url(guild.id)
        if not webhook_url:
            logging.error(
                "DropTheTrack: no webhook URL configured for guild %s. "
                "Set features.drop_the_track.webhook_url in config.yaml or via DB.",
                guild.id,
            )
            return None

        # Create a dated thread
        date_label = utc_today_yyyymmdd()
        thread_name = f"Drop • {date_label}"
        try:
            thread = await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,  # 24h
                reason="Drop The Track daily round",
            )
        except Exception as e:
            logging.warning(
                f"DropTheTrack: failed to create thread in #{channel.name}: {e}"
            )
            return None

        start_ts = unix_now()
        end_ts = start_ts + max(30, int(duration_seconds))
        prompt = (prompt_text or self.default_prompt).strip()
        drop_message = random.choice(self.drop_message_variants).format(
            duration=humanize_seconds(duration_seconds)
        )
        time_line = f"{prompt}\n\n{drop_message}"

        # Optional role ping goes in the thread prompt (webhook message)
        ping = f"<@&{int(ping_role_id)}>\n" if ping_role_id else ""

        # Send prompt via webhook into the thread
        prompt_msg = await self._webhook_send(
            webhook_url,
            content=f"{ping}{time_line}",
            thread=thread,
            allowed_mentions=discord.AllowedMentions(
                roles=True, users=False, everyone=False
            ),
        )

        # Persist round in DB
        cursor.execute(
            """
            INSERT INTO drop_track_rounds
                (guild_id, channel_id, thread_id, start_time, end_time, status, prompt_text,
                 prompt_message_id, created_at)
            VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)
            """,
            (
                guild.id,
                channel.id,
                thread.id,
                start_ts,
                end_ts,
                prompt,
                (int(prompt_msg.id) if prompt_msg else None),
                start_ts,
            ),
        )
        conn.commit()
        round_id = int(cursor.lastrowid)

        # Pin prompt if possible (pinning requires a Message object in-channel; webhook msg might not be fetchable as bot)
        # We skip pinning to keep this robust across perms and webhook fetch differences.

        return round_id

    async def _end_round(self, round_row: sqlite3.Row) -> None:
        """
        Computes winner and posts results via webhook, then locks thread.
        """
        if round_row["status"] != "running":
            return

        guild = self.bot.get_guild(int(round_row["guild_id"]))
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(int(round_row["guild_id"]))
            except Exception:
                return

        channel_id = int(round_row["channel_id"])
        thread_id = int(round_row["thread_id"])

        # Fetch channel + thread
        try:
            channel = guild.get_channel(channel_id) or await guild.fetch_channel(
                channel_id
            )
            if not isinstance(channel, discord.TextChannel):
                return
        except Exception:
            return

        try:
            thread = guild.get_thread(thread_id)
            if thread is None:
                fetched = await guild.fetch_channel(thread_id)
                thread = fetched if isinstance(fetched, discord.Thread) else None
            if thread is None:
                return
        except Exception:
            return

        settings = self._get_settings(guild.id)
        webhook_url = self._resolve_webhook_url(guild.id)
        allow_domains = settings["allow_domains"] or self.default_allow_domains
        if not webhook_url:
            logging.error(
                "DropTheTrack: cannot end round %s in guild %s because webhook_url is unset.",
                int(round_row["round_id"]),
                guild.id,
            )
            return

        # Determine submissions
        subs = self._get_submissions(int(round_row["round_id"]))

        # Score by 🔥 reactions on each submission message
        best: Dict[str, int] = {
            "user_id": 0,
            "message_id": 0,
            "score": -1,
        }
        best_url: Optional[str] = None

        # We fetch each message to get latest reactions
        for s in subs:
            mid = int(s["message_id"])
            try:
                msg = await thread.fetch_message(mid)
            except Exception:
                continue

            # Ensure the url is still allowed (in case you later change allowlist)
            url = str(s["url"])
            if not is_domain_allowed(url, str(allow_domains)):
                continue

            score = 0
            for r in msg.reactions:
                try:
                    if str(r.emoji) == "🔥":
                        score = int(r.count)
                        break
                except Exception:
                    continue

            if score > best["score"]:
                best = {"user_id": int(s["user_id"]), "message_id": mid, "score": score}
                best_url = url

        # Mark ended in DB
        cursor.execute(
            """
            UPDATE drop_track_rounds
            SET status = 'ended',
                winner_user_id = ?,
                winner_message_id = ?,
                winner_score = ?
            WHERE round_id = ?
            """,
            (
                (best["user_id"] if best["score"] >= 0 else None),
                (best["message_id"] if best["score"] >= 0 else None),
                (best["score"] if best["score"] >= 0 else 0),
                int(round_row["round_id"]),
            ),
        )
        conn.commit()

        # Winner announcement (in the parent channel) via webhook
        winners_message_id: Optional[int] = None
        if best["score"] >= 0 and best["user_id"] and best_url:
            content = f"🔥 **Top Track Drop** by <@{best['user_id']}> with **{best['score']}** 🔥\n{best_url}"
        else:
            content = "No valid submissions this round. Try again tomorrow 🎵"

        ann = await self._webhook_send(
            webhook_url or "",
            content=content,
            thread=None,
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=False, everyone=False
            ),
        )
        if ann:
            winners_message_id = int(ann.id)

        # Closing message inside the thread via webhook
        closing = await self._webhook_send(
            webhook_url or "",
            content="Thanks for dropping! See you tomorrow 🎵",
            thread=thread,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        # Store winners message id best-effort
        cursor.execute(
            "UPDATE drop_track_rounds SET winners_message_id = ? WHERE round_id = ?",
            (winners_message_id, int(round_row["round_id"])),
        )
        conn.commit()

        # Lock + archive the thread after a 1-hour grace period.
        asyncio.create_task(self._lock_and_archive_thread_later(thread))

    async def _lock_and_archive_thread_later(self, thread: discord.Thread) -> None:
        await asyncio.sleep(self.post_round_lock_delay_seconds)
        try:
            await thread.edit(
                locked=True,
                archived=True,
                reason="Drop The Track round ended (1 hour grace elapsed)",
            )
        except Exception:
            try:
                await thread.edit(
                    locked=True,
                    reason="Drop The Track round ended (1 hour grace elapsed)",
                )
            except Exception as e:
                logging.warning(
                    "DropTheTrack: failed to lock/archive thread %s after grace period: %s",
                    getattr(thread, "id", "unknown"),
                    e,
                )

    # --------------------------------------------------------
    # Message listener (collect submissions)
    # --------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            return
        if not isinstance(message.channel, discord.Thread):
            return

        # Find the running round for this thread
        thread_id = int(message.channel.id)
        guild_id = int(message.guild.id)

        cursor.execute(
            """
            SELECT * FROM drop_track_rounds
            WHERE guild_id = ? AND thread_id = ? AND status = 'running'
            ORDER BY end_time DESC
            LIMIT 1
            """,
            (guild_id, thread_id),
        )
        round_row = cursor.fetchone()
        if not round_row:
            return

        # Ignore anything after end time (extra safety)
        if int(round_row["end_time"]) <= unix_now():
            return

        settings = self._get_settings(guild_id)
        allow_domains = settings["allow_domains"] or self.default_allow_domains

        url = extract_first_url(message.content or "")
        if not url:
            return
        if not is_domain_allowed(url, str(allow_domains)):
            return

        # One submission per user per round (simple and matches the typical “drop” style)
        if self._has_user_submitted(int(round_row["round_id"]), int(message.author.id)):
            return

        # Store submission and add 🔥 reaction to standardise voting
        try:
            self._store_submission(round_row, message, url)
        except Exception:
            return

        try:
            await message.add_reaction("🔥")
        except Exception:
            pass

    # --------------------------------------------------------
    # Background tick loop
    # --------------------------------------------------------
    @tasks.loop(seconds=20)
    async def _tick_loop(self) -> None:
        # End overdue rounds
        now = unix_now()
        cursor.execute(
            "SELECT * FROM drop_track_rounds WHERE status = 'running' AND end_time <= ? ORDER BY end_time ASC",
            (now,),
        )
        overdue = cursor.fetchall()
        for row in overdue:
            try:
                await self._end_round(row)
            except Exception as e:
                logging.warning(
                    f"DropTheTrack: failed ending round {row['round_id']}: {e}"
                )

        # Daily start checks
        # Only start if daily_enabled and channel configured, and no round started today.
        # Start condition: current UTC time matches HH:MM exactly (within the tick interval).
        # We also prevent duplicate triggers via _round_already_started_today.
        utc = datetime.datetime.now(datetime.timezone.utc)
        hhmm_now = f"{utc.hour:02d}:{utc.minute:02d}"

        # Fetch all guild settings where daily enabled
        cursor.execute(
            "SELECT * FROM drop_track_settings WHERE daily_enabled = 1 AND channel_id IS NOT NULL"
        )
        rows = cursor.fetchall()
        for s in rows:
            try:
                guild_id = int(s["guild_id"])
                scheduled = self._get_today_daily_hhmm(s)
                if scheduled != hhmm_now:
                    continue

                # If there is already a running round, skip
                if self._get_running_round(guild_id):
                    continue

                # If already started today (UTC), skip
                if self._round_already_started_today(guild_id):
                    continue

                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    continue

                chan_id = int(s["channel_id"])
                channel = guild.get_channel(chan_id)
                if channel is None:
                    try:
                        fetched = await guild.fetch_channel(chan_id)
                        channel = (
                            fetched
                            if isinstance(fetched, discord.TextChannel)
                            else None
                        )
                    except Exception:
                        channel = None
                if channel is None:
                    continue

                dur = int(s["duration_seconds"] or self.default_duration_seconds)
                ping_role_id = int(s["ping_role_id"]) if s["ping_role_id"] else None

                # Start round (prompt text can be overridden per config.yaml only for now)
                cfg = (self.config.get("features", {}) or {}).get(
                    "drop_the_track", {}
                ) or {}
                prompt_text = str(cfg.get("prompt_text", self.default_prompt))

                await self._start_round(
                    guild=guild,
                    channel=channel,
                    prompt_text=prompt_text,
                    duration_seconds=dur,
                    ping_role_id=ping_role_id,
                )
            except Exception as e:
                logging.warning(
                    f"DropTheTrack: daily start failed for guild {s['guild_id']}: {e}"
                )

    @_tick_loop.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()

    # --------------------------------------------------------
    # Slash commands
    # --------------------------------------------------------
    def _is_manager(self, member: discord.Member) -> bool:
        # Match your giveaways approach: admin OR manage_guild
        return bool(
            member.guild_permissions.administrator
            or member.guild_permissions.manage_guild
        )

    def _embed(
        self, title: str, description: str, colour: discord.Color
    ) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=colour)

    @app_commands.command(
        name="drop_config", description="Configure Drop The Track for this server."
    )
    @app_commands.describe(
        channel="Channel to host the daily game (thread created here).",
        ping_role="Role to ping at the start of each round (optional).",
        duration_minutes="How long each round runs (default 10).",
        daily_enabled="Enable daily auto-start (randomized daily between 08:00 and 19:00 UTC).",
        allow_domains_csv="Comma-separated allowlist domains for submissions.",
    )
    async def drop_config(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        ping_role: Optional[discord.Role] = None,
        duration_minutes: Optional[int] = None,
        daily_enabled: Optional[bool] = None,
        allow_domains_csv: Optional[str] = None,
    ):
        actor = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(actor, discord.Member):
            await interaction.response.send_message(
                embed=self._embed(
                    "Server only", "Use this in a server.", self.error_colour
                ),
                ephemeral=True,
            )
            return
        if not self._is_manager(actor):
            await interaction.response.send_message(
                embed=self._embed(
                    "No permission",
                    "You need Manage Server or Administrator.",
                    self.error_colour,
                ),
                ephemeral=True,
            )
            return

        self._get_settings(guild.id)  # ensure row exists

        updates = {}

        if channel is not None:
            updates["channel_id"] = int(channel.id)

        if ping_role is not None:
            updates["ping_role_id"] = int(ping_role.id)

        if duration_minutes is not None:
            dur = max(1, int(duration_minutes)) * 60
            updates["duration_seconds"] = max(30, dur)

        if daily_enabled is not None:
            updates["daily_enabled"] = 1 if daily_enabled else 0

        if allow_domains_csv is not None:
            updates["allow_domains"] = (
                str(allow_domains_csv).strip()
                if allow_domains_csv.strip()
                else self.default_allow_domains
            )

        if updates:
            self._update_settings(guild.id, **updates)

        s = self._get_settings(guild.id)
        self._get_today_daily_hhmm(s)
        s = self._get_settings(guild.id)
        desc = (
            f"**Channel:** {('<#' + str(s['channel_id']) + '>') if s['channel_id'] else 'Not set'}\n"
            f"**Ping role:** {('<@&' + str(s['ping_role_id']) + '>') if s['ping_role_id'] else 'None'}\n"
            f"**Duration:** {humanize_seconds(int(s['duration_seconds']))}\n"
            f"**Daily:** {'Enabled' if int(s['daily_enabled']) == 1 else 'Disabled'}\n"
            f"**Today's random UTC start:** {s['daily_hhmm_utc']}\n"
            f"**Webhook set:** {'Yes' if s['webhook_url'] else 'No'}\n"
            f"**Allow domains:** {s['allow_domains']}\n"
        )

        await interaction.response.send_message(
            embed=self._embed("Drop The Track configured", desc, self.success_colour),
            ephemeral=True,
        )

    @app_commands.command(
        name="drop_start", description="Start a Drop The Track round now."
    )
    @app_commands.describe(
        channel="Channel to host the round (thread created here). Defaults to configured channel.",
        duration_minutes="Round duration in minutes. Defaults to configured duration.",
        ping_role="Role to ping for this round only (optional).",
        prompt="Prompt text override (optional).",
    )
    async def drop_start(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        duration_minutes: Optional[int] = None,
        ping_role: Optional[discord.Role] = None,
        prompt: Optional[str] = None,
    ):
        actor = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(actor, discord.Member):
            await interaction.response.send_message(
                embed=self._embed(
                    "Server only", "Use this in a server.", self.error_colour
                ),
                ephemeral=True,
            )
            return
        if not self._is_manager(actor):
            await interaction.response.send_message(
                embed=self._embed(
                    "No permission",
                    "You need Manage Server or Administrator.",
                    self.error_colour,
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        settings = self._get_settings(guild.id)

        if self._get_running_round(guild.id):
            await interaction.followup.send(
                embed=self._embed(
                    "Already running",
                    "There is already a running round.",
                    self.error_colour,
                ),
                ephemeral=True,
            )
            return

        target_channel = channel
        if target_channel is None and settings["channel_id"]:
            try:
                fetched = guild.get_channel(
                    int(settings["channel_id"])
                ) or await guild.fetch_channel(int(settings["channel_id"]))
                target_channel = (
                    fetched if isinstance(fetched, discord.TextChannel) else None
                )
            except Exception:
                target_channel = None

        if target_channel is None:
            await interaction.followup.send(
                embed=self._embed(
                    "Channel required",
                    "Set a channel with /drop_config or pass one to /drop_start.",
                    self.error_colour,
                ),
                ephemeral=True,
            )
            return

        dur = int(settings["duration_seconds"] or self.default_duration_seconds)
        if duration_minutes is not None:
            dur = max(30, int(duration_minutes) * 60)

        pr = (
            int(ping_role.id)
            if ping_role
            else (int(settings["ping_role_id"]) if settings["ping_role_id"] else None)
        )
        prompt_text = (
            prompt
            or (
                (self.config.get("features", {}) or {}).get("drop_the_track", {}) or {}
            ).get("prompt_text")
            or self.default_prompt
        )

        rid = await self._start_round(
            guild=guild,
            channel=target_channel,
            prompt_text=str(prompt_text),
            duration_seconds=dur,
            ping_role_id=pr,
        )

        if not rid:
            await interaction.followup.send(
                embed=self._embed(
                    "Failed",
                    "Could not start the round. Check I can create threads and send messages using the configured Drop The Track webhook URL.",
                    self.error_colour,
                ),
                ephemeral=True,
            )
            return

        row = self._fetch_round(rid)
        thread_id = int(row["thread_id"]) if row else 0
        await interaction.followup.send(
            embed=self._embed(
                "Round started",
                f"Started a new round in {target_channel.mention} (thread <#{thread_id}>).",
                self.success_colour,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="drop_end", description="End the current Drop The Track round now."
    )
    async def drop_end(self, interaction: discord.Interaction):
        actor = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(actor, discord.Member):
            await interaction.response.send_message(
                embed=self._embed(
                    "Server only", "Use this in a server.", self.error_colour
                ),
                ephemeral=True,
            )
            return
        if not self._is_manager(actor):
            await interaction.response.send_message(
                embed=self._embed(
                    "No permission",
                    "You need Manage Server or Administrator.",
                    self.error_colour,
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        row = self._get_running_round(guild.id)
        if not row:
            await interaction.followup.send(
                embed=self._embed(
                    "No round", "There is no running round.", self.error_colour
                ),
                ephemeral=True,
            )
            return

        # Force end
        cursor.execute(
            "UPDATE drop_track_rounds SET end_time = ? WHERE round_id = ?",
            (unix_now(), int(row["round_id"])),
        )
        conn.commit()
        row = self._fetch_round(int(row["round_id"]))
        await self._end_round(row)

        await interaction.followup.send(
            embed=self._embed(
                "Ended", "Ended the round and announced results.", self.info_colour
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="drop_status",
        description="Show Drop The Track configuration and current round status.",
    )
    async def drop_status(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=self._embed(
                    "Server only", "Use this in a server.", self.error_colour
                ),
                ephemeral=True,
            )
            return

        s = self._get_settings(guild.id)
        self._get_today_daily_hhmm(s)
        s = self._get_settings(guild.id)
        running = self._get_running_round(guild.id)

        lines = [
            f"**Channel:** {('<#' + str(s['channel_id']) + '>') if s['channel_id'] else 'Not set'}",
            f"**Duration:** {humanize_seconds(int(s['duration_seconds']))}",
            f"**Daily:** {'Enabled' if int(s['daily_enabled']) == 1 else 'Disabled'}",
            f"**Today's random UTC start:** {s['daily_hhmm_utc']}",
            f"**Webhook set:** {'Yes' if s['webhook_url'] else 'No'}",
            f"**Allow domains:** {s['allow_domains']}",
        ]

        if running:
            ends = int(running["end_time"])
            lines.append("")
            lines.append(
                f"**Running round:** `{running['round_id']}` in thread <#{running['thread_id']}>"
            )
            lines.append(f"**Ends:** <t:{ends}:R>")

        await interaction.response.send_message(
            embed=self._embed(
                "Drop The Track status", "\n".join(lines), self.info_colour
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DropTheTrack(bot))
