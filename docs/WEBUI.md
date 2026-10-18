# magbot web UI

A full Stoat client that drives the **magbot** bot account — servers, channels, DMs,
group chats, live messages — served behind a reverse proxy (the original deployment used nginx + a Cloudflare tunnel).

* URL: **https://<your-domain>/bot/**  (password protected; password lives in
  `/opt/stoat-bot/webui/config.json`, generated on first start)
* Service: `stoat-webui.service` (systemd, enabled, `Restart=always`), listens on
  `127.0.0.1:8099`
* Files: `/opt/stoat-bot/webui/{server.py,static/}`
* Logs: `journalctl -u stoat-webui -f`
* nginx: `/etc/nginx/sites-available/<site>` → `location /bot/` proxies to 8099
  with websocket upgrade.

## Architecture

```
browser  ──https──> (tunnel / TLS proxy) ──> nginx :80 ──/bot/──> aiohttp :8099
                                                                 ├─ /api/*        REST  -> api.stoat.chat/0.8  (X-Bot-Token)
                                                                 ├─ /api/upload   files -> cdn.stoatusercontent.com/attachments
                                                                 └─ /api/ws       live  -> wss://events.stoat.chat (own session)
```

The backend keeps its own gateway connection and a mirror of the Ready state
(servers, channels, members, users, emoji, voice, unreads); browsers get an initial
snapshot from `GET /api/state` and then a stream of raw gateway events over
`/api/ws`. `bot.py` keeps its own separate session — concurrent sessions are fine.

Frontend is plain ES2020, no build step: `static/{index.html,style.css}` +
`static/js/{md,state,ui,app}.js` (helpers/state-reducer/renderers/glue).

The frontend mirror covers everything the bot can see; messages you send from the UI go
out as magbot. `bot.py` keeps its own separate session — **concurrent sessions work**
(verified live: the gateway fans events out to every session of the token, so the bot
daemon, its AI bridge and this UI all run at once; the bot keeps answering people
normally while you use the UI, and its replies show up in the UI immediately). One
consequence worth knowing: `bot.py` deliberately ignores messages authored by itself, so
**messages you type in the UI do not trigger the AI** — that guard is what stops a bot
from talking to itself. The engine pill exists so you can always see that the daemon is
still alive behind the UI.

## Deploying changes

Cloudflare caches `.js`/`.css` at the edge (its Browser Cache TTL overrides the origin's
headers), so **bump the `?v=` query on the assets in `static/index.html` every deploy** —
otherwise browsers keep the old bundle for up to 4 hours. HTML itself is served
`no-store`, so a fresh page load always picks up the new asset URLs.

```bash
# from the box that holds the working copy:
cd <checkout> && tar --exclude 'config.json' -czf /tmp/webui.tgz webui
scp /tmp/webui.tgz root@<vps>:/tmp/ && ssh root@<vps> \
  'tar xzf /tmp/webui.tgz -C /opt/stoat-bot && systemctl restart stoat-webui'
```

## What works

* server rail with unread + mention pills, channel sidebar with categories, voice
  channels and who is in them, DM / group / Saved Messages list
* message history (paginated, infinite scroll up), live messages, edits, deletes,
  bulk deletes, reactions (unicode + custom emoji), replies, attachments
  (image / video / audio / file, lightbox), embeds, day dividers, grouping,
  role colours, nicknames, mentions, markdown, code blocks, custom emoji
* composer: Enter to send, drafts per channel, **@-mention picker** (type `@` — members
  of the current server / recipients of a DM-GC, `↑`/`↓` + `Enter` or `Tab`, `Esc` to
  dismiss; the displayed `@Name` is converted to Stoat's `<@ULID>` on send, and the
  server then derives the real mention list. Opening an existing message in the editor
  turns `<@ULID>` back into `@Name`), file upload (drag & drop, paste,
  picker), emoji picker (1360+ unicode + server emoji), edit in place, reply bar,
  2000-char counter, drag-drop overlay
* "magbot engine" pill in the account bar: polls `/api/botstatus` and shows whether the
  bot daemon + AI bridge are still running *while this UI is open* (green = both up,
  amber = engine only, red = engine gone). Hovering it shows the PIDs and uptimes.
* member list grouped by hoisted roles/online, profile cards (bio, roles, joined),
  moderation actions shown **only when magbot actually holds the permission**
* server menu (info, create channel, emoji, roles, bans, settings), channel menu
  (edit, permissions editor with the full bit list, delete, leave group),
  quick switcher (Ctrl/⌘+K), bot settings (display name, pronouns, status text,
  presence, avatar, bio, background), desktop notifications on mention/DM
* responsive/mobile layout with drawer navigation

## What Stoat blocks for bots (verified against the API, not guessed)

| feature | result |
| --- | --- |
| create invites | `400 IsBot` — hardcoded in `delta` before any permission check |
| message search (`POST /channels/{id}/search`) | `400 IsBot` |
| typing indicator | route removed (`404`) |
| ack / read receipts (`PUT .../ack`, `PUT /servers/{id}/ack`) | `400 IsBot` — unread is tracked client-side only |
| create group chat (`POST /channels/create`) | `400 IsBot` |
| pin / clear reactions | needs `ManageMessages`; works only where magbot has it |
| bans list | needs `BanMembers` |
| default avatar | served as a PNG, so the backend proxies the bytes |

Channel-level permission overwrites are only returned by `GET /channels/{id}` when
the caller may manage the channel; elsewhere the UI falls back to server-level
permissions. The permission maths uses BigInt because Stoat's bits run past 31.

## Operations

```bash
systemctl status stoat-webui          # is it up
systemctl restart stoat-webui         # after editing files
journalctl -u stoat-webui -n 50       # logs (incl. the generated password on first run)
/opt/stoat-bot/venv/bin/python /opt/stoat-bot/webui/server.py   # run in foreground
```

Rotate the password: delete `webui/config.json` and restart; a new password is generated
and logged once (this also rotates the session secret, so every device is signed out).

