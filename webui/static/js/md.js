/* magbot web UI — helpers: assets, markdown, permissions, formatting.
   Part of a 3-file vanilla-JS client (no build step). */
(function () {
  const MB = (window.MB = window.MB || {});
  const API = 'https://api.stoat.chat/0.8';
  const CDN = 'https://cdn.stoatusercontent.com';

  MB.API = API;
  MB.CDN = CDN;

  // ---------------------------------------------------------------- assets
  MB.asset = (tag, id) => `${CDN}/${tag}/${id}`;
  const defaultCache = new Map();

  MB.asset = (tag, id) => `${MB.CDN || 'https://cdn.stoatusercontent.com'}/${tag}/${id}`;

  MB.avatarUrl = function (user) {
    if (!user || !user._id) return placeholderAvatar(user || {});
    if (user.avatar && user.avatar._id) return MB.asset('avatars', user.avatar._id);
    // Stoat serves a generated default avatar image; let the browser cache it.
    const base = location.pathname.replace(/[^/]*$/, '');
    return `${base}api/users/${user._id}/default_avatar`;
  };

  MB.iconUrl = (server) => (server && server.icon && server.icon._id ? MB.asset('icons', server.icon._id) : null);
  MB.bannerUrl = (server) => (server && server.banner && server.banner._id ? MB.asset('banners', server.banner._id) : null);
  MB.emojiUrl = (id) => MB.asset('emojis', id);

  function hue(str) {
    let h = 0;
    for (let i = 0; i < (str || '').length; i++) h = (h * 31 + str.charCodeAt(i)) % 360;
    return h;
  }

  MB.placeholderColor = (id) => `hsl(${hue(id)} 45% 42%)`;

  function placeholderAvatar(user) {
    const ch = ((user && (user.username || ''))[0] || '?').toUpperCase();
    const c = MB.placeholderColor((user && user._id) || '');
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128"><rect width="128" height="128" rx="64" fill="${c}"/>` +
      `<text x="64" y="82" font-family="system-ui,sans-serif" font-size="58" font-weight="600" fill="rgba(255,255,255,.92)" text-anchor="middle">${ch}</text></svg>`;
    return 'data:image/svg+xml;base64,' + btoa(svg);
  }
  MB.placeholderAvatar = placeholderAvatar;

  MB.userName = (user) => {
    if (!user) return 'unknown';
    return user.display_name || user.username || 'unknown';
  };
  MB.discriminator = (user) => (user && user.discriminator ? '#' + user.discriminator : '');
  MB.tag = (user) => (user ? `${MB.userName(user)}${MB.discriminator(user)}` : 'unknown');

  // ------------------------------------------------------------ formatting
  MB.fmtTime = (d) => new Date(d).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  MB.fmtDate = (d) => {
    const date = new Date(d);
    const now = new Date();
    const sameDay = date.toDateString() === now.toDateString();
    if (sameDay) return 'Today at ' + MB.fmtTime(date);
    const y = new Date(now.getTime() - 86400000).toDateString() === date.toDateString();
    if (y) return 'Yesterday at ' + MB.fmtTime(date);
    return date.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' }) + ' ' + MB.fmtTime(date);
  };
  MB.fmtFull = (d) => new Date(d).toLocaleString();
  MB.fmtDay = (d) => {
    const date = new Date(d);
    const now = new Date();
    if (date.toDateString() === now.toDateString()) return 'Today';
    if (new Date(now.getTime() - 86400000).toDateString() === date.toDateString()) return 'Yesterday';
    return date.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
  };
  MB.fmtSize = (n) => {
    if (n == null) return '';
    for (const u of ['B', 'KB', 'MB', 'GB']) {
      if (n < 1024) return `${n < 10 && u !== 'B' ? n.toFixed(1) : Math.round(n)} ${u}`;
      n /= 1024;
    }
    return n.toFixed(1) + ' TB';
  };

  MB.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  MB.timeAgo = (d) => {
    const s = Math.max(1, Math.floor((Date.now() - new Date(d).getTime()) / 1000));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    if (s < 2592000) return `${Math.floor(s / 86400)}d ago`;
    return MB.fmtDate(d);
  };

  // ------------------------------------------------------------ markdown
  const codeBlocks = [];

  MB.renderMarkdown = function (text, ctx) {
    ctx = ctx || {};
    codeBlocks.length = 0;
    let out = MB.esc(text);

    // code fences
    out = out.replace(/```(\w+)?\n?([\s\S]*?)```/g, (m, lang, code) => {
      const i = codeBlocks.push({ lang: lang || '', code }) - 1;
      return `\u0000CODE${i}\u0000`;
    });
    // inline code
    out = out.replace(/`([^`\n]+)`/g, '<code class="inline">$1</code>');
    // block quotes
    out = out.split('\n').map((line) => (/^&gt;\s?/.test(line) ? `<blockquote>${line.replace(/^&gt;\s?/, '')}</blockquote>` : line)).join('\n');
    // links
    out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>');
    out = out.replace(/(^|[\s(])((?:https?:\/\/|www\.)[^\s<)]+)/g, (m, pre, url) => {
      const href = url.startsWith('http') ? url : 'https://' + url;
      return `${pre}<a href="${href}" target="_blank" rel="noreferrer noopener">${url}</a>`;
    });
    // styling
    out = out.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/(^|[^*\w])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    out = out.replace(/__([^_]+)__/g, '<u>$1</u>');
    out = out.replace(/~~([^~]+)~~/g, '<s>$1</s>');

    // custom emoji :name:
    out = out.replace(/:([A-Za-z0-9_]{1,32}):/g, (m, name) => {
      const e = ctx.emojis && ctx.emojis[name];
      if (!e) return m;
      return `<img class="emoji custom" src="${MB.emojiUrl(e._id)}" alt=":${name}:" title=":${name}:" draggable="false">`;
    });

    // mentions
    out = out.replace(/&lt;@([0-9A-Z]{26})&gt;/g, (m, id) => mention('user', id, ctx));
    out = out.replace(/&lt;#([0-9A-Z]{26})&gt;/g, (m, id) => mention('channel', id, ctx));
    out = out.replace(/&lt;@&amp;([0-9A-Z]{26})&gt;/g, (m, id) => mention('role', id, ctx));
    out = out.replace(/@everyone/g, '<span class="mention">@everyone</span>');
    out = out.replace(/@online/g, '<span class="mention">@online</span>');

    // restore code blocks (escaped content already)
    out = out.replace(/\u0000CODE(\d+)\u0000/g, (m, i) => {
      const b = codeBlocks[+i] || { lang: '', code: '' };
      return `<div class="codeblock"><div class="codeblock-head"><span>${MB.esc(b.lang || 'text')}</span>` +
        `<button class="mini copy-code" data-code="${MB.esc(b.code.trim())}">copy</button></div>` +
        `<pre><code>${MB.esc(b.code.replace(/\n$/, ''))}</code></pre></div>`;
    });
    return out;
  };

  function mention(kind, id, ctx) {
    if (kind === 'user') {
      const u = ctx.users && ctx.users[id];
      const label = u ? '@' + MB.userName(u) : '@unknown';
      const self = ctx.me && id === ctx.me;
      return `<span class="mention${self ? ' self' : ''}" data-mention-user="${id}">${MB.esc(label)}</span>`;
    }
    if (kind === 'channel') {
      const c = ctx.channels && ctx.channels[id];
      const label = c ? '#' + (c.name || 'channel') : '#unknown';
      return `<span class="mention channel" data-mention-channel="${id}">${MB.esc(label)}</span>`;
    }
    const srv = ctx.server && ctx.server.roles ? ctx.server.roles[id] : null;
    return `<span class="mention role" data-mention-role="${id}">@${MB.esc(srv ? srv.name : 'role')}</span>`;
  }
  MB.mention = mention;

  MB.splitSystemMessage = (content) => {
    // capitalise + tidy the platform's system messages ("{user} joined the server")
    return MB.esc(content).replace(/&lt;(@|#)([0-9A-Z]{26})&gt;/g, (m, k, id) => `<span data-mention-${k === '@' ? 'user' : 'channel'}="${id}">${k === '@' ? '@user' : '#channel'}</span>`);
  };

  // ------------------------------------------------------------- mentions
  // The composer shows "@Display Name"; Stoat needs "<@ULID>" in the content
  // (the server parses mentions out of the message body — there is no
  // `mentions` field on the send payload).
  // Inverse of mentionsToContent: show "<@ULID>" as "@Display Name" when a
  // message is opened in the editor, so nobody has to read raw ULIDs.
  MB.contentToDisplay = function (text) {
    const cur = MB.current();
    let out = String(text || '');
    out = out.replace(/<@([0-9A-Z]{26})>/g, (m, id) => {
      const u = MB.store.users[id];
      return u ? '@' + MB.displayName(cur.server, id) : m;
    });
    out = out.replace(/<#([0-9A-Z]{26})>/g, (m, id) => {
      const c = MB.channel(id);
      return c ? '#' + c.name : m;
    });
    return out;
  };

  MB.mentionsToContent = function (text, pending) {
    if (!text) return text;
    let out = String(text);
    const sub = (token, id) => {
      const esc = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      out = out.replace(new RegExp('(^|\\s|<)' + esc + '(?=\\s|$|[.,!?;:)])', 'gi'),
                        (mm, pre) => pre + '<@' + id + '>');
    };
    for (const p of (pending || []).slice().sort((a, b) => b.text.length - a.text.length)) sub(p.text, p.id);
    const names = [], seen = new Set();
    const add = (serverId, uid) => {
      const u = MB.store.users[uid];
      if (!u || seen.has(uid)) return;
      seen.add(uid);
      for (const n of [MB.displayName(serverId, uid), u.display_name, u.username]) {
        if (n && !/[<>]/.test(n)) names.push({ n: String(n), id: uid });
      }
    };
    const cur = MB.current();
    if (cur.server) Object.keys(MB.store.members[cur.server] || {}).forEach((uid) => add(cur.server, uid));
    else ((MB.channel(cur.channel) || {}).recipients || []).forEach((uid) => add(null, uid));
    names.sort((a, b) => b.n.length - a.n.length);
    const done = new Set();
    for (const k of names) {
      if (done.has(k.id)) continue;
      done.add(k.id);
      sub('@' + k.n, k.id);
    }
    return out;
  };

  // ---------------------------------------------------------- permissions
  // Stoat permission bits run past 31, so the maths uses BigInt (plain JS
  // bitwise ops are 32-bit signed and would silently corrupt them).
  const bt = (n) => (1n << BigInt(n));
  const P = {
    A: {
      ManageChannel: bt(1), ManageServer: bt(2), ManagePermissions: bt(3), ManageRole: bt(4),
      ManageCustomisation: bt(5), KickMembers: bt(6), BanMembers: bt(7), TimeoutMembers: bt(8),
      AssignRoles: bt(9), ChangeNickname: bt(10), ManageNicknames: bt(11), ChangeAvatar: bt(12),
      RemoveAvatars: bt(13), ViewChannel: bt(20), ReadMessageHistory: bt(21), SendMessage: bt(22),
      ManageMessages: bt(23), EmbedLinks: bt(24), UploadFiles: bt(25), DeleteOtherMessages: bt(26),
      MentionEveryone: bt(27), InviteOthers: bt(28), ManageWebhooks: bt(29), SendEmbeds: bt(30),
      React: bt(31), Connect: bt(32), Speak: bt(33), Video: bt(34), MuteMembers: bt(35),
      DeafenMembers: bt(36), MoveMembers: bt(37),
    },
  };
  MB.P = P.A;
  MB.PERM_LABELS = {
    ManageChannel: 'Manage Channel', ManageServer: 'Manage Server', ManagePermissions: 'Manage Permissions',
    ManageRole: 'Manage Roles', ManageCustomisation: 'Manage Emoji', KickMembers: 'Kick Members',
    BanMembers: 'Ban Members', TimeoutMembers: 'Timeout Members', AssignRoles: 'Assign Roles',
    ChangeNickname: 'Change Nickname', ManageNicknames: 'Manage Nicknames', ViewChannel: 'View Channel',
    ReadMessageHistory: 'Read History', SendMessage: 'Send Messages', ManageMessages: 'Manage Messages',
    EmbedLinks: 'Embed Links', UploadFiles: 'Attach Files', MentionEveryone: 'Mention Everyone',
    InviteOthers: 'Create Invites', SendEmbeds: 'Send Embeds', React: 'Add Reactions',
  };

  MB.permsFor = function (server, channel, userId) {
    const ALL = (1n << 64n) - 1n;
    const none = { bits: 0n, has: () => false, raw: () => false };
    if (!server) return none;
    if (server.owner === userId) return { bits: ALL, has: () => true, raw: () => true };
    const roles = server.roles || {};
    const member = (MB.store.members[server._id] || {})[userId];
    let bits = 0n;
    const apply = (perm) => {
      if (perm === null || perm === undefined) return;
      if (typeof perm === 'number') { bits |= BigInt(perm); return; }   // server.default_permissions is plain allow-bits
      if (typeof perm === 'string') { bits |= BigInt(perm); return; }
      bits |= BigInt(perm.a || 0); bits &= ~BigInt(perm.d || 0);
    };
    apply(server.default_permissions);
    const ids = [...((member && member.roles) || [])];
    if (roles[server._id]) ids.push(server._id);
    const sorted = ids.map((id) => roles[id]).filter(Boolean).sort((a, b) => a.rank - b.rank);
    for (const role of sorted) apply(role.permissions);
    if (channel) {
      apply(channel.default_permissions);
      for (const role of sorted) if (channel.role_permissions && channel.role_permissions[role._id]) apply(channel.role_permissions[role._id]);
    }
    return {
      bits,
      has: (name) => { const b = P.A[name]; return b === undefined ? false : (bits & b) === b; },
      raw: (bit) => (bits & BigInt(bit)) === BigInt(bit),
    };
  };

  MB.roleColor = function (server, userId) {
    if (!server || !server.roles) return null;
    const member = (MB.store.members[server._id] || {})[userId];
    const ids = (member && member.roles) || [];
    let best = null;
    for (const id of ids) {
      const r = server.roles[id];
      if (r && r.colour && (!best || r.rank < best.rank)) best = r;
    }
    return best ? best.colour : null;
  };

  MB.memberNick = function (serverId, userId) {
    const m = (MB.store.members[serverId] || {})[userId];
    return m && m.nickname ? m.nickname : null;
  };

  MB.displayName = function (serverId, userId) {
    const nick = serverId ? MB.memberNick(serverId, userId) : null;
    return nick || MB.userName(MB.store.users[userId]);
  };

  MB.rolesOf = function (server, userId) {
    if (!server || !server.roles) return [];
    const m = (MB.store.members[server._id] || {})[userId];
    const ids = (m && m.roles) || [];
    return ids.map((id) => server.roles[id]).filter(Boolean).sort((a, b) => a.rank - b.rank);
  };

  MB.highestRole = (server, userId) => MB.rolesOf(server, userId)[0] || null;
})();
