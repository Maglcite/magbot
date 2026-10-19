/* magbot web UI — bootstrap + glue. Provides the app-level functions that
   ui.js/state.js/md.js call, plus interactions, menus, settings and the
   quick switcher. Loaded last. */
(function () {
  const MB = (window.MB = window.MB || {});
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const h = (s) => { const t = document.createElement('template'); t.innerHTML = s.trim(); return t.content.firstElementChild; };

  // ------------------------------------------------------------- primitives
  const B32 = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
  MB.snowflakeTime = function (id) {
    if (!id || id.length < 10) return Date.now();
    let ms = 0;
    for (let i = 0; i < 10; i++) {
      const v = B32.indexOf(String(id[i]).toUpperCase());
      if (v < 0) return Date.now();
      ms = ms * 32 + v;
    }
    return ms;
  };

  MB.serverById = (id) => MB.store.servers.find((s) => s._id === id) || null;
  MB.serverChannels = (server) => (server ? (server.channels || []).map((c) => MB.store.channels[c]).filter(Boolean) : []);
  MB.channelTitle = function (ch) {
    if (!ch) return '';
    if (ch.server) return '#' + (ch.name || 'channel');
    if (ch.channel_type === 'SavedMessages') return 'Saved Messages';
    return MB.dmName(ch);
  };
  MB.initials = (name) => (name || '?').split(/\s+/).map((w) => w[0]).filter(Boolean).join('').slice(0, 2).toUpperCase();
  MB.messageById = (cid, mid) => (MB.messages[cid] || []).find((m) => m._id === mid) || null;

  // emoji values coming out of the picker are either a unicode char or ":name:"
  MB.emojiKey = function (value) {
    if (typeof value !== 'string') return null;
    const m = /^:([a-zA-Z0-9_\-+]+):$/.exec(value);
    if (m) { const e = MB.store.emojiByName[m[1]]; return e ? e._id : null; }
    return value;
  };
  MB.emojiText = function (value) {
    const m = /^:([a-zA-Z0-9_\-+]+):$/.exec(value || '');
    if (m) { const e = MB.store.emojiByName[m[1]]; return e ? `:${e.name}:` : value; }
    return value;
  };

  MB.can = function (server, channel, perm) {
    if (!server) return true;                 // DMs/groups: no server perms apply
    return MB.permsFor(server, channel, MB.store.me && MB.store.me._id).has(perm);
  };
  MB.canSend = function (ch) {
    if (!ch) return false;
    if (!ch.server) return true;
    return MB.can(MB.serverById(ch.server), ch, 'SendMessage');
  };

  MB.absorbUsers = function (resp) {
    if (!resp) return resp;
    const s = MB.store;
    for (const u of resp.users || []) if (u && u._id) s.users[u._id] = u;
    for (const m of resp.members || []) {
      const mid = m._id ? m._id : { server: m.server, user: m.user };
      if (!mid || !mid.server) continue;
      s.members[mid.server] = s.members[mid.server] || {};
      s.members[mid.server][mid.user] = { user: mid.user, server: mid.server, roles: m.roles || [], nickname: m.nickname, joined_at: m.joined_at };
    }
    return resp;
  };

  MB.pins = {};      // cid -> Set of pinned message ids (local, live-tracked)

  // ------------------------------------------------------------ session/boot
  let booted = false;
  MB.load = async function () {
    const snap = await MB.api('GET', '/state');
    MB.applySnapshot(snap);
    booted = true;
  };

  MB.renderAll = function () {
    if (!MB.store.ready) return;
    MB.renderRail();
    MB.renderSidebar();
    MB.renderChat();
    MB.renderMembers();
    MB.renderMeBar();
    MB.updateTitle();
    const cur = MB.current();
    if (cur.channel) { MB.renderComposer(MB.channel(cur.channel)); MB.renderTyping(MB.channel(cur.channel)); }
  };

  async function start() {
    await MB.load();
    $('#boot').classList.add('hidden');
    $('#login').classList.add('hidden');
    $('#main').classList.remove('hidden');
    MB.renderAll();
    if (!MB.current().channel) {
      const first = MB.store.servers[0];
      if (first) {
        const chans = MB.serverChannels(first).filter((c) => c.channel_type === 'TextChannel');
        pickChannel(chans.length ? chans[0]._id : null, first._id);
      } else {
        const dms = MB.dmList();
        if (dms.length) pickChannel(dms[0]._id, null);
      }
    }
    MB.connect();
    MB.requestNotify().catch(() => {});
    setInterval(() => MB.updateTitle(), 30000);
  }

  async function trySession() {
    try {
      const res = await fetch('api/session', { credentials: 'same-origin' });
      const data = res.ok ? await res.json() : null;
      if (!data || !data.authenticated) throw new Error('no session');
      await start();
    } catch (e) {
      $('#boot').classList.add('hidden');
      $('#login').classList.remove('hidden');
      const pass = $('#login-pass');
      if (pass) setTimeout(() => pass.focus(), 100);
    }
  }

  const form = $('#login-form');
  if (form) form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = $('#login-go');
    const err = $('#login-err');
    err.textContent = '';
    btn.disabled = true;
    btn.textContent = 'Signing in…';
    try {
      await MB.api('POST', '/login', { password: $('#login-pass').value });
      $('#login-pass').value = '';
      await start();
    } catch (ex) {
      err.textContent = ex.message === 'bad password' ? 'Wrong password.' : (ex.message || 'login failed');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Sign in';
    }
  });

  // ------------------------------------------------------------ channel open
  let scrollWired = false;
  async function pickChannel(cid, serverId) {
    const s = MB.store;
    if (serverId !== undefined && serverId !== null) MB.setCurrent(serverId, cid);
    else {
      const ch = MB.channel(cid);
      MB.setCurrent(ch && ch.server ? ch.server : null, cid);
    }
    const cur = MB.current();
    // gateway payloads do not carry permission overwrites - fetch details once
    if (cur.server) {
      const srv = MB.serverById(cur.server);
      if (srv && !srv.default_permissions) {
        try {
          const d = await MB.api('GET', `/servers/${cur.server}`);
          if (d && d._id) Object.assign(srv, d);
        } catch (e) { /* ignore */ }
      }
    }
    if (cid) {
      const ch = MB.store.channels[cid];
      if (ch && !ch.default_permissions && ch.channel_type !== 'SavedMessages') {
        try {
          const d = await MB.api('GET', `/channels/${cid}`);
          if (d && d._id) MB.store.channels[cid] = Object.assign({}, ch, d);
        } catch (e) { /* ignore */ }
      }
    }
    MB.renderRail();
    MB.renderSidebar();
    MB.renderChat();
    MB.renderMembers();
    if (cid) { MB.renderComposer(MB.channel(cid)); MB.renderTyping(MB.channel(cid)); }
    const box = $('#messages');
    if (box && !scrollWired) {
      scrollWired = true;
      box.addEventListener('scroll', () => {
        if (box.scrollTop < 140) loadHistory(MB.current().channel, true);
      });
    }
    if (!cid) return;
    if (!MB.messages[cid]) await loadHistory(cid, false);
    else MB.renderMessages(MB.channel(cid));
    MB.markRead(cid);
    document.body.classList.remove('show-rail', 'show-side');
    setTimeout(() => MB.scrollToBottom(), 20);
  }

  async function loadHistory(cid, older) {
    if (!cid || MB.loadingMore[cid]) return;
    if (older && !MB.hasMore[cid]) return;
    MB.loadingMore[cid] = true;
    const list = MB.messages[cid] || (MB.messages[cid] = []);
    const box = $('#messages');
    const keepH = box ? box.scrollHeight : 0;
    try {
      const q = older && list.length ? '?limit=50&before=' + encodeURIComponent(list[0]._id) : '?limit=50';
      const resp = MB.absorbUsers(await MB.api('GET', `/channels/${cid}/messages${q}`));
      const msgs = ((resp && resp.messages) || []).slice().sort((a, b) => (a._id < b._id ? -1 : 1));
      if (!older) {
        MB.messages[cid] = msgs;
      } else {
        const seen = new Set(list.map((m) => m._id));
        MB.messages[cid] = msgs.filter((m) => !seen.has(m._id)).concat(list);
      }
      MB.hasMore[cid] = msgs.length >= 50;
    } catch (e) {
      MB.hasMore[cid] = false;
      if (String(e.message).indexOf('MissingPermission') >= 0) MB.toast('magbot cannot read that channel.', 'error');
    } finally {
      MB.loadingMore[cid] = false;
      if (MB.current().channel === cid) {
        MB.renderMessages(MB.channel(cid));
        if (older && box) box.scrollTop = box.scrollHeight - keepH;
        else if (!older) MB.scrollToBottom();
      }
    }
  }
  MB.loadHistory = loadHistory;
  MB.pickChannel = pickChannel;

  // ------------------------------------------------------------------- bus
  MB.bus.on('open', (p) => {
    if (p && p.channel) pickChannel(p.channel, p.server === undefined ? undefined : p.server);
    else if (p && p.server !== undefined) {
      const server = p.server ? MB.serverById(p.server) : null;
      if (server) {
        const cur = MB.current();
        const chans = MB.serverChannels(server).filter((c) => c.channel_type === 'TextChannel');
        pickChannel(cur.server === server._id && cur.channel ? cur.channel : (chans[0] ? chans[0]._id : null), server._id);
      } else { MB.setCurrent(null, null); MB.renderRail(); MB.renderSidebar(); MB.renderChat(); }
    }
  });
  MB.bus.on('reload', () => { booted = false; start().catch(() => {}); });
  MB.bus.on('unreads', () => { MB.renderRail(); MB.renderSidebar(); MB.updateTitle(); });
  MB.bus.on('user', () => { MB.renderRail(); MB.renderSidebar(); });
  MB.bus.on('unauthorized', () => { location.reload(); });
  MB.bus.on('gateway', (ok) => {
    const b = $('#conn-banner');
    if (b) b.classList.toggle('hidden', !!ok);
    const d = $('#conn-dot');
    if (d) d.className = 'conn-dot ' + (ok ? 'ok' : 'bad');
  });
  MB.bus.on('ws', (st) => {
    const d = $('#conn-dot');
    if (d && st.state === 'closed') d.className = 'conn-dot bad';
  });

  let sidebarTimer = null;
  function softRefresh() {
    clearTimeout(sidebarTimer);
    sidebarTimer = setTimeout(() => { MB.renderSidebar(); MB.renderRail(); MB.updateTitle(); }, 250);
  }

  MB.bus.on('event', (ev) => {
    const cur = MB.current();
    const s = MB.store;
    switch (ev.type) {
      case 'Message': {
        const uid = (ev.user && ev.user._id) || ev.user || null;
        const mine = uid && uid === (s.me && s.me._id);
        const mentioned = (ev.mentions || []).includes(s.me && s.me._id);
        if (cur.channel === ev.channel) {
          const list = MB.messages[ev.channel] || (MB.messages[ev.channel] = []);
          const norm = { ...ev, user: uid, author: uid };
          const i = list.findIndex((m) => m._id === ev._id);
          if (i >= 0) { list[i] = Object.assign({}, list[i], norm); }
          else if (!list.some((m) => m._id === ev._id)) { list.push(norm); }
          if (!document.hidden) { MB.renderMessages(MB.channel(ev.channel)); MB.scrollToBottom(); }
          if (document.hasFocus()) MB.markRead(ev.channel);
        }
        if (!mine && (mentioned || (MB.channel(ev.channel) || {}).channel_type === 'DirectMessage')) {
          const who = ev.user ? MB.userName(ev.user) : 'someone';
          const body = (ev.content || '').slice(0, 120) || '(attachment)';
          MB.notify(`${who} · ${MB.channelTitle(MB.channel(ev.channel))}`, body, ev._id);
        }
        softRefresh();
        break;
      }
      case 'MessageUpdate': {
        const cid = ev.channel, mid = ev.id || (ev.data && ev.data.id);
        if (cid && mid) {
          const m = MB.messageById(cid, mid);
          if (m) { m.content = (ev.data && ev.data.content !== undefined) ? ev.data.content : m.content; m.edited = ev.data ? (ev.data.edited || m.edited) : m.edited; }
          if (cur.channel === cid) MB.renderMessages(MB.channel(cid));
        } else {
          const resp = null;
          for (const cid2 of Object.keys(MB.messages)) if (MB.messageById(cid2, ev.id)) { /* fallthrough below */ }
          if (cur.channel) MB.refreshMessage(cur.channel, ev.id).catch(() => {});
        }
        break;
      }
      case 'MessageDelete': {
        for (const cid of Object.keys(MB.messages)) {
          const before = MB.messages[cid].length;
          MB.messages[cid] = MB.messages[cid].filter((m) => m._id !== ev.id);
          if (before !== MB.messages[cid].length && cur.channel === cid) MB.renderMessages(MB.channel(cid));
        }
        break;
      }
      case 'MessageDeleteBulk': {
        const ids = new Set(ev.ids || []);
        if (cur.channel) {
          MB.messages[cur.channel] = (MB.messages[cur.channel] || []).filter((m) => !ids.has(m._id));
          MB.renderMessages(MB.channel(cur.channel));
        }
        break;
      }
      case 'MessageReact':
      case 'MessageUnreact': {
        const m = MB.messageById(ev.channel, ev.id);
        if (m) {
          m.reactions = m.reactions || {};
          const list = new Set(m.reactions[ev.emoji_id] || []);
          if (ev.type === 'MessageReact') list.add(ev.user_id); else list.delete(ev.user_id);
          if (list.size) m.reactions[ev.emoji_id] = Array.from(list); else delete m.reactions[ev.emoji_id];
          if (cur.channel === ev.channel) MB.renderMessages(MB.channel(ev.channel));
        }
        break;
      }
      case 'MessageClearReaction': {
        const m = MB.messageById(ev.channel, ev.id);
        if (m) {
          if (ev.emoji_id) delete (m.reactions || {})[ev.emoji_id];
          if (ev.user_id && m.reactions) for (const k of Object.keys(m.reactions)) m.reactions[k] = m.reactions[k].filter((u) => u !== ev.user_id);
          if (cur.channel === ev.channel) MB.renderMessages(MB.channel(ev.channel));
        }
        break;
      }
      case 'ChannelStartTyping':
      case 'ChannelStopTyping':
        if (cur.channel === ev.id) MB.renderTyping(MB.channel(ev.id));
        break;
      case 'ServerMemberUpdate': {
        const sid = ev.id, uid = ev.data && (ev.data.user || (ev.data._id && ev.data._id.user));
        if (sid && uid) {
          const mem = (s.members[sid] || {})[uid];
          if (mem && ev.data) {
            if (ev.data.nickname !== undefined) mem.nickname = ev.data.nickname;
            if (ev.data.roles) mem.roles = ev.data.roles;
          }
          if (cur.server === sid) { MB.renderMembers(); if (cur.channel) MB.renderMessages(MB.channel(cur.channel)); }
        }
        break;
      }
      case 'ChannelAck':
      case 'MessagePin':
        break;
      case 'UserUpdate': {
        if (ev.id && ev.data) {
          const u = s.users[ev.id] || { _id: ev.id };
          Object.assign(u, ev.data);
          s.users[ev.id] = u;
          if (ev.data.avatar !== undefined) softRefresh();
        }
        break;
      }
      case 'ChannelCreate':
      case 'ChannelDelete':
      case 'ChannelUpdate':
      case 'ServerCreate':
      case 'ServerUpdate':
      case 'ServerDelete':
      case 'ServerMemberJoin':
      case 'ServerMemberRemove':
      case 'UserRelationshipUpdate':
      case 'ServerRoleUpdate':
      case 'ServerRoleDelete':
        softRefresh();
        if (cur.channel) MB.renderComposer(MB.channel(cur.channel));
        break;
      default:
        break;
    }
  });

  // typing indicator rerender on typing events already handled above; keep
  // members pane fresh when presence changes
  MB.bus.on('typing', () => { const cur = MB.current(); if (cur.channel) MB.renderTyping(MB.channel(cur.channel)); });

  // ----------------------------------------------------------- message ops
  function pct(v) { return encodeURIComponent(v); }

  async function setReaction(cid, mid, key, add) {
    const m = MB.messageById(cid, mid);
    const me = MB.store.me && MB.store.me._id;
    if (m) {
      m.reactions = m.reactions || {};
      const set = new Set(m.reactions[key] || []);
      if (add) set.add(me); else set.delete(me);
      if (set.size) m.reactions[key] = Array.from(set); else delete m.reactions[key];
      if (MB.current().channel === cid) MB.renderMessages(MB.channel(cid));
    }
    try {
      await MB.api(add ? 'PUT' : 'DELETE', `/channels/${cid}/messages/${mid}/reactions/${pct(key)}`);
    } catch (err) {
      if (m) {
        const set = new Set(m.reactions[key] || []);
        if (add) set.delete(me); else set.add(me);
        if (set.size) m.reactions[key] = Array.from(set); else delete m.reactions[key];
        if (MB.current().channel === cid) MB.renderMessages(MB.channel(cid));
      }
      MB.toast(err.message, 'error');
    }
  }

  MB.react = function (mid, emojiValue, cid, mine) {
    const key = MB.emojiKey(emojiValue);
    if (!key) { MB.toast('Unknown emoji', 'error'); return; }
    const m = MB.messageById(cid, mid);
    const me = MB.store.me && MB.store.me._id;
    const had = !!(m && m.reactions && (m.reactions[key] || []).includes(me));
    setReaction(cid, mid, key, !had);
  };

  MB.editMessage = function (m, cid) {
    if (!m) return;
    const me = MB.store.me && MB.store.me._id;
    if ((m.user || m.author) !== me) { MB.toast("magbot can only edit its own messages", 'error'); return; }
    MB.setEditing(m);
  };

  MB.deleteMessage = async function (m, cid) {
    if (!m) return;
    const ok = await MB.confirm('Delete message', 'Delete this message for everyone?', 'Delete');
    if (!ok) return;
    try {
      await MB.api('DELETE', `/channels/${cid}/messages/${m._id}`);
      MB.messages[cid] = (MB.messages[cid] || []).filter((x) => x._id !== m._id);
      if (MB.current().channel === cid) MB.renderMessages(MB.channel(cid));
      MB.toast('deleted');
    } catch (err) { MB.toast(err.message, 'error'); }
  };

  MB.messageMenu = function (m, ch, e) {
    const me = MB.store.me && MB.store.me._id;
    const mine = (m.user || m.author) === me;
    const server = ch && ch.server ? MB.serverById(ch.server) : null;
    const manage = MB.can(server, ch, 'ManageMessages');
    const items = [
      { label: 'Add reaction', icon: '😊', action: () => MB.emojiPicker(document.body, (v) => MB.react(m._id, v, ch._id)) },
      { label: 'Reply', icon: '↩', action: () => MB.setReply(m) },
      { label: 'Copy text', icon: '⧉', action: () => navigator.clipboard.writeText(m.content || '').then(() => MB.toast('copied')) },
      { label: 'Copy message ID', icon: '#', action: () => navigator.clipboard.writeText(m._id).then(() => MB.toast('copied')) },
      { sep: true },
    ];
    if (mine) items.push({ label: 'Edit', icon: '✎', action: () => MB.editMessage(m, ch._id) });
    items.push({ label: MB.pins[ch._id] && MB.pins[ch._id].has(m._id) ? 'Unpin' : 'Pin', icon: '📌', action: () => togglePin(m, ch._id, server) });
    if (mine || manage) items.push({ label: 'Delete', icon: '🗑', danger: true, action: () => MB.deleteMessage(m, ch._id) });
    MB.menu(items, e.clientX, e.clientY);
  };

  async function togglePin(m, cid, server) {
    const pinned = MB.pins[cid] && MB.pins[cid].has(m._id);
    MB.pins[cid] = MB.pins[cid] || new Set();
    try {
      await MB.api(pinned ? 'DELETE' : 'POST', `/channels/${cid}/messages/${m._id}/pin`);
      if (pinned) MB.pins[cid].delete(m._id); else MB.pins[cid].add(m._id);
      MB.toast(pinned ? 'unpinned' : 'pinned');
      if (MB.current().channel === cid) MB.renderMessages(MB.channel(cid));
    } catch (err) { MB.toast(err.message, 'error'); }
  }

  // ---------------------------------------------------------- channel menus
  MB.channelMenu = function (c, server, e) {
    const cid = c._id;
    const items = [];
    if (c.server) {
      const canEdit = MB.can(server, c, 'ManageChannel');
      if (canEdit) items.push({ label: 'Edit channel', icon: '✎', action: () => editChannel(c, server) });
      if (MB.can(server, c, 'ManagePermissions')) items.push({ label: 'Permissions', icon: '🔒', action: () => channelPerms(c, server) });
      items.push({ label: 'Copy channel ID', icon: '#', action: () => navigator.clipboard.writeText(cid).then(() => MB.toast('copied')) });
      items.push({ label: 'Mark as read', icon: '✓', action: () => { MB.markRead(cid); MB.renderSidebar(); } });
      if (canEdit) { items.push({ sep: true }); items.push({ label: 'Delete channel', icon: '🗑', danger: true, action: () => deleteChannel(c, server) }); }
    } else if (c.channel_type === 'Group') {
      items.push({ label: 'Group members', icon: '👥', action: () => MB.openChannelInfo(c, null) });
      if (c.owner === (MB.store.me && MB.store.me._id)) items.push({ label: 'Rename group', icon: '✎', action: () => renameGroup(c) });
      items.push({ label: 'Copy group ID', icon: '#', action: () => navigator.clipboard.writeText(cid).then(() => MB.toast('copied')) });
      items.push({ sep: true });
      items.push({ label: 'Leave group', icon: '🚪', danger: true, action: () => leaveGroup(c) });
    } else {
      const other = (c.recipients || []).find((r) => r !== (MB.store.me && MB.store.me._id));
      if (other) items.push({ label: 'View profile', icon: '👤', action: () => MB.openProfile(other, null) });
      items.push({ label: 'Copy channel ID', icon: '#', action: () => navigator.clipboard.writeText(cid).then(() => MB.toast('copied')) });
      items.push({ label: 'Mark as read', icon: '✓', action: () => { MB.markRead(cid); MB.renderSidebar(); } });
    }
    MB.menu(items, e.clientX, e.clientY);
  };

  async function editChannel(c, server) {
    const body = h(`<div class="form">
      <label>Name<input type="text" id="ch-name"></label>
      <label>Description<input type="text" id="ch-desc"></label>
      <label class="check"><input type="checkbox" id="ch-nsfw"> Age-restricted channel</label>
      <div class="row end"><button class="btn ghost">Cancel</button><button class="btn primary">Save</button></div></div>`);
    $('#ch-name', body).value = c.name || '';
    $('#ch-desc', body).value = c.description || '';
    $('#ch-nsfw', body).checked = !!c.nsfw;
    const m = MB.modal('Edit channel', body);
    $('.ghost', body).onclick = () => m.close();
    $('.primary', body).onclick = async () => {
      try {
        await MB.api('PATCH', `/channels/${c._id}`, { name: $('#ch-name', body).value.trim(), description: $('#ch-desc', body).value.trim(), nsfw: $('#ch-nsfw', body).checked });
        m.close(); MB.toast('channel updated'); MB.renderSidebar(); MB.renderChat();
      } catch (err) { MB.toast(err.message, 'error'); }
    };
  }

  async function channelPerms(c, server) {
    const body = h('<div class="form"><p class="muted">Choose a role to edit its allow/deny bits in this channel.</p><div id="p-roles"></div></div>');
    const roles = Object.values(server.roles || {});
    const wrap = $('#p-roles', body);
    for (const r of roles.sort((a, b) => a.rank - b.rank)) {
      const row = h('<button class="row-btn"><span class="txt"><b></b><small></small></span></button>');
      $('b', row).textContent = r.name;
      const ov = (c.role_permissions || {})[r._id] || { a: 0, d: 0 };
      $('small', row).textContent = `allow ${ov.a} · deny ${ov.d}`;
      row.onclick = () => { m.close(); permEditor(c, r, ov); };
      wrap.appendChild(row);
    }
    const m = MB.modal(`Permissions · #${c.name}`, body, { wide: true });
  }

  function permEditor(c, role, ov) {
    const bits = Object.entries(MB.P).filter(([k]) => !['_'].includes(k));
    const body = h('<div class="form"><p class="muted">Role: <b id="rn"></b></p><div class="perm-grid" id="pg"></div><div class="row end"><button class="btn ghost">Cancel</button><button class="btn primary">Save</button></div></div>');
    $('#rn', body).textContent = role.name;
    const grid = $('#pg', body);
    let allow = BigInt(ov.a || 0), deny = BigInt(ov.d || 0);
    for (const [name, bit] of bits) {
      if (typeof bit !== 'bigint') continue;
      const row = h(`<div class="perm-row"><span>${MB.PERM_LABELS[name] || name}</span>
        <select><option value="i">inherit</option><option value="a">allow</option><option value="d">deny</option></select></div>`);
      const sel = $('select', row);
      sel.value = (allow & bit) ? 'a' : ((deny & bit) ? 'd' : 'i');
      sel.onchange = () => {
        allow &= ~bit; deny &= ~bit;
        if (sel.value === 'a') allow |= bit; else if (sel.value === 'd') deny |= bit;
      };
      grid.appendChild(row);
    }
    const m = MB.modal(`Permissions · ${role.name}`, body, { wide: true });
    $('.ghost', body).onclick = () => m.close();
    $('.primary', body).onclick = async () => {
      try {
        await MB.api('PUT', `/channels/${c._id}/permissions/${role._id}`, { allow: Number(allow), deny: Number(deny) });
        m.close(); MB.toast('permissions updated');
      } catch (err) { MB.toast(err.message, 'error'); }
    };
  }

  async function deleteChannel(c, server) {
    const ok = await MB.confirm('Delete channel', `Delete #${c.name}? This cannot be undone.`, 'Delete');
    if (!ok) return;
    try { await MB.api('DELETE', `/channels/${c._id}`); MB.toast('channel deleted'); }
    catch (err) { MB.toast(err.message, 'error'); }
  }

  async function renameGroup(c) {
    const name = await MB.prompt('Rename group', c.name || '', 'group name');
    if (!name) return;
    try { await MB.api('PATCH', `/channels/${c._id}`, { name }); MB.toast('renamed'); MB.renderSidebar(); MB.renderChat(); }
    catch (err) { MB.toast(err.message, 'error'); }
  }

  async function leaveGroup(c) {
    const ok = await MB.confirm('Leave group', 'Remove magbot from this group chat?', 'Leave');
    if (!ok) return;
    try {
      await MB.api('DELETE', `/channels/${c._id}/recipients/${MB.store.me._id}`);
      MB.toast('left group'); MB.renderSidebar(); MB.renderRail();
    } catch (err) { MB.toast(err.message, 'error'); }
  }

  // ------------------------------------------------------------ channel info
  MB.openChannelInfo = async function (ch, server) {
    if (!ch) return;
    const body = h(`<div class="chan-info"><h3 class="ci-title"></h3><div class="kv" id="kv"></div><div id="ci-extra"></div></div>`);
    $('.ci-title', body).textContent = MB.channelTitle(ch);
    const kv = $('#kv', body);
    const add = (k, v) => {
      const r = h('<div class="kv-row"><span class="k"></span><span class="v"></span></div>');
      $('.k', r).textContent = k; $('.v', r).textContent = v; kv.appendChild(r);
    };
    add('Type', ch.channel_type);
    add('ID', ch._id);
    add('Created', new Date(MB.snowflakeTime(ch._id)).toLocaleString());
    if (ch.description) add('Topic', ch.description);
    if (ch.nsfw) add('Age restricted', 'yes');
    if (server) {
      add('Server', server.name);
      const perms = MB.permsFor(server, ch, MB.store.me._id);
      add('magbot can send', perms.has('SendMessage') ? 'yes' : 'no');
      add('magbot can manage messages', perms.has('ManageMessages') ? 'yes' : 'no');
    }
    const extra = $('#ci-extra', body);
    if (ch.channel_type === 'Group' || ch.channel_type === 'DirectMessage') {
      const hd = h('<h4 class="ci-h">Recipients</h4>');
      extra.appendChild(hd);
      const list = h('<div class="member-list"></div>');
      for (const uid of ch.recipients || []) {
        const u = await MB.ensureUser(uid);
        const row = h('<div class="member-row"><span class="av"></span><span class="nm"></span><span class="sp"></span></div>');
        $('.av', row).appendChild(MB.avatar(uid, 28, { status: true }));
        $('.nm', row).textContent = u ? MB.userName(u) : uid;
        if (ch.owner === (MB.store.me && MB.store.me._id) && uid !== MB.store.me._id) {
          const b = h('<button class="btn tiny danger">Remove</button>');
          b.onclick = async () => {
            try { await MB.api('DELETE', `/channels/${ch._id}/recipients/${uid}`); b.textContent = 'removed'; }
            catch (err) { MB.toast(err.message, 'error'); }
          };
          $('.sp', row).appendChild(b);
        }
        list.appendChild(row);
      }
      extra.appendChild(list);
    }
    const notes = h(`<div class="notes"><h4 class="ci-h">Platform limits magbot hits here</h4>
      <ul class="muted small">
        <li>no message search (bot tokens are blocked)</li>
        <li>no typing indicator (route removed for bots)</li>
        <li>no read receipts / marking unread on the server (blocked)</li>
        <li>no invite creation (blocked for every bot)</li>
      </ul></div>`);
    extra.appendChild(notes);
    MB.modal('Channel info', body, { wide: true });
  };
  // ------------------------------------------------------------ server menu
  MB.openServerMenu = function (server, e) {
    const items = [
      { label: 'Server info', icon: 'ℹ', action: () => serverInfo(server) },
      { label: 'Create channel', icon: '＋', action: () => MB.createChannel(server) },
      { label: 'Copy server ID', icon: '#', action: () => navigator.clipboard.writeText(server._id).then(() => MB.toast('copied')) },
      { label: 'Mark server as read', icon: '✓', action: () => { for (const c of MB.serverChannels(server)) MB.markRead(c._id); MB.renderSidebar(); MB.renderRail(); } },
      { sep: true },
    ];
    if (MB.can(server, null, 'ManageCustomisation')) items.push({ label: 'Emoji', icon: '😺', action: () => serverEmojis(server) });
    if (MB.can(server, null, 'ManageRole')) items.push({ label: 'Roles', icon: '🎭', action: () => serverRoles(server) });
    if (MB.can(server, null, 'BanMembers')) items.push({ label: 'Bans', icon: '🔨', action: () => serverBans(server) });
    if (MB.can(server, null, 'ManageServer')) items.push({ label: 'Server settings', icon: '⚙', action: () => serverSettings(server) });
    MB.menu(items, e.clientX, e.clientY);
  };

  async function serverInfo(server) {
    const body = h('<div class="chan-info"><div class="si-head"></div><div class="kv" id="kv"></div><div id="extra"></div></div>');
    $('.si-head', body).appendChild(MB.serverIcon(server, 56, true));
    const info = h('<div class="si-text"><h3></h3><p class="muted small" id="desc"></p></div>');
    $('h3', info).textContent = server.name;
    $('p', info).textContent = server.description || 'no description';
    $('.si-head', body).appendChild(info);
    const kv = $('#kv', body);
    const add = (k, v) => { const r = h('<div class="kv-row"><span class="k"></span><span class="v"></span></div>'); $('.k', r).textContent = k; $('.v', r).textContent = v; kv.appendChild(r); };
    add('ID', server._id);
    add('Created', new Date(MB.snowflakeTime(server._id)).toLocaleString());
    const members = Object.keys(MB.store.members[server._id] || {}).length;
    add('Members', String(members || server.approximate_member_count || '?'));
    add('Channels', String((server.channels || []).length));
    const emos = MB.store.emojiByServer[server._id] || [];
    add('Custom emoji', String(emos.length));
    if (server.analytics) add('Messages sent', String(server.analytics.messages || 0));
    const me = MB.store.me && MB.store.me._id;
    if (me === server.owner) add('magbot is', 'server owner');
    const perms = MB.permsFor(server, null, me);
    const granted = Object.keys(MB.P.A).filter((k) => perms.has(k));
    add('magbot permissions', granted.length ? granted.join(', ') : 'none');
    const btn = h('<button class="btn tiny">View owner profile</button>');
    btn.onclick = () => { m.close(); MB.openProfile(server.owner, server); };
    $('#extra', body).appendChild(btn);
    const m = MB.modal(server.name, body, { wide: true });
  }

  async function serverSettings(server) {
    const body = h(`<div class="form">
      <div class="avatar-edit" id="ico"></div>
      <label>Server name<input type="text" id="s-name"></label>
      <label>Description<textarea rows="3" id="s-desc"></textarea></label>
      <div class="row end"><button class="btn ghost">Cancel</button><button class="btn primary">Save</button></div></div>`);
    $('#s-name', body).value = server.name || '';
    $('#s-desc', body).value = server.description || '';
    const m = MB.modal('Server settings', body, { wide: true });
    $('.ghost', body).onclick = () => m.close();
    $('.primary', body).onclick = async () => {
      try {
        await MB.api('PATCH', `/servers/${server._id}`, { name: $('#s-name', body).value.trim(), description: $('#s-desc', body).value.trim() });
        m.close(); MB.toast('server updated'); MB.renderRail(); MB.renderSidebar();
      } catch (err) { MB.toast(err.message, 'error'); }
    };
  }

  async function serverEmojis(server) {
    const body = h(`<div class="chan-info"><div class="emoji-grid" id="g"></div>
      <div class="row"><input type="file" id="ef" accept="image/png,image/jpeg,image/webp,image/gif"><button class="btn primary" id="eu">Upload emoji</button></div></div>`);
    const g = $('#g', body);
    const list = MB.store.emojiByServer[server._id] || [];
    for (const e of list) {
      const cell = h('<div class="emoji-cell"><img alt=""><span class="en"></span><button class="icon-btn del" title="delete">✕</button></div>');
      $('img', cell).src = MB.emojiUrl(e._id, e.animated);
      $('.en', cell).textContent = ':' + e.name + ':';
      $('.del', cell).onclick = async () => {
        try { await MB.api('DELETE', `/emoji/${e._id}`); cell.remove(); MB.toast('emoji deleted'); }
        catch (err) { MB.toast(err.message, 'error'); }
      };
      g.appendChild(cell);
    }
    const m = MB.modal('Server emoji', body, { wide: true });
    $('#eu', body).onclick = async () => {
      const f = $('#ef', body).files[0];
      if (!f) return;
      try {
        const fd = new FormData(); fd.append('file', f);
        const up = await MB.api('POST', '/upload', fd);
        const name = (f.name || 'emoji').replace(/\.[a-z0-9]+$/i, '').replace(/[^a-zA-Z0-9_\-]/g, '_').slice(0, 32);
        await MB.api('POST', `/servers/${server._id}/emojis`, { name, image: up.id, nsfw: false, animated: (f.type === 'image/gif') });
        MB.toast('emoji added'); m.close();
      } catch (err) { MB.toast(err.message, 'error'); }
    };
  }

  async function serverRoles(server) {
    const body = h('<div class="chan-info"><div id="rl"></div><button class="btn primary" id="nr">Create role</button></div>');
    const rl = $('#rl', body);
    for (const r of Object.values(server.roles || {}).sort((a, b) => a.rank - b.rank)) {
      const row = h('<div class="role-row"><span class="dot"></span><b></b><span class="muted small"></span><button class="icon-btn del">✕</button></div>');
      $('.dot', row).style.background = r.colour ? '#' + r.colour : 'var(--muted)';
      $('b', row).textContent = r.name;
      $('.muted', row).textContent = `rank ${r.rank}${r.hoist ? ' · hoisted' : ''}`;
      $('.del', row).onclick = async () => {
        const ok = await MB.confirm('Delete role', `Delete role ${r.name}?`, 'Delete');
        if (!ok) return;
        try { await MB.api('DELETE', `/servers/${server._id}/roles/${r._id}`); row.remove(); MB.toast('role deleted'); }
        catch (err) { MB.toast(err.message, 'error'); }
      };
      rl.appendChild(row);
    }
    const m = MB.modal('Roles', body, { wide: true });
    $('#nr', body).onclick = async () => {
      const name = await MB.prompt('Create role', '', 'role name');
      if (!name) return;
      try { await MB.api('POST', `/servers/${server._id}/roles`, { name }); MB.toast('role created'); m.close(); }
      catch (err) { MB.toast(err.message, 'error'); }
    };
  }

  async function serverBans(server) {
    const body = h('<div class="chan-info"><div id="bl" class="member-list"></div></div>');
    try {
      const res = await MB.api('GET', `/servers/${server._id}/bans`);
      const list = $('#bl', body);
      const users = (res && res.users) || [];
      for (const b of (res && res.bans) || []) {
        const u = users.find((x) => x._id === (b._id && b._id.user));
        const row = h('<div class="member-row"><span class="av"></span><span class="nm"></span><button class="btn tiny">Unban</button></div>');
        $('.av', row).appendChild(MB.avatar(b._id && b._id.user, 28));
        $('.nm', row).textContent = u ? MB.userName(u) : (b._id && b._id.user);
        $('button', row).onclick = async () => {
          try { await MB.api('DELETE', `/servers/${server._id}/bans/${b._id.user}`); row.remove(); MB.toast('unbanned'); }
          catch (err) { MB.toast(err.message, 'error'); }
        };
        list.appendChild(row);
      }
      if (!((res && res.bans) || []).length) list.appendChild(h('<p class="muted">No bans.</p>'));
    } catch (err) {
      $('#bl', body).appendChild(h('<p class="muted">magbot cannot read bans here (needs BanMembers).</p>'));
    }
    MB.modal('Bans', body, { wide: true });
  }

  MB.createChannel = async function (server) {
    const name = await MB.prompt('Create channel', '', 'channel name');
    if (!name) return;
    try {
      await MB.api('POST', `/servers/${server._id}/channels`, { name: name.replace(/\s+/g, '-').toLowerCase(), type: 'Text' });
      MB.toast('channel created'); MB.renderSidebar();
    } catch (err) { MB.toast(err.message, 'error'); }
  };

  MB.openNewGroup = function () {
    const body = h(`<div class="chan-info">
      <p class="muted">Stoat blocks bot accounts from creating new group chats, so this pane manages the
      groups magbot is already in. To add someone, ask them to add magbot themselves.</p>
      <div id="gl" class="row-list"></div></div>`);
    const gl = $('#gl', body);
    const groups = MB.dmList().filter((d) => d.kind === 'group');
    for (const g of groups) {
      const row = h('<div class="row-btn"><span class="txt"><b></b><small></small></span></button>');
      $('b', row).textContent = MB.dmName(g);
      $('small', row).textContent = (g.recipients || []).map((r) => { const u = MB.store.users[r]; return u ? MB.userName(u) : r; }).join(', ');
      row.onclick = () => { m.close(); MB.pickChannel(g._id, null); };
      gl.appendChild(row);
    }
    if (!groups.length) gl.appendChild(h('<p class="muted">magbot is not in any group chats.</p>'));
    const m = MB.modal('Group chats', body, { wide: true });
  };

  // ---------------------------------------------------------- quick switcher
  MB.openSwitcher = function () {
    const body = h(`<div class="switcher"><input type="text" placeholder="Jump to a server, channel or DM…" id="sw-in"><div id="sw-list" class="row-list"></div></div>`);
    const m = MB.modal('Quick switcher', body);
    const input = $('#sw-in', body);
    const list = $('#sw-list', body);
    const entries = () => {
      const out = [];
      for (const s of MB.store.servers) {
        out.push({ kind: 'server', label: s.name, sub: 'server', id: s._id });
        for (const c of MB.serverChannels(s)) out.push({ kind: 'channel', label: '#' + (c.name || ''), sub: s.name, id: c._id, server: s._id });
      }
      for (const d of MB.dmList()) out.push({ kind: d.kind, label: MB.dmName(d), sub: d.kind === 'group' ? 'group' : (d.kind === 'saved' ? 'saved' : 'dm'), id: d._id });
      return out;
    };
    let rows = [];
    const draw = (q) => {
      list.innerHTML = '';
      rows = [];
      const all = entries().filter((e) => !q || e.label.toLowerCase().includes(q) || e.sub.toLowerCase().includes(q));
      for (const e of all.slice(0, 40)) {
        const r = h('<button class="row-btn"><span class="txt"><b></b><small></small></span></button>');
        $('b', r).textContent = e.label || e.id;
        $('small', r).textContent = e.sub;
        r.onclick = () => pick(e);
        list.appendChild(r);
        rows.push(r);
      }
    };
    function pick(e) {
      m.close();
      if (e.kind === 'server') MB.pickChannel(null, e.id);
      else MB.pickChannel(e.id, e.server === undefined ? null : e.server);
    }
    input.oninput = () => draw(input.value.trim().toLowerCase());
    input.onkeydown = (ev) => {
      if (ev.key === 'Enter' && rows.length) pick(entries().filter((e) => e.label.toLowerCase().includes(input.value.trim().toLowerCase()) || !input.value)[0]);
      if (ev.key === 'ArrowDown') { ev.preventDefault(); if (rows[0]) rows[0].focus(); }
    };
    draw('');
    setTimeout(() => input.focus(), 30);
  };

  // ----------------------------------------------------------- me bar/status
  // The bot daemon keeps running on the VPS while this UI holds its own
  // gateway session; surface that so it's obvious.
  MB.pollEngine = async function () {
    let st;
    try { st = await MB.api('GET', '/botstatus'); } catch (e) { return; }
    const pill = document.getElementById('engine-pill');
    if (!pill) return;
    const on = !!st.engine;
    pill.className = 'engine-pill ' + (on ? 'ok' : 'bad');
    pill.textContent = on ? 'bot online' : 'bot offline';
    const mins = on && st.engine.age != null ? Math.round(st.engine.age / 60) : null;
    pill.title = (on ? `magbot pid ${st.engine.pid}` + (mins != null ? ` · up ${mins} min` : '') : 'magbot is not running')
      + '\nmessages you type here do NOT trigger the AI (loop guard) — it keeps answering everyone else normally.';
  };

  MB.renderMeBar = function () {
    const bar = $('#me-bar');
    if (!bar || !MB.store.me) return;
    bar.innerHTML = '';
    const me = MB.store.me;
    const wrap = h('<div class="me-wrap"><span class="me-av"></span><span class="me-txt"><b></b><small></small></span><span class="engine-pill" id="engine-pill" title="magbot engine"></span><span class="conn-dot" id="conn-dot"></span></div>');
    $('.me-av', wrap).appendChild(MB.avatar(me._id, 34, { status: true }));
    $('b', wrap).textContent = MB.userName(me);
    $('small', wrap).textContent = (me.status && me.status.text) || (me.bot ? 'bot account' : '');
    $('.conn-dot', wrap).id = 'conn-dot';
    $('.conn-dot', wrap).className = 'conn-dot ' + (MB.store.gateway ? 'ok' : 'bad');
    wrap.onclick = () => MB.openUserSettings();
    bar.appendChild(wrap);
    MB.pollEngine();
  };

  MB.openUserSettings = function () {
    const me = MB.store.me || {};
    const st = me.status || {};
    const body = h(`<div class="form">
      <div class="settings-head"><div class="avatar-edit" id="av"></div>
        <div class="row"><input type="file" id="avf" accept="image/*"><button class="btn tiny" id="avu">Upload</button><button class="btn tiny ghost" id="avr">Remove</button></div>
      </div>
      <label>Display name<input type="text" id="d-name" placeholder="Fembot"></label>
      <label>Pronouns<input type="text" id="d-pron" placeholder="clanker"></label>
      <label>Status text<input type="text" id="d-status" placeholder="what are you up to?"></label>
      <label>Presence<select id="d-pres">
        <option value="Online">Online</option><option value="Idle">Idle</option>
        <option value="Busy">Busy</option><option value="Focus">Focus</option><option value="Invisible">Invisible</option>
      </select></label>
      <hr class="hr">
      <label>Profile bio<textarea rows="4" id="d-bio" placeholder="about magbot"></textarea></label>
      <label>Profile background<input type="text" id="d-bg" placeholder="#1b1030 or a colour"></label>
      <hr class="hr">
      <label>Username<input type="text" id="d-user"><small class="muted" id="d-userhint">changing the username is usually blocked for bots</small></label>
      <div class="row end"><button class="btn ghost">Close</button><button class="btn primary">Save</button></div></div>`);
    $('#av', body).appendChild(MB.avatar(me._id, 72));
    $('#d-name', body).value = me.display_name || '';
    $('#d-pron', body).value = me.pronouns || '';
    $('#d-status', body).value = st.text || '';
    $('#d-pres', body).value = st.presence || 'Online';
    $('#d-bio', body).value = (me.profile && me.profile.content) || '';
    $('#d-bg', body).value = (me.profile && me.profile.background) || '';
    $('#d-user', body).value = me.username || '';
    const m = MB.modal('magbot settings', body, { wide: true });
    $('.ghost', body).onclick = () => m.close();
    $('#avu', body).onclick = async () => {
      const f = $('#avf', body).files[0];
      if (!f) return;
      try {
        const fd = new FormData(); fd.append('file', f);
        const up = await MB.api('POST', '/upload', fd);
        await MB.api('PATCH', '/users/@me', { avatar: up.id });
        MB.toast('avatar updated — reloading'); setTimeout(() => location.reload(), 700);
      } catch (err) { MB.toast(err.message, 'error'); }
    };
    $('#avr', body).onclick = async () => {
      try { await MB.api('PATCH', '/users/@me', { remove: ['Avatar'] }); MB.toast('avatar removed — reloading'); setTimeout(() => location.reload(), 700); }
      catch (err) { MB.toast(err.message, 'error'); }
    };
    $('.primary', body).onclick = async () => {
      const payload = {
        display_name: $('#d-name', body).value.trim() || null,
        pronouns: $('#d-pron', body).value.trim() || null,
        status: { text: $('#d-status', body).value.trim() || null, presence: $('#d-pres', body).value },
        profile: { content: $('#d-bio', body).value.trim() || null, background: $('#d-bg', body).value.trim() || null },
      };
      try {
        await MB.api('PATCH', '/users/@me', payload);
        const newName = $('#d-user', body).value.trim();
        if (newName && newName !== me.username) {
          try { await MB.api('PATCH', '/users/@me/username', { username: newName }); }
          catch (err) { MB.toast('username not changed: ' + err.message, 'error'); }
        }
        MB.toast('saved'); m.close();
        Object.assign(MB.store.me, payload);
        MB.renderMeBar(); MB.renderChat();
      } catch (err) { MB.toast(err.message, 'error'); }
    };
  };

  // ---------------------------------------------------------------- keyboard
  document.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA)$/.test((e.target && e.target.tagName) || '');
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); MB.openSwitcher(); return; }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'u') { e.preventDefault(); MB.openUserSettings(); return; }
    if (e.key === 'Escape' && !document.querySelector('.modal-back')) {
      if (MB.editing) { MB.setEditing(null); return; }
      if (MB.replyTo) { MB.setReply(null); return; }
      // Deliberately NOT blurring the composer: blurring on Esc breaks the
      // "@" picker flow (dismiss popup -> keep typing -> Enter) and drops a
      // half-typed message's focus. Clear reply/edit only.
    }
  });
  // ---------------------------------------------------------------- bootstrap
  // click a rendered @mention -> profile card; #channel -> jump to it
  document.addEventListener('click', (e) => {
    const mu = e.target.closest('[data-mention-user]');
    if (mu) { e.preventDefault(); MB.openProfile(mu.dataset.mentionUser, MB.current().server); return; }
    const mc = e.target.closest('[data-mention-channel]');
    if (mc) {
      e.preventDefault();
      const ch = MB.channel(mc.dataset.mentionChannel);
      if (ch) MB.bus.emit('open', { server: ch.server || null, channel: ch._id });
      else MB.toast('magbot can\'t see that channel', 'error');
    }
  });
  // right-click a message -> actions menu
  const msgBox0 = $('#messages');
  if (msgBox0) msgBox0.addEventListener('contextmenu', (e) => {
    const node = e.target.closest ? e.target.closest('.msg') : null;
    if (!node || !node.dataset.id) return;
    const cid = MB.current().channel;
    const m = MB.messageById(cid, node.dataset.id);
    if (!m) return;
    e.preventDefault();
    MB.messageMenu(m, MB.channel(cid), e);
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  function boot() {
    trySession();
    // poll only once signed in — an unauthenticated call would 401 and bounce
    setInterval(() => { if (MB.store.me) MB.pollEngine(); }, 30000);
  }
})();
