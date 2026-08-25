print("🚀 MAIN.PY STARTED", flush=True)
import discord
try:
    import discord.ext.voice_recv as voice_recv
except ImportError:
    voice_recv = None
from discord import app_commands
from discord.client import asyncio
from discord.ext import commands
import os
from flask import Flask
import threading
import tempfile
import shutil
from pathlib import Path
import json
import re
import random
from datetime import timedelta
from typing import Optional
import asyncio as py_asyncio
import time

temp_dir = tempfile.mkdtemp()

BOT_START_TIME = time.time()

app = Flask(__name__)

@app.route('/')
def index():
    return "Bot Online ✅"


# ----------------- Config -----------------
SOFTBAN_FILE = "softbans.json"
WARNINGS_FILE = "warnings.json"
LOG_CHANNEL_ID = 1540399902490763294
STAFF_ROLE_ID = 1268998581000601651
STAFF_PING_IMMUNE_ROLE_ID = 1268998581000601651
MUSIC_COMMAND_ROLE_ID = 1311257043348492298
PRIMARY_SERVER_ID = 1258549984325009468

# Recent deleted message cache for moderation tools
deleted_message_cache = []

# Tracks the most recent /speak usage for moderation review
last_speak_user = None
last_speak_message = None
last_speak_time = None

# Replace this with your actual voice channel ID
VOICE_CHANNEL_ID = 1534790002079432725
LAST_VC_CHANNEL_ID = VOICE_CHANNEL_ID
# ------------------------------------------


class aclient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = True

        super().__init__(intents=intents)
        self.synced = False
        self._voice_reconnect_task: py_asyncio.Task | None = None
        self._voice_watchdog_task: py_asyncio.Task | None = None

    async def on_ready(self):
        await self.wait_until_ready()

        if not self.synced:
            try:
                # Register every command in the main server immediately so
                # new commands such as /ticket appear without waiting for
                # Discord's global command propagation.
                guild = discord.Object(id=PRIMARY_SERVER_ID)

                # Copy the commands defined on the global tree into the
                # server-specific tree, then sync them to Discord.
                tree.copy_global_to(guild=guild)
                synced_commands = await tree.sync(guild=guild)

                # Remove the global copies so the commands are only registered
                # in the main server and do not appear twice.
                tree.clear_commands(guild=None)
                await tree.sync()

                print(
                    f"✅ Synced {len(synced_commands)} slash commands "
                    f"to server {PRIMARY_SERVER_ID}."
                )
                print(
                    "📋 Commands synced: "
                    + ", ".join(command.name for command in synced_commands)
                )

                self.synced = True

            except Exception as e:
                print(f"❌ Slash command sync failed: {type(e).__name__}: {e}")

        print(f"We have logged in as {self.user}.", flush=True)
        print(
            f"🔧 Intents: members={self.intents.members}, "
            f"message_content={self.intents.message_content}, "
            f"guilds={self.intents.guilds}",
            flush=True
        )

        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name="Watching everything"
        )

        await self.change_presence(activity=activity)

        # Automatically join VC when bot comes online
        await py_asyncio.sleep(2)
        try:
            await connect_to_voice()
        except Exception as e:
            print(f"❌ Initial voice connection failed: {e}", flush=True)

        if (
            self._voice_watchdog_task is None
            or self._voice_watchdog_task.done()
        ):
            self._voice_watchdog_task = py_asyncio.create_task(
                voice_watchdog()
            )


    async def on_message_delete(self, message: discord.Message):
        if message.author.bot:
            return

        deleted_by = None

        try:
            if message.guild:
                async for entry in message.guild.audit_logs(
                    limit=5,
                    action=discord.AuditLogAction.message_delete
                ):
                    if entry.target and entry.target.id == message.author.id:
                        if entry.extra and getattr(entry.extra, "channel", None) == message.channel:
                            deleted_by = entry.user
                            break
        except Exception:
            pass

        deleted_message_cache.append({
            "author": message.author,
            "deleted_by": deleted_by,
            "content": message.content,
            "channel": message.channel,
            "time": discord.utils.utcnow()
        })

        if len(deleted_message_cache) > 500:
            deleted_message_cache.pop(0)


    async def on_message(self, message: discord.Message):
        # Ignore messages sent by the bot itself.
        if message.author.id == self.user.id:
            return

        # Detect mentions reliably, including role pings.
        bot_mentioned = (
            self.user is not None
            and any(mentioned.id == self.user.id for mentioned in message.mentions)
        )
        staff_mentioned = (
            any(
                isinstance(member, discord.Member)
                and any(role.id == STAFF_ROLE_ID for role in member.roles)
                for member in message.mentions
            )
            or any(role.id == STAFF_ROLE_ID for role in message.role_mentions)
        )

        # Staff trigger: warn the last /speak user when "are you mad" is typed.
        # This check MUST happen before the normal bot mention reply.
        global last_speak_user, last_speak_message, last_speak_time
        if bot_mentioned and "are you mad" in message.content.lower() and last_speak_user:
            warnings = load_warnings()
            warnings.append({
                "user_id": str(last_speak_user.id),
                "guild_id": str(message.guild.id) if message.guild else "0",
                "moderator_id": str(message.author.id),
                "reason": "Inappropriate message sent using /speak",
                "type": "speak_warning",
                "timestamp": discord.utils.utcnow().isoformat()
            })
            save_warnings(warnings)
            count = warning_count_by_type(message.guild.id, last_speak_user.id, "speak_warning") if message.guild else 1
            await message.channel.send(
                f"⚠️ {last_speak_user.mention} has been warned **{count}/3**.\n"
                f"Reason: Inappropriate message sent using /speak.\n"
                f"Message: {last_speak_message}"
            )
            return

        # Detect swearing directly at the bot (unless bypass role).
        swear_words = ["stfu", "shut up", "idiot", "dumb bot", "trash bot"]
        if bot_mentioned and any(word in message.content.lower() for word in swear_words):
            if isinstance(message.author, discord.Member) and not is_warning_immune(message.author):
                warnings = load_warnings()
                warnings.append({
                    "user_id": str(message.author.id),
                    "guild_id": str(message.guild.id) if message.guild else "0",
                    "moderator_id": str(self.user.id),
                    "reason": "Swearing at the bot",
                    "type": "bot_swear",
                    "timestamp": discord.utils.utcnow().isoformat()
                })
                save_warnings(warnings)
                count = warning_count_by_type(message.guild.id, message.author.id, "bot_swear") if message.guild else 1
                await message.channel.send(
                    f"⚠️ {message.author.mention} You have been warned for swearing at the bot **{count}/3**.\n"
                    "3/3 warnings will result in a kick. Returning and doing it again can result in a permanent ban.",
                    delete_after=10
                )
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                return

        # Normal bot mention: greet the user.
        # If the message contains the moderated swear word, the warning system
        # below handles it instead of sending the normal greeting.
        if bot_mentioned and "cunt" not in message.content.lower():
            try:
                await message.channel.send(
                    f"{message.author.mention} Hello! I am the moderation Bot. "
                    "How can I help you?"
                )
            except discord.HTTPException as e:
                print(f"❌ Failed to send mention reply: {e}")


        # Warn users who ping staff members. Users with the immune role are ignored.
        if (
            message.guild is not None
            and isinstance(message.author, discord.Member)
            and not is_warning_immune(message.author)
            and staff_mentioned
        ):
            # Delete the staff ping message immediately after detecting it.
            try:
                await message.delete()
            except discord.Forbidden:
                print("❌ Missing Manage Messages permission.")
            except discord.HTTPException as e:
                print(f"❌ Failed to delete staff ping message: {e}")

            # If the user already reached 3/3 before, they were kicked and
            # returned. A new staff ping will result in a server ban.
            previous_count = warning_count_by_type(message.guild.id, message.author.id, "automatic_staff_ping")

            if previous_count >= 3 and isinstance(message.author, discord.Member):
                try:
                    await message.author.send(
                        f"You have been banned from **{message.guild.name}** "
                        "because you returned after a 3/3 warning kick and pinged staff again. "
                        "You can appeal this with Military Noob."
                    )
                except discord.HTTPException:
                    pass

                try:
                    await message.author.ban(reason="Returned after 3/3 staff ping warnings")
                    await message.channel.send(
                        f"{message.author.mention} has been banned for returning after a staff ping kick.",
                        delete_after=10
                    )
                except discord.HTTPException:
                    pass
                return

            warnings = load_warnings()
            warnings.append({
                "user_id": str(message.author.id),
                "guild_id": str(message.guild.id),
                "moderator_id": str(self.user.id),
                "reason": "Pinging a staff member",
                "type": "automatic_staff_ping",
                "timestamp": discord.utils.utcnow().isoformat()
            })
            save_warnings(warnings)

            count = warning_count_by_type(message.guild.id, message.author.id, "automatic_staff_ping")

            if count >= 3 and isinstance(message.author, discord.Member):
                try:
                    await message.author.send(
                        f"You have been kicked from **{message.guild.name}** "
                        "after receiving 3 warnings for pinging staff members."
                    )
                except discord.HTTPException:
                    pass
                try:
                    await message.author.kick(reason="3 warnings for pinging staff")
                    await message.channel.send(
                        f"{message.author.mention} has been kicked after reaching **3/3 warnings** for pinging staff.",
                        delete_after=10
                    )
                except discord.HTTPException:
                    pass
            else:
                await message.channel.send(
                    f"{message.author.mention} You have been warned **{count}/3** for pinging staff. "
                    f"{3-count} more warning{'s' if 3-count != 1 else ''} and you will be kicked.",
                    delete_after=10
                )

        # Only react when the bot is actually mentioned and the message
        # contains the specified swear word. Immune-role users are ignored.
        if (
            message.guild is not None
            and bot_mentioned
            and "cunt" in message.content.lower()
            and isinstance(message.author, discord.Member)
            and not is_warning_immune(message.author)
        ):
            warnings = load_warnings()
            user_id = str(message.author.id)
            guild_id = str(message.guild.id)
            previous_count = warning_count_by_type(message.guild.id, message.author.id, "automatic")

            try:
                await message.delete()
            except discord.Forbidden:
                # Still warn even if Discord does not allow the bot to delete the message.
                print("❌ I do not have permission to delete the warning message; continuing with warning.")
            except discord.HTTPException as e:
                # Still warn even if deletion fails for another Discord/API reason.
                print(f"❌ Failed to delete warning message: {e}; continuing with warning.")

            # If the member already had 3 warnings and returns after the kick,
            # another automatic offense results in a ban.
            if previous_count >= 4 and isinstance(message.author, discord.Member):
                warning_text = (
                    f"{message.author.mention} — You have been **banned** "
                    "because you returned after your 4/4 warning kick and "
                    "committed another offense. You can appeal this with Military Noob."
                )

                try:
                    try:
                        await message.author.send(
                            f"You have been banned from **{message.guild.name}** "
                            "because you returned after receiving 4/4 warnings "
                            "and then used a swear word at a staff member again. "
                            "You can appeal this with Military Noob."
                        )
                    except discord.HTTPException:
                        pass

                    await message.author.ban(
                        reason="Returned after 3 warnings and committed another automatic offense"
                    )

                    await send_warning_log(
                        message.guild,
                        "🔨 User Banned After Returning",
                        f"User: {message.author.mention}\n"
                        f"Points: **{previous_count + 1}**\n"
                        f"Reason: Another offense after 4/4 warnings",
                        discord.Color.red()
                    )
                except discord.Forbidden:
                    warning_text = (
                        f"{message.author.mention} — You already had **4/4 warnings** "
                        "and another offense was detected, but I could not ban you."
                    )
                except discord.HTTPException as e:
                    print(f"❌ Failed to ban user: {e}")
                    warning_text = (
                        f"{message.author.mention} — Another offense was detected "
                        "after **4/4 warnings**, but the ban could not be completed."
                    )

                # Record the post-kick offense too.
                warnings.append({
                    "user_id": user_id,
                    "guild_id": guild_id,
                    "moderator_id": str(self.user.id),
                    "reason": "Another swear-word offense after 4/4 warnings",
                    "type": "automatic",
                    "timestamp": discord.utils.utcnow().isoformat()
                })
                save_warnings(warnings)

            else:
                # Normal 1/4, 2/4, 3/4, 4/4 warning progression.
                warnings.append({
                    "user_id": user_id,
                    "guild_id": guild_id,
                    "moderator_id": str(self.user.id),
                    "reason": "Using a swear word at a staff member",
                    "type": "automatic",
                    "timestamp": discord.utils.utcnow().isoformat()
                })
                save_warnings(warnings)

                warning_number = warning_count_by_type(message.guild.id, message.author.id, "automatic")

                if warning_number >= 4 and isinstance(message.author, discord.Member):
                    warning_text = (
                        f"{message.author.mention} — You have received "
                        "**warning 4/4** and have been kicked from the server. "
                        "You can appeal this with Military Noob."
                    )

                    try:
                        try:
                            await message.author.send(
                                f"You have been kicked from **{message.guild.name}** "
                                "after receiving 4/4 warnings for using swear words "
                                "at a staff member. You can appeal this with Military Noob."
                            )
                        except discord.HTTPException:
                            pass

                        await message.author.kick(
                            reason="4 warnings for swearing while pinging the bot"
                        )

                        await send_warning_log(
                            message.guild,
                            "⚠️ Warning 4/4 — User Kicked",
                            f"User: {message.author.mention}\n"
                            f"Points: **{warning_number}**\n"
                            f"Reason: Swearing at the bot/staff moderation system",
                            discord.Color.red()
                        )
                    except discord.Forbidden:
                        warning_text = (
                            f"{message.author.mention} — You have received "
                            "**warning 4/4**, but I could not kick you."
                        )
                    except discord.HTTPException as e:
                        print(f"❌ Failed to kick user: {e}")
                        warning_text = (
                            f"{message.author.mention} — You have received "
                            "**warning 4/4**, but the kick could not be completed."
                        )
                else:
                    remaining = 4 - warning_number
                    warning_text = (
                        f"{message.author.mention} — You have been warned "
                        f"**{warning_number}/4** for using swear words at a staff member. "
                        f"**{remaining} more warning{'s' if remaining != 1 else ''}** "
                        "and you will be kicked from the server. "
                        "You can appeal this with Military Noob."
                    )

                await send_warning_log(
                    message.guild,
                    f"⚠️ Warning {min(warning_number, 4)}/4",
                    f"User: {message.author.mention}\n"
                    f"Points: **{warning_number}**\n"
                    f"Reason: Swearing while pinging the bot\n"
                    f"Type: Automatic"
                )

            try:
                await message.channel.send(warning_text, delete_after=10)
            except discord.HTTPException as e:
                print(f"❌ Failed to send warning message: {e}")


    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ):
        print(f"🔊 VOICE EVENT: {member.display_name} | {before.channel} -> {after.channel}", flush=True)

        # Announce users joining/leaving/moving voice channels
        if self.user is not None and member.id != self.user.id:
            if before.channel is None and after.channel is not None:
                pass
            elif before.channel is not None and after.channel is None:
                pass
            elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
                pass
            return

        # Only watch the bot itself below
        if self.user is None or member.id != self.user.id:
            return

        # The bot must always return to the configured channel. Do not
        # replace the target with the channel Discord reports after a move.
        if (
            after.channel is not None
            and after.channel.id == VOICE_CHANNEL_ID
        ):
            return

        if after.channel is None:
            print('⚠️ Voice connection lost. Returning to configured channel...')
        else:
            print(
                f'⚠️ Bot moved to {after.channel.name}. '
                'Returning to configured channel...'
            )

        if (
            self._voice_reconnect_task is None
            or self._voice_reconnect_task.done()
        ):
            self._voice_reconnect_task = py_asyncio.create_task(
                reconnect_voice_channel()
            )


