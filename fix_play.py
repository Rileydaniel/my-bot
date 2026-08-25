#!/usr/bin/env python3
import re

with open('main.py', 'r') as f:
    content = f.read()

# Find and replace the check that requires user to be in voice
old_check = '''    if interaction.user.voice is None or interaction.user.voice.channel is None:
        return await interaction.response.send_message(
            "❌ Join a voice channel first.",
            ephemeral=True
        )

    target_channel = interaction.user.voice.channel'''

new_check = '''    # Get the user's voice channel, or use the configured default
    if interaction.user.voice is None or interaction.user.voice.channel is None:
        # Try to use the configured VOICE_CHANNEL_ID as fallback
        target_channel = client.get_channel(VOICE_CHANNEL_ID)
        if target_channel is None:
            return await interaction.response.send_message(
                "❌ Join a voice channel first, or configure VOICE_CHANNEL_ID.",
                ephemeral=True
            )
    else:
        target_channel = interaction.user.voice.channel'''

content = content.replace(old_check, new_check)

with open('main.py', 'w') as f:
    f.write(content)

print("✅ Fixed /play command to support default voice channel")

