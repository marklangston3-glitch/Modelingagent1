#!/usr/bin/env python3
"""
jarvis_bot.py — Jarvis, The Soup Kitchen's bot for Langston's Financial Intelligence.

Environment variables:
    DISCORD_BOT_TOKEN   — Discord bot token (required)
    ANTHROPIC_API_KEY   — Anthropic API key (required)
    DISCORD_GUILD_ID    — Guild ID for instant slash-command sync (optional but recommended)
    COFOUNDER_IDS       — Comma-separated Discord user IDs who can trigger polls by @mention
                          (e.g. "123456789,987654321"). If unset, falls back to role names + guild owner.

Roles that can create polls (slash command AND @mention):
    Admin, Moderator, Co-Founder, Founder, Owner, Mod

All decisions and errors are logged to jarvis_bot.log.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import traceback
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

import anthropic
import discord
from discord import app_commands
from discord.ext import tasks

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    filename="jarvis_bot.log",
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("jarvis")

# Also log to stdout so Railway surfaces it in its dashboard
_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
log.addHandler(_console)

# ── Configuration ─────────────────────────────────────────────────────────────

DISCORD_TOKEN     = os.environ.get("DISCORD_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GUILD_ID_STR      = os.environ.get("DISCORD_GUILD_ID", "")

CALENDAR_CHANNEL_NAME = "jarvis-calendar"
CALENDAR_CATEGORY     = "FREE ANALYSIS"
CALENDAR_FEED_URL     = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Comma-separated Discord user IDs of founders who can trigger polls by @mention.
# Set COFOUNDER_IDS=123456789,987654321 in Railway env vars.
# If empty, falls back to guild owner + role name matching.
_COFOUNDER_ID_SET: set[int] = {
    int(x.strip())
    for x in os.environ.get("COFOUNDER_IDS", "").split(",")
    if x.strip().isdigit()
}

# Role names (lowercase) whose holders can create polls via @mention or /poll.
# Intentionally broad so different servers' naming conventions all work.
_POLL_AUTHOR_ROLES: set[str] = {
    "co-founder", "co founder", "cofounder",
    "founder", "owner",
    "admin", "administrator",
    "moderator", "mod",
}

CALENDAR_RE = re.compile(
    r"\b(calendar|earnings|red\s+folder|news\s+this\s+week|fomc|cpi|nfp|"
    r"jobs\s+report|fed\s+meeting|economic\s+data|macro\s+data|economic\s+events)\b",
    re.IGNORECASE,
)

ET      = ZoneInfo("America/New_York")
AI_MODEL = "claude-opus-4-7"

REACTION_NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

SYSTEM_PROMPT = """\
You are Jarvis, The Soup Kitchen's bot for Langston's Financial Intelligence Discord.
You help members understand markets, macro economics, and trading ideas.
Write like a smart friend who works on Wall Street — clear, direct, no jargon unless
you explain it in the same breath. Keep responses concise (under 400 words unless the
question truly needs more). End every response with 🍜.

You ARE Jarvis, The Soup Kitchen's only bot, and you HAVE these real capabilities:
- Creating native Discord polls (founders ask you directly; members can suggest one to founders)
- /pnl logging and the Friday leaderboard + Top Trader crown
- /watchlist, /trade alerts, /indicator guide
- /calendar with live red-folder economic data (ForexFactory feed, real current-week events)
- Live TradingView signal alerts posted into #markys-alerts
- Welcome DMs to new members
- Weekly red-folder rundown every Sunday 7:30 PM ET in #jarvis-calendar