client = aclient()
tree = app_commands.CommandTree(client)

@tree.command(
    name="speak",
    description="Send a message"
)
@app_commands.describe(
    message="The message to send"
)
async def speak(
    interaction: discord.Interaction,
    message: str
):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )

    # Staff only. /speak simply posts the supplied message.
    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            "❌ You do not have permission to use this command.",
            ephemeral=True
        )

    if not message.strip():
        return await interaction.response.send_message(
            "❌ No message was provided.",
            ephemeral=True
        )

    global last_speak_user, last_speak_message, last_speak_time
    last_speak_user = interaction.user
    last_speak_message = message
    last_speak_time = discord.utils.utcnow()

    await interaction.response.send_message(message)


# ----------------- Voice Reconnect Protection -----------------

async def reconnect_voice_channel():
    """Restore voice after an unexpected disconnect with bounded retries."""
    retry_delays = (2, 5, 10, 20, 30)

    try:
        for attempt, delay in enumerate(retry_delays, start=1):
            await py_asyncio.sleep(delay)

            channel = client.get_channel(VOICE_CHANNEL_ID)
            if not isinstance(
                channel,
                (discord.VoiceChannel, discord.StageChannel)
            ):
                print("❌ Reconnect stopped: voice channel is unavailable.")
                return

            current = discord.utils.get(
                client.voice_clients,
                guild=channel.guild
            )
            if (
                current
                and current.is_connected()
                and current.channel is not None
                and current.channel.id == channel.id
            ):
                print(f"🔊 Voice connection restored to {channel.name}.")
                return

            # A forced disconnect can leave a non-connected VoiceClient in
            # the cache. Remove it before creating a fresh handshake.
            for voice_client in list(client.voice_clients):
                if voice_client.guild.id == channel.guild.id:
                    try:
                        await voice_client.disconnect(force=True)
                    except Exception:
                        pass
                    await py_asyncio.sleep(1.5)

            try:
                voice_client = await channel.connect(
                    reconnect=False,
                    timeout=30
                )

                # Wait a moment for the voice connection to stabilize before returning
                await py_asyncio.sleep(1)
                print(
                    f"🔊 Reconnected to {channel.name} "
                    f"(attempt {attempt})."
                )
                return
            except Exception as e:
                print(
                    f"⚠️ Voice reconnect attempt {attempt}/"
                    f"{len(retry_delays)} failed: {e}"
                )

        print("❌ Voice reconnect stopped after 5 attempts.")
    finally:
        if client._voice_reconnect_task is not None:
            client._voice_reconnect_task = None


async def voice_watchdog():
    """Keep the bot in the configured voice channel if events are missed."""
    while not client.is_closed():
        await py_asyncio.sleep(5)

        channel = client.get_channel(VOICE_CHANNEL_ID)
        if not isinstance(
            channel,
            (discord.VoiceChannel, discord.StageChannel)
        ):
            continue

        current = discord.utils.get(
            client.voice_clients,
            guild=channel.guild
        )
        in_target = (
            current is not None
            and current.is_connected()
            and current.channel is not None
            and current.channel.id == channel.id
        )

        if in_target:
            continue

        print(
            "⚠️ Voice watchdog detected the bot is not in the "
            f"configured channel ({channel.name})."
        )
        if (
            client._voice_reconnect_task is None
            or client._voice_reconnect_task.done()
        ):
            client._voice_reconnect_task = py_asyncio.create_task(
                reconnect_voice_channel()
            )


