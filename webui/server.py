#!/usr/bin/env python3
"""
magbot web UI — a normal-account Stoat client for the magbot bot token.

Backend: aiohttp only (already in /opt/stoat-bot/venv).
  * REST  -> https://api.stoat.chat/0.8   (X-Bot-Token)
  * files -> https://cdn.stoatusercontent.com  (Autumn)
  * live  -> wss://events.stoat.chat      (gateway, own connection; the
             running bot.py keeps its own — concurrent sessions are fine)

Serves a small SPA out of ./static and exposes /api/* which the browser calls.
Password-protected: the tunnel publishes this to the open internet.

Run: /opt/stoat-bot/venv/bin/python /opt/stoat-bot/webui/server.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import pathlib
import secrets
import time

from aiohttp import ClientSession, ClientTimeout, FormData, WSMsgType, web

HERE = pathlib.Path(__file__).resolve().parent
STATIC = HERE / 'static'
ENV_FILE = pathlib.Path(os.environ.get('ENV_FILE', '/opt/stoat-bot/.env'))
CONF_FILE = HERE / 'config.json'

API = os.environ.get('STOAT_API', 'https://api.stoat.chat/0.8')
CDN = os.environ.get('STOAT_CDN', 'https://cdn.stoatusercontent.com')
GATEWAY = os.environ.get('STOAT_GATEWAY', 'wss://events.stoat.chat/')
PORT = int(os.environ.get('WEBUI_PORT', '8099'))
COOKIE = 'magbot_ui'
SESSION_TTL = 60 * 60 * 24 * 30  # 30 days

log = logging.getLogger('magbot-ui')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

WORDS = ('ember harbor quartz canyon lantern meadow orbit pebble sable timber violet '
         'willow amber basalt cobalt dune falcon garnet indigo jasper kelp lilac maple '
         'nectar onyx paprika quill raven saffron thistle umber velvet walrus zephyr').split()


# --------------------------------------------------------------------------- config
def load_env_token() -> str:
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith('STOAT_BOT_TOKEN='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise SystemExit('STOAT_BOT_TOKEN missing from %s' % ENV_FILE)


def make_password() -> str:
    return '-'.join(secrets.choice(WORDS) for _ in range(3)) + '-%02d' % secrets.randbelow(90)


def load_config() -> dict:
    if CONF_FILE.exists():
        return json.loads(CONF_FILE.read_text())
    pw = make_password()
    conf = {
        'password': pw,                 # shown once here; hash kept below
        'password_salt': secrets.token_hex(16),
        'secret': secrets.token_hex(32),
        'port': PORT,
    }
    conf['password_hash'] = hash_pw(pw, conf['password_salt'])
    save_config(conf)
    log.warning('generated web UI password: %s', pw)
    return conf


def save_config(conf: dict) -> None:
    CONF_FILE.write_text(json.dumps(conf, indent=2))
    os.chmod(CONF_FILE, 0o600)


def hash_pw(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 200_000).hex()


def sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- errors
class ApiError(Exception):
    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload
        super().__init__(str(payload))


def friendly(payload, status: int) -> str:
    """Turn a Stoat error payload into something readable for a human."""
    if isinstance(payload, dict):
        t = payload.get('type')
        if t == 'IsBot':
            return ('Stoat does not allow bots to do this (the platform blocks it for '
                    'bot accounts: %s)' % payload.get('location', ''))
        if t == 'MissingPermission':
            return 'magbot lacks the %s permission here' % payload.get('permission', '?')
        if t == 'NotFound':
            return 'Not found on Stoat (%s)' % payload.get('location', '')
        if t in ('InvalidOperation', 'InvalidProperty', 'InvalidCredentials'):
            return '%s: %s' % (t, payload.get('location', ''))
        if 'error' in payload:
            return str(payload['error'])
        if t:
            return '%s (%s)' % (t, payload.get('location', ''))
    return 'Stoat error (HTTP %s): %s' % (status, str(payload)[:300])


# --------------------------------------------------------------------------- bot
class Bot:
    """Owns the token, the gateway connection and a mirror of the Ready state."""

    def __init__(self, token: str):
        self.token = token
        self.http: ClientSession | None = None
        self.me: dict | None = None
        self.users: dict[str, dict] = {}
        self.servers: dict[str, dict] = {}
        self.channels: dict[str, dict] = {}
        self.members: dict[str, dict[str, dict]] = {}   # server -> user -> member
        self.emojis: dict[str, list] = {}                # server -> [emoji]
        self.voice: dict[str, list] = {}                 # channel -> [state]
        self.dms: list[dict] = []
        self.unreads: list[dict] = []
        self.ready = False
        self.gw_task: asyncio.Task | None = None
        self.ws_clients: set[web.WebSocketResponse] = set()
        self.typing: dict[str, dict[str, float]] = {}    # channel -> user -> ts
        self._seq = 0

    # -- lifecycle ---------------------------------------------------------
    async def start(self):
        self.http = ClientSession(timeout=ClientTimeout(total=60), headers={
            'X-Bot-Token': self.token,
            'User-Agent': 'magbot-webui/1.0',
        })
        self.gw_task = asyncio.create_task(self._gateway_loop())

    async def close(self):
        if self.gw_task:
            self.gw_task.cancel()
        if self.http:
            await self.http.close()

    # -- REST --------------------------------------------------------------
    async def api(self, method: str, path: str, *, body=None, params=None, binary=False):
        url = path if path.startswith('http') else API + path
        async with self.http.request(method, url, json=body, params=params) as r:
            raw = await r.read()
            ctype = r.headers.get('content-type', '')
            if r.status >= 400:
                try:
                    payload = json.loads(raw.decode('utf-8'))
                except Exception:
                    payload = raw[:400].decode('utf-8', 'replace')
                raise ApiError(r.status, payload)
            if not raw:
                return None
            if binary:
                return raw, ctype
            if 'json' in ctype:
                try:
                    return json.loads(raw.decode('utf-8'))
                except Exception:
                    return raw.decode('utf-8', 'replace')
            return raw.decode('utf-8', 'replace')

    async def upload(self, data: bytes, filename: str, ctype: str) -> str:
        form = FormData()
        form.add_field('file', data, filename=filename, content_type=ctype or 'application/octet-stream')
        async with self.http.post(CDN + '/attachments', data=form) as r:
            text = await r.text()
            if r.status >= 400:
                try:
                    payload = json.loads(text)
                except Exception:
                    payload = text[:300]
                raise ApiError(r.status, payload)
            return json.loads(text)['id']

    # -- gateway -----------------------------------------------------------
    async def _gateway_loop(self):
        backoff = 2
        while True:
            try:
                await self._gateway_once()
                backoff = 2
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning('gateway error: %s', e)
            self.ready = False
            self.broadcast({'type': 'GatewayStatus', 'connected': False})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _gateway_once(self):
        async with self.http.ws_connect(GATEWAY, heartbeat=None, max_msg_size=0) as ws:
            await ws.send_json({'type': 'Authenticate', 'token': self.token})
            last = time.time()
            async for msg in ws:
                if msg.type is WSMsgType.TEXT:
                    last = time.time()
                    try:
                        event = json.loads(msg.data)
                    except Exception:
                        continue
                    t = event.get('type')
                    if t == 'Ping':
                        await ws.send_json({'type': 'Pong', 'data': event.get('data')})
                        continue
                    if t == 'Authenticated':
                        log.info('gateway authenticated')
                        continue
                    if t == 'Ready':
                        await self.apply_ready(event)
                        log.info('gateway ready: %d servers, %d channels, %d dms',
                                 len(self.servers), len(self.channels), len(self.dms))
                        self.broadcast({'type': 'GatewayStatus', 'connected': True})
                        continue
                    self.handle_event(event)
                    self.broadcast(event)
                elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                    raise ConnectionError('gateway closed: %s' % msg.type)
                if time.time() - last > 120:
                    raise ConnectionError('gateway silent for 120s')

    async def apply_ready(self, ev: dict):
        self.me = ev.get('me') or ev.get('user') or self.me
        self.users = {u['_id']: u for u in ev.get('users', [])}
        if not self.me:
            # Stoat's Ready does not carry the bot's own user object; fetch it.
            try:
                self.me = await self.api('GET', '/users/@me')
            except Exception as e:
                log.warning('could not fetch /users/@me: %s', e)
        if self.me:
            self.users[self.me['_id']] = self.me
        self.servers = {s['_id']: s for s in ev.get('servers', [])}
        self.channels = {c['_id']: c for c in ev.get('channels', [])}
        self.members = {}
        for m in ev.get('members', []):
            mid = m['_id'] if isinstance(m.get('_id'), dict) else {'server': m.get('server'), 'user': m.get('user')}
            self.members.setdefault(mid['server'], {})[mid['user']] = {
                'user': mid['user'], 'server': mid['server'],
                'joined_at': m.get('joined_at'), 'roles': m.get('roles') or [],
                'nickname': m.get('nickname'), 'timeout': m.get('timeout'),
            }
        self.emojis = {}
        for e in ev.get('emojis', []):
            sid = (e.get('parent') or {}).get('id')
            self.emojis.setdefault(sid, []).append(e)
        self.voice = {}
        for v in ev.get('voice_states', []) or []:
            self.voice.setdefault(v.get('channel_id'), []).append(v)
        self.ready = True

    # -- event -> state ----------------------------------------------------
    def handle_event(self, ev: dict):
        t = ev.get('type')
        try:
            if t == 'Message':
                ch = self.channels.get(ev.get('channel'))
                if ch:
                    ch['last_message_id'] = ev.get('_id')
                if ev.get('user'):
                    self.users.setdefault(ev['user']['_id'], ev['user'])
            elif t == 'MessageUpdate':
                pass
            elif t == 'MessageDelete':
                pass
            elif t == 'ChannelCreate':
                self.channels[ev['_id']] = ev
                srv = self.servers.get(ev.get('server'))
                if srv and ev['_id'] not in (srv.get('channels') or []):
                    srv.setdefault('channels', []).append(ev['_id'])
                if t == 'ChannelCreate' and ev.get('channel_type') == 'Group':
                    self.dms.append(ev)
            elif t == 'ChannelUpdate':
                ch = self.channels.get(ev.get('_id'))
                if ch:
                    ch.update(ev)
                else:
                    self.channels[ev['_id']] = ev
            elif t == 'ChannelDelete':
                self.channels.pop(ev.get('id') or ev.get('_id'), None)
                for srv in self.servers.values():
                    if srv.get('channels'):
                        srv['channels'] = [c for c in srv['channels'] if c != (ev.get('id') or ev.get('_id'))]
                self.dms = [d for d in self.dms if d.get('_id') != (ev.get('id') or ev.get('_id'))]
            elif t in ('ChannelGroupJoin', 'ChannelGroupLeave'):
                gid = ev.get('id')
                ch = self.channels.get(gid)
                uid = ev.get('user')
                if ch is not None:
                    rec = ch.setdefault('recipients', [])
                    if t == 'ChannelGroupJoin' and uid and uid not in rec:
                        rec.append(uid)
                    elif t == 'ChannelGroupLeave' and uid in rec:
                        rec.remove(uid)
                if t == 'ChannelGroupJoin' and uid and uid not in [d.get('_id') for d in self.dms]:
                    pass
            elif t in ('ChannelStartTyping', 'ChannelStopTyping'):
                cid, uid = ev.get('id'), ev.get('user')
                if t == 'ChannelStartTyping':
                    self.typing.setdefault(cid, {})[uid] = time.time()
                else:
                    self.typing.get(cid, {}).pop(uid, None)
            elif t == 'ChannelAck':
                pass
            elif t == 'ServerCreate':
                self.servers[ev['_id']] = ev
                for m in ev.get('members', []) or []:
                    mid = m['_id'] if isinstance(m.get('_id'), dict) else {'server': ev['_id'], 'user': m.get('user')}
                    self.members.setdefault(ev['_id'], {})[mid['user']] = {
                        'user': mid['user'], 'server': ev['_id'], 'joined_at': m.get('joined_at'),
                        'roles': m.get('roles') or [], 'nickname': m.get('nickname')}
                for c in ev.get('channels', []) or []:
                    self.channels[c['_id']] = c
            elif t == 'ServerUpdate':
                srv = self.servers.get(ev.get('id') or ev.get('_id'))
                if srv:
                    data = dict(ev)
                    chans = data.pop('channels', None)
                    srv.update({k: v for k, v in data.items() if k not in ('type', '_id', 'id')})
                    if chans is not None:
                        srv['channels'] = chans
            elif t == 'ServerDelete':
                sid = ev.get('id')
                self.servers.pop(sid, None)
                self.members.pop(sid, None)
                self.emojis.pop(sid, None)
            elif t in ('ServerMemberJoin', 'ServerMemberUpdate', 'ServerMemberRemove'):
                sid = ev.get('id') or (ev.get('_id') or {}).get('server')
                uid = ev.get('user') or (ev.get('_id') or {}).get('user')
                if t == 'ServerMemberRemove':
                    self.members.get(sid, {}).pop(uid, None)
                else:
                    data = dict(ev)
                    data.pop('id', None)
                    mid = data.pop('_id', None)
                    if isinstance(mid, dict):
                        sid, uid = mid.get('server'), mid.get('user')
                    data.pop('type', None)
                    cur = self.members.setdefault(sid, {}).get(uid, {'user': uid, 'server': sid})
                    cur.update(data)
                    self.members[sid][uid] = cur
            elif t == 'ServerRoleUpdate':
                if ev.get('id'):
                    srv = self.servers.get(ev['id'])
                    if srv is not None:
                        srv.setdefault('roles', {})[ev['role_id']] = ev.get('data')
            elif t == 'ServerRoleDelete':
                srv = self.servers.get(ev.get('id'))
                if srv:
                    srv.setdefault('roles', {}).pop(ev.get('role_id'), None)
            elif t == 'ServerRoleRanksUpdate':
                srv = self.servers.get(ev.get('id'))
                if srv:
                    for r in srv.get('roles', {}).values():
                        if r and r.get('_id') in (ev.get('ranks') or {}):
                            r['rank'] = ev['ranks'][r['_id']]
            elif t == 'ServerEmojiCreate':
                sid = (ev.get('parent') or {}).get('id')
                self.emojis.setdefault(sid, []).append(ev)
            elif t == 'ServerEmojiDelete':
                for lst in self.emojis.values():
                    lst[:] = [e for e in lst if e.get('_id') != ev.get('id')]
            elif t == 'UserUpdate':
                uid = ev.get('id')
                u = self.users.get(uid)
                if u:
                    data = dict(ev)
                    data.pop('id', None)
                    data.pop('type', None)
                    u.update(data)
            elif t == 'UserRelationshipUpdate':
                u = self.users.get(ev.get('user_id'))
                if u:
                    u['relationship'] = ev.get('status')
            elif t in ('VoiceChannelJoin',):
                self.voice.setdefault(ev.get('id'), []).append({
                    'channel_id': ev.get('id'), 'user_id': ev.get('user'), 'state': ev.get('state')})
            elif t in ('VoiceChannelLeave',):
                lst = self.voice.get(ev.get('id'), [])
                self.voice[ev.get('id')] = [v for v in lst if v.get('user_id') != ev.get('user')]
            elif t in ('VoiceChannelMove', 'UserMoveVoiceChannel'):
                for cid, lst in self.voice.items():
                    self.voice[cid] = [v for v in lst if v.get('user_id') != ev.get('user')]
                self.voice.setdefault(ev.get('to'), []).append({'channel_id': ev.get('to'), 'user_id': ev.get('user')})
            elif t == 'UserVoiceStateUpdate':
                for lst in self.voice.values():
                    for v in lst:
                        if v.get('user_id') == ev.get('id'):
                            v['state'] = ev.get('state')
        except Exception as e:  # never let a bad event kill the socket
            log.debug('event %s handling failed: %s', t, e)

    # -- browser fanout ----------------------------------------------------
    def broadcast(self, payload: dict):
        if not self.ws_clients:
            return
        self._seq += 1
        msg = json.dumps(payload, separators=(',', ':'))
        for ws in list(self.ws_clients):
            if ws.closed:
                self.ws_clients.discard(ws)
                continue
            asyncio.create_task(self._safe_send(ws, msg))

    @staticmethod
    async def _safe_send(ws, msg):
        try:
            await ws.send_str(msg)
        except Exception:
            pass

    # -- snapshot ----------------------------------------------------------
    @staticmethod
    def _trim_user(u: dict) -> dict:
        return {k: u.get(k) for k in ('_id', 'username', 'discriminator', 'display_name',
                                      'avatar', 'status', 'bot', 'online', 'relationship',
                                      'flags', 'pronouns', 'is_online', 'badges', 'profile')
                if k in u}

    def snapshot(self) -> dict:
        users = {uid: self._trim_user(u) for uid, u in self.users.items()}
        members = {sid: m for sid, m in self.members.items()}
        return {
            'me': self._trim_user(self.me) if self.me else None,
            'users': users,
            'servers': list(self.servers.values()),
            'channels': self.channels,
            'members': members,
            'emojis': self.emojis,
            'voice': self.voice,
            'dms': self.dms,
            'unreads': self.unreads,
            'typing': {c: list(u.keys()) for c, u in self.typing.items()},
            'ready': self.ready,
            'gateway': {'connected': self.ready},
            'instance': {'api': API, 'cdn': CDN, 'gateway': GATEWAY},
        }

    async def refresh_dms(self):
        self.dms = await self.api('GET', '/users/dms') or []

    async def refresh_unreads(self):
        try:
            self.unreads = await self.api('GET', '/sync/unreads') or []
        except ApiError:
            self.unreads = []


# --------------------------------------------------------------------------- web app
def json_response(payload, status=200):
    return web.json_response(payload, status=status, dumps=lambda o: json.dumps(o, separators=(',', ':')))


def make_app(bot: Bot, conf: dict) -> web.Application:
    login_fails: dict[str, list] = {}

    @web.middleware
    async def auth_mw(request: web.Request, handler):
        p = request.path
        open_paths = ('/api/login', '/api/health', '/api/session')
        if p.startswith('/api/') and p not in open_paths:
            if not valid_session(request, conf):
                return json_response({'error': 'unauthorized'}, 401)
        return await handler(request)

    @web.middleware
    async def cache_mw(request, handler):
        """Keep the browser (and Cloudflare's edge) from serving stale UI files.

        Static js/css get `no-cache` (store, but revalidate), HTML gets `no-store`,
        API responses are never cached.
        """
        resp = await handler(request)
        path = request.path
        if path.startswith('/api/'):
            resp.headers.setdefault('Cache-Control', 'no-store')
        elif path.endswith(('.js', '.css')):
            resp.headers.setdefault('Cache-Control', 'no-cache, must-revalidate')
        elif path in ('/', '/index.html') or path.endswith('.html'):
            resp.headers.setdefault('Cache-Control', 'no-store')
        return resp


    @web.middleware
    async def error_mw(request, handler):
        try:
            return await handler(request)
        except ApiError as e:
            return json_response({'error': friendly(e.payload, e.status), 'raw': e.payload}, 400)
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception('unhandled error on %s %s', request.method, request.path)
            return json_response({'error': '%s: %s' % (type(e).__name__, e)}, 500)

    def set_cookie(resp: web.StreamResponse):
        exp = int(time.time()) + SESSION_TTL
        payload = 'magbot.%d' % exp
        val = '%s.%s' % (payload, sign(conf['secret'], payload))
        resp.set_cookie(COOKIE, val, max_age=SESSION_TTL, httponly=True, samesite='Lax',
                        secure=False, path='/')

    app = web.Application(middlewares=[error_mw, auth_mw, cache_mw], client_max_size=64 * 1024 * 1024)
    app['bot'] = bot

    # ---- auth --------------------------------------------------------
    async def health(request):
        return json_response({'ok': True, 'ready': bot.ready, 'password_required': True})

    def _proc_info(needle):
        """Find a running process whose cmdline contains `needle`."""
        for pid in os.listdir('/proc'):
            if not pid.isdigit():
                continue
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as fh:
                    cl = fh.read().decode('utf8', 'replace')
            except OSError:
                continue
            if needle in cl:
                try:
                    with open(f'/proc/{pid}/stat', 'rb') as fh:
                        fields = fh.read().decode('utf8', 'replace').rsplit(')', 1)[1].split()
                    start = float(fields[19]) / 100.0        # clock ticks (USER_HZ)
                    with open('/proc/uptime') as fh:
                        up = float(fh.read().split()[0])
                    age = int(up - start)
                except Exception:
                    age = None
                return {'pid': int(pid), 'age': age}
        return None

    async def botstatus(request):
        """Is the bot daemon + its AI bridge still alive while this UI is open?

        Both run as separate processes on this host, so we can report them
        independently of the gateway connection this web UI holds.
        """
        engine = _proc_info('bot.py')
        bridge = _proc_info('fembot_hermes_bridge.py')
        return json_response({
            'engine': engine,
            'bridge': bridge,
            'gateway': bool(bot.ready),
            'own_ignore': True,   # bot.py ignores its own messages (loop guard)
        })

    async def session(request):
        authed = valid_session(request, conf)
        return json_response({'authenticated': authed,
                              'password_required': True,
                              'ready': bot.ready,
                              'me': Bot._trim_user(bot.me) if (authed and bot.me) else None})

    async def login(request):
        data = await request.json()
        pw = str(data.get('password') or '')
        ip = request.headers.get('CF-Connecting-IP') or request.remote or '?'
        now = time.time()
        hits = [t for t in login_fails.get(ip, []) if now - t < 300]
        if len(hits) >= 8:
            return json_response({'error': 'too many attempts, wait a few minutes'}, 429)
        if not pw or not hmac.compare_digest(hash_pw(pw, conf['password_salt']), conf['password_hash']):
            hits.append(now)
            login_fails[ip] = hits
            await asyncio.sleep(0.6)
            return json_response({'error': 'wrong password'}, 401)
        login_fails.pop(ip, None)
        resp = json_response({'ok': True, 'me': Bot._trim_user(bot.me) if bot.me else None})
        set_cookie(resp)
        return resp

    async def logout(request):
        resp = json_response({'ok': True})
        resp.del_cookie(COOKIE, path='/')
        return resp

    # ---- state -------------------------------------------------------
    async def state(request):
        if not bot.ready:
            for _ in range(60):
                await asyncio.sleep(0.5)
                if bot.ready:
                    break
        await bot.refresh_dms()
        await bot.refresh_unreads()
        return json_response(bot.snapshot())

    async def resync(request):
        await bot.refresh_dms()
        await bot.refresh_unreads()
        return json_response(bot.snapshot())

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=8 * 1024 * 1024)
        await ws.prepare(request)
        bot.ws_clients.add(ws)
        try:
            await ws.send_str(json.dumps({'type': 'GatewayStatus', 'connected': bot.ready}))
            async for msg in ws:
                if msg.type is WSMsgType.TEXT:
                    # only keepalive / client pings come from the browser
                    continue
        finally:
            bot.ws_clients.discard(ws)
        return ws

    # ---- channels / messages ----------------------------------------
    async def history(request):
        cid = request.match_info['cid']
        params = {k: v for k, v in request.query.items() if k in ('limit', 'before', 'after', 'sort', 'nearby')}
        params.setdefault('limit', '50')
        params['include_users'] = 'true'
        data = await bot.api('GET', '/channels/%s/messages' % cid, params=params)
        return json_response(data)

    async def send(request):
        cid = request.match_info['cid']
        body = await request.json()
        payload = {}
        for k in ('content', 'attachments', 'replies', 'embeds', 'masquerade', 'interactions', 'nonce'):
            if body.get(k) not in (None, '', []):
                payload[k] = body[k]
        if not payload.get('content') and not payload.get('attachments') and not payload.get('embeds'):
            return json_response({'error': 'nothing to send'}, 400)
        data = await bot.api('POST', '/channels/%s/messages' % cid, body=payload)
        return json_response(data)

    async def edit(request):
        cid, mid = request.match_info['cid'], request.match_info['mid']
        body = await request.json()
        data = await bot.api('PATCH', '/channels/%s/messages/%s' % (cid, mid), body={'content': body.get('content', '')})
        return json_response(data)

    async def delete(request):
        cid, mid = request.match_info['cid'], request.match_info['mid']
        await bot.api('DELETE', '/channels/%s/messages/%s' % (cid, mid))
        return json_response({'ok': True})

    async def react(request):
        cid, mid, emoji = request.match_info['cid'], request.match_info['mid'], request.match_info['emoji']
        if request.method == 'PUT':
            await bot.api('PUT', '/channels/%s/messages/%s/reactions/%s' % (cid, mid, emoji))
        else:
            await bot.api('DELETE', '/channels/%s/messages/%s/reactions/%s' % (cid, mid, emoji))
        return json_response({'ok': True})

    async def clear_reactions(request):
        cid, mid = request.match_info['cid'], request.match_info['mid']
        await bot.api('DELETE', '/channels/%s/messages/%s/reactions' % (cid, mid))
        return json_response({'ok': True})

    async def pin(request):
        cid, mid = request.match_info['cid'], request.match_info['mid']
        if request.method == 'POST':
            await bot.api('POST', '/channels/%s/messages/%s/pin' % (cid, mid))
        else:
            await bot.api('DELETE', '/channels/%s/messages/%s/pin' % (cid, mid))
        return json_response({'ok': True})

    # ---- channels ----------------------------------------------------
    async def channel_patch(request):
        cid = request.match_info['cid']
        body = await request.json()
        payload = {k: v for k, v in body.items() if k in ('name', 'description', 'nsfw', 'archived',
                                                          'icon', 'category', 'default_permissions', 'role_permissions')}
        data = await bot.api('PATCH', '/channels/%s' % cid, body=payload)
        if isinstance(data, dict):
            bot.channels.setdefault(cid, {}).update(data)
        return json_response(data)

    async def channel_delete(request):
        cid = request.match_info['cid']
        await bot.api('DELETE', '/channels/%s' % cid)
        bot.channels.pop(cid, None)
        bot.dms = [d for d in bot.dms if d.get('_id') != cid]
        for srv in bot.servers.values():
            if srv.get('channels'):
                srv['channels'] = [c for c in srv['channels'] if c != cid]
        return json_response({'ok': True})

    async def channel_get(request):
        """Channel detail (default_permissions / role_permissions are only on REST)."""
        cid = request.match_info['cid']
        data = await bot.api('GET', '/channels/%s' % cid)
        if isinstance(data, dict) and data.get('_id'):
            bot.channels[data['_id']] = data
        return json_response(data)

    async def server_get(request):
        sid = request.match_info['sid']
        data = await bot.api('GET', '/servers/%s' % sid)
        if isinstance(data, dict) and data.get('_id'):
            bot.servers[data['_id']] = data
        return json_response(data)

    async def channel_members(request):
        cid = request.match_info['cid']
        return json_response(await bot.api('GET', '/channels/%s/members' % cid))

    async def channel_recipient(request):
        cid, uid = request.match_info['cid'], request.match_info['uid']
        if request.method == 'PUT':
            await bot.api('PUT', '/channels/%s/recipients/%s' % (cid, uid))
        else:
            await bot.api('DELETE', '/channels/%s/recipients/%s' % (cid, uid))
        return json_response({'ok': True})

    async def channel_create(request):
        body = await request.json()
        payload = {k: v for k, v in body.items() if k in ('type', 'name', 'description', 'nsfw', 'server', 'category')}
        data = await bot.api('POST', '/channels/create', body=payload)
        if isinstance(data, dict) and data.get('_id'):
            bot.channels[data['_id']] = data
            srv = bot.servers.get(data.get('server'))
            if srv is not None and data['_id'] not in (srv.get('channels') or []):
                srv.setdefault('channels', []).append(data['_id'])
        return json_response(data)

    async def server_channels(request):
        sid = request.match_info['sid']
        body = await request.json()
        payload = {'type': body.get('type') or 'Text', 'name': body.get('name') or 'new-channel'}
        for k in ('description', 'nsfw', 'category', 'voice'):
            if body.get(k) is not None:
                payload[k] = body[k]
        data = await bot.api('POST', '/servers/%s/channels' % sid, body=payload)
        if isinstance(data, dict) and data.get('_id'):
            bot.channels[data['_id']] = data
            srv = bot.servers.get(sid)
            if srv is not None:
                srv.setdefault('channels', []).append(data['_id'])
        return json_response(data)

    # ---- servers -----------------------------------------------------
    async def server_members(request):
        sid = request.match_info['sid']
        data = await bot.api('GET', '/servers/%s/members' % sid)
        if isinstance(data, dict):
            lst = data.get('members') or []
            for m in lst:
                mid = m['_id'] if isinstance(m.get('_id'), dict) else {'server': sid, 'user': m.get('user')}
                bot.members.setdefault(sid, {})[mid['user']] = {
                    'user': mid['user'], 'server': sid, 'joined_at': m.get('joined_at'),
                    'roles': m.get('roles') or [], 'nickname': m.get('nickname'), 'timeout': m.get('timeout')}
            for u in data.get('users') or []:
                bot.users.setdefault(u['_id'], u)
            out = {'members': [dict(x) for x in bot.members[sid].values()],
                   'users': {u['_id']: Bot._trim_user(u) for u in (data.get('users') or [])}}
            return json_response(out)
        return json_response(data)

    async def server_patch(request):
        sid = request.match_info['sid']
        body = await request.json()
        data = await bot.api('PATCH', '/servers/%s' % sid, body={
            k: v for k, v in body.items()
            if k in ('name', 'description', 'icon', 'banner', 'categories', 'default_permissions',
                     'system_messages', 'nsfw', 'discoverable', 'analytics')})
        if isinstance(data, dict):
            bot.servers.setdefault(sid, {}).update(data)
        return json_response(data)

    async def server_emojis(request):
        sid = request.match_info['sid']
        data = await bot.api('GET', '/servers/%s/emojis' % sid)
        if isinstance(data, list):
            bot.emojis[sid] = data
        return json_response(data)

    async def emoji_create(request):
        sid = request.match_info['sid']
        body = await request.json()
        payload = {}
        if body.get('name'):
            payload['name'] = body['name']
        if body.get('file_id'):
            payload['parent'] = {'type': 'Server', 'id': sid}
            payload['id'] = body['file_id']
        data = await bot.api('POST', '/custom/emoji/%s' % body.get('file_id', ''), body={'name': body.get('name')})
        if isinstance(data, dict):
            payload['_id'] = data.get('id')
            bot.emojis.setdefault(sid, []).append({'id': data.get('id'), 'name': body.get('name'),
                                                   'parent': {'type': 'Server', 'id': sid},
                                                   'creator_id': bot.me['_id']})
        return json_response(data)

    async def emoji_delete(request):
        eid = request.match_info['eid']
        await bot.api('DELETE', '/custom/emoji/%s' % eid)
        for lst in bot.emojis.values():
            lst[:] = [e for e in lst if e.get('_id') != eid and e.get('id') != eid]
        return json_response({'ok': True})

    async def member_patch(request):
        sid, uid = request.match_info['sid'], request.match_info['uid']
        body = await request.json()
        payload = {k: v for k, v in body.items() if k in ('nickname', 'roles', 'timeout', 'can_publish', 'can_receive')}
        await bot.api('PATCH', '/servers/%s/members/%s' % (sid, uid), body=payload)
        cur = bot.members.setdefault(sid, {}).setdefault(uid, {'user': uid, 'server': sid})
        cur.update({k: v for k, v in payload.items() if k != 'timeout'})
        return json_response({'ok': True, 'member': cur})

    async def member_kick(request):
        sid, uid = request.match_info['sid'], request.match_info['uid']
        await bot.api('DELETE', '/servers/%s/members/%s' % (sid, uid))
        bot.members.get(sid, {}).pop(uid, None)
        return json_response({'ok': True})

    async def bans(request):
        sid = request.match_info['sid']
        return json_response(await bot.api('GET', '/servers/%s/bans' % sid))

    async def ban(request):
        sid, uid = request.match_info['sid'], request.match_info['uid']
        if request.method == 'PUT':
            body = await request.json()
            await bot.api('PUT', '/servers/%s/bans/%s' % (sid, uid), body={'reason': (body or {}).get('reason')})
            bot.members.get(sid, {}).pop(uid, None)
        else:
            await bot.api('DELETE', '/servers/%s/bans/%s' % (sid, uid))
        return json_response({'ok': True})

    async def role_create(request):
        sid = request.match_info['sid']
        body = await request.json()
        data = await bot.api('POST', '/servers/%s/roles' % sid, body={
            k: v for k, v in body.items() if k in ('name', 'colour', 'hoist', 'rank', 'permissions')} or {'name': 'new role'})
        if isinstance(data, dict):
            r = data.get('role') if isinstance(data.get('role'), dict) else data
            bot.servers.setdefault(sid, {}).setdefault('roles', {})[r.get('id') or r.get('_id')] = r
        return json_response(data)

    async def role_patch(request):
        sid, rid = request.match_info['sid'], request.match_info['rid']
        body = await request.json()
        data = await bot.api('PATCH', '/servers/%s/roles/%s' % (sid, rid), body={
            k: v for k, v in body.items() if k in ('name', 'colour', 'hoist', 'rank', 'permissions')})
        srv = bot.servers.get(sid)
        if srv is not None:
            role = srv.setdefault('roles', {}).get(rid, {})
            role.update({k: v for k, v in body.items() if k in ('name', 'colour', 'hoist', 'rank')})
            if body.get('permissions'):
                role['permissions'] = body['permissions']
        return json_response(data)

    async def role_delete(request):
        sid, rid = request.match_info['sid'], request.match_info['rid']
        await bot.api('DELETE', '/servers/%s/roles/%s' % (sid, rid))
        bot.servers.get(sid, {}).setdefault('roles', {}).pop(rid, None)
        return json_response({'ok': True})

    async def perm_set(request):
        """PUT /api/channels/{cid}/permissions/{role_id|default}  {allow, deny}"""
        cid, rid = request.match_info['cid'], request.match_info['role_id']
        body = await request.json()
        payload = {'permissions': {'allow': int(body.get('allow') or 0), 'deny': int(body.get('deny') or 0)}}
        await bot.api('PUT', '/channels/%s/permissions/%s' % (cid, rid), body=payload)
        ch = bot.channels.get(cid)
        if ch is not None:
            if rid == 'default':
                ch['default_permissions'] = {'a': payload['permissions']['allow'], 'd': payload['permissions']['deny']}
            else:
                ch.setdefault('role_permissions', {})[rid] = {'a': payload['permissions']['allow'],
                                                              'd': payload['permissions']['deny']}
        return json_response({'ok': True})

    # ---- users -------------------------------------------------------
    async def user_get(request):
        uid = request.match_info['uid']
        data = await bot.api('GET', '/users/%s' % uid)
        if isinstance(data, dict):
            bot.users[uid] = data
        return json_response(data)

    async def user_profile(request):
        uid = request.match_info['uid']
        return json_response(await bot.api('GET', '/users/%s/profile' % uid))

    async def user_default_avatar(request):
        uid = request.match_info['uid']
        out = await bot.api('GET', '/users/%s/default_avatar' % uid, binary=True)
        if isinstance(out, tuple):
            body, ctype = out
            return web.Response(body=body, content_type=(ctype or 'image/png').split(';')[0],
                                headers={'Cache-Control': 'public, max-age=86400'})
        return json_response(out)

    async def user_dm(request):
        uid = request.match_info['uid']
        data = await bot.api('GET', '/users/%s/dm' % uid)
        if isinstance(data, dict) and data.get('_id'):
            bot.channels[data['_id']] = data
            if data.get('channel_type') in ('DirectMessage', 'Group') and \
                    data['_id'] not in [d.get('_id') for d in bot.dms]:
                bot.dms.append(data)
        return json_response(data)

    async def me_get(request):
        return json_response(bot.me)

    async def me_patch(request):
        body = await request.json()
        payload = {}
        for k in ('status', 'profile', 'display_name', 'pronouns', 'avatar', 'badges', 'flags'):
            if k in body:
                payload[k] = body[k]
        data = await bot.api('PATCH', '/users/@me', body=payload)
        if isinstance(data, dict):
            bot.me = data
            bot.users[data['_id']] = data
        bot.broadcast({'type': 'UserUpdate', 'id': bot.me['_id'], **{k: v for k, v in payload.items() if k in ('status', 'display_name', 'pronouns', 'profile', 'avatar')}})
        return json_response(data)

    async def me_username(request):
        body = await request.json()
        payload = {'username': body.get('username'), 'password': body.get('password') or ''}
        if not payload['username']:
            return json_response({'error': 'username required'}, 400)
        data = await bot.api('PATCH', '/users/@me/username', body=payload)
        return json_response(data or {'ok': True})

    # ---- upload ------------------------------------------------------
    async def upload(request):
        reader = await request.multipart()
        field = await reader.next()
        while field is not None and field.name != 'file':
            field = await reader.next()
        if field is None:
            return json_response({'error': 'no file field'}, 400)
        data = await field.read(decode=False)
        limit = 20 * 1024 * 1024
        if len(data) > limit:
            return json_response({'error': 'file too big (20 MB max)'}, 400)
        fid = await bot.upload(data, field.filename or 'upload', field.headers.get('Content-Type'))
        return json_response({'id': fid, 'url': '%s/attachments/%s' % (CDN, fid),
                              'filename': field.filename, 'size': len(data)})

    # ---- static ------------------------------------------------------
    async def index(request):
        return web.FileResponse(STATIC / 'index.html')

    async def stylesheet(request):
        return web.FileResponse(STATIC / 'style.css')

    # ---- routes ------------------------------------------------------
    r = app.router
    r.add_get('/api/health', health)
    r.add_get('/api/botstatus', botstatus)
    r.add_get('/api/session', session)
    r.add_post('/api/login', login)
    r.add_post('/api/logout', logout)
    r.add_get('/api/state', state)
    r.add_get('/api/resync', resync)
    r.add_get('/api/ws', ws_handler)

    r.add_get('/api/channels/{cid}/messages', history)
    r.add_post('/api/channels/{cid}/messages', send)
    r.add_patch('/api/channels/{cid}/messages/{mid}', edit)
    r.add_delete('/api/channels/{cid}/messages/{mid}', delete)
    r.add_put('/api/channels/{cid}/messages/{mid}/reactions/{emoji}', react)
    r.add_delete('/api/channels/{cid}/messages/{mid}/reactions/{emoji}', react)
    r.add_delete('/api/channels/{cid}/messages/{mid}/reactions', clear_reactions)
    r.add_post('/api/channels/{cid}/messages/{mid}/pin', pin)
    r.add_delete('/api/channels/{cid}/messages/{mid}/pin', pin)

    r.add_patch('/api/channels/{cid}', channel_patch)
    r.add_delete('/api/channels/{cid}', channel_delete)
    r.add_get('/api/channels/{cid}', channel_get)
    r.add_get('/api/channels/{cid}/members', channel_members)
    r.add_put('/api/channels/{cid}/recipients/{uid}', channel_recipient)
    r.add_delete('/api/channels/{cid}/recipients/{uid}', channel_recipient)
    r.add_post('/api/channels/create', channel_create)
    r.add_post('/api/servers/{sid}/channels', server_channels)
    r.add_put('/api/channels/{cid}/permissions/{role_id}', perm_set)

    r.add_get('/api/servers/{sid}/members', server_members)
    r.add_get('/api/servers/{sid}', server_get)
    r.add_patch('/api/servers/{sid}', server_patch)
    r.add_get('/api/servers/{sid}/emojis', server_emojis)
    r.add_post('/api/servers/{sid}/emojis', emoji_create)
    r.add_delete('/api/emoji/{eid}', emoji_delete)
    r.add_patch('/api/servers/{sid}/members/{uid}', member_patch)
    r.add_delete('/api/servers/{sid}/members/{uid}', member_kick)
    r.add_get('/api/servers/{sid}/bans', bans)
    r.add_put('/api/servers/{sid}/bans/{uid}', ban)
    r.add_delete('/api/servers/{sid}/bans/{uid}', ban)
    r.add_post('/api/servers/{sid}/roles', role_create)
    r.add_patch('/api/servers/{sid}/roles/{rid}', role_patch)
    r.add_delete('/api/servers/{sid}/roles/{rid}', role_delete)

    r.add_get('/api/users/@me', me_get)
    r.add_patch('/api/users/@me', me_patch)
    r.add_patch('/api/users/@me/username', me_username)
    r.add_get('/api/users/{uid}', user_get)
    r.add_get('/api/users/{uid}/profile', user_profile)
    r.add_get('/api/users/{uid}/default_avatar', user_default_avatar)
    r.add_get('/api/users/{uid}/dm', user_dm)

    r.add_post('/api/upload', upload)

    r.add_get('/', index)
    r.add_get('/style.css', stylesheet)
    r.add_static('/js', STATIC / 'js')
    r.add_static('/static', STATIC)
    return app


def valid_session(request: web.Request, conf: dict) -> bool:
    raw = request.cookies.get(COOKIE)
    if not raw or '.' not in raw:
        return False
    payload, sig = raw.rsplit('.', 1)
    if not hmac.compare_digest(sign(conf['secret'], payload), sig):
        return False
    try:
        _, exp = payload.split('.', 1)
        return int(exp) > time.time()
    except Exception:
        return False


async def main():
    conf = load_config()
    token = load_env_token()
    bot = Bot(token)
    await bot.start()
    app = make_app(bot, conf)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', int(conf.get('port') or PORT))
    await site.start()
    log.info('magbot web UI listening on http://127.0.0.1:%s (password in %s)',
             conf.get('port') or PORT, CONF_FILE)
    try:
        await asyncio.Event().wait()
    finally:
        await bot.close()
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
