#!/usr/bin/env python3
"""
jarvis_bot.py — Jarvis, the AI assistant for Langston's Financial Intelligence Discord.

Environment variables:
    DISCORD_BOT_TOKEN   — Discord bot token (required)
    ANTHROPIC_API_KEY   — Anthropic API key (required)
    DISCORD_GUILD_ID    — Guild ID for instant slash-command sync (optional but recommended)

Roles recognized:
    Admin, Moderator    — can use /poll
    Co-Founder          — AI auto-generates polls on @mention request
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

import anthropic
import discord
from discord import app_commands
from discord.ext import tasks

# ── Configuration ─────────────────────────────────────────────────────────────

DISCORD_TOKEN     = os.environ.get("DISCORD_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GUILD_ID_STR      = os.environ.get("DISCORD_GUILD_ID", "")

CALENDAR_CHANNEL_NAME = "jarvis-calendar"
CALENDAR_CATEGORY     = "FREE ANALYSIS"
CALENDAR_FEED_URL     = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

POLL_ROLES      = {"admin", "moderator", "co-founder", "co founder"}
COFOUNDER_ROLES = {"co-founder", "co founder", "cofounder"}

# Comma-separated Discord user IDs of co-founders — checked before role names.
# Set COFOUNDER_IDS=123456789,987654321 in Railway env vars.
_COFOUNDER_ID_SET: set[int] = {
    int(x.strip())
    for x in os.environ.get("COFOUNDER_IDS", "").split(",")
    if x.strip().isdigit()
}

CALENDAR_RE = re.compile(
    r"\b(calendar|earnings|red\s+folder|news\s+this\s+week|fomc|cpi|nfp|"
    r"jobs\s+report|fed\s+meeting|economic\s+data|macro\s+data|economic\s+events)\b",
    re.IGNORECASE,
)

POLL_REQUEST_RE = re.compile(
    r"\b(make|create|post|run|do)\s+(a\s+)?(poll|vote)\b",
    re.IGNORECASE,
)

ET = ZoneInfo("America/New_York")

AI_MODEL = "claude-opus-4-7"

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

REACTION_NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

# ── Discord client ─────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.guilds          = True
intents.members         = True

client = discord.Client(intents=intents)
tree   = app_commands.CommandTree(client)
ai     = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_calendar_posted_date: datetime.date | None = None


# ── Role helpers ───────────────────────────────────────────────────────────────

def has_poll_permission(member: discord.Member) -> bool:
    return any(r.name.lower() in POLL_ROLES for r in member.roles)


def is_cofounder(member: discord.Member) -> bool:
    if _COFOUNDER_ID_SET:
        return member.id in _COFOUNDER_ID_SET
    return any(r.name.lower() in COFOUNDER_ROLES for r in member.roles)


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
        # Sort by event date
        high_usd.sort(key=lambda e: e.get("date", ""))
        return True, high_usd
    except Exception:
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
    """Plain-text calendar context injected into Claude prompts."""
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


# ── Poll helpers ───────────────────────────────────────────────────────────────

def _ai_generate_poll_sync(topic: str) -> dict:
    """Synchronous Claude call — run via asyncio.to_thread."""
    response = ai.messages.create(
        model=AI_MODEL,
        max_tokens=300,
        system=(
            "You are a financial Discord bot. Generate a poll for a trading community "
            "about the given topic. Respond ONLY with valid JSON in this exact format "
            "(no markdown fences): "
            '{"question": "...", "options": ["Option 1", "Option 2", "Option 3"]}. '
            "2 to 4 options. Keep question under 100 characters."
        ),
        messages=[{"role": "user", "content": f"Create a poll about: {topic}"}],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    return json.loads(raw)


async def post_poll(
    channel: discord.abc.Messageable,
    question: str,
    options: list[str],
    duration_hours: int = 24,
) -> discord.Message:
    """
    Post a poll. Tries native Discord Poll first; falls back to an embed
    with numbered reactions if the API call fails for any reason.
    """
    # ── Attempt 1: native Discord Poll ────────────────────────────────────────
    try:
        poll = discord.Poll(
            question=question,
            duration=datetime.timedelta(hours=duration_hours),
            multiple=False,
        )
        for opt in options:
            poll.add_answer(text=opt)
        return await channel.send(poll=poll)
    except (discord.HTTPException, AttributeError):
        pass  # fall through to reaction embed

    # ── Attempt 2: embed + reaction voting ────────────────────────────────────
    lines = [f"{REACTION_NUMBERS[i]}  {opt}" for i, opt in enumerate(options)]
    embed = discord.Embed(
        title=f"📊 {question}",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Poll · closes in {duration_hours}h · react to vote")
    msg = await channel.send(embed=embed)
    for i in range(len(options)):
        await msg.add_reaction(REACTION_NUMBERS[i])
    return msg


# ── /poll slash command ────────────────────────────────────────────────────────

@tree.command(name="poll", description="Post a poll to this channel (Admin / Mod only)")
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
            "❌ Only Admins and Moderators can create polls.", ephemeral=True
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
    await post_poll(interaction.channel, question, parsed, duration)
    await interaction.followup.send("✅ Poll posted!", ephemeral=True)


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

    if now.weekday() != 6:           # Sunday only
        return
    if now.hour != 19 or now.minute != 30:
        return
    if _calendar_posted_date == today:
        return

    _calendar_posted_date = today
    feed_ok, events       = await asyncio.to_thread(fetch_ff_calendar)
    embed                 = format_calendar_embed(feed_ok, events)

    # Prepend "WEEK-AHEAD" to the title
    embed.title = embed.title.replace("📅 ", "📅 WEEK-AHEAD — ", 1)

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
            except discord.Forbidden:
                continue  # bot lacks permission to create channels in this guild

        try:
            await channel.send(
                content=(
                    "**@everyone** — Week-ahead red folder is up. "
                    "Plan around these or sit them out. 🍜"
                ),
                embed=embed,
            )
        except discord.Forbidden:
            pass


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

    # Remove all @mention tokens
    content = re.sub(r"<@!?\d+>", "", message.content).strip()
    if not content:
        content = "Hello"

    async with message.channel.typing():
        # ── Co-founder poll generation ─────────────────────────────────────────
        if is_cofounder(message.author) and POLL_REQUEST_RE.search(content):
            # Strip "make/create/run a poll [about/on/for/asking/regarding]" prefix
            # so Claude receives only the topic, not the command scaffolding.
            topic = re.sub(
                r"(?i)^.*?\bpoll\b\s*(?:about|on|for|regarding|asking|re:?)?\s*",
                "",
                content,
            ).strip() or content

            try:
                poll_data = await asyncio.to_thread(_ai_generate_poll_sync, topic)
                question  = poll_data["question"]
                options   = poll_data.get("options", [])[:10]
                if len(options) < 2:
                    raise ValueError("AI returned fewer than 2 options")
                await post_poll(message.channel, question, options, duration_hours=24)
                await message.reply("Poll's live 🍜")
            except Exception as exc:
                await message.reply(
                    f"⚠️ Couldn't generate that poll (`{exc}`). "
                    "Try `/poll question:\"...\" options:\"Option A, Option B\"` instead. 🍜"
                )
            return

        # ── Calendar keyword: inject live data into Claude context ─────────────
        extra_context = ""
        if CALENDAR_RE.search(content):
            feed_ok, events = await asyncio.to_thread(fetch_ff_calendar)
            if not feed_ok:
                await message.reply(
                    "Calendar feed is down — check ForexFactory directly. 🍜\n"
                    "<https://www.forexfactory.com/calendar>"
                )
                return
            # Feed succeeded — inject real data (may be empty for a quiet week)
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
        except Exception as exc:
            reply = f"⚠️ AI hiccup — try again in a moment. (`{exc}`) 🍜"

        if len(reply) > 1990:
            reply = reply[:1987] + "…"
        await message.reply(reply)


# ── Bot ready ──────────────────────────────────────────────────────────────────

@client.event
async def on_ready() -> None:
    print(f"Jarvis online — {client.user} (ID: {client.user.id})")

    guild_id = int(GUILD_ID_STR) if GUILD_ID_STR.isdigit() else None
    if guild_id:
        guild_obj = discord.Object(id=guild_id)
        tree.copy_global_to(guild=guild_obj)
        synced = await tree.sync(guild=guild_obj)
    else:
        synced = await tree.sync()

    print(f"Slash commands registered: {[c.name for c in synced]}")

    if not calendar_weekly_post.is_running():
        calendar_weekly_post.start()
    print("Sunday 7:30 PM ET calendar task: running")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN environment variable is not set")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")
    client.run(DISCORD_TOKEN)