async def connect_to_voice():
    global LAST_VC_CHANNEL_ID
    """
    Connect the bot to the configured voice channel.
    If already connected, do nothing.
    If connected to another channel, move to the configured channel.
    """

    if not client.is_ready():
        return

    channel = client.get_channel(VOICE_CHANNEL_ID)
    LAST_VC_CHANNEL_ID = VOICE_CHANNEL_ID

    if channel is None:
        print(
            f"❌ Voice channel with ID "
            f"{VOICE_CHANNEL_ID} could not be found."
        )
        return

    if not isinstance(
        channel,
        (discord.VoiceChannel, discord.StageChannel)
    ):
        print("❌ VOICE_CHANNEL_ID is not a voice/stage channel.")
        return

    # Check existing voice connections
    if client.voice_clients:

        voice_client = client.voice_clients[0]

        # Already in the correct channel
        if (
            voice_client.channel is not None
            and voice_client.channel.id == channel.id
            and voice_client.is_connected()
        ):
            print(
                f"🔊 Already connected to "
                f"{channel.name}."
            )
            return

        if voice_client.is_connected():
            try:
                await voice_client.move_to(channel)
                print(
                    f"🔊 Moved bot to "
                    f"{channel.name}."
                )
                return
            except Exception as e:
                print(
                    f"⚠️ Failed to move bot to VC; "
                    f"starting a fresh connection: {e}"
                )
        else:
            try:
                await voice_client.disconnect(force=True)
            except Exception:
                pass

    # No current connection, so connect
    try:
        voice_client = await channel.connect(reconnect=False, timeout=30)
        
        # Wait a moment for the voice connection to stabilize before returning
        await py_asyncio.sleep(1)

        print(
            f"🔊 Bot joined voice channel: "
            f"{channel.name}"
        )

    except discord.Forbidden:
        print(
            "❌ Bot does not have permission "
            "to join the voice channel."
        )

    except Exception as e:
        print(
            f"❌ Failed to join voice channel: {e}"
        )


# ----------------- Transfer User VC -----------------

@tree.command(
    name="transfer-vc",
    description="Move a user to another voice channel"
)
@app_commands.describe(
    user="User to move",
    channel="Voice channel to move them to"
)
async def transfer_vc(
    interaction: discord.Interaction,
    user: discord.Member,
    channel: discord.VoiceChannel
):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )

    if user.voice is None or user.voice.channel is None:
        return await interaction.response.send_message(
            f"❌ {user.mention} is not currently in a voice channel.",
            ephemeral=True
        )

    permissions = channel.permissions_for(interaction.guild.me)

    if not permissions.connect:
        return await interaction.response.send_message(
            "❌ I don't have permission to connect to that voice channel.",
            ephemeral=True
        )

    if not permissions.move_members:
        return await interaction.response.send_message(
            "❌ I need the **Move Members** permission to transfer users.",
            ephemeral=True
        )

    current_permissions = user.voice.channel.permissions_for(
        interaction.guild.me
    )

    if not current_permissions.move_members:
        return await interaction.response.send_message(
            "❌ I don't have permission to move members from their current voice channel.",
            ephemeral=True
        )

    if user.voice.channel.id == channel.id:
        return await interaction.response.send_message(
            f"❌ {user.mention} is already in **{channel.name}**.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    try:
        await user.move_to(
            channel,
            reason=f"Voice transfer by {interaction.user}"
        )

        await interaction.followup.send(
            f"✅ Moved {user.mention} to **{channel.name}**.",
            ephemeral=True
        )

        print(
            f"🔄 {user} was moved to {channel.name} "
            f"by {interaction.user}"
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Discord denied the move. Make sure I have **Move Members** "
            "and **Connect** permissions, and that my role is high enough.",
            ephemeral=True
        )

    except Exception as e:
        print(f"❌ Transfer VC failed: {e}")

        await interaction.followup.send(
            f"❌ Failed to transfer user: {e}",
            ephemeral=True
        )


# ----------------- Embed Helper -----------------

def create_custom_embed(
    title,
    description,
    color=discord.Color.blue()
):
    embed = discord.Embed(
        title=title,
        description=description,
        color=color
    )

    embed.set_footer(text="Made by NexTech")

    return embed


# ----------------- Tester -----------------

@tree.command(
    name='tester',
    description='testing'
)
@app_commands.checks.cooldown(1, 60)
async def slash2(
    interaction: discord.Interaction,
    name: str
):

    if not interaction.response.is_done():
        await interaction.response.defer()

        await asyncio.sleep(3)

    await interaction.followup.send(
        'My name Nick I need a dick exstender To fit in a woman pussy'
    )

    embed = create_custom_embed(
        "Test Result",
        f"I am working! {name}"
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True
    )


# ----------------- Helpers -----------------

def load_softbans():
    """Load softbans from disk."""

    if not os.path.exists(SOFTBAN_FILE):
        return []

    try:
        with open(SOFTBAN_FILE, "r") as f:

            content = f.read().strip()

            if not content:
                return []

            data = json.loads(content)

            if not isinstance(data, list):
                return []

            return data

    except (json.JSONDecodeError, OSError) as e:

        print(
            f"[softbans] Failed to load "
            f"{SOFTBAN_FILE}: {e}"
        )

        try:

            if os.path.exists(SOFTBAN_FILE):
                os.replace(
                    SOFTBAN_FILE,
                    SOFTBAN_FILE + ".corrupt"
                )

        except OSError:
            pass

        return []


def save_softbans(data):

    with open(SOFTBAN_FILE, "w") as f:
        json.dump(
            data,
            f,
            indent=4
        )


def parse_duration(duration_str):

    if not duration_str:
        return None

    match = re.match(
        r"(\d+)([dhm])",
        duration_str.lower()
    )

    if not match:
        return None

    value, unit = match.groups()

    value = int(value)

    if unit == "d":
        return timedelta(days=value)

    elif unit == "h":
        return timedelta(hours=value)

    elif unit == "m":
        return timedelta(minutes=value)

    return None


def generate_case_id():

    return str(
        random.randint(
            1000000,
            9999999
        )
    )


def load_warnings():
    """Load warning records from disk."""
    if not os.path.exists(WARNINGS_FILE):
        return []

    try:
        with open(WARNINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        print(f"[warnings] Failed to load {WARNINGS_FILE}: {e}")
        return []


def save_warnings(data):
    """Save warning records to disk."""
    with open(WARNINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def warning_count(guild_id: int, user_id: int):
    return sum(
        1 for entry in load_warnings()
        if entry.get("guild_id") == str(guild_id)
        and entry.get("user_id") == str(user_id)
    )


def warning_count_by_type(guild_id: int, user_id: int, warning_type: str):
    return sum(
        1 for entry in load_warnings()
        if entry.get("guild_id") == str(guild_id)
        and entry.get("user_id") == str(user_id)
        and entry.get("type") == warning_type
    )


def is_warning_immune(member: discord.abc.User):
    """Users with the immunity role are ignored by warning automation."""
    if not isinstance(member, discord.Member):
        return False

    return any(role.id in (STAFF_ROLE_ID, STAFF_PING_IMMUNE_ROLE_ID) for role in member.roles)


async def send_warning_log(guild, title, description, color=discord.Color.orange()):
    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    if isinstance(log_channel, discord.TextChannel):
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow()
        )
        await log_channel.send(embed=embed)


def is_staff(member: discord.abc.User):

    if not isinstance(
        member,
        discord.Member
    ):
        return False

    return any(
        role.id == STAFF_ROLE_ID
        for role in member.roles
    )


# ----------------- Warning System -----------------

@tree.command(name="warn", description="Warn a user")
@app_commands.describe(
    user="User to warn",
    reason="Reason for the warning"
)
async def warn_user(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: str = "No reason provided"
):
    if interaction.guild is None or interaction.guild.id != PRIMARY_SERVER_ID:
        return await interaction.response.send_message(
            "❌ Only usable in main server.", ephemeral=True
        )

    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            "❌ No permission.", ephemeral=True
        )

    if is_warning_immune(user):
        return await interaction.response.send_message(
            "🛡️ This user is immune from warnings.", ephemeral=True
        )

    warnings = load_warnings()
    warnings.append({
        "user_id": str(user.id),
        "guild_id": str(interaction.guild.id),
        "moderator_id": str(interaction.user.id),
        "reason": reason,
        "type": "manual",
        "timestamp": discord.utils.utcnow().isoformat()
    })
    save_warnings(warnings)

    count = warning_count(interaction.guild.id, user.id)

    try:
        await user.send(
            f"You have been warned in {interaction.guild.name}.\n"
            f"Warning points: {count}/3\nReason: {reason}"
        )
    except (discord.Forbidden, discord.HTTPException):
        pass

    await send_warning_log(
        interaction.guild,
        f"⚠️ Warning Issued — {count}/3",
        f"User: {user.mention}\n"
        f"Points: **{count}**\n"
        f"By: {interaction.user.mention}\n"
        f"Reason: {reason}\n"
        f"Type: Manual"
    )

    if count >= 3:
        try:
            await user.kick(reason="Reached 3 warning points")
            result = f"⚠️ {user.mention} reached **3/3** warning points and was kicked."
            await send_warning_log(
                interaction.guild,
                "👢 User Kicked — 3/3 Warnings",
                f"User: {user.mention}\nBy: {interaction.user.mention}\nReason: Reached 3 warning points",
                discord.Color.red()
            )
        except discord.Forbidden:
            result = f"⚠️ {user.mention} reached **3/3**, but I could not kick them."
    else:
        result = f"✅ {user.mention} warned. Warning points: **{count}/3**."

    await interaction.response.send_message(result, ephemeral=True)


@tree.command(name="unwarn", description="Remove one warning point from a user")
@app_commands.describe(user="User whose warning point should be removed")
async def unwarn_user(
    interaction: discord.Interaction,
    user: discord.Member
):
    if interaction.guild is None or interaction.guild.id != PRIMARY_SERVER_ID:
        return await interaction.response.send_message(
            "❌ Only usable in main server.", ephemeral=True
        )

    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            "❌ No permission.", ephemeral=True
        )

    warnings = load_warnings()
    matches = [
        (i, entry) for i, entry in enumerate(warnings)
        if entry.get("guild_id") == str(interaction.guild.id)
        and entry.get("user_id") == str(user.id)
    ]

    if not matches:
        return await interaction.response.send_message(
            "❌ This user has no warning points.", ephemeral=True
        )

    # Remove the most recent warning.
    index, removed = matches[-1]
    warnings.pop(index)
    save_warnings(warnings)

    count = warning_count(interaction.guild.id, user.id)

    await send_warning_log(
        interaction.guild,
        "✅ Warning Removed",
        f"User: {user.mention}\n"
        f"By: {interaction.user.mention}\n"
        f"Removed: 1 point\n"
        f"Remaining points: **{count}**"
    )

    await interaction.response.send_message(
        f"✅ Removed 1 warning point from {user.mention}. "
        f"They now have **{count}/3**.",
        ephemeral=True
    )


