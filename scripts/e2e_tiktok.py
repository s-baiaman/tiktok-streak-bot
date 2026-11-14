"""Manual live checks against a real TikTok account.

The sessionid is read from a file so it never lands in shell history:

    python scripts/e2e_tiktok.py --sessionid-file PATH whoami
    python scripts/e2e_tiktok.py --sessionid-file PATH region
    python scripts/e2e_tiktok.py --sessionid-file PATH chats
    python scripts/e2e_tiktok.py --sessionid-file PATH profile USERNAME
    python scripts/e2e_tiktok.py --sessionid-file PATH send USERNAME TEXT   (asks to confirm)
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from streakbot.parsing import normalize_username, parse_sessionid
from streakbot.tiktok.client import TikTokClient
from streakbot.tiktok.cookies import build_cookies


async def run(args: argparse.Namespace) -> None:
    sessionid = parse_sessionid(Path(args.sessionid_file).read_text())
    client = TikTokClient()
    try:
        if args.command == "region":
            print(await client.detect_region(sessionid))
            return
        cookies = build_cookies(sessionid, args.region)
        if args.command == "whoami":
            print(await client.whoami(cookies))
        elif args.command == "chats":
            for p in await client.list_recent_chats(cookies):
                print(f"{p.display_name} (@{p.username})")
        elif args.command == "profile":
            print(await client.lookup_profile(cookies, normalize_username(args.username)))
        elif args.command == "send":
            username = normalize_username(args.username)
            answer = input(f"About to send {args.text!r} to @{username} — type the username again to confirm: ")
            if normalize_username(answer) != username:
                print("Aborted: username did not match.")
                return
            results, refreshed = await client.send_messages(cookies, [username], args.text)
            print(results, f"(refreshed cookies: {len(refreshed)})")
    finally:
        await client.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sessionid-file", required=True)
    parser.add_argument("--region", default="useast5")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("whoami")
    sub.add_parser("region")
    sub.add_parser("chats")
    sub.add_parser("profile").add_argument("username")
    send = sub.add_parser("send")
    send.add_argument("username")
    send.add_argument("text")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
