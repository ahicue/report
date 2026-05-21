#!/usr/bin/env python3
"""Listen for Discord commands and trigger paper pushes."""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import threading

import discord

import paper_robot


DEFAULT_CONFIG = "config.json"
DEFAULT_ENV = ".env"
DEFAULT_SEEN = str(paper_robot.DEFAULT_SEEN_PATH)


def push_next_paper(config_path: str, seen_path: str) -> str:
    config = paper_robot.read_json(pathlib.Path(config_path))
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        return "DISCORD_WEBHOOK_URL is missing."

    seen_file = pathlib.Path(seen_path)
    seen = paper_robot.load_seen(seen_file)
    papers = paper_robot.collect_new_papers(config, seen)
    if not papers:
        return "没有找到新的候选论文。"

    for paper in papers:
        paper_robot.post_to_discord(webhook_url, paper_robot.discord_payload(paper, config), dry_run=False)
        seen.add(paper["id"])
    paper_robot.save_seen(seen_file, seen)
    return f"已推送 {len(papers)} 篇新论文。"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Listen for '继续' in Discord and push the next paper.")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to config JSON.")
    parser.add_argument("--seen", default=DEFAULT_SEEN, help="Path to seen-paper state.")
    parser.add_argument("--env", default=DEFAULT_ENV, help="Path to .env file.")
    args = parser.parse_args()

    paper_robot.load_dotenv(pathlib.Path(args.env))
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    allowed_channel = os.environ.get("DISCORD_COMMAND_CHANNEL_ID", "").strip()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is required.")

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    lock = asyncio.Lock()

    @client.event
    async def on_ready() -> None:
        print(f"Logged in as {client.user}. Listening for 继续.")

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        if allowed_channel and str(message.channel.id) != allowed_channel:
            return
        if message.content.strip() != "继续":
            return

        async with lock:
            await message.add_reaction("👀")
            result = await asyncio.to_thread(push_next_paper, args.config, args.seen)
            await message.channel.send(result)

    stop_event = threading.Event()
    try:
        await client.start(token)
    finally:
        stop_event.set()


if __name__ == "__main__":
    asyncio.run(main())