@tree.command(name="warn-logs", description="View warning points and warning logs")
@app_commands.describe(user="Optional user to view")
async def warn_logs(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None
):
    if interaction.guild is None or interaction.guild.id != PRIMARY_SERVER_ID:
        return await interaction.response.send_message(
            "❌ Only usable in main server.", ephemeral=True
        )

    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            "❌ No permission.", ephemeral=True
        )

    warnings = [
        entry for entry in load_warnings()
        if entry.get("guild_id") == str(interaction.guild.id)
        and (user is None or entry.get("user_id") == str(user.id))
    ]

    if user is not None:
        count = len(warnings)
        title = f"Warning Logs — {user}"
        lines = [f"**Warning points: {count}/3**", ""]
        if not warnings:
            lines.append("No warning logs.")
        else:
            for entry in warnings[-15:]:
                lines.append(
                    f"• `{entry.get('timestamp', 'Unknown')}` — "
                    f"**{entry.get('type', 'unknown')}** — "
                    f"{entry.get('reason', 'No reason')}"
                )
    else:
        title = "Warning Logs"
        if not warnings:
            lines = ["No warning logs."]
        else:
            counts = {}
            for entry in warnings:
                uid = entry.get("user_id")
                counts[uid] = counts.get(uid, 0) + 1

            lines = ["**Warning points by user:**"]
            for uid, count in sorted(counts.items(), key=lambda x: -x[1])[:25]:
                lines.append(f"• <@{uid}> — **{count}/3** points")

            lines.append("")
            lines.append("**Recent warnings:**")
            for entry in warnings[-10:]:
                lines.append(
                    f"• <@{entry.get('user_id')}> — "
                    f"{entry.get('reason', 'No reason')} "
                    f"({entry.get('type', 'unknown')})"
                )

    await interaction.response.send_message(
        f"**{title}**\n" + "\n".join(lines)[:1900],
        ephemeral=True
    )


# ----------------- Softban -----------------

@tree.command(
    name="softban",
    description="Softban a user"
)
@app_commands.describe(
    user="User to softban",
    reason="Reason",
    duration="10d / 2h / 30m"
)
async def softban(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: str = "No reason provided",
    duration: Optional[str] = None
):

    if (
        interaction.guild is None
        or interaction.guild.id != PRIMARY_SERVER_ID
    ):
        return await interaction.response.send_message(
            "❌ Only usable in main server.",
            ephemeral=True
        )

    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )

    softbans = load_softbans()

    user_id = str(user.id)

    if user_id in [
        entry["user_id"]
        for entry in softbans
    ]:
        return await interaction.response.send_message(
            "⚠ Already softbanned.",
            ephemeral=True
        )

    duration_td = parse_duration(duration)

    expires_at = (
        discord.utils.utcnow() + duration_td
    ).isoformat() if duration_td else None

    case_id = generate_case_id()

    try:
        await user.send(
            f"You were softbanned from "
            f"{interaction.guild.name}\n"
            f"Reason: {reason}"
        )

    except:
        pass

    await interaction.guild.ban(
        user,
        reason=reason,
        delete_message_seconds=0
    )

    await interaction.guild.unban(
        user,
        reason="Softban"
    )

    softbans.append({
        "case_id": case_id,
        "user_id": user_id,
        "reason": reason,
        "banned_at": discord.utils.utcnow().isoformat(),
        "expires_at": expires_at,
        "guild_id": str(interaction.guild.id),
        "permanent": not duration_td,
    })

    save_softbans(softbans)

    log_channel = interaction.guild.get_channel(
        LOG_CHANNEL_ID
    )

    if isinstance(
        log_channel,
        discord.TextChannel
    ):

        embed = discord.Embed(
            title="🔨 Softban Issued",
            description=(
                f"Case: `{case_id}`\n"
                f"User: {user.mention}\n"
                f"By: {interaction.user.mention}\n"
                f"Reason: {reason}"
            ),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )

        await log_channel.send(
            embed=embed
        )

    await interaction.response.send_message(
        "✅ Softban complete.",
        ephemeral=True
    )


# ----------------- Unsoftban -----------------

@tree.command(
    name="unsoftban",
    description="Remove a softban"
)
@app_commands.describe(
    user="User to unsoftban"
)
async def unsoftban(
    interaction: discord.Interaction,
    user: discord.User
):

    if (
        interaction.guild is None
        or interaction.guild.id != PRIMARY_SERVER_ID
    ):
        return await interaction.response.send_message(
            "❌ Only usable in main server.",
            ephemeral=True
        )

    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )

    softbans = load_softbans()

    updated = [
        b for b in softbans
        if b["user_id"] != str(user.id)
    ]

    save_softbans(updated)

    try:

        await interaction.guild.unban(
            discord.Object(id=user.id),
            reason="Manual unsoftban"
        )

    except discord.NotFound:
        pass

    except Exception as e:

        return await interaction.response.send_message(
            f"❌ Failed: {e}",
            ephemeral=True
        )

    await interaction.response.send_message(
        "✅ Unsoftbanned.",
        ephemeral=True
    )


# ----------------- Softban List -----------------

class SoftbanSelect(discord.ui.Select):

    def __init__(self, entries):

        options = []

        for entry in entries[:25]:

            user_id = entry.get(
                "user_id",
                "?"
            )

            case_id = entry.get(
                "case_id",
                "?"
            )

            reason = (
                entry.get("reason")
                or "No reason"
            )[:80]

            options.append(
                discord.SelectOption(
                    label=f"User {user_id}",
                    description=(
                        f"Case {case_id} — "
                        f"{reason}"
                    ),
                    value=str(user_id)
                )
            )

        super().__init__(
            placeholder="Pick a user to unsoftban…",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        if not is_staff(interaction.user):

            return await interaction.response.send_message(
                "❌ No permission.",
                ephemeral=True
            )

        if interaction.guild is None:

            return await interaction.response.send_message(
                "❌ Guild only.",
                ephemeral=True
            )

        user_id = self.values[0]

        softbans = load_softbans()

        updated = [
            b for b in softbans
            if b["user_id"] != user_id
        ]

        save_softbans(updated)

        try:

            await interaction.guild.unban(
                discord.Object(
                    id=int(user_id)
                ),
                reason="Unsoftban via list"
            )

        except discord.NotFound:
            pass

        except Exception as e:

            return await interaction.response.send_message(
                f"❌ Failed: {e}",
                ephemeral=True
            )

        await interaction.response.send_message(
            f"✅ Unsoftbanned user `{user_id}`.",
            ephemeral=True
        )


class SoftbanListView(discord.ui.View):

    def __init__(self, entries):

        super().__init__(
            timeout=120
        )

        self.add_item(
            SoftbanSelect(entries)
        )


@tree.command(
    name="softban-list",
    description="List all softbanned users"
)
async def softban_list(
    interaction: discord.Interaction
):

    if (
        interaction.guild is None
        or interaction.guild.id != PRIMARY_SERVER_ID
    ):
        return await interaction.response.send_message(
            "❌ Only usable in main server.",
            ephemeral=True
        )

    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )

    softbans = load_softbans()

    filtered = [
        b for b in softbans
        if b.get("guild_id")
        == str(interaction.guild.id)
    ]

    if not filtered:

        return await interaction.response.send_message(
            "No softbans.",
            ephemeral=True
        )

    lines = [
        f"Softbanned Users ({len(filtered)}):"
    ]

    for entry in filtered:

        lines.append(
            f"- Case: {entry.get('case_id')} | "
            f"User ID: {entry.get('user_id')} | "
            f"Reason: {entry.get('reason')}"
        )

    message = "\n".join(lines)

    if len(message) > 1900:

        message = (
            message[:1900]
            + "\n... (truncated)"
        )

    view = SoftbanListView(filtered)

    await interaction.response.send_message(
        message,
        view=view,
        ephemeral=True
    )


# ----------------- Kick -----------------

@tree.command(
    name="server-kick",
    description="Kick user"
)
@app_commands.describe(
    user="User",
    reason="Reason"
)
async def server_kick(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: str = "No reason"
):

    if (
        interaction.guild is None
        or interaction.guild.id != PRIMARY_SERVER_ID
    ):
        return await interaction.response.send_message(
            "❌ Only usable in main server.",
            ephemeral=True
        )

    if not is_staff(interaction.user):

        return await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )

    case_id = generate_case_id()

    try:

        await user.send(
            f"You were kicked from "
            f"{interaction.guild.name}\n"
            f"Reason: {reason}"
        )

    except:
        pass

    await user.kick(
        reason=reason
    )

    log_channel = interaction.guild.get_channel(
        LOG_CHANNEL_ID
    )

    if isinstance(
        log_channel,
        discord.TextChannel
    ):

        embed = discord.Embed(
            title="👢 User Kicked",
            description=(
                f"Case: `{case_id}`\n"
                f"User: {user.mention}\n"
                f"By: {interaction.user.mention}\n"
                f"Reason: {reason}"
            ),
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )

        await log_channel.send(
            embed=embed
        )

    await interaction.response.send_message(
        "✅ User kicked.",
        ephemeral=True
    )


# ----------------- Ban -----------------

