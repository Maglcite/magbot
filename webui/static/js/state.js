/* magbot web UI — state store, REST client, gateway event application. */
(function () {
  const MB = (window.MB = window.MB || {});

  const bus = {};
  MB.bus = {
    on(name, fn) { (bus[name] = bus[name] || []).push(fn); return () => MB.bus.off(name, fn); },
    off(name, fn) { bus[name] = (bus[name] || []).filter((f) => f !== fn); },
    emit(name, data) { (bus[name] || []).forEach((f) => { try { f(data); } catch (e) { console.error(e); } }); },
  };

  MB.store = {
    ready: false,
    gateway: false,
    me: null,
    users: {},
    servers: [],
    channels: {},
    members: {},
    emojis: {},
    emojiByName: {},
    emojiByServer: {},
    voice: {},
    dms: [],
    unreads: {},
    typing: {},
    instance: {},
    fetchedUsers: {},
  };

  const current = { channel: null, server: null };
  MB.current = () => current;
  MB.setCurrent = (serverId, channelId) => { current.server = serverId; current.channel = channelId; };

  // ------------------------------------------------------------------ REST
  MB.api = async function (method, path, body) {
    const opts = { method, credentials: 'same-origin', headers: {} };
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    const res = await fetch('api' + path, opts);
    if (res.status === 401) { MB.bus.emit('unauthorized'); throw new Error('unauthorized'); }
    let data = null;
    const text = await res.text();
    if (text) { try { data = JSON.parse(text); } catch (e) { data = { error: text }; } }
    if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
    return data;
  };

  // ----------------------------------------------------------------- state
  MB.applySnapshot = function (snap) {
    const s = MB.store;
    s.ready = !!snap.ready;
    s.gateway = !!(snap.gateway && snap.gateway.connected);
    s.me = snap.me;
    s.users = snap.users || {};
    s.servers = snap.servers || [];
    s.channels = snap.channels || {};
    s.members = snap.members || {};
    s.voice = snap.voice || {};
    s.dms = snap.dms || [];
    s.instance = snap.instance || {};
    s.emojis = {};
    s.emojiByName = {};
    s.emojiByServer = {};
    for (const [sid, list] of Object.entries(snap.emojis || {})) {
      const real = sid && sid !== 'null' ? sid : null;
      for (const e of list) {
        const id = e._id || e.id;
        const obj = { _id: id, name: e.name, animated: e.animated, server: real, creator_id: e.creator_id };
        s.emojiByName[e.name] = obj;
        (s.emojiByServer[real || 'global'] = s.emojiByServer[real || 'global'] || []).push(obj);
      }
    }
    s.unreads = {};
    for (const u of snap.unreads || []) {
      const id = (u._id && u._id.channel) || u.channel;
      if (!id) continue;
      const last = MB.lastMessageId(id);
      const mentions = u.mentions || [];
      const read = u.last_id || u.last_message_id || last;
      if (last && read && last !== read) {
        if (!s.unreads[id]) s.unreads[id] = { count: -1, mentions: mentions.length, lastReadId: read, lastId: last };
      } else if (mentions.length && s.me && mentions.includes(s.me._id)) {
        s.unreads[id] = { count: 1, mentions: 1, lastReadId: read, lastId: last, mentionOnly: true };
      }
    }
    for (const [cid, users] of Object.entries(snap.typing || {})) s.typing[cid] = users.slice();
    if (snap.instance && snap.instance.cdn) MB.CDN = snap.instance.cdn;
  };

  MB.lastMessageId = function (channelId) {
    const ch = MB.store.channels[channelId];
    if (ch) return ch.last_message_id || ch.last_message || null;
    const dm = MB.store.dms.find((d) => d._id === channelId);
    return dm ? dm.last_message_id || dm.last_message || null : null;
  };

  MB.server = (id) => MB.store.servers.find((s) => s._id === id) || null;
  MB.channel = (id) => MB.store.channels[id] || MB.store.dms.find((d) => d._id === id) || null;
  MB.user = (id) => MB.store.users[id] || null;

  MB.ensureUser = async function (id) {
    if (!id || MB.store.users[id]) return MB.store.users[id];
    if (MB.store.fetchedUsers[id]) return null;
    MB.store.fetchedUsers[id] = true;
    try {
      const u = await MB.api('GET', `/users/${id}`);
      if (u && u._id) { MB.store.users[u._id] = u; MB.bus.emit('user', u); return u; }
    } catch (e) { /* ignore */ }
    return null;
  };

  MB.channelsOfServer = function (server) {
    const s = MB.store;
    const out = [];
    for (const cid of server.channels || []) {
      const c = s.channels[cid];
      if (c && c.channel_type !== 'SavedMessages') out.push(c);
    }
    const byCat = {};
    const uncategorised = [];
    const cats = server.categories || [];
    const catOf = {};
    for (const cat of cats) for (const cid of cat.channels || []) catOf[cid] = cat.id;
    for (const c of out) (catOf[c._id] ? (byCat[catOf[c._id]] = byCat[catOf[c._id]] || []).push(c) : uncategorised.push(c));
    const rank = (c) => (c.channel_type === 'TextChannel' ? 0 : 1);
    const sortIn = (arr) => arr.sort((a, b) => rank(a) - rank(b) || (a.name || '').localeCompare(b.name || ''));
    return {
      categories: cats.map((cat) => ({ cat, channels: sortIn(byCat[cat.id] || []) })).filter((g) => g.channels.length),
      uncategorised: sortIn(uncategorised),
    };
  };

  MB.dmList = function () {
    const s = MB.store;
    const list = [];
    for (const d of s.dms) {
      if (d.channel_type === 'SavedMessages') list.push({ ...d, kind: 'saved' });
      else if (d.channel_type === 'Group') list.push({ ...d, kind: 'group' });
      else list.push({ ...d, kind: 'dm' });
    }
    const weight = (d) => (d.kind === 'saved' ? 0 : d.kind === 'group' ? 1 : 2);
    return list.sort((a, b) => weight(a) - weight(b) || (a.name || '').localeCompare(b.name || ''));
  };

  MB.dmName = function (dm) {
    if (!dm) return 'unknown';
    if (dm.name) return dm.name;
    const others = (dm.recipients || []).filter((r) => r !== (MB.store.me && MB.store.me._id));
    const users = others.map((id) => MB.store.users[id]).filter(Boolean);
    if (!users.length) return dm.channel_type === 'SavedMessages' ? 'Saved Messages' : 'unknown';
    return users.map((u) => MB.userName(u)).join(', ');
  };

  // -------------------------------------------------------------- unreads
  MB.unreadOf = (cid) => MB.store.unreads[cid] || null;

  MB.markRead = function (cid) {
    const last = MB.lastMessageId(cid);
    const cur = MB.store.unreads[cid];
    if (cur && cur.lastId && last && cur.lastId === last) return;
    delete MB.store.unreads[cid];
    MB.bus.emit('unreads');
  };

  MB.bumpUnread = function (cid, mentions) {
    if (current.channel === cid && document.hasFocus()) return;
    const u = MB.store.unreads[cid] || { count: 0, mentions: 0 };
    u.count = (u.count || 0) + 1;
    if (mentions) u.mentions = (u.mentions || 0) + 1;
    u.lastId = MB.lastMessageId(cid);
    MB.store.unreads[cid] = u;
    MB.bus.emit('unreads');
  };

  // ---------------------------------------------------------------- drafts
  const DKEY = 'magbot.drafts';
  MB.drafts = (() => { try { return JSON.parse(localStorage.getItem(DKEY) || '{}'); } catch (e) { return {}; } })();
  MB.saveDraft = function (cid, text) {
    if (text) MB.drafts[cid] = text; else delete MB.drafts[cid];
    try { localStorage.setItem(DKEY, JSON.stringify(MB.drafts)); } catch (e) {}
  };

  // -------------------------------------------------------------- gateway
  let ws = null;
  let wsTries = 0;

  MB.connect = function () {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const base = location.pathname.replace(/\/[^/]*$/, '/');
    ws = new WebSocket(`${proto}://${location.host}${base}api/ws`);
    ws.onopen = () => { wsTries = 0; MB.bus.emit('ws', { state: 'open' }); };
    ws.onmessage = (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch (err) { return; }
      MB.applyEvent(ev);
      MB.bus.emit('event', ev);
      MB.bus.emit(ev.type, ev);
    };
    ws.onclose = () => {
      MB.bus.emit('ws', { state: 'closed' });
      wsTries++;
      const delay = Math.min(1000 * Math.pow(1.6, wsTries), 20000);
      setTimeout(MB.connect, delay);
    };
    ws.onerror = () => {};
  };

  // --------------------------------------------------------- event reducer
  MB.applyEvent = function (ev) {
    const s = MB.store;
    switch (ev.type) {
      case 'GatewayStatus':
        s.gateway = !!ev.connected;
        MB.bus.emit('gateway', s.gateway);
        break;
      case 'Ready':
        MB.applySnapshot(MB.readyFromEvent(ev));
        MB.bus.emit('reload');
        break;
      case 'Message': {
        const ch = s.channels[ev.channel];
        if (ch) ch.last_message_id = ev._id;
        const dm = s.dms.find((d) => d._id === ev.channel);
        if (dm) dm.last_message_id = ev._id;
        if (ev.user && ev.user._id) s.users[ev.user._id] = ev.user;
        if (ev.member) {
          const sid = ev.member._id ? ev.member._id.server : null;
          if (sid) {
            const mid = ev.member._id.user;
            s.members[sid] = s.members[sid] || {};
            s.members[sid][mid] = { user: mid, server: sid, roles: ev.member.roles || [], nickname: ev.member.nickname, joined_at: ev.member.joined_at };
          }
        }
        const me = s.me && s.me._id;
        const mentioned = ev.mentions && me && ev.mentions.includes(me);
        if (ev.user && ev.user._id !== me) MB.bumpUnread(ev.channel, mentioned);
        break;
      }
      case 'MessageUpdate':
        break;
      case 'MessageDelete': {
        for (const c of Object.values(s.channels)) if (c.last_message_id === ev.id) c.last_message_id = null;
        break;
      }
      case 'ChannelCreate':
        s.channels[ev._id] = ev;
        if (ev.server) {
          const srv = MB.server(ev.server);
          if (srv && !(srv.channels || []).includes(ev._id)) srv.channels.push(ev._id);
        } else if (ev.channel_type === 'Group' || ev.channel_type === 'DirectMessage') {
          if (!s.dms.find((d) => d._id === ev._id)) s.dms.push(ev);
        }
        break;
      case 'ChannelUpdate': {
        const id = ev.id || ev._id;
        const ch = s.channels[id];
        if (ch) ch.name = ev.data && ev.data.name ? ev.data.name : (ev.name || ch.name);
        if (ev.data && ev.data.name) {
          const d = s.dms.find((x) => x._id === id);
          if (d) d.name = ev.data.name;
        }
        break;
      }
      case 'ChannelDelete': {
        const id = ev.id;
        delete s.channels[id];
        s.dms = s.dms.filter((d) => d._id !== id);
        for (const srv of s.servers) srv.channels = (srv.channels || []).filter((c) => c !== id);
        break;
      }
      case 'ChannelGroupJoin': {
        const ch = s.channels[ev.id] || s.dms.find((d) => d._id === ev.id);
        if (ch) { ch.recipients = ch.recipients || []; if (!ch.recipients.includes(ev.user)) ch.recipients.push(ev.user); }
        break;
      }
      case 'ChannelGroupLeave': {
        const ch = s.channels[ev.id] || s.dms.find((d) => d._id === ev.id);
        if (ch) ch.recipients = (ch.recipients || []).filter((r) => r !== ev.user);
        MB.ensureUser(ev.user);
        break;
      }
      case 'ChannelStartTyping':
      case 'ChannelStopTyping': {
        const list = s.typing[ev.id] = s.typing[ev.id] || [];
        if (ev.type === 'ChannelStartTyping') { if (!list.includes(ev.user)) list.push(ev.user); }
        else s.typing[ev.id] = list.filter((u) => u !== ev.user);
        MB.ensureUser(ev.user);
        break;
      }
      case 'ServerCreate':
        s.servers.push(ev);
        if (ev.members) {
          s.members[ev._id] = s.members[ev._id] || {};
          for (const m of ev.members) {
            const mid = m._id ? m._id.user : m.user;
            s.members[ev._id][mid] = { user: mid, server: ev._id, roles: m.roles || [], nickname: m.nickname, joined_at: m.joined_at };
          }
        }
        if (ev.channels) for (const c of ev.channels) s.channels[c._id] = c;
        break;
      case 'ServerUpdate': {
        const srv = MB.server(ev.id);
        if (srv) {
          const data = { ...ev };
          delete data.type; delete data.id;
          Object.assign(srv, data);
        }
        for (const [mid, member] of Object.entries(s.members[ev.id] || {})) member.server = ev.id;
        break;
      }
      case 'ServerDelete':
        s.servers = s.servers.filter((x) => x._id !== ev.id);
        delete s.members[ev.id];
        break;
      case 'ServerMemberJoin':
        s.members[ev.id] = s.members[ev.id] || {};
        s.members[ev.id][ev.user] = { user: ev.user, server: ev.id, roles: [], joined_at: new Date().toISOString() };
        MB.ensureUser(ev.user);
        break;
      case 'ServerMemberUpdate': {
        const sid = ev.id, uid = ev.user || (ev._id && ev._id.user);
        s.members[sid] = s.members[sid] || {};
        const cur = s.members[sid][uid] || { user: uid, server: sid };
        if (ev.data) Object.assign(cur, ev.data);
        if (ev.roles) cur.roles = ev.roles;
        s.members[sid][uid] = cur;
        break;
      }
      case 'ServerMemberRemove':
        if (s.members[ev.id]) delete s.members[ev.id][ev.user];
        break;
      case 'ServerRoleUpdate': {
        const srv = MB.server(ev.id);
        if (srv) { srv.roles = srv.roles || {}; srv.roles[ev.role_id] = ev.data; }
        break;
      }
      case 'ServerRoleDelete': {
        const srv = MB.server(ev.id);
        if (srv && srv.roles) delete srv.roles[ev.role_id];
        break;
      }
      case 'ServerRoleRanksUpdate': {
        const srv = MB.server(ev.id);
        if (srv && srv.roles) for (const [rid, rank] of Object.entries(ev.ranks || {})) if (srv.roles[rid]) srv.roles[rid].rank = rank;
        break;
      }
      case 'ServerEmojiCreate': {
        const sid = ev.parent && ev.parent.id;
        const obj = { _id: ev._id, name: ev.name, animated: ev.animated, server: sid };
        (s.emojiByServer[sid] = s.emojiByServer[sid] || []).push(obj);
        s.emojiByName[ev.name] = obj;
        break;
      }
      case 'ServerEmojiDelete': {
        for (const k of Object.keys(s.emojiByName)) if (s.emojiByName[k]._id === ev.id) delete s.emojiByName[k];
        for (const k of Object.keys(s.emojiByServer)) s.emojiByServer[k] = s.emojiByServer[k].filter((e) => e._id !== ev.id);
        break;
      }
      case 'UserUpdate': {
        const u = s.users[ev.id];
        if (u) {
          const data = { ...ev };
          delete data.type; delete data.id;
          Object.assign(u, data);
        } else {
          MB.ensureUser(ev.id);
        }
        break;
      }
      case 'UserRelationshipUpdate': {
        const u = s.users[ev.user_id];
        if (u) u.relationship = ev.status;
        break;
      }
      case 'VoiceChannelJoin':
        (s.voice[ev.id] = s.voice[ev.id] || []).push({ channel_id: ev.id, user_id: ev.user, state: ev.state });
        MB.ensureUser(ev.user);
        break;
      case 'VoiceChannelLeave':
        s.voice[ev.id] = (s.voice[ev.id] || []).filter((v) => v.user_id !== ev.user);
        break;
      case 'VoiceChannelMove':
      case 'UserMoveVoiceChannel':
        for (const k of Object.keys(s.voice)) s.voice[k] = s.voice[k].filter((v) => v.user_id !== ev.user);
        (s.voice[ev.to] = s.voice[ev.to] || []).push({ channel_id: ev.to, user_id: ev.user });
        break;
      default:
        break;
    }
  };

  MB.readyFromEvent = function (ev) {
    return {
      ready: true,
      gateway: { connected: true },
      me: ev.me || ev.user,
      users: Object.fromEntries((ev.users || []).map((u) => [u._id, u])),
      servers: ev.servers || [],
      channels: Object.fromEntries((ev.channels || []).map((c) => [c._id, c])),
      members: (ev.members || []).reduce((acc, m) => {
        const mid = m._id ? m._id : { server: m.server, user: m.user };
        acc[mid.server] = acc[mid.server] || {};
        acc[mid.server][mid.user] = { user: mid.user, server: mid.server, roles: m.roles || [], nickname: m.nickname, joined_at: m.joined_at };
        return acc;
      }, {}),
      emojis: (ev.emojis || []).reduce((acc, e) => {
        const sid = e.parent && e.parent.id;
        (acc[sid] = acc[sid] || []).push(e);
        return acc;
      }, {}),
      voice: (ev.voice_states || []).reduce((acc, v) => { (acc[v.channel_id] = acc[v.channel_id] || []).push(v); return acc; }, {}),
      dms: MB.store.dms,
      unreads: MB.store.unreads,
      instance: MB.store.instance,
    };
  };

  // -------------------------------------------------------- notifications
  MB.notify = function (title, body, tag) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    if (document.hasFocus()) return;
    try { new Notification(title, { body, tag, icon: '/favicon.ico' }); } catch (e) {}
  };

  MB.requestNotify = async function () {
    if (!('Notification' in window)) return false;
    if (Notification.permission === 'granted') return true;
    const p = await Notification.requestPermission();
    return p === 'granted';
  };
})();
