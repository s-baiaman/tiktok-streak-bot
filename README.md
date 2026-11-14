<p align="center"><img src="streakbot/web/static/logo.png" alt="" width="120"></p>

# TikTok Streak Panel

A self-hosted web panel that keeps TikTok streaks alive. You add "bots" (each one is a logged-in TikTok
account), choose who each bot messages, what it says and when, and the panel sends it every day.
Everything happens on one password-protected page on your home network.

TikTok has no public API for direct messages, so the panel drives a real (headless) Firefox browser
against tiktok.com.

- **One page, live:** see every bot, whose account it sends from, who it messages and when; changes save instantly
- **Test send** to check a bot can actually send before you rely on it
- **No TikTok passwords, phone numbers or QR codes:** you add an account by pasting one cookie
- **Home network only:** nothing is exposed to the internet

> ⚠️ Unofficial project. Automating TikTok may break TikTok's terms of service. Use at your own risk.

---

## Requirements

- A Linux server (Debian 12 or Ubuntu 22.04+ recommended) with internet access
- Root or sudo access
- **Proxmox LXC containers:** Docker needs nesting. On the Proxmox host run
  `pct set <CTID> --features nesting=1,keyctl=1 && pct reboot <CTID>`.
  The installer detects this and prints the command for you.

Everything else (Docker, Compose, git, a browser) is installed by the script.

## Install

```bash
git clone https://github.com/s-baiaman/tiktok-streak-bot
cd tiktok-streak-bot
./install.sh
```

The installer asks for:

1. A **panel password** (at least 8 characters). Only a hash of it is stored.
2. The **port** for the panel (default 8080)
3. A **default timezone** for new bots

It then builds and starts the panel in Docker and prints the address, e.g. `http://192.168.1.50:8080`.
Open it from any device on your home network and log in. The panel restarts automatically after
crashes and reboots.

**Don't forward the port on your router.** The panel uses plain HTTP and is meant for your home
network only. If you want it from outside, put it behind a VPN such as WireGuard or Tailscale.

## Adding a bot

Click **Add bot** and paste the **`sessionid`** cookie of the TikTok account that should send the
messages. It can be your account or a friend's, as long as they give you their cookie.

### Getting the `sessionid` cookie

On a computer:

1. Open [tiktok.com](https://www.tiktok.com) and make sure you're logged in.
2. Press **F12** (or right-click → Inspect).
3. **Chrome/Edge:** Application tab → Cookies → `https://www.tiktok.com`
   **Firefox:** Storage tab → Cookies → `https://www.tiktok.com`
4. Find the row named **`sessionid`** and copy its **Value**. Pasting the whole cookie line works too.

**Don't log out of TikTok in that browser afterwards**: logging out kills the cookie. If a bot's login
stops working later, its dot turns red and the panel says **Login expired**. Click **Update login** and
paste a fresh `sessionid`. Adding the same account again also refreshes its login.

### Setting it up

Select the bot on the left. On the right you can change:

- **Message:** what gets sent (default 🔥)
- **Send time** and **timezone:** when it goes out every day. Changing the time takes effect
  immediately: change 20:00 to 19:30 at 19:00 and it sends at 19:30 today.
- **Recipients:** click **Show recent chats** and tick people, or type an @username or profile link and
  click **Add**. The panel checks the account exists first. Display names aren't unique, so the panel
  always shows the @username too.

Buttons:

| Button | What it does |
|---|---|
| **Test send** | Sends one message (default "test") to one person right away and shows whether it worked. Doesn't count as the day's send. |
| **Send now** | Sends the daily message to everyone right away. The scheduled send still happens. |
| **Pause / Resume** | Stops or restarts the daily sends. |
| **Update login** | Paste a fresh `sessionid` for this account. |
| **Delete bot** | Removes the bot, its saved login and its recipients. |

The panel updates live. While a bot is sending its dot pulses, and the result ("Sent 3/3", or who
failed and why) appears without refreshing. A few hours before each send the panel checks the login
still works. When TikTok reveals when a login expires, the panel warns you about a week ahead.

## The region cookie (`tt-target-idc`)

TikTok's chat only loads when the browser also has a **`tt-target-idc`** cookie, which says which of
TikTok's data centers the account lives on (e.g. `useast5`). You only paste `sessionid`; the panel
works out `tt-target-idc` itself by trying known data centers until the inbox opens.

If an account is on a data center the panel doesn't know (more likely outside the US), the Add bot
form asks you to also paste **`tt-target-idc`**. It sits in the same cookie list as `sessionid`.
If sends for that bot fail with "TikTok's page changed", check the logs for `initSdkCore failed`;
that means the region value is wrong.

## Privacy & trust

- Saved TikTok logins are **encrypted on disk**. The key is `data/secret.key`, stored **next to the
  database**, so a copy of the whole `data/` folder can still be decrypted. If you back up `data/`,
  keep `secret.key` out of that backup (or store the backup somewhere only you can read). If
  `secret.key` is ever lost, every bot needs **Update login**.
- **Whoever runs the panel can use every added account's TikTok session**, because the panel needs
  the sessions to send messages. Only give your cookie to someone you trust.
- Cookie values and recipient usernames are never written to logs.
- The panel password is stored only as a scrypt hash in `.env`. Logging in sets a cookie that lasts
  30 days; changing the password logs out every browser. After 10 wrong passwords from one device,
  logins from it are blocked for 15 minutes.

## Updating and changing the password

```bash
cd tiktok-streak-bot
./install.sh      # 1) Update   2) Change the password or settings
```

## Troubleshooting

| Problem | Fix |
|---|---|
| A bot shows **Login expired** | Copy a fresh `sessionid` from that account and click **Update login**. |
| "TikTok's page changed" in a send result | TikTok changed its website. Update (`./install.sh` → 1). If it persists, the selectors in `streakbot/tiktok/client.py` need updating. |
| Inbox never loads / `initSdkCore failed` in logs | Wrong region. Update the login and paste `tt-target-idc` when asked. |
| Can't open the panel | Check `docker compose ps`, and that you're using the server's LAN address and the port you chose. |
| Forgot the panel password | Run `./install.sh` → 2 and set a new one. |
| Docker fails inside Proxmox LXC | Enable nesting: `pct set <CTID> --features nesting=1,keyctl=1 && pct reboot <CTID>`. |
| See what's happening | `docker compose logs -f` |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/python scripts/dev_panel.py     # the panel with a fake TikTok: http://127.0.0.1:8080, password "dev"
```

In the dev panel, a `sessionid` starting with `bad` is rejected, one starting with `region` asks for
the region cookie, and usernames containing `no` don't exist.

`scripts/e2e_tiktok.py` runs live checks against a real account (`whoami`, `region`, `chats`,
`profile`, `send`). It reads the `sessionid` from a file and asks you to confirm before any real send.