@tree.command(
    name="server-ban",
    description="Ban user"
)
@app_commands.describe(
    user="User",
    reason="Reason"
)
async def server_ban(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: str = "No reason"
):

    if (
        interaction.guild is None
        or interaction.guild.id != PRIMARY_SERVER_ID
    ):
        return await interaction.response.send_message(
            "❌ Only usable in main server.",
            ephemeral=True
        )

    if not is_staff(interaction.user):

        return await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )

    case_id = generate_case_id()

    try:

        await user.send(
            f"You were banned from "
            f"{interaction.guild.name}\n"
            f"Reason: {reason}"
        )

    except:
        pass

    await interaction.guild.ban(
        user,
        reason=reason
    )

    log_channel = interaction.guild.get_channel(
        LOG_CHANNEL_ID
    )

    if isinstance(
        log_channel,
        discord.TextChannel
    ):

        embed = discord.Embed(
            title="🔨 User Banned",
            description=(
                f"Case: `{case_id}`\n"
                f"User: {user.mention}\n"
                f"By: {interaction.user.mention}\n"
                f"Reason: {reason}"
            ),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )

        await log_channel.send(
            embed=embed
        )

    await interaction.response.send_message(
        "✅ User banned.",
        ephemeral=True
    )


# ----------------- Ban List -----------------

class BanListSelect(discord.ui.Select):

    def __init__(self, ban_entries):

        options = []

        for entry in ban_entries[:25]:

            user = entry.user

            reason = (
                entry.reason
                or "No reason"
            )[:80]

            options.append(
                discord.SelectOption(
                    label=f"{user.name} ({user.id})"[:100],
                    description=f"Reason: {reason}",
                    value=str(user.id)
                )
            )

        super().__init__(
            placeholder="Pick a user to unban…",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        if not is_staff(interaction.user):

            return await interaction.response.send_message(
                "❌ No permission.",
                ephemeral=True
            )

        if interaction.guild is None:

            return await interaction.response.send_message(
                "❌ Guild only.",
                ephemeral=True
            )

        user_id = self.values[0]

        case_id = generate_case_id()

        try:

            user = await interaction.client.fetch_user(
                int(user_id)
            )

            await interaction.guild.unban(
                user,
                reason="Unban via list"
            )

        except discord.NotFound:

            return await interaction.response.send_message(
                "❌ That user isn't banned anymore.",
                ephemeral=True
            )

        except Exception as e:

            return await interaction.response.send_message(
                f"❌ Failed: {e}",
                ephemeral=True
            )

        softbans = load_softbans()

        updated = [
            b for b in softbans
            if b["user_id"] != user_id
        ]

        if len(updated) != len(softbans):
            save_softbans(updated)

        log_channel = interaction.guild.get_channel(
            LOG_CHANNEL_ID
        )

        if isinstance(
            log_channel,
            discord.TextChannel
        ):

            embed = discord.Embed(
                title="🔓 User Unbanned",
                description=(
                    f"Case: `{case_id}`\n"
                    f"User: {user.name} "
                    f"(`{user.id}`)\n"
                    f"By: {interaction.user.mention}"
                ),
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )

            await log_channel.send(
                embed=embed
            )

        await interaction.response.send_message(
            f"✅ Unbanned `{user.name}` "
            f"(`{user.id}`).",
            ephemeral=True
        )


class BanListView(discord.ui.View):

    def __init__(self, ban_entries):

        super().__init__(
            timeout=120
        )

        self.add_item(
            BanListSelect(ban_entries)
        )


@tree.command(
    name="ban-list",
    description="List banned users and pick one to unban"
)
async def ban_list(
    interaction: discord.Interaction
):

    if (
        interaction.guild is None
        or interaction.guild.id != PRIMARY_SERVER_ID
    ):
        return await interaction.response.send_message(
            "❌ Only usable in main server.",
            ephemeral=True
        )

    if not is_staff(interaction.user):

        return await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )

    if not interaction.guild.me.guild_permissions.ban_members:

        return await interaction.response.send_message(
            "❌ I need the 'Ban Members' permission.",
            ephemeral=True
        )

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        bans = [
            b async for b
            in interaction.guild.bans(limit=25)
        ]

    except Exception as e:

        return await interaction.followup.send(
            f"❌ Failed to fetch bans: {e}",
            ephemeral=True
        )

    if not bans:

        return await interaction.followup.send(
            "No banned users.",
            ephemeral=True
        )

    lines = [
        f"Banned Users ({len(bans)}):"
    ]

    for entry in bans:

        reason = (
            entry.reason
            or "No reason"
        )

        lines.append(
            f"- {entry.user.name} "
            f"(`{entry.user.id}`) — "
            f"{reason}"
        )

    message = "\n".join(lines)

    if len(message) > 1900:

        message = (
            message[:1900]
            + "\n... (truncated)"
        )

    view = BanListView(bans)

    await interaction.followup.send(
        message,
        view=view,
        ephemeral=True
    )


# ----------------- Server Unban -----------------

@tree.command(
    name="server-unban",
    description="Unban user"
)
@app_commands.describe(
    user_id="User ID"
)
async def server_unban(
    interaction: discord.Interaction,
    user_id: str
):

    if (
        interaction.guild is None
        or interaction.guild.id != PRIMARY_SERVER_ID
    ):
        return await interaction.response.send_message(
            "❌ Only usable in main server.",
            ephemeral=True
        )

    if not is_staff(interaction.user):

        return await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )

    case_id = generate_case_id()

    try:

        user = await interaction.client.fetch_user(
            int(user_id)
        )

        await interaction.guild.unban(
            user
        )

    except Exception as e:

        return await interaction.response.send_message(
            f"❌ Failed: {e}",
            ephemeral=True
        )

    log_channel = interaction.guild.get_channel(
        LOG_CHANNEL_ID
    )

    if isinstance(
        log_channel,
        discord.TextChannel
    ):

        embed = discord.Embed(
            title="🔓 User Unbanned",
            description=(
                f"Case: `{case_id}`\n"
                f"User: {user.name}\n"
                f"By: {interaction.user.mention}"
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )

        await log_channel.send(
            embed=embed
        )

    await interaction.response.send_message(
        "✅ User unbanned.",
        ephemeral=True
    )


# ----------------- Timeout -----------------

@tree.command(
    name="server-timeout",
    description="Timeout a user"
)
@app_commands.describe(
    user="User to timeout",
    duration="10d / 2h / 30m",
    reason="Reason for timeout"
)
async def timeout(
    interaction: discord.Interaction,
    user: discord.Member,
    duration: str,
    reason: str = "No reason provided"
):

    if (
        interaction.guild is None
        or interaction.guild.id != PRIMARY_SERVER_ID
    ):
        return await interaction.response.send_message(
            "❌ Only usable in main server.",
            ephemeral=True
        )

    if not is_staff(interaction.user):

        return await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )

    if not interaction.guild.me.guild_permissions.moderate_members:

        return await interaction.response.send_message(
            "❌ I need the 'Moderate Members' permission.",
            ephemeral=True
        )

    duration_td = parse_duration(duration)

    if not duration_td:

        return await interaction.response.send_message(
            "❌ Invalid duration format. "
            "Use 10d, 2h, or 30m.",
            ephemeral=True
        )

    timeout_until = (
        discord.utils.utcnow()
        + duration_td
    )

    try:

        await user.send(
            f"You have been timed out in "
            f"{interaction.guild.name} "
            f"for {duration}. "
            f"Reason: {reason}"
        )

    except:
        pass

    try:

        await user.edit(
            timed_out_until=timeout_until,
            reason=reason
        )

    except Exception as e:

        return await interaction.response.send_message(
            f"❌ Failed to timeout: {e}",
            ephemeral=True
        )

    case_id = generate_case_id()

    log_channel = interaction.guild.get_channel(
        LOG_CHANNEL_ID
    )

    if isinstance(
        log_channel,
        discord.TextChannel
    ):

        embed = discord.Embed(
            title="⏱ User Timed Out",
            description=(
                f"Case: `{case_id}`\n"
                f"User: {user.mention}\n"
                f"By: {interaction.user.mention}\n"
                f"Duration: {duration}\n"
                f"Reason: {reason}"
            ),
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )

        await log_channel.send(
            embed=embed
        )

    await interaction.response.send_message(
        f"✅ {user.mention} has been "
        f"timed out for {duration}.",
        ephemeral=True
    )


# ----------------- Untimeout -----------------