NEVER claim you lack a capability on this list. NEVER recommend other bots like Carl-bot,
Poll Bot, or any third-party bot — you are The Soup Kitchen's only bot and you handle all
of this natively. If unsure whether you can do something NOT on this list, say you'll flag
it to the team instead of denying it."""

# ── Discord client ─────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.guilds          = True
intents.members         = True

client = discord.Client(intents=intents)
tree   = app_commands.CommandTree(client)
ai     = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_calendar_posted_date: datetime.date | None = None


# ── Auth helpers ───────────────────────────────────────────────────────────────

def is_poll_author(member: discord.Member) -> bool:
    """
    True if this member is allowed to trigger conversational poll creation.
    Priority order:
      1. COFOUNDER_IDS env var (explicit user IDs)
      2. Guild owner (always a founder)
      3. Any role whose lowercase name is in _POLL_AUTHOR_ROLES
    """
    # 1 — Explicit IDs
    if _COFOUNDER_ID_SET:
        if member.id in _COFOUNDER_ID_SET:
            return True
        # IDs are set but this user isn't in the list — deny
        return False

    # 2 — Guild owner
    if hasattr(member, "guild") and member.guild and member.id == member.guild.owner_id:
        return True

    # 3 — Role names
    member_roles_lower = {r.name.lower() for r in member.roles}
    return bool(member_roles_lower & _POLL_AUTHOR_ROLES)


def has_poll_permission(member: discord.Member) -> bool:
    """True if member can use the /poll slash command (same set as is_poll_author)."""
    return is_poll_author(member)


def is_poll_intent(text: str) -> bool:
    """True if the message is asking Jarvis to create a poll."""
    t = text.lower()
    intent_words = ("make", "create", "post", "run", "start", "put up", "launch")
    return "poll" in t and any(w in t for w in intent_words)


# ── ForexFactory calendar ──────────────────────────────────────────────────────

def fetch_ff_calendar() -> tuple[bool, list[dict]]:
    """
    Fetch this week's high-impact USD events from ForexFactory.
    Returns (feed_ok, events_list). feed_ok=False on any network/parse error.
    """
    try:
        req = urllib.request.Request(
            CALENDAR_FEED_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JarvisBot/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        high_usd = [
            e for e in data
            if e.get("country", "").upper() == "USD"
            and e.get("impact", "").lower() == "high"
        ]
        high_usd.sort(key=lambda e: e.get("date", ""))
        return True, high_usd
    except Exception:
        log.exception("ForexFactory feed fetch failed")
        return False, []


def _fmt_event_line(e: dict) -> str:
    try:
        dt       = datetime.datetime.fromisoformat(e["date"]).astimezone(ET)
        day_str  = dt.strftime("%A")
        time_str = dt.strftime("%-I:%M %p ET")
    except Exception:
        day_str, time_str = "TBD", ""

    title    = e.get("title", "Unknown")
    forecast = e.get("forecast", "")
    previous = e.get("previous", "")
    details  = []
    if forecast:
        details.append(f"Forecast: {forecast}")
    if previous:
        details.append(f"Prev: {previous}")
    suffix = f"  ({', '.join(details)})" if details else ""

    if time_str:
        return f"🔴 **{day_str}** {time_str} — {title}{suffix}"
    return f"🔴 **{day_str}** — {title}{suffix}"


def format_calendar_embed(feed_ok: bool, events: list[dict]) -> discord.Embed:
    now_et   = datetime.datetime.now(ET)
    week_mon = now_et - datetime.timedelta(days=now_et.weekday())
    week_of  = week_mon.strftime("%B %d, %Y")

    embed = discord.Embed(
        title=f"📅 THIS WEEK'S RED FOLDER — Week of {week_of}",
        color=discord.Color.red(),
    )

    if not feed_ok:
        embed.description = (
            "Calendar feed is down — check ForexFactory directly. 🍜\n"
            "<https://www.forexfactory.com/calendar>"
        )
        return embed

    if not events:
        embed.description = (
            "No high-impact USD events scheduled this week — it's a quiet one. 🍜"
        )
        return embed

    lines = ["━━━━━━━━━━━━━━━━━━━"]
    for e in events:
        lines.append(_fmt_event_line(e))
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("Trade around these or don't trade them at all. 🍜")
    embed.description = "\n".join(lines)
    return embed


def calendar_context_str(events: list[dict]) -> str:
    now_et   = datetime.datetime.now(ET)
    week_mon = now_et - datetime.timedelta(days=now_et.weekday())
    week_of  = week_mon.strftime("%B %d, %Y")

    if not events:
        return (
            f"THIS WEEK'S HIGH-IMPACT USD ECONOMIC EVENTS (week of {week_of}):\n"
            "None scheduled — quiet macro week."
        )

    lines = [f"THIS WEEK'S HIGH-IMPACT USD ECONOMIC EVENTS (week of {week_of}):"]
    for e in events:
        try:
            dt       = datetime.datetime.fromisoformat(e["date"]).astimezone(ET)
            day_str  = dt.strftime("%A")
            time_str = dt.strftime("%-I:%M %p ET")
        except Exception:
            day_str, time_str = "TBD", ""

        title    = e.get("title", "Unknown")
        forecast = e.get("forecast", "")
        previous = e.get("previous", "")
        parts    = [f"- {day_str} {time_str} — {title}"]
        if forecast:
            parts.append(f"Forecast: {forecast}")
        if previous:
            parts.append(f"Previous: {previous}")
        lines.append("  ".join(parts))

    return "\n".join(lines)


# ── Poll: Claude extraction ────────────────────────────────────────────────────

def _extract_poll_data(raw_request: str) -> dict:
    """
    Call Claude to extract a poll question + 2-4 options from the founder's raw request.
    Returns {"question": str, "options": [str, ...]}. Raises on parse failure.
    """
    response = ai.messages.create(
        model=AI_MODEL,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                "Extract a poll question and 2-4 short answer options from this request. "
                "Return ONLY valid JSON with no markdown fences and no other text:\n"
                '{"question":"...","options":["...","..."]}\n\n'
                f"Request: {raw_request}"
            ),
        }],
    )
    raw = response.content[0].text.strip()
    # Strip any accidental code fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    data = json.loads(raw)
    if "question" not in data or "options" not in data:
        raise ValueError(f"Claude JSON missing required keys: {list(data.keys())}")
    return data


# ── Poll: create native Discord Poll (or log + fall back, never silently drop) ─

async def create_poll_from_request(message: discord.Message, raw_request: str) -> None:
    """
    Given a founder's @mention poll request, call Claude to extract question + options,
    then post a real native Discord Poll.

    Failure policy (NEVER silently fall back to plain text):
      - Claude extraction fails  → log traceback, reply with error + traceback snippet
      - discord.Poll send fails  → log traceback, attempt reaction-embed fallback
      - Reaction embed also fails → log traceback, reply with full error
    """
    log.info("create_poll_from_request: author=%s(%s) request=%r",
             message.author, message.author.id, raw_request)

    # ── Step 1: Extract question + options via Claude ──────────────────────────
    try:
        poll_data = await asyncio.to_thread(_extract_poll_data, raw_request)
        question  = poll_data["question"].strip()
        options   = [str(o).strip() for o in poll_data.get("options", [])][:4]
        if len(options) < 2:
            raise ValueError(
                f"Claude returned only {len(options)} option(s) — need at least 2. "
                f"Full response: {poll_data}"
            )
        log.info("Poll extracted — question=%r  options=%r", question, options)
    except Exception:
        tb = traceback.format_exc()
        log.error("Poll extraction failed:\n%s", tb)
        snippet = tb[-500:].replace("```", "'''")
        await message.reply(
            "⚠️ Couldn't parse a poll from that request — Claude didn't return valid JSON.\n"
            f"Use `/poll question:\"...\" options:\"A, B, C\"` as a fallback. 🍜\n"
            f"```\n{snippet}\n```"
        )
        return

    # ── Step 2: Try native discord.Poll ───────────────────────────────────────
    try:
        poll = discord.Poll(
            question=question,
            duration=datetime.timedelta(hours=24),
            multiple=False,
        )
        for opt in options:
            poll.add_answer(text=opt)
        await message.channel.send(poll=poll)
        await message.reply("Poll's live 🍜")
        log.info("Native discord.Poll posted successfully — %r", question)
        return
    except Exception:
        tb = traceback.format_exc()
        log.error("Native discord.Poll failed — falling back to reaction embed:\n%s", tb)
        # Continue to reaction fallback; do NOT return here

    # ── Step 3: Reaction-embed fallback (logged, not silent) ──────────────────
    log.warning("Using reaction-embed fallback for poll: %r", question)
    try:
        lines = [f"{REACTION_NUMBERS[i]}  {opt}" for i, opt in enumerate(options)]
        embed = discord.Embed(
            title=f"📊 {question}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(
            text="Poll · 24h · react to vote  (native poll unavailable — check bot permissions)"
        )
        msg = await message.channel.send(embed=embed)
        for i in range(len(options)):
            await msg.add_reaction(REACTION_NUMBERS[i])
        await message.reply("Poll's live (reaction mode) 🍜")
        log.info("Reaction-embed poll posted — %r", question)
    except Exception:
        tb = traceback.format_exc()
        log.error("Reaction-embed fallback also failed:\n%s", tb)
        snippet = tb[-500:].replace("```", "'''")
        await message.reply(
            f"⚠️ Poll creation failed completely. Use `/poll` instead. 🍜\n"
            f"```\n{snippet}\n```"
        )


# ── /poll slash command ────────────────────────────────────────────────────────

@tree.command(name="poll", description="Post a poll to this channel (Admin / Mod / Founder only)")
@app_commands.describe(
    question="The poll question",
    options="Comma-separated options, 2–10 (e.g. Bullish 🟢, Bearish 🔴, Neutral ⚪)",
    duration="Duration in hours (default 24, max 168)",
)
async def poll_command(
    interaction: discord.Interaction,
    question: str,
    options: str,
    duration: int = 24,
) -> None:
    if not has_poll_permission(interaction.user):
        await interaction.response.send_message(
            "❌ Only Admins, Moderators, and Founders can create polls.", ephemeral=True
        )
        return

    parsed = [o.strip() for o in options.split(",") if o.strip()]
    if len(parsed) < 2:
        await interaction.response.send_message(
            "❌ Provide at least 2 comma-separated options.", ephemeral=True
        )
        return
    if len(parsed) > 10:
        await interaction.response.send_message(
            "❌ Maximum 10 options allowed.", ephemeral=True
        )
        return
    if not (1 <= duration <= 168):
        await interaction.response.send_message(
            "❌ Duration must be 1–168 hours.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        poll = discord.Poll(
            question=question,
            duration=datetime.timedelta(hours=duration),
            multiple=False,
        )
        for opt in parsed:
            poll.add_answer(text=opt)
        await interaction.channel.send(poll=poll)
        await interaction.followup.send("✅ Poll posted!", ephemeral=True)
        log.info("/poll command: %r by %s", question, interaction.user)
    except Exception:
        tb = traceback.format_exc()
        log.error("/poll command failed:\n%s", tb)
        # Reaction embed fallback for slash command too
        lines = [f"{REACTION_NUMBERS[i]}  {opt}" for i, opt in enumerate(parsed[:10])]
        embed = discord.Embed(
            title=f"📊 {question}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Poll · {duration}h · react to vote")
        msg = await interaction.channel.send(embed=embed)
        for i in range(len(parsed)):
            await msg.add_reaction(REACTION_NUMBERS[i])
        await interaction.followup.send("✅ Poll posted (reaction mode)!", ephemeral=True)


# ── /calendar slash command ────────────────────────────────────────────────────

@tree.command(name="calendar", description="Show this week's high-impact economic events (USD red folder)")
async def calendar_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    feed_ok, events = await asyncio.to_thread(fetch_ff_calendar)
    embed = format_calendar_embed(feed_ok, events)
    await interaction.followup.send(embed=embed)


# ── Sunday 7:30 PM ET calendar post ───────────────────────────────────────────

@tasks.loop(minutes=1)
async def calendar_weekly_post() -> None:
    global _calendar_posted_date

    now   = datetime.datetime.now(ET)
    today = now.date()

    if now.weekday() != 6:
        return
    if now.hour != 19 or now.minute != 30:
        return
    if _calendar_posted_date == today:
        return

    _calendar_posted_date = today
    log.info("Sunday calendar post firing")
    feed_ok, events = await asyncio.to_thread(fetch_ff_calendar)
    embed           = format_calendar_embed(feed_ok, events)
    embed.title     = embed.title.replace("📅 ", "📅 WEEK-AHEAD — ", 1)

    for guild in client.guilds:
        category = discord.utils.find(
            lambda c: c.name.upper() == CALENDAR_CATEGORY,
            guild.categories,
        )
        channel = discord.utils.get(guild.text_channels, name=CALENDAR_CHANNEL_NAME)

        if channel is None:
            try:
                channel = await guild.create_text_channel(
                    CALENDAR_CHANNEL_NAME,
                    category=category,
                    topic="Weekly red folder economic events — live from ForexFactory via Jarvis",
                )
                log.info("Created #%s in guild %s", CALENDAR_CHANNEL_NAME, guild.name)
            except discord.Forbidden:
                log.warning("No permission to create #%s in %s", CALENDAR_CHANNEL_NAME, guild.name)
                continue

        try:
            await channel.send(
                content=(
                    "**@everyone** — Week-ahead red folder is up. "
                    "Plan around these or sit them out. 🍜"
                ),
                embed=embed,
            )
            log.info("Sunday calendar posted to #%s in %s", CALENDAR_CHANNEL_NAME, guild.name)
        except discord.Forbidden:
            log.warning("No permission to send in #%s in %s", CALENDAR_CHANNEL_NAME, guild.name)


@calendar_weekly_post.before_loop
async def before_calendar_task() -> None:
    await client.wait_until_ready()


# ── AI @mention handler ────────────────────────────────────────────────────────

@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    if client.user not in message.mentions:
        return

    content = re.sub(r"<@!?\d+>", "", message.content).strip()
    if not content:
        content = "Hello"

    # Log every @mention so we can trace routing decisions
    author_roles = [r.name for r in getattr(message.author, "roles", [])]
    poll_auth    = is_poll_author(message.author)
    poll_intent  = is_poll_intent(content)
    log.info(
        "on_message: author=%s(id=%s) roles=%s is_poll_author=%s poll_intent=%s content=%r",
        message.author, message.author.id, author_roles, poll_auth, poll_intent, content[:120],
    )

    async with message.channel.typing():

        # ── INTERCEPT: founder poll request ───────────────────────────────────
        # This branch MUST fire before the general AI reply path.
        if poll_auth and poll_intent:
            log.info("Routing to create_poll_from_request")
            await create_poll_from_request(message, content)
            return

        # ── Calendar keywords: inject live ForexFactory data ──────────────────
        extra_context = ""
        if CALENDAR_RE.search(content):
            feed_ok, events = await asyncio.to_thread(fetch_ff_calendar)
            if not feed_ok:
                await message.reply(
                    "Calendar feed is down — check ForexFactory directly. 🍜\n"
                    "<https://www.forexfactory.com/calendar>"
                )
                return
            extra_context = "\n\n" + calendar_context_str(events)

        # ── Standard Claude response ───────────────────────────────────────────
        user_prompt = content + extra_context
        try:
            response = await asyncio.to_thread(
                ai.messages.create,
                model=AI_MODEL,
                max_tokens=600,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            reply = response.content[0].text.strip()
        except Exception:
            log.exception("Claude API call failed")
            reply = "⚠️ AI hiccup — try again in a moment. 🍜"

        if len(reply) > 1990:
            reply = reply[:1987] + "…"
        await message.reply(reply)


# ── Bot ready ──────────────────────────────────────────────────────────────────

@client.event
async def on_ready() -> None:
    log.info("Jarvis online — %s (ID: %s)", client.user, client.user.id)
    log.info("COFOUNDER_IDS set: %s  (%d IDs)", bool(_COFOUNDER_ID_SET), len(_COFOUNDER_ID_SET))
    log.info("discord.py version: %s", discord.__version__)

    guild_id = int(GUILD_ID_STR) if GUILD_ID_STR.isdigit() else None
    if guild_id:
        guild_obj = discord.Object(id=guild_id)
        tree.copy_global_to(guild=guild_obj)
        synced = await tree.sync(guild=guild_obj)
    else:
        synced = await tree.sync()

    log.info("Slash commands registered: %s", [c.name for c in synced])

    if not calendar_weekly_post.is_running():
        calendar_weekly_post.start()
    log.info("Sunday 7:30 PM ET calendar task: running")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN environment variable is not set")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")
    client.run(DISCORD_TOKEN)