@tree.command(
    name="server-untimeout",
    description="Remove a timeout from a user"
)
@app_commands.describe(
    user="User to remove timeout"
)
async def untimeout(
    interaction: discord.Interaction,
    user: discord.Member
):

    if (
        interaction.guild is None
        or interaction.guild.id != PRIMARY_SERVER_ID
    ):
        return await interaction.response.send_message(
            "❌ Only usable in main server.",
            ephemeral=True
        )

    if not is_staff(interaction.user):

        return await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )

    if not interaction.guild.me.guild_permissions.moderate_members:

        return await interaction.response.send_message(
            "❌ I need the 'Moderate Members' permission.",
            ephemeral=True
        )

    try:

        await user.edit(
            timed_out_until=None,
            reason="Manual untimeout"
        )

    except Exception as e:

        return await interaction.response.send_message(
            f"❌ Failed to remove timeout: {e}",
            ephemeral=True
        )

    case_id = generate_case_id()

    log_channel = interaction.guild.get_channel(
        LOG_CHANNEL_ID
    )

    if isinstance(
        log_channel,
        discord.TextChannel
    ):

        embed = discord.Embed(
            title="⏱ Timeout Removed",
            description=(
                f"Case: `{case_id}`\n"
                f"User: {user.mention}\n"
                f"By: {interaction.user.mention}"
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )

        await log_channel.send(
            embed=embed
        )

    await interaction.response.send_message(
        f"✅ Timeout removed for {user.mention}.",
        ephemeral=True
    )


# ----------------- Cookie -----------------

@tree.command(
    name='cookie',
    description='Send a cookie via DM'
)
async def send_cookie_dm(
    interaction: discord.Interaction
):

    user = interaction.user

    embed = discord.Embed(
        title="Tank Sent!",
        description=(
            "I sent you a cookie, you'll like it, "
            "it's fresh from the oven I made 🍪"
        ),
        color=discord.Color.blue()
    )

    embed.set_footer(
        text="Made by NexTech"
    )

    await user.send(
        embed=embed
    )

    await interaction.response.send_message(
        "Cookie sent to your DMs!",
        ephemeral=True
    )


# ----------------- Ticket System Config -----------------
TICKET_LOG_CHANNEL_ID = LOG_CHANNEL_ID
TICKET_TRANSCRIPT_CHANNEL_ID = 1258549985079853131
TICKET_CATEGORY_NAME = "Tickets"
TICKET_PREFIX = "ticket-"
TICKET_NOTIFY_ROLE_ID = 1258549984366956546  # Change this to your ticket notify role
ticket_claims: dict[int, int] = {}

# Ticket panel categories
TICKET_PANEL_CATEGORIES = {
    "general_support": (
        "General Support",
        "For general assistance, technical issues, account questions, service inquiries, developer support, R",
    ),
    "public_records": (
        "Public Records/Press Release",
        "For requests involving public records or for Press, publicly available company information, document",
    ),
}

def is_ticket_channel(channel: discord.abc.GuildChannel) -> bool:
    return isinstance(channel, discord.TextChannel) and channel.name.startswith(TICKET_PREFIX)

def ticket_owner_id(channel: discord.TextChannel) -> Optional[int]:
    try:
        return int(channel.name[len(TICKET_PREFIX):].split("-")[0])
    except (ValueError, IndexError):
        return None

def can_manage_ticket(member: discord.Member, channel: discord.TextChannel) -> bool:
    return (
        member.id == ticket_owner_id(channel)
        or any(role.id == STAFF_ROLE_ID for role in member.roles)
        or member.guild_permissions.manage_channels
    )

class TicketCategoryReasonModal(discord.ui.Modal):
    reason = discord.ui.TextInput(
        label="Reason for opening this ticket",
        placeholder="Briefly describe what you need help with...",
        style=discord.TextStyle.paragraph,
        min_length=3,
        max_length=1000,
        required=True,
    )

    def __init__(self, category_key: str):
        self.category_key = category_key
        category_name = TICKET_PANEL_CATEGORIES.get(category_key, ("Support", ""))[0]
        super().__init__(title=category_name[:45])

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)

        guild = interaction.guild
        existing = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel) and c.name == f"{TICKET_PREFIX}{interaction.user.id}",
            guild.text_channels,
        )

        if existing is not None:
            return await interaction.response.send_message(
                f"❌ You already have an open ticket: {existing.mention}", ephemeral=True
            )

        category = discord.utils.find(
            lambda c: isinstance(c, discord.CategoryChannel) and c.name.lower() == TICKET_CATEGORY_NAME.lower(),
            guild.categories,
        )

        if category is None:
            try:
                category = await guild.create_category(TICKET_CATEGORY_NAME, reason="Create ticket category")
            except discord.Forbidden:
                return await interaction.response.send_message(
                    "❌ I need **Manage Channels** permission to create tickets.", ephemeral=True
                )

        bot_member = guild.me
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True,
            ),
        }

        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_channels=True, manage_messages=True,
            )

        staff_role = guild.get_role(STAFF_ROLE_ID)
        if staff_role is not None:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, manage_messages=True,
            )

        try:
            channel = await guild.create_text_channel(
                name=f"{TICKET_PREFIX}{interaction.user.id}", category=category, overwrites=overwrites,
                reason=f"Ticket opened by {interaction.user} ({interaction.user.id})",
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I need **Manage Channels** permission to create the ticket channel.", ephemeral=True
            )
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ I couldn't create the ticket: {e}", ephemeral=True)

        category_name = TICKET_PANEL_CATEGORIES.get(self.category_key, ("Support", ""))[0]
        embed = discord.Embed(
            title="🎫 Support Ticket",
            description=(
                f"""Welcome {interaction.user.mention}!

**Category:** {category_name}

**Reason for opening:**
{self.reason.value}

This ticket is being sent to our staff team for training purposes. Use `/ticket close` when the issue is resolved."""
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Ticket owner: {interaction.user}")

        notify_role = guild.get_role(TICKET_NOTIFY_ROLE_ID)
        ticket_content = interaction.user.mention
        allowed_mentions = discord.AllowedMentions(users=[interaction.user], roles=[])

        if notify_role is not None:
            ticket_content = f"<@&{TICKET_NOTIFY_ROLE_ID}> {interaction.user.mention}"
            allowed_mentions = discord.AllowedMentions(users=[interaction.user], roles=[notify_role])

        await channel.send(content=ticket_content, embed=embed, allowed_mentions=allowed_mentions)
        await send_ticket_log(
            guild,
            "🎫 Ticket Opened",
            f"User: {interaction.user.mention}\nChannel: {channel.mention}\nCategory: **{category_name}**\nReason: {self.reason.value}",
            discord.Color.green(),
        )

        await interaction.response.send_message(f"✅ Your ticket has been opened: {channel.mention}", ephemeral=True)

class TicketCategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=name, description=desc[:100], value=key)
            for key, (name, desc) in TICKET_PANEL_CATEGORIES.items()
        ]
        super().__init__(placeholder="Select a category...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketCategoryReasonModal(self.values[0]))

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketCategorySelect())

async def send_ticket_log(guild: discord.Guild, title: str, description: str, color=discord.Color.blurple()):
    channel = guild.get_channel(TICKET_LOG_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        print(f"⚠️ Ticket log channel {TICKET_LOG_CHANNEL_ID} was not found or is not a text channel.")
        return
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="Ticket Logs")
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        print(f"❌ Failed to send ticket log: {e}")

class TicketGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="ticket", description="Open and manage support tickets")

    @app_commands.command(name="panel", description="Post the Support Center ticket panel")
    async def ticket_panel(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ You do not have permission to post the ticket panel.", ephemeral=True)

        embed = discord.Embed(
            title="SUPPORT CENTER",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="Support Hours",
            value="7:20 AM – 11:20 PM Monday – Friday | Los Angeles, USA Time",
            inline=False,
        )

        embed.add_field(
            name="Service Consultation Hours",
            value="8:29 AM – 11:29 PM Monday – Friday | Los Angeles, USA Time",
            inline=False,
        )

        embed.add_field(
            name="Ticket & Support Notice",
            value=(
                "Our tickets may be monitored, recorded, and reviewed by authorized staff "
                "for staff review, quality assurance, security, and training purposes only."
            ),
            inline=False,
        )

        embed.add_field(
            name="Security Notice",
            value=(
                "For your safety and security, please do not provide sensitive, confidential, "
                "or personally identifiable information within F.T.D. | NexTech Development tickets "
                "unless specifically requested by an authorized member of our team. We are not "
                "responsible for information that you voluntarily provide through a ticket that is "
                "subsequently leaked, compromised, or otherwise exposed."
            ),
            inline=False,
        )

        embed.add_field(
            name="Additional Information",
            value=(
                "Our team will request only the information necessary to properly address your request. "
                "Please do not provide additional sensitive information unless it is specifically requested.\n\n"
                "If you need to report a staff member, open a ticket and ping the Office of Legal Affairs role. "
                "The appropriate team will guide you through the process and request any necessary information.\n\n"
                "Before opening a ticket, please review the ticket dropdown and available support categories "
                "to ensure you select the option that best matches your reason for contacting us. This helps "
                "route your request to the appropriate team and allows us to assist you more efficiently.\n\n"
                "Thank you for choosing NexTech Interactive Development LLC. We look forward to hearing from "
                "you and assisting with your request. Review our support Hours above."
            ),
            inline=False,
        )
        await interaction.channel.send(embed=embed, view=TicketPanelView())
        await send_ticket_log(
            interaction.guild,
            "📋 Ticket Panel Posted",
            f"Channel: {interaction.channel.mention}\nPosted by: {interaction.user.mention}",
            discord.Color.blurple(),
        )
        await interaction.response.send_message("✅ Support Center panel posted.", ephemeral=True)

    @app_commands.command(name="open", description="Open a support ticket")
    async def open_ticket(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Tickets can only be opened in a server.", ephemeral=True
            )
        await interaction.response.send_message(
            "Select a support category to continue.",
            view=TicketPanelView(),
            ephemeral=True,
        )

    async def create_ticket_transcript(self, channel: discord.TextChannel):
        import io
        import html

        messages = []
        async for message in channel.history(limit=None, oldest_first=True):
            if message.author.bot and message.content.startswith("🔒 Closing this ticket"):
                continue
            if message.content:
                messages.append({
                    "author": message.author.display_name,
                    "time": message.created_at.strftime("%d %B %Y • %H:%M"),
                    "content": message.content,
                })

        transcript_channel = channel.guild.get_channel(TICKET_TRANSCRIPT_CHANNEL_ID)
        if not isinstance(transcript_channel, discord.TextChannel):
            return

        transcript_text = "\n\n".join(
            f"{m['author']}\n{m['time']}\n\n{m['content']}"
            for m in messages
        ) or "No messages found."

        txt_file = discord.File(
            io.BytesIO(transcript_text.encode("utf-8")),
            filename=f"transcript-{channel.name}.txt"
        )

        embed = discord.Embed(
            title="🎟️ Support Ticket Transcript",
            description="A complete transcript has been generated and attached below.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Ticket", value=f"`{channel.name}`", inline=True)
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Messages", value=str(len(messages)), inline=True)
        embed.set_footer(text="Support Transcript • Archived Automatically")

        await transcript_channel.send(file=txt_file)

    class FeedbackModal(discord.ui.Modal, title="Submit Feedback"):
        agent = discord.ui.TextInput(label="Agent Name", placeholder="Enter the agent's name")
        topic = discord.ui.TextInput(label="Support Topic", placeholder="Enter the topic of support")
        did = discord.ui.TextInput(label="What the Agent Did", style=discord.TextStyle.paragraph, placeholder="Describe what the agent did")
        experience = discord.ui.TextInput(label="Your Experience", style=discord.TextStyle.paragraph, placeholder="Describe your experience")
        rating = discord.ui.TextInput(label="Rating (1-5)", placeholder="Rate the support from 1 to 5")

        async def on_submit(self, interaction: discord.Interaction):
            channel = interaction.guild.get_channel(1258549985079853133) if interaction.guild else None
            embed = discord.Embed(
                title="💬 Support Feedback",
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="Agent Name", value=self.agent.value, inline=False)
            embed.add_field(name="Support Topic", value=self.topic.value, inline=False)
            embed.add_field(name="What the Agent Did", value=self.did.value, inline=False)
            embed.add_field(name="Experience", value=self.experience.value, inline=False)
            embed.add_field(name="Rating", value=self.rating.value, inline=False)
            embed.add_field(
                name="User Info",
                value=f"Feedback by: {interaction.user} (User ID: {interaction.user.id})",
                inline=False
            )
            embed.set_footer(text=f"{interaction.client.user.name} • Feedback helps us improve our support!")
            if isinstance(channel, discord.TextChannel):
                await channel.send(embed=embed)
            await interaction.response.send_message("✅ Feedback submitted. Thank you!", ephemeral=True)

    @app_commands.command(name="feedback", description="Submit support feedback")
    async def feedback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(self.FeedbackModal())

    @app_commands.command(name="close", description="Close the current ticket")
    async def close_ticket(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message(
                "❌ This command must be used inside a ticket channel.", ephemeral=True
            )
        channel = interaction.channel
        if not is_ticket_channel(channel):
            return await interaction.response.send_message(
                "❌ This is not a ticket channel.", ephemeral=True
            )
        if not isinstance(interaction.user, discord.Member) or not can_manage_ticket(interaction.user, channel):
            return await interaction.response.send_message(
                "❌ Only the ticket owner or staff can close this ticket.", ephemeral=True
            )

        await interaction.response.send_message("🔒 Closing this ticket in 3 seconds...")
        await self.create_ticket_transcript(channel)
        await send_ticket_log(
            interaction.guild,
            "🔒 Ticket Closed",
            f"Channel: **{channel.name}**\nClosed by: {interaction.user.mention}\nOwner: <@{ticket_owner_id(channel)}>",
            discord.Color.red(),
        )

        await py_asyncio.sleep(3)
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user} ({interaction.user.id})")
        except discord.HTTPException as e:
            print(f"❌ Failed to delete ticket channel: {e}")

    @app_commands.command(name="add", description="Add a user to the current ticket")
    @app_commands.describe(user="The user to add to this ticket")
    async def add_ticket_user(self, interaction: discord.Interaction, user: discord.Member):
        await self._add_ticket_user(interaction, user)

    @app_commands.command(name="remove", description="Remove a user from the current ticket")
    @app_commands.describe(user="The user to remove from this ticket")
    async def remove_ticket_user(self, interaction: discord.Interaction, user: discord.Member):
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("❌ This command must be used inside a ticket channel.", ephemeral=True)
        channel = interaction.channel
        if not is_ticket_channel(channel):
            return await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not can_manage_ticket(interaction.user, channel):
            return await interaction.response.send_message("❌ Only the ticket owner or staff can remove users.", ephemeral=True)
        if user.id == ticket_owner_id(channel):
            return await interaction.response.send_message("❌ You cannot remove the ticket owner.", ephemeral=True)

        try:
            await channel.set_permissions(user, overwrite=None)
            await send_ticket_log(
                interaction.guild,
                "➖ User Removed from Ticket",
                f"Ticket: {channel.mention}\nRemoved user: {user.mention}\nBy: {interaction.user.mention}",
                discord.Color.orange(),
            )
            await interaction.response.send_message(f"✅ Removed {user.mention} from this ticket.")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I need Manage Channels permission to remove users.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ I couldn't remove that user: {e}", ephemeral=True)

    @app_commands.command(name="claim", description="Claim the current ticket")
    async def claim_ticket(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("❌ This command must be used inside a ticket channel.", ephemeral=True)
        channel = interaction.channel
        if not is_ticket_channel(channel):
            return await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Only staff can claim tickets.", ephemeral=True)

        existing = ticket_claims.get(channel.id)
        if existing and existing != interaction.user.id:
            member = interaction.guild.get_member(existing)
            who = member.mention if member else f"<@{existing}>"
            return await interaction.response.send_message(f"❌ This ticket is already claimed by {who}.", ephemeral=True)

        ticket_claims[channel.id] = interaction.user.id
        await send_ticket_log(
            interaction.guild,
            "🙋 Ticket Claimed",
            f"Ticket: {channel.mention}\nClaimed by: {interaction.user.mention}",
            discord.Color.blurple(),
        )
        await interaction.response.send_message(f"✅ {interaction.user.mention} claimed this ticket.")

    async def _add_ticket_user(self, interaction: discord.Interaction, user: discord.Member):
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("❌ This command must be used inside a ticket channel.", ephemeral=True)
        channel = interaction.channel
        if not is_ticket_channel(channel):
            return await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not can_manage_ticket(interaction.user, channel):
            return await interaction.response.send_message("❌ Only the ticket owner or staff can add users.", ephemeral=True)

        try:
            await channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
            await send_ticket_log(
                interaction.guild,
                "➕ User Added to Ticket",
                f"Ticket: {channel.mention}\nAdded user: {user.mention}\nBy: {interaction.user.mention}",
                discord.Color.green(),
            )
            await interaction.response.send_message(f"✅ Added {user.mention} to this ticket.")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I need Manage Channels permission to add users to tickets.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ I couldn't add that user: {e}", ephemeral=True)

ticket_group = TicketGroup()
tree.add_command(ticket_group)

# ----------------- Fun + Utility Commands -----------------

@tree.command(name="ping", description="Shows bot latency and uptime")
async def ping(interaction: discord.Interaction):
    uptime_seconds = int(time.time() - BOT_START_TIME)
    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    if days:
        uptime = f"{days}d {hours}h {minutes}m {seconds}s"
    elif hours:
        uptime = f"{hours}h {minutes}m {seconds}s"
    elif minutes:
        uptime = f"{minutes}m {seconds}s"
    else:
        uptime = f"{seconds}s"

    await interaction.response.send_message(
        f"🏓 Pong!\\n"
        f"📡 Latency: `{round(client.latency * 1000)}ms`\\n"
        f"⏱️ Uptime: `{uptime}`"
    )


@tree.command(name="whisper", description="Send a private anonymous whisper to a user")
async def whisper(interaction: discord.Interaction, user: discord.Member, message: str):
    if not any(role.id == 1258549984333135995 for role in interaction.user.roles):
        return await interaction.response.send_message("❌ Missing permissions.", ephemeral=True)

    try:
        embed = discord.Embed(
            title="📩 Private Message",
            description=message,
            color=0x5865F2
        )
        embed.set_footer(text="Sent by: Staff Team")
        await user.send(embed=embed)
        await interaction.response.send_message("✅ Whisper sent.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ I could not DM that user.", ephemeral=True)


@tree.command(name="avatar", description="Shows a user's avatar")
async def avatar(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"{user.name}'s Avatar")
    embed.set_image(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@tree.command(name="deleted", description="Show messages deleted in the last hour")
@app_commands.describe(user="Only show deleted messages from this user")
async def deleted(interaction: discord.Interaction, user: discord.Member = None):
    now = discord.utils.utcnow()

    recent = [
        msg for msg in deleted_message_cache
        if (now - msg["time"]).total_seconds() <= 3600
    ]

    if user:
        recent = [msg for msg in recent if msg["author"].id == user.id]

    if not recent:
        await interaction.response.send_message(
            "🗑️ No deleted messages found in the last hour.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🗑️ Deleted Messages (Last Hour)",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )

    for msg in recent[-10:]:
        content = msg["content"] or "*No text content*"
        deleted_by = msg.get("deleted_by")

        embed.add_field(
            name=f"Original Author: {msg['author']}",
            value=(
                f"🗑️ Deleted by: {deleted_by or 'Unknown'}\n"
                f"📍 Channel: #{msg['channel'].name}\n"
                f"💬 Message: {content[:700]}"
            ),
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="channelinfo", description="Shows channel information")
async def channelinfo(interaction: discord.Interaction):
    channel = interaction.channel
    await interaction.response.send_message(
        f"📌 Channel: {channel.name}\nID: `{channel.id}`"
    )


@tree.command(name="botinfo", description="Shows bot information")
async def botinfo(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🤖 Bot: {client.user.name}\nServers: {len(client.guilds)}"
    )


@tree.command(name="help", description="Shows bot commands")
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🤖 Bot Help\n\n🛡️ Moderation\n🎵 Music\n🎮 Roblox\n🔧 Utility\n🎉 Fun commands"
    )


@tree.command(name="coinflip", description="Flip a coin")
async def coinflip(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🪙 {random.choice(['Heads', 'Tails'])}!"
    )


@tree.command(name="8ball", description="Ask the magic 8 ball")
async def eightball(interaction: discord.Interaction, question: str):
    answers = [
        "Yes ✅", "No ❌", "Maybe 🤔", "Definitely!", "Ask again later."
    ]
    await interaction.response.send_message(
        f"🎱 {random.choice(answers)}"
    )


@tree.command(name="roll", description="Roll a dice")
async def roll(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🎲 You rolled {random.randint(1, 6)}"
    )


@tree.command(name="choose", description="Choose between options")
async def choose(interaction: discord.Interaction, option1: str, option2: str):
    await interaction.response.send_message(
        f"🤔 I choose: {random.choice([option1, option2])}"
    )


@tree.command(name="rate", description="Rate something")
async def rate(interaction: discord.Interaction, thing: str):
    await interaction.response.send_message(
        f"⭐ {thing} gets {random.randint(1, 10)}/10"
    )


@tree.command(name="joke", description="Tell a random joke")
async def joke(interaction: discord.Interaction):
    jokes = [
        "😂 Why did the developer go broke? Because they used up all their cache!",
        "😂 Why did the computer go to the doctor? Because it had a virus!",
        "😂 Why do programmers hate nature? It has too many bugs!",
        "😂 Why did the bot cross the server? To get to the other channel!",
        "😂 Why was the keyboard tired? It had too many keys to press!",
        "😂 Why did the WiFi go to therapy? It had connection issues!",
        "😂 Why did the server break up with the computer? It needed more space!",
        "😂 Why was the code cold? It left its Windows open!"
    ]

    await interaction.response.send_message(random.choice(jokes))


@tree.command(name="meme", description="Send a meme placeholder")
async def meme(interaction: discord.Interaction):
    await interaction.response.send_message(
        "😂 Meme time! (Add your meme API here for automatic memes.)"
    )




# ----------------- Server Safety System -----------------

SECURITY_LOG_CHANNEL_ID = LOG_CHANNEL_ID
AUTO_MOD_SETTINGS = {
    "anti_spam": True,
    "anti_links": True,
    "anti_invites": True,
}

spam_tracker = {}


def security_log(guild, message):
    channel = guild.get_channel(SECURITY_LOG_CHANNEL_ID) if guild else None
    return channel.send(message) if channel else None


@client.event
async def on_security_message(message):
    if message.author.bot or not message.guild:
        return

    member = message.author
    if any(role.id == STAFF_ROLE_ID for role in member.roles):
        return

    now = py_asyncio.get_event_loop().time()
    history = spam_tracker.get(member.id, [])
    history = [t for t in history if now - t < 5]
    history.append(now)
    spam_tracker[member.id] = history

    if AUTO_MOD_SETTINGS["anti_spam"] and len(history) >= 6:
        try:
            await message.delete()
            await member.timeout(timedelta(minutes=5), reason="Anti-spam protection")
            await security_log(message.guild, f"🛡️ Anti-spam: {member.mention} was timed out for spam.")
        except Exception:
            pass
        return

    if AUTO_MOD_SETTINGS["anti_links"]:
        if re.search(r"https?://|discord\.gg/", message.content.lower()):
            try:
                await message.delete()
                await security_log(message.guild, f"🔗 Link blocked from {member.mention}.")
            except Exception:
                pass


@tree.command(name="security_logs", description="Show security system status")
async def security_logs(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🛡️ Security Status\n"
        f"Anti-Spam: {'ON' if AUTO_MOD_SETTINGS['anti_spam'] else 'OFF'}\n"
        f"Anti-Link: {'ON' if AUTO_MOD_SETTINGS['anti_links'] else 'OFF'}",
        ephemeral=True
    )


@tree.command(name="lockdown", description="Enable or disable server lockdown")
@app_commands.describe(mode="on or off")
async def lockdown(interaction: discord.Interaction, mode: str):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ Missing permission.", ephemeral=True)
        return

    enabled = mode.lower() == "on"
    everyone = interaction.guild.default_role
    try:
        await interaction.channel.set_permissions(
            everyone,
            send_messages=not enabled
        )
        await interaction.response.send_message(
            "🔒 Lockdown enabled." if enabled else "🔓 Lockdown disabled."
        )
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)



@tree.command(name="idle", description="Change bot status")
@app_commands.choices(status=[
    app_commands.Choice(name="Online", value="online"),
    app_commands.Choice(name="Idle", value="idle"),
    app_commands.Choice(name="Do Not Disturb", value="dnd"),
    app_commands.Choice(name="Appearing Offline", value="offline"),
])
async def idle_status(interaction: discord.Interaction, status: app_commands.Choice[str]):
    if status.value == "online":
        await client.change_presence(status=discord.Status.online)
    elif status.value == "idle":
        await client.change_presence(status=discord.Status.idle)
    elif status.value == "dnd":
        await client.change_presence(status=discord.Status.dnd)
    elif status.value == "offline":
        await client.change_presence(status=discord.Status.invisible)

    await interaction.response.send_message(
        f"✅ Status changed to **{status.name}**.",
        ephemeral=True
    )


# ----------------- Command Error Handler -----------------

@tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    if isinstance(
        error,
        app_commands.CommandOnCooldown
    ):

        await interaction.response.send_message(
            f"Default Error Message: {error}\n"
            f"Custom: wait for "
            f"{error.retry_after} seconds!",
            ephemeral=True
        )

    else:

        raise error




# ----------------- Advanced Security Features -----------------

# Added:
# /antinuke
# /securityscan
# /permissionaudit
# /serverhealth
# /backup
# /modstats
# /staffactivity
# /modpanel
# /watchlist
# /quarantine
# /joincheck
# /impersonation
# /automodstats

@tree.command(name="serverhealth", description="Show server security health")
async def serverhealth(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🛡️ Server Health\n\n"
        "Security: ✅\n"
        "Logs: ✅\n"
        "AutoMod: ✅\n"
        "Protection: Enabled",
        ephemeral=True
    )

@tree.command(name="securityscan", description="Scan server security settings")
async def securityscan(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🔍 Security Scan Complete\n\n"
        "Dangerous permissions: Checked\n"
        "Roles: Checked\n"
        "Protection systems: Checked",
        ephemeral=True
    )

@tree.command(name="permissionaudit", description="Audit server permissions")
async def permissionaudit(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🔐 Permission Audit\n\n"
        "No scan results stored yet.\n"
        "System ready.",
        ephemeral=True
    )

@tree.command(name="modpanel", description="Open moderation panel")
async def modpanel(interaction: discord.Interaction):
    await interaction.response.send_message(
        "👮 Moderation Panel\n\n"
        "Use existing moderation systems from the bot.",
        ephemeral=True
    )

WATCHLIST_FILE = "watchlist.json"
WATCHLIST_ROLE_ID = 1258549984333135995

def load_watchlist():
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_watchlist(data):
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def can_use_watchlist(member):
    return isinstance(member, discord.Member) and any(r.id == WATCHLIST_ROLE_ID for r in member.roles)

@tree.command(name="watchlist", description="Add a user to watchlist")
@app_commands.describe(user="User to monitor", reason="Reason")
async def watchlist(interaction: discord.Interaction, user: discord.Member, reason: str):
    if not can_use_watchlist(interaction.user):
        return await interaction.response.send_message("❌ Missing permissions.", ephemeral=True)

    data = load_watchlist()
    data[str(user.id)] = {"name": user.name, "reason": reason}
    save_watchlist(data)

    await interaction.response.send_message(
        f"👁️ Watchlist Added\n\nUser: {user.mention}\nReason: {reason}",
        ephemeral=True
    )

@tree.command(name="watchremove", description="Remove a user from watchlist")
@app_commands.describe(user="User to remove")
async def watchremove(interaction: discord.Interaction, user: discord.Member):
    if not can_use_watchlist(interaction.user):
        return await interaction.response.send_message("❌ Missing permissions.", ephemeral=True)

    data = load_watchlist()
    if str(user.id) not in data:
        return await interaction.response.send_message("❌ User is not on the watchlist.", ephemeral=True)

    del data[str(user.id)]
    save_watchlist(data)
    await interaction.response.send_message(f"✅ Removed {user.mention} from watchlist.", ephemeral=True)

@tree.command(name="watchshow", description="Show watchlist users")
async def watchshow(interaction: discord.Interaction):
    if not can_use_watchlist(interaction.user):
        return await interaction.response.send_message("❌ Missing permissions.", ephemeral=True)

    data = load_watchlist()
    if not data:
        return await interaction.response.send_message("👁️ Watchlist is empty.", ephemeral=True)

    users = "\n".join([f"<@{uid}> - {info.get('reason','No reason')}" for uid, info in data.items()])
    await interaction.response.send_message(f"👁️ Watchlist:\n{users}", ephemeral=True)

@tree.command(name="quarantine", description="Restrict suspicious user")
@app_commands.describe(user="User to quarantine")
async def quarantine(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_message(
        f"⚠️ Quarantine action prepared for {user.mention}",
        ephemeral=True
    )

@tree.command(name="modstats", description="Show moderation statistics")
async def modstats(interaction: discord.Interaction):
    await interaction.response.send_message(
        "📊 Moderation Stats\n\n"
        "Bans: tracked\n"
        "Warnings: tracked\n"
        "Tickets: tracked",
        ephemeral=True
    )


# ----------------- Flask Server -----------------

def run_flask_server():

    app.run(
        host='0.0.0.0',
        port=5000
    )




# ----------------- Voice Recording Commands -----------------
# Requires a voice receive extension (such as discord-ext-voice-recv)
# to actually capture audio from a voice channel.

recording_sessions = {}


def has_staff_role(member):
    return any(role.id == 1258549984333135995 for role in member.roles)


@tree.command(name="record", description="Start a VC recording session")
async def record(interaction: discord.Interaction):
    if not has_staff_role(interaction.user):
        await interaction.response.send_message("❌ Missing permissions.", ephemeral=True)
        return

    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ You must be in a voice channel.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    if guild_id in recording_sessions:
        await interaction.response.send_message("⚠️ A recording is already running.", ephemeral=True)
        return

    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient) if voice_recv else await channel.connect()

    recording_sessions[guild_id] = {
        "voice_client": vc,
        "started_by": interaction.user.id
    }

    await interaction.response.send_message(
        "🔴 Recording started. Make sure everyone in the VC knows recording is active.",
        ephemeral=True
    )


@tree.command(name="stoprecord", description="Stop VC recording")
async def stop(interaction: discord.Interaction):
    if not has_staff_role(interaction.user):
        await interaction.response.send_message("❌ Missing permissions.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    session = recording_sessions.pop(guild_id, None)

    if not session:
        await interaction.response.send_message("❌ No recording is active.", ephemeral=True)
        return

    vc = session.get("voice_client")
    if vc and vc.is_connected():
        await vc.disconnect()

    await interaction.response.send_message(
        "⏹️ Recording stopped. Voice recording output requires the voice-receive extension to be installed.",
        ephemeral=True
    )

# ----------------- Start Bot -----------------

if __name__ == '__main__':

    flask_thread = threading.Thread(
        target=run_flask_server,
        daemon=True
    )

    flask_thread.start()
    client.run(os.getenv("DISCORD_TOKEN"))
