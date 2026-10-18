/* magbot web UI — rendering layer: rail, sidebar, chat, members, widgets. */
(function () {
  const MB = (window.MB = window.MB || {});
  const el = (sel, root) => (root || document).querySelector(sel);
  const els = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const h = (html) => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; };
  MB.el = el; MB.els = els; MB.h = h;

  MB.messages = {};      // channelId -> array (oldest first)
  MB.hasMore = {};       // channelId -> bool
  MB.loadingMore = {};

  // ------------------------------------------------------------- widgets
  MB.toast = function (msg, kind) {
    const box = el('#toasts');
    const t = h(`<div class="toast ${kind || ''}"></div>`);
    t.textContent = msg;
    box.appendChild(t);
    requestAnimationFrame(() => t.classList.add('in'));
    setTimeout(() => { t.classList.remove('in'); setTimeout(() => t.remove(), 250); }, kind === 'error' ? 6000 : 3500);
  };

  MB.modal = function (title, bodyNode, opts) {
    opts = opts || {};
    const back = h(`<div class="modal-back"></div>`);
    const m = h(`<div class="modal ${opts.wide ? 'wide' : ''}">
      <header><h2></h2><button class="icon-btn close" title="Close">✕</button></header>
      <div class="modal-body"></div></div>`);
    el('h2', m).textContent = title;
    el('.modal-body', m).append(bodyNode);
    back.appendChild(m);
    el('#modals').appendChild(back);
    requestAnimationFrame(() => back.classList.add('in'));
    const close = () => { back.classList.remove('in'); setTimeout(() => back.remove(), 180); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    el('.close', m).onclick = close;
    back.onclick = (e) => { if (e.target === back) close(); };
    return { close, root: m };
  };

  MB.confirm = function (title, message, confirmLabel) {
    return new Promise((resolve) => {
      const body = h(`<div class="confirm"><p></p><div class="row end"><button class="btn ghost">Cancel</button><button class="btn danger"></button></div></div>`);
      el('p', body).textContent = message;
      el('.danger', body).textContent = confirmLabel || 'Confirm';
      const m = MB.modal(title, body);
      el('.danger', body).onclick = () => { m.close(); resolve(true); };
      el('.ghost', body).onclick = () => { m.close(); resolve(false); };
    });
  };

  MB.prompt = function (title, value, placeholder, multiline) {
    return new Promise((resolve) => {
      const body = h(`<div class="prompt">${multiline ? '<textarea rows="4"></textarea>' : '<input type="text">'}<div class="row end"><button class="btn ghost">Cancel</button><button class="btn primary">Save</button></div></div>`);
      const field = multiline ? el('textarea', body) : el('input', body);
      field.value = value || '';
      field.placeholder = placeholder || '';
      const m = MB.modal(title, body);
      setTimeout(() => field.focus(), 30);
      el('.primary', body).onclick = () => { m.close(); resolve(field.value.trim()); };
      el('.ghost', body).onclick = () => { m.close(); resolve(null); };
      field.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !multiline) { m.close(); resolve(field.value.trim()); } });
    });
  };

  MB.menu = function (items, x, y) {
    const existing = el('#menu-pop'); if (existing) existing.remove();
    const pop = h(`<div id="menu-pop" class="popmenu"></div>`);
    for (const it of items) {
      if (it.sep) { pop.appendChild(h('<div class="sep"></div>')); continue; }
      const b = h(`<button class="menu-item ${it.danger ? 'danger' : ''}"></button>`);
      if (it.icon) b.appendChild(h(`<span class="mi-icon">${it.icon}</span>`));
      b.appendChild(h(`<span class="mi-label"></span>`));
      el('.mi-label', b).textContent = it.label;
      b.onclick = () => { pop.remove(); document.removeEventListener('mousedown', away); it.action && it.action(); };
      pop.appendChild(b);
    }
    document.body.appendChild(pop);
    const rect = pop.getBoundingClientRect();
    pop.style.left = Math.min(x, window.innerWidth - rect.width - 8) + 'px';
    pop.style.top = Math.min(y, window.innerHeight - rect.height - 8) + 'px';
    const away = (e) => { if (!pop.contains(e.target)) { pop.remove(); document.removeEventListener('mousedown', away); } };
    setTimeout(() => document.addEventListener('mousedown', away), 0);
    return pop;
  };

  const EMOJI = {
    'Smileys': ['😀','😃','😄','😁','😆','😅','😂','🤣','🥲','🥹','😊','😇','🙂','🙃','😉','😌','😍','🥰','😘','😗','😙','😚','😋','😛','😝','😜','🤪','🤨','🧐','🤓','😎','🥸','🤩','🥳','😏','😒','😞','😔','😟','😕','🙁','😣','😖','😫','😩','🥺','😢','😭','😤','😠','😡','🤬','🤯','😳','🥵','🥶','😱','😨','😰','😥','😓','🤗','🤔','🫣','🤭','🤫','🤥','😶','😐','😑','😬','🙄','😯','😦','😧','😮','😲','🥱','😴','🤤','😪','😵','🤐','🥴','🤢','🤮','🤧','😷','🤒','🤕','🤑','🤠','😈','👿','👹','👺','🤡','💩','👻','💀','☠️','👽','👾','🤖','🎃','😺','😸','😹','😻','😼','😽','🙀','😿','😾','🫶','👋','🤚','🖐','✋','🖖','👌','🤌','🤏','✌️','🤞','🫰','🤟','🤘','🤙','👈','👉','👆','🖕','👇','☝️','👍','👎','✊','👊','🤛','🤜','👏','🙌','🫂','👐','🤲','🤝','🙏','💅','🤳','💪','🦾','🦿','🦵','🦶','👂','🦻','👃','🧠','🫀','🫁','🦷','🦴','👀','👁','👅','👄','💋','🩸'],
    'People': ['👶','🧒','👦','👧','🧑','👨','👩','🧓','👴','👵','🙍','🙎','🙅','🙆','💁','🙋','🧏','🙇','🤦','🤷','👮','🕵','💂','🥷','👷','🤴','👸','👳','👲','🧕','🤵','👰','🤰','🤱','👼','🎅','🤶','🦸','🦹','🧙','🧚','🧛','🧜','🧝','🧞','🧟','💆','💇','🚶','🧍','🧎','🏃','💃','🕺','🕴','👯','🧖','🧗','🤺','🏇','⛷','🏂','🏌','🏄','🚣','🏊','⛹','🏋','🚴','🚵','🤸','🤼','🤽','🤾','🤹','🧘','🛀','🛌','🧑‍🤝‍🧑','💏','💑','💐','💞','💓','💗','💖','💘','💝','💟','❣️','💔','❤️','🧡','💛','💚','💙','💜','🤎','🖤','🤍','💯','💢','💥','💫','💦','💨','🕳','💬','🗨','🗯','💭','💤','✨','⭐','🌟','💠','🔥','🎵','🎶','🎼'],
    'Animals': ['🐶','🐱','🐭','🐹','🐰','🦊','🐻','🐼','🐻‍❄️','🐨','🐯','🦁','🐮','🐷','🐽','🐸','🐵','🙈','🙉','🙊','🐒','🐔','🐧','🐦','🐤','🐣','🐥','🦆','🦅','🦉','🦇','🐺','🐗','🐴','🦄','🐝','🪱','🐛','🦋','🐌','🐞','🐜','🪰','🪲','🕷','🦂','🐢','🐍','🦎','🦖','🦕','🐙','🦑','🦐','🦞','🦀','🐡','🐠','🐟','🐬','🐳','🐋','🦈','🐊','🐅','🐆','🦓','🦍','🦧','🐘','🦛','🦏','🐪','🐫','🦒','🦘','🐃','🐂','🐄','🐎','🐖','🐏','🐑','🦙','🐐','🦌','🐕','🐩','🦮','🐈','🪶','🐓','🦃','🦚','🦜','🦢','🦩','🕊','🐇','🦝','🦨','🦡','🦦','🦥','🐁','🐀','🐿','🦔','🌵','🎄','🌲','🌳','🌴','🌱','🌿','☘️','🍀','🎍','🎋','🍃','🍂','🍁','🍄','🐚','🌾','💐','🌷','🌹','🥀','🌺','🌸','🌼','🌻','🌞','🌝','🌚','🌙','🌛','🌜','🌕','🌖','🌗','🌘','🌑','🌒','🌓','🌔','🌎','🌍','🌏','🪐','💫','⭐','🌠','☄️','🌪','🌈','☀️','🌤','⛅','🌥','☁️','🌦','🌧','⛈','🌩','🌨','❄️','☃️','⛄','🌬','💨','💧','💦','☔','☂️','🌊','🌫'],
    'Food': ['🍏','🍎','🍐','🍊','🍋','🍌','🍉','🍇','🍓','🫐','🍈','🍒','🍑','🥭','🍍','🥥','🥝','🍅','🍆','🥑','🥦','🥬','🥒','🌶','🫑','🌽','🥕','🫒','🧄','🧅','🥔','🍠','🥐','🥯','🍞','🥖','🥨','🧀','🥚','🍳','🧈','🥞','🧇','🥓','🥩','🍗','🍖','🌭','🍔','🍟','🍕','🥪','🥙','🧆','🌮','🌯','🫔','🥗','🥘','🫕','🥫','🍝','🍜','🍲','🍛','🍣','🍱','🥟','🦪','🍤','🍙','🍚','🍘','🍥','🥠','🥮','🍢','🍡','🍧','🍨','🍦','🥧','🧁','🍰','🎂','🍮','🍭','🍬','🍫','🍿','🍩','🍪','🌰','🥜','🍯','🥛','🍼','☕','🫖','🍵','🧃','🥤','🧋','🍶','🍺','🍻','🥂','🍷','🥃','🍸','🍹','🧉','🍾','🧊'],
    'Activities': ['⚽','🏀','🏈','⚾','🥎','🎾','🏐','🏉','🥏','🎱','🪀','🏓','🏸','🏒','🏑','🥍','🏏','🪃','🥅','⛳','🪁','🏹','🎣','🤿','🥊','🥋','🎽','🛹','🛼','🛷','⛸','🥌','🎿','⛷','🏂','🪂','🏋','🤼','🤸','⛹','🤺','🤾','🏌','🏇','🧘','🏄','🏊','🤽','🚣','🧗','🚵','🚴','🏆','🥇','🥈','🥉','🏅','🎖','🏵','🎗','🎫','🎟','🎪','🤹','🎭','🩰','🎨','🎬','🎤','🎧','🎼','🎹','🥁','🪘','🎷','🎺','🪗','🎸','🪕','🎻','🎲','♟','🎯','🎳','🎮','🎰','🧩'],
    'Travel': ['🚗','🚕','🚙','🚌','🚎','🏎','🚓','🚑','🚒','🚐','🛻','🚚','🚛','🚜','🦯','🦽','🦼','🛴','🚲','🛵','🏍','🛺','🚨','🚔','🚍','🚘','🚖','🚡','🚠','🚟','🚃','🚋','🚞','🚝','🚄','🚅','🚈','🚂','🚆','🚇','🚊','🚉','✈️','🛫','🛬','🛩','💺','🛰','🚀','🛸','🚁','🛶','⛵','🚤','🛥','🛳','⛴','🚢','⚓','🪝','⛽','🚧','🚦','🚥','🗺','🗿','🗽','🗼','🏰','🏯','🏟','🎡','🎢','🎠','⛲','⛱','🏖','🏝','🏜','🌋','⛰','🏔','🗻','🏕','⛺','🏠','🏡','🏘','🏚','🏗','🏭','🏢','🏬','🏣','🏤','🏥','🏦','🏨','🏪','🏫','🏩','💒','🏛','⛪','🕌','🕍','🛕','🕋','⛩','🛤','🛣','🗾','🎑','🏞','🌅','🌄','🌠','🎇','🎆','🌇','🌆','🏙','🌃','🌌','🌉','🌁'],
    'Objects': ['⌚','📱','💻','⌨️','🖥','🖨','🖱','🖲','🕹','🗜','💽','💾','💿','📀','📼','📷','📸','📹','🎥','📽','🎞','📞','☎️','📟','📠','📺','📻','🎙','🎚','🎛','🧭','⏱','⏲','⏰','🕰','⌛','⏳','📡','🔋','🪫','🔌','💡','🔦','🕯','🪔','🧯','🛢','💸','💵','💴','💶','💷','🪙','💰','💳','💎','⚖️','🪜','🧰','🪛','🔧','🔨','⚒','🛠','⛏','🪚','🔩','⚙️','🪤','🧱','⛓','🧲','🔫','💣','🧨','🪓','🔪','🗡','⚔️','🛡','🚬','⚰️','🪦','⚱️','🏺','🔮','📿','🧿','💈','⚗️','🔭','🔬','🕳','🩹','🩺','🩻','🩼','💊','💉','🩸','🧬','🦠','🧫','🧪','🌡','🧹','🧺','🧻','🚽','🚰','🚿','🛁','🛀','🧼','🪥','🪒','🧽','🪣','🧴','🛎','🔑','🗝','🚪','🪑','🛋','🛏','🛌','🧸','🪆','🖼','🪞','🪟','🛍','🛒','🎁','🎈','🎏','🎀','🪄','🪅','🎊','🎉','🎎','🏮','🎐','🧧','✉️','📩','📨','📧','💌','📥','📤','📦','🏷','🪧','📪','📫','📬','📭','📮','📯','📜','📃','📄','📑','🧾','📊','📈','📉','🗒','🗓','📆','📅','🗑','📇','🗃','🗳','🗄','📋','📁','📂','🗂','🗞','📰','📓','📔','📒','📕','📗','📘','📙','📚','📖','🔖','🧷','🔗','📎','🖇','📐','📏','🧮','📌','📍','✂️','🖊','🖋','✒️','🖌','🖍','📝','✏️','🔍','🔎','🔏','🔐','🔒','🔓'],
    'Symbols': ['❤️','🧡','💛','💚','💙','💜','🖤','🤍','🤎','💔','❣️','💕','💞','💓','💗','💖','💘','💝','💟','☮️','✝️','☪️','🕉','☸️','✡️','🔯','🕎','☯️','☦️','🛐','⛎','♈','♉','♊','♋','♌','♍','♎','♏','♐','♑','♒','♓','🆔','⚛️','🉑','☢️','☣️','📴','📳','🈶','🈚','🈸','🈺','🈷️','✴️','🆚','💮','🉐','㊙️','㊗️','🈴','🈵','🈹','🈲','🅰️','🅱️','🆎','🆑','🅾️','🆘','❌','⭕','🛑','⛔','📛','🚫','💯','💢','♨️','🚷','🚯','🚳','🚱','🔞','📵','🚭','❗','❕','❓','❔','‼️','⁉️','🔅','🔆','〽️','⚠️','🚸','🔱','⚜️','🔰','♻️','✅','🈯','💹','❇️','✳️','❎','🌐','💠','Ⓜ️','🌀','💤','🏧','🚾','♿','🅿️','🈳','🈂️','🛂','🛃','🛄','🛅','🚹','🚺','🚼','🚻','🚮','🎦','📶','🈁','🔣','ℹ️','🔤','🔡','🔠','🆖','🆗','🆙','🆒','🆕','🆓','0️⃣','1️⃣','2️⃣','3️⃣','4️⃣','5️⃣','6️⃣','7️⃣','8️⃣','9️⃣','🔟','🔢','#️⃣','*️⃣','⏏️','▶️','⏸','⏯','⏹','⏺','⏭','⏮','⏩','⏪','⏫','⏬','◀️','🔼','🔽','➡️','⬅️','⬆️','⬇️','↗️','↘️','↙️','↖️','↕️','↔️','↪️','↩️','⤴️','⤵️','🔀','🔁','🔂','🔄','🔃','🎵','🎶','➕','➖','➗','✖️','♾','💲','💱','™️','©️','®️','〰️','➰','➿','🔚','🔙','🔛','🔝','🔜','✔️','☑️','🔘','🔴','🟠','🟡','🟢','🔵','🟣','⚫','⚪','🟤','🔺','🔻','🔸','🔹','🔶','🔷','🔳','🔲','▪️','▫️','◾','◽','◼️','◻️','🟥','🟧','🟨','🟩','🟦','🟪','⬛','⬜','🟫','🔈','🔇','🔉','🔊','🔔','🔕','📣','📢','👁‍🗨','💬','💭','🗯','♠️','♣️','♥️','♦️','🃏','🎴','🀄','🕐','🕑','🕒','🕓','🕔','🕕','🕖','🕗','🕘','🕙','🕚','🕛'],
  };
  MB.EMOJI = EMOJI;

  MB.emojiFor = function (key) {
    const s = MB.store;
    if (s.emojiById && s.emojiById[key]) return s.emojiById[key];
    if (key && key.length === 26) {
      for (const e of Object.values(s.emojiByName)) if (e._id === key) return e;
      for (const e of Object.values(s.emojiById || {})) if (e._id === key) return e;
    }
    return null;
  };

  MB.emojiPicker = function (anchor, onPick) {
    const old = el('#emoji-pop'); if (old) old.remove();
    const custom = Object.values(MB.store.emojiByName);
    const pop = h(`<div id="emoji-pop" class="emoji-pop">
      <div class="emoji-search"><input type="text" placeholder="Search emoji…"></div>
      <div class="emoji-scroll"></div></div>`);
    const scroll = el('.emoji-scroll', pop);
    function draw(filter) {
      scroll.innerHTML = '';
      const c = custom.filter((e) => !filter || e.name.includes(filter));
      if (c.length) {
        scroll.appendChild(h(`<div class="emoji-cat"><span>Server emoji</span></div>`));
        const grid = h('<div class="emoji-grid"></div>');
        for (const e of c) grid.appendChild(emojiBtn(`<img src="${MB.emojiUrl(e._id)}" alt=":${e.name}:" title=":${e.name}:">`, `:${e.name}:`));
        scroll.appendChild(grid);
      }
      for (const [cat, list] of Object.entries(EMOJI)) {
        const items = filter ? list.filter((x) => x.includes(filter)) : list;
        if (!items.length) continue;
        scroll.appendChild(h(`<div class="emoji-cat"><span>${cat}</span></div>`));
        const grid = h('<div class="emoji-grid"></div>');
        for (const x of items) grid.appendChild(emojiBtn(x, x));
        scroll.appendChild(grid);
      }
      if (!scroll.children.length) scroll.appendChild(h('<div class="empty-pad">nothing found</div>'));
    }
    function emojiBtn(inner, value) {
      const b = h(`<button class="emoji">${inner}</button>`);
      b.onclick = () => { onPick(value); };
      return b;
    }
    draw('');
    el('input', pop).oninput = (e) => draw(e.target.value.trim().toLowerCase());
    document.body.appendChild(pop);
    const r = anchor.getBoundingClientRect();
    const rect = pop.getBoundingClientRect();
    pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - rect.width - 8)) + 'px';
    pop.style.top = Math.max(8, r.top - rect.height - 8) + 'px';
    const away = (e) => { if (!pop.contains(e.target) && e.target !== anchor) { pop.remove(); document.removeEventListener('mousedown', away); } };
    setTimeout(() => document.addEventListener('mousedown', away), 0);
    setTimeout(() => el('input', pop).focus(), 30);
  };

  // --------------------------------------------------------------- avatar
  MB.avatar = function (userId, size, opts) {
    opts = opts || {};
    const user = MB.store.users[userId];
    const a = h(`<img class="avatar" draggable="false" style="width:${size}px;height:${size}px" src="${MB.avatarUrl(user)}" alt="">`);
    a.dataset.user = userId;
    if (opts.status) {
      const wrap = h(`<span class="avatar-wrap" style="width:${size}px;height:${size}px"></span>`);
      wrap.appendChild(a);
      const presence = user && user.status && user.status.presence;
      const online = user ? user.online : false;
      const cls = presence === 'Invisible' || presence === 'Offline' ? 'off' : !online && !opts.forceOnline ? 'off' : (presence || 'Online').toLowerCase();
      wrap.appendChild(h(`<span class="presence-dot ${cls}" title="${presence || (online ? 'Online' : 'Offline')}"></span>`));
      return wrap;
    }
    return a;
  };

  MB.serverIcon = function (server, size) {
    const url = MB.iconUrl(server);
    if (url) return h(`<img class="srv-icon" style="width:${size}px;height:${size}px" src="${url}" alt="">`);
    const initials = (server.name || '??').split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
    return h(`<span class="srv-icon letter" style="width:${size}px;height:${size}px;background:${MB.placeholderColor(server._id)}">${MB.esc(initials)}</span>`);
  };

  // ----------------------------------------------------------------- rail
  MB.renderRail = function () {
    const rail = el('#rail');
    rail.innerHTML = '';
    const home = h(`<button class="rail-btn home ${MB.store.dms.length ? '' : ''}" title="Direct messages">
      <span class="home-inner">${MB.store.me ? MB.esc(MB.userName(MB.store.me)[0].toUpperCase()) : 'M'}</span></button>`);
    home.onclick = () => MB.bus.emit('open', { server: null });
    rail.appendChild(home);
    const unreadDms = MB.dmList().some((d) => MB.unreadOf(d._id));
    if (unreadDms) rail.appendChild(h('<span class="rail-badge"></span>'));
    rail.appendChild(h('<div class="rail-sep"></div>'));
    const cur = MB.current();
    for (const s of MB.store.servers) {
      const unread = (s.channels || []).some((c) => MB.unreadOf(c));
      const mentions = (s.channels || []).some((c) => { const u = MB.unreadOf(c); return u && u.mentions; });
      const btn = h(`<button class="rail-btn ${cur.server === s._id ? 'active' : ''}" title="${MB.esc(s.name)}"></button>`);
      btn.appendChild(MB.serverIcon(s, 46));
      if (unread) btn.appendChild(h('<span class="rail-pill"></span>'));
      if (mentions) btn.appendChild(h('<span class="rail-pill mention"></span>'));
      btn.onclick = () => MB.bus.emit('open', { server: s._id });
      btn.dataset.server = s._id;
      btn.oncontextmenu = (e) => { e.preventDefault(); MB.openServerMenu(s, e); };
      rail.appendChild(btn);
    }
    const add = h(`<button class="rail-btn add" title="Servers magbot is in / join">+</button>`);
    add.onclick = () => MB.openServerList();
    rail.appendChild(add);
  };

  MB.openServerList = function () {
    const body = h(`<div class="server-list"><p class="muted">Servers magbot is a member of — click to open. Bots can't accept invites, but they can join through <code>invite</code> links a human posts… actually no: joining is done by the platform, not the bot.</p><div class="list"></div></div>`);
    const list = el('.list', body);
    const servers = [...MB.store.servers].sort((a, b) => (b.approximate_member_count || 0) - (a.approximate_member_count || 0));
    for (const s of servers) {
      const row = h(`<button class="row-btn"><span class="ico"></span><span class="txt"><b></b><small></small></span></button>`);
      el('.ico', row).appendChild(MB.serverIcon(s, 32));
      el('b', row).textContent = s.name;
      el('small', row).textContent = `${Object.keys(MB.store.members[s._id] || {}).length} members · ${(s.channels || []).length} channels · id ${s._id}`;
      row.onclick = () => { m.close(); MB.bus.emit('open', { server: s._id }); };
      list.appendChild(row);
    }
    const m = MB.modal('Servers magbot is in', body);
    return m;
  };

  // -------------------------------------------------------------- sidebar
  MB.renderSidebar = function () {
    const side = el('#sidebar');
    side.innerHTML = '';
    const cur = MB.current();
    if (!cur.server) {
      const head = h(`<div class="side-head"><span class="side-title">Direct messages</span>
        <button class="icon-btn" id="new-group" title="New group chat">＋</button></div>`);
      side.appendChild(head);
      const list = h('<div class="side-scroll"></div>');
      for (const d of MB.dmList()) {
        list.appendChild(MB.dmRow(d, cur.channel === d._id));
      }
      side.appendChild(list);
      const ng = el('#new-group', side);
      ng.onclick = () => MB.openNewGroup();
      return;
    }
    const server = MB.server(cur.server);
    if (!server) { MB.setCurrent(null, null); return MB.renderSidebar(); }
    const head = h(`<div class="side-head server">
      <button class="side-title server-name" id="server-menu"><span>${MB.esc(server.name)}</span><svg width="10" height="10" viewBox="0 0 10 10"><path d="M1 3l4 4 4-4" stroke="currentColor" fill="none" stroke-width="1.5"/></svg></button>
      <button class="icon-btn" id="new-channel" title="Create channel">＋</button></div>`);
    side.appendChild(head);
    const perms = MB.permsFor(server, null, MB.store.me._id);
    el('#new-channel', side).style.display = perms.has('ManageChannel') ? '' : 'none';
    el('#new-channel', side).onclick = () => MB.createChannel(server);
    el('#server-menu', side).onclick = (e) => MB.openServerMenu(server, e);
    const scroll = h('<div class="side-scroll"></div>');
    const groups = MB.channelsOfServer(server);
    for (const g of groups.categories) {
      scroll.appendChild(catHeader(g.cat, server));
      for (const c of g.channels) scroll.appendChild(MB.channelRow(c, server, cur.channel === c._id));
    }
    for (const c of groups.uncategorised) scroll.appendChild(MB.channelRow(c, server, cur.channel === c._id));
    scroll.appendChild(h('<div class="side-pad"></div>'));
    side.appendChild(scroll);
  };

  function catHeader(cat, server) {
    const collapsed = (MB.collapsed || {})[cat.id];
    const row = h(`<button class="cat ${collapsed ? 'collapsed' : ''}"><span class="chev">▾</span><span class="cat-name"></span></button>`);
    el('.cat-name', row).textContent = cat.title || 'category';
    row.onclick = () => { MB.collapsed = MB.collapsed || {}; MB.collapsed[cat.id] = !collapsed; MB.renderSidebar(); };
    return row;
  }

  MB.channelRow = function (c, server, active, depth) {
    const isVoice = c.channel_type === 'VoiceChannel';
    const unread = MB.unreadOf(c._id);
    const row = h(`<button class="chan ${active ? 'active' : ''} ${unread ? 'unread' : ''} ${isVoice ? 'voice' : ''}" style="padding-left:${8 + (depth || 0)}px">
      <span class="chan-ico">${isVoice ? '🔊' : '#'}</span><span class="chan-name"></span></button>`);
    el('.chan-name', row).textContent = c.name || 'channel';
    if (unread) {
      row.appendChild(h(`<span class="unread-pill ${unread.mentions ? 'mention' : ''}">${unread.mentions ? unread.mentions : (unread.count > 0 ? unread.count : '')}</span>`));
    }
    row.onclick = () => MB.bus.emit('open', { server: server ? server._id : null, channel: c._id });
    row.oncontextmenu = (e) => { e.preventDefault(); MB.channelMenu(c, server, e); };
    if (isVoice) {
      const inside = MB.store.voice[c._id] || [];
      if (inside.length && !(MB.collapsed || {})[c._id]) {
        const wrap = h(`<div class="voice-block"></div>`);
        wrap.appendChild(row);
        for (const v of inside) {
          const u = MB.store.users[v.user_id];
          const r = h(`<div class="voice-user"><span class="voice-av"></span><span class="voice-name"></span></div>`);
          el('.voice-av', r).appendChild(MB.avatar(v.user_id, 22, { status: true }));
          el('.voice-name', r).textContent = MB.displayName(server ? server._id : null, v.user_id);
          r.dataset.user = v.user_id;
          wrap.appendChild(r);
          MB.ensureUser(v.user_id);
        }
        return wrap;
      }
    }
    return row;
  };

  MB.dmRow = function (d, active) {
    const unread = MB.unreadOf(d._id);
    const row = h(`<button class="chan dm ${active ? 'active' : ''} ${unread ? 'unread' : ''}">
      <span class="chan-ico av"></span><span class="chan-name"></span></button>`);
    const label = d.kind === 'saved' ? 'Saved Messages' : MB.dmName(d);
    el('.chan-name', row).textContent = label;
    if (d.kind === 'dm') {
      const other = (d.recipients || []).find((r) => r !== MB.store.me._id) || d.recipients[0];
      el('.chan-ico', row).appendChild(MB.avatar(other, 24, { status: !d.active }));
      MB.ensureUser(other);
    } else if (d.kind === 'group') {
      el('.chan-ico', row).appendChild(h(`<span class="group-badge" style="background:${MB.placeholderColor(d._id)}">${MB.esc((d.name || 'G')[0].toUpperCase())}</span>`));
      (d.recipients || []).slice(0, 6).forEach((r) => MB.ensureUser(r));
    } else {
      el('.chan-ico', row).appendChild(h('<span class="saved-badge">🔖</span>'));
    }
    if (unread) row.appendChild(h(`<span class="unread-pill ${unread.mentions ? 'mention' : ''}">${unread.mentions ? unread.mentions : ''}</span>`));
    row.onclick = () => MB.bus.emit('open', { server: null, channel: d._id });
    row.oncontextmenu = (e) => { e.preventDefault(); MB.channelMenu(d, null, e); };
    return row;
  };

  // ----------------------------------------------------------------- chat
  MB.renderChat = function () {
    const chat = el('#chat');
    const cur = MB.current();
    const ch = cur.channel ? MB.channel(cur.channel) : null;
    chat.classList.toggle('empty', !ch);
    if (!ch) {
      el('#chat-head').innerHTML = '';
      el('#messages').innerHTML = '';
      el('#composer-wrap').innerHTML = '';
      el('#chat').appendChild(h('<div class="blank"><div class="blank-art">✿</div><h3>Pick a channel</h3><p>Server channels and DMs on the left. Ctrl/⌘+K to jump anywhere.</p></div>'));
      return;
    }
    el('.blank', chat)?.remove();
    renderChatHead(ch);
    renderMessages(ch);
    MB.renderComposer(ch);
    MB.renderTyping(ch);
  };

  function renderChatHead(ch) {
    const cur = MB.current();
    const head = el('#chat-head');
    head.innerHTML = '';
    const server = cur.server ? MB.server(cur.server) : null;
    const name = server ? (ch.name || 'channel') : MB.dmName(ch);
    const isVoice = ch.channel_type === 'VoiceChannel';
    const left = h(`<div class="head-left">
      <button class="icon-btn only-mobile" id="hamburger">☰</button>
      <span class="head-ico">${isVoice ? '🔊' : (server ? '#' : (ch.channel_type === 'Group' ? '👥' : '@'))}</span>
      <h1></h1></div>`);
    el('h1', left).textContent = name;
    if (ch.description) left.appendChild(h(`<span class="topic" title="${MB.esc(ch.description)}">${MB.esc(ch.description)}</span>`));
    if (ch.channel_type === 'Group' && (ch.recipients || []).length) {
      left.appendChild(h(`<span class="topic">${ch.recipients.length + 1} members</span>`));
    }
    head.appendChild(left);
    const right = h(`<div class="head-right">
      ${server ? '<button class="icon-btn" id="toggle-members" title="Member list">👤</button>' : ''}
      ${ch.channel_type === 'Group' || (server && false) ? '<button class="icon-btn" id="chan-menu" title="Channel menu">⋯</button>' : ''}
      <button class="icon-btn" id="pins" title="Pinned & info">ℹ</button></div>`);
    head.appendChild(right);
    el('#hamburger', right.parentElement).onclick = () => document.body.classList.toggle('show-rail');
    const tm = el('#toggle-members', right);
    if (tm) tm.onclick = () => { document.body.classList.toggle('show-members'); MB.renderMembers(); };
    const cm = el('#chan-menu', right);
    if (cm) cm.onclick = (e) => MB.channelMenu(ch, server, e);
    el('#pins', right).onclick = () => MB.openChannelInfo(ch, server);
  }

  function renderMessages(ch) {
    const box = el('#messages');
    const list = MB.messages[ch._id] || [];
    const keepScroll = box.scrollTop;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    box.innerHTML = '';
    if (MB.loadingMore[ch._id]) box.appendChild(h('<div class="loading-more">loading…</div>'));
    let lastUser = null, lastTime = 0, lastDay = null, prevEl = null;
    for (const m of list) {
      const day = new Date(m._id && m._id.length === 26 ? MB.snowflakeTime(m._id) : (m.created_at || Date.now())).toDateString();
      const t = MB.messageTime(m);
      if (day !== lastDay) {
        box.appendChild(h(`<div class="day-divider"><span>${MB.fmtDay(t)}</span></div>`));
        lastUser = null; lastDay = day;
      }
      const grouped = lastUser === m.author && (t - lastTime) < 7 * 60 * 1000 && !m.replies && m.type !== 'system';
      const node = MB.messageNode(m, grouped, ch);
      box.appendChild(node);
      lastUser = m.author; lastTime = t;
    }
    if (!list.length) box.appendChild(h('<div class="blank small"><p>No messages yet.</p></div>'));
    if (atBottom) box.scrollTop = box.scrollHeight;
    else box.scrollTop = keepScroll;
  }
  MB.renderMessages = renderMessages;

  MB.scrollToBottom = function (smooth) {
    const box = el('#messages');
    if (!box) return;
    box.scrollTo({ top: box.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
  };

  MB.messageTime = (m) => {
    if (m.created_at) return new Date(m.created_at).getTime();
    if (m._id && m._id.length === 26) return MB.snowflakeTime(m._id);
    return Date.now();
  };

  MB.messageNode = function (m, grouped, ch) {
    const s = MB.store;
    const server = (ch && ch.server) ? MB.server(ch.server) : (MB.current().server ? MB.server(MB.current().server) : null);
    const serverId = server ? server._id : null;
    const author = m.user || m.author;
    if (m.type === 'system' || (!author && m.content)) return systemNode(m, serverId);
    const user = s.users[author];
    const color = serverId ? MB.roleColor(server, author) : null;
    const name = MB.displayName(serverId, author);
    const node = h(`<div class="msg ${grouped ? 'grouped' : ''}" data-id="${m._id}"></div>`);
    // hover actions
    const actions = h(`<div class="msg-actions"></div>`);
    const addAction = (icon, title, fn, cls) => {
      const b = h(`<button class="ma ${cls || ''}" title="${title}">${icon}</button>`);
      b.onclick = (e) => { e.stopPropagation(); fn(e); };
      actions.appendChild(b);
    };
    addAction('😊', 'Add reaction', (e) => MB.emojiPicker(e.currentTarget, (emoji) => MB.react(m._id, emoji, ch._id)));
    addAction('↩', 'Reply', () => MB.setReply(m));
    if (author === s.me._id) addAction('✎', 'Edit', () => MB.editMessage(m, ch._id));
    if (author === s.me._id || MB.permsFor(server, ch, s.me._id).has('ManageMessages')) addAction('🗑', 'Delete', () => MB.deleteMessage(m, ch._id));
    addAction('⋯', 'More', (e) => MB.messageMenu(m, ch, e));
    node.appendChild(actions);
    if (!grouped) {
      const av = h('<div class="msg-av"></div>');
      av.appendChild(MB.avatar(author, 40, { status: false }));
      av.dataset.user = author;
      node.appendChild(av);
    }
    const body = h('<div class="msg-body"></div>');
    if (!grouped) {
      const meta = h(`<div class="msg-meta"><span class="author"></span><span class="sep">·</span><span class="stamp"></span></div>`);
      const a = el('.author', meta);
      a.textContent = name;
      if (color) a.style.color = color;
      a.dataset.user = author;
      el('.stamp', meta).textContent = MB.fmtTime(MB.messageTime(m));
      el('.stamp', meta).title = MB.fmtFull(MB.messageTime(m));
      if (user && user.bot) meta.insertBefore(h('<span class="bot-tag">BOT</span>'), a);
      body.appendChild(meta);
    }
    if (m.replies && m.replies.length) {
      const target = m.replies[0];
      const t = (MB.messages[ch._id] || []).find((x) => x._id === (target.id || target._id));
      const r = h(`<div class="reply-line"><span class="reply-av"></span><span class="reply-name"></span><span class="reply-text"></span></div>`);
      const tid = target.id || target._id;
      el('.reply-av', r).appendChild(MB.avatar(t ? (t.user || t.author) : null, 16));
      el('.reply-name', r).textContent = t ? MB.displayName(serverId, t.user || t.author) : 'a message';
      el('.reply-text', r).textContent = t ? (t.content || '').slice(0, 120) : 'click to jump';
      r.onclick = () => MB.jumpTo(tid);
      body.appendChild(r);
    }
    const content = h('<div class="msg-content"></div>');
    if (m.content) {
      const ctx = { users: s.users, channels: s.channels, emojis: s.emojiByName, me: s.me._id, server };
      content.innerHTML = MB.renderMarkdown(m.content, ctx);
      if (m.edited) content.appendChild(h('<span class="edited">(edited)</span>'));
      if (m.mentions && m.mentions.includes(s.me._id)) node.classList.add('mentioned');
      if (m.masquerade && m.masquerade.name) {
        const badge = h(`<span class="masq">sent as ${MB.esc(m.masquerade.name)}</span>`);
        content.appendChild(badge);
      }
    } else if (!m.attachments || !m.attachments.length) {
      content.innerHTML = '<span class="muted">(no content)</span>';
    }
    body.appendChild(content);
    if (m.attachments && m.attachments.length) body.appendChild(attachmentsNode(m.attachments));
    if (m.embeds && m.embeds.length) for (const e of m.embeds) body.appendChild(embedNode(e));
    // reactions
    if (m.reactions && Object.keys(m.reactions).length) {
      const bar = h('<div class="reactions"></div>');
      for (const [key, users] of Object.entries(m.reactions)) {
        const mine = users.includes(s.me._id);
        const btn = h(`<button class="react ${mine ? 'mine' : ''}" title="${users.length} reaction${users.length > 1 ? 's' : ''}"></button>`);
        const cust = MB.emojiFor(key);
        if (cust) btn.appendChild(h(`<img class="emoji custom" src="${MB.emojiUrl(cust._id)}" alt="">`));
        else btn.appendChild(document.createTextNode(key));
        btn.appendChild(h(`<span class="count">${users.length}</span>`));
        btn.onclick = () => MB.react(m._id, key, ch._id, mine);
        btn.dataset.emoji = key;
        bar.appendChild(btn);
      }
      const add = h('<button class="react add" title="Add reaction">＋</button>');
      add.onclick = (e) => MB.emojiPicker(e.currentTarget, (emoji) => MB.react(m._id, emoji, ch._id));
      bar.appendChild(add);
      body.appendChild(bar);
    }
    node.appendChild(body);
    prevEl = node;
    return node;
  };

  function systemNode(m, serverId) {
    const node = h(`<div class="msg system" data-id="${m._id}"><div class="sys-line"></div></div>`);
    const s = MB.store;
    let text = m.content || '';
    if (m.user && m.content) {
      const name = MB.displayName(serverId, m.user);
      text = text.replace(/<@[0-9A-Z]{26}>/g, name).replace(/^@?/, '');
    }
    text = text.replace(/<#[0-9A-Z]{26}>/g, (x) => (s.channels[x.slice(2, -1)] || {}).name ? '#' + s.channels[x.slice(2, -1)].name : x);
    text = text.replace(/<@[0-9A-Z]{26}>/g, (x) => MB.displayName(serverId, x.slice(2, -1)));
    el('.sys-line', node).textContent = text.trim();
    return node;
  }

  function attachmentsNode(list) {
    const wrap = h('<div class="atts"></div>');
    for (const a of list) {
      const url = MB.asset(a.tag || 'attachments', a._id);
      const type = (a.metadata && a.metadata.type) || (a.content_type || '').split('/')[0] || 'File';
      const name = a.filename || 'file';
      if (type === 'Image') {
        const box = h(`<div class="att image"><img loading="lazy" src="${url}" alt="${MB.esc(name)}"><a class="att-dl" href="${url}" target="_blank" rel="noreferrer">open</a></div>`);
        el('img', box).onclick = () => MB.lightbox(url, name);
        wrap.appendChild(box);
      } else if (type === 'Video') {
        wrap.appendChild(h(`<div class="att video"><video controls preload="metadata" src="${url}"></video></div>`));
      } else if (type === 'Audio') {
        wrap.appendChild(h(`<div class="att audio"><audio controls preload="metadata" src="${url}"></audio><span>${MB.esc(name)}</span></div>`));
      } else {
        wrap.appendChild(h(`<a class="att file" href="${url}" target="_blank" rel="noreferrer"><span class="file-ico">📄</span><span class="file-meta"><b>${MB.esc(name)}</b><small>${MB.fmtSize(a.size)}${a.content_type ? ' · ' + MB.esc(a.content_type) : ''}</small></span></a>`));
      }
    }
    return wrap;
  }

  function embedNode(e) {
    const box = h('<div class="embed"></div>');
    if (e.colour) box.style.borderLeftColor = e.colour;
    if (e.site_name || e.icon_url) {
      const head = h(`<div class="embed-head">${e.icon_url ? `<img src="${MB.esc(e.icon_url)}" alt="">` : ''}<span>${MB.esc(e.site_name || '')}</span></div>`);
      box.appendChild(head);
    }
    if (e.title) box.appendChild(h(`<div class="embed-title">${MB.esc(e.title)}</div>`));
    if (e.description) box.appendChild(h(`<div class="embed-desc">${MB.renderMarkdown(e.description || '', { users: MB.store.users, channels: MB.store.channels, emojis: MB.store.emojiByName })}</div>`));
    if (e.media && e.media.url) {
      const m = e.media;
      if (m.metadata && m.metadata.type === 'Video') box.appendChild(h(`<video controls preload="metadata" src="${MB.esc(m.url)}"></video>`));
      else box.appendChild(h(`<img loading="lazy" class="embed-img" src="${MB.esc(m.url)}" alt="">`));
    }
    if (e.special && e.special.type === 'Image') {
      for (const a of Object.values(e.special)) if (a && a.url) box.appendChild(h(`<img loading="lazy" class="embed-img" src="${MB.esc(a.url)}" alt="">`));
    }
    return box;
  }

  MB.lightbox = function (url, name) {
    const back = h(`<div class="lightbox"><img src="${url}" alt=""><button class="close">✕</button></div>`);
    back.onclick = (e) => { if (e.target === back || e.target.classList.contains('close')) back.remove(); };
    document.addEventListener('keydown', function esc(e) { if (e.key === 'Escape') { back.remove(); document.removeEventListener('keydown', esc); } });
    document.body.appendChild(back);
  };

  // ------------------------------------------------------------- composer
  MB.renderComposer = function (ch) {
    const wrap = el('#composer-wrap');
    wrap.innerHTML = '';
    const s = MB.store;
    const server = MB.current().server ? MB.server(MB.current().server) : null;
    const perms = MB.permsFor(server, ch, s.me._id);
    const canSend = !server || perms.has('SendMessage') || ch.channel_type === 'DirectMessage' || ch.channel_type === 'Group' || ch.channel_type === 'SavedMessages';
    if (!canSend) {
      wrap.appendChild(h(`<div class="no-perm">magbot doesn't have <b>Send Messages</b> in this channel — the platform would reject it.</div>`));
      return;
    }
    const box = h(`<div class="composer">
      <div class="pending hidden" id="pending"></div>
      <div class="replybar hidden" id="replybar"><span class="rb-text"></span><button class="icon-btn" id="rb-cancel">✕</button></div>
      <div class="compose-row">
        <button class="icon-btn attach" id="attach" title="Attach file">＋</button>
        <textarea id="input" rows="1" placeholder="Message ${MB.esc(server ? '#' + (ch.name || '') : MB.dmName(ch))}"></textarea>
        <button class="icon-btn emoji" id="emoji-btn" title="Emoji">😊</button>
        <button class="icon-btn send" id="send" title="Send">➤</button>
      </div>
      <input type="file" id="file-input" multiple hidden>
      <div class="compose-foot"><span class="hint">Enter to send · Shift+Enter newline</span><span id="counter"></span></div>
    </div>`);
    wrap.appendChild(box);
    const input = el('#input', box);
    const pending = el('#pending', box);
    let pendingFiles = [];
    const saved = MB.drafts[ch._id] || '';
    input.value = saved;
    MB.autoGrow(input);
    input.addEventListener('input', () => {
      updatePop();
      MB.autoGrow(input);
      const len = input.value.length;
      const counter = el('#counter', box);
      counter.textContent = len > 1700 ? `${len}/2000` : '';
      counter.classList.toggle('warn', len > 2000);
      MB.saveDraft(ch._id, input.value);
    });
    input.addEventListener('keydown', (e) => {
      if (!mpop.classList.contains('hidden') && mCands.length) {
        if (e.key === 'ArrowDown') { e.preventDefault(); mIndex = (mIndex + 1) % mCands.length; updatePopHighlight(); return; }
        if (e.key === 'ArrowUp') { e.preventDefault(); mIndex = (mIndex - 1 + mCands.length) % mCands.length; updatePopHighlight(); return; }
        if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); applyMention(mCands[mIndex]); return; }
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); hidePop(); return; }
      }
      if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey) { e.preventDefault(); send(); }
      if (e.key === 'Escape') { if (MB.editing) MB.setEditing(null); else MB.clearReply(); }
      if (e.key === 'ArrowUp' && !input.value) {
        const mine = (MB.messages[ch._id] || []).filter((m) => (m.user || m.author) === s.me._id);
        if (mine.length) { MB.editMessage(mine[mine.length - 1], ch._id); }
      }
    });
    input.addEventListener('click', updatePop);
    el('#attach', box).onclick = () => el('#file-input', box).click();
    el('#file-input', box).onchange = (e) => addFiles(Array.from(e.target.files));
    el('#emoji-btn', box).onclick = (e) => MB.emojiPicker(e.currentTarget, (emoji) => {
      const start = input.selectionStart;
      input.value = input.value.slice(0, start) + emoji + input.value.slice(input.selectionEnd);
      input.selectionStart = input.selectionEnd = start + emoji.length;
      input.focus();
      MB.saveDraft(ch._id, input.value);
    });
    el('#send', box).onclick = () => send();
    input.addEventListener('paste', (e) => {
      const files = Array.from(e.clipboardData.files || []);
      if (files.length) { e.preventDefault(); addFiles(files); }
    });
    box.addEventListener('dragover', (e) => { e.preventDefault(); box.classList.add('drop'); });
    box.addEventListener('dragleave', () => box.classList.remove('drop'));
    box.addEventListener('drop', (e) => {
      e.preventDefault(); box.classList.remove('drop');
      const files = Array.from(e.dataTransfer.files || []);
      if (files.length) addFiles(files);
    });

    MB.composerInput = () => input;
    MB.setReply = (m) => {
      MB.replyTo = m;
      const rb = el('#replybar', box);
      rb.classList.remove('hidden');
      el('.rb-text', rb).textContent = `Replying to ${MB.displayName(server ? server._id : null, m.user || m.author)}: ${(m.content || '').slice(0, 80)}`;
      el('#rb-cancel', rb).onclick = () => MB.clearReply();
      input.focus();
    };
    MB.clearReply = () => { MB.replyTo = null; el('#replybar', box)?.classList.add('hidden'); };
    MB.setEditing = (m) => {
      MB.editing = m;
      if (!m) return;
      input.value = MB.contentToDisplay(m.content || '');
      MB.autoGrow(input);
      input.placeholder = 'Editing message — Enter to save, Esc to cancel';
      input.focus();
      const el2 = el(`.msg[data-id="${m._id}"]`);
      el2 && el2.classList.add('editing');
    };
    MB.clearEditing = () => {
      MB.editing = null;
      el('.msg.editing')?.classList.remove('editing');
      input.placeholder = `Message ${MB.esc(server ? '#' + (ch.name || '') : MB.dmName(ch))}`;
      input.value = MB.drafts[ch._id] || '';
      MB.autoGrow(input);
    };

    async function addFiles(files) {
      if (!files.length) return;
      for (const f of files) {
        if (f.size > 20 * 1024 * 1024) { MB.toast(`${f.name} is too big (20 MB max)`, 'error'); continue; }
        const item = h(`<div class="pending-item"><span class="pi-name"></span><span class="pi-bar"><i></i></span></div>`);
        el('.pi-name', item).textContent = f.name;
        pending.appendChild(item);
        pending.classList.remove('hidden');
        try {
          const fd = new FormData();
          fd.append('file', f, f.name);
          const up = await MB.api('POST', '/upload', fd);
          pendingFiles.push(up.id);
          item.classList.add('done');
          el('.pi-bar i', item).style.width = '100%';
        } catch (err) {
          item.classList.add('fail');
          MB.toast(`Upload failed: ${err.message}`, 'error');
          setTimeout(() => item.remove(), 4000);
        }
      }
    }

    // ------------------------------------------------ @mention autocomplete
    const mpop = h('<div id="mention-pop" class="hidden"></div>');
    box.appendChild(mpop);
    let mCands = [], mIndex = 0, mQuery = null;
    const pend = (MB.pendingMentions = MB.pendingMentions || {});

    function mentionUsers() {
      const list = [], seen = new Set();
      const add = (uid) => {
        const u = MB.store.users[uid];
        if (!u || seen.has(uid)) return;
        seen.add(uid);
        const name = MB.displayName(server ? server._id : null, uid);
        list.push({ id: uid, name, user: u,
                    key: `${name} ${u.display_name || ''} ${u.username}`.toLowerCase() });
      };
      if (server) Object.keys(MB.store.members[server._id] || {}).forEach(add);
      else (ch.recipients || []).forEach(add);
      return list;
    }

    function detectQuery() {
      const caret = input.selectionStart;
      if (caret === null || caret === undefined) return null;
      const before = input.value.slice(0, caret);
      const at = before.lastIndexOf('@');
      if (at < 0) return null;
      if (at > 0 && !/\s/.test(before[at - 1])) return null;
      const q = before.slice(at + 1);
      if (/[\n<>]/.test(q) || q.length > 40) return null;
      return { start: at, end: caret, q: q.toLowerCase() };
    }

    function hidePop() { mpop.classList.add('hidden'); mpop.innerHTML = ''; mCands = []; mQuery = null; }
    MB.popupOpen = () => !mpop.classList.contains('hidden');

    function updatePopHighlight() {
      Array.from(mpop.children).forEach((row, i) => row.classList.toggle('active', i === mIndex));
      const active = mpop.children[mIndex];
      if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
    }

    function updatePop() {
      const d = detectQuery();
      if (!d) return hidePop();
      const all = mentionUsers();
      const starts = [], contains = [];
      for (const c of all) {
        const nm = c.name.toLowerCase();
        if (!d.q || nm.startsWith(d.q) || c.user.username.toLowerCase().startsWith(d.q)) starts.push(c);
        else if (!d.q || nm.includes(d.q) || c.key.includes(d.q)) contains.push(c);
      }
      const byName = (a, b) => (b.user.online ? 1 : 0) - (a.user.online ? 1 : 0) || a.name.localeCompare(b.name);
      const list = starts.sort(byName).concat(contains.sort(byName)).slice(0, 8);
      if (!list.length) return hidePop();
      mQuery = d; mCands = list; mIndex = Math.min(mIndex, list.length - 1);
      mpop.innerHTML = '';
      list.forEach((c, i) => {
        const row = h(`<div class="mention-row${i === mIndex ? ' active' : ''}"></div>`);
        row.appendChild(MB.avatar(c.id, 24));
        const nm = h('<span class="mr-name"></span>');
        nm.textContent = c.name;
        const col = MB.roleColor(server, c.id);
        if (col) nm.style.color = col;
        row.appendChild(nm);
        const tag = h('<span class="mr-tag"></span>');
        tag.textContent = '#' + c.user.discriminator;
        row.appendChild(tag);
        row.onmousedown = (e) => { e.preventDefault(); applyMention(c); };
        mpop.appendChild(row);
      });
      mpop.classList.remove('hidden');
    }

    function applyMention(c) {
      if (!mQuery) return;
      const text = '@' + c.name + ' ';
      input.value = input.value.slice(0, mQuery.start) + text + input.value.slice(mQuery.end);
      const caret = mQuery.start + text.length;
      input.setSelectionRange(caret, caret);
      pend[ch._id] = (pend[ch._id] || []).filter((p) => p.id !== c.id);
      pend[ch._id].push({ id: c.id, text: '@' + c.name });
      hidePop();
      MB.autoGrow(input);
      MB.saveDraft(ch._id, input.value);
      input.focus();
    }

    async function send() {
      const raw = input.value.trim();
      if (!raw && !pendingFiles.length) return;
      const content = MB.mentionsToContent(raw, pend[ch._id] || []);
      if (MB.editing) {
        const m = MB.editing;
        try {
          await MB.api('PATCH', `/channels/${ch._id}/messages/${m._id}`, { content });
          pend[ch._id] = [];
          MB.clearEditing();
          MB.refreshMessage(ch._id, m._id);
          MB.saveDraft(ch._id, '');
        } catch (err) { MB.toast(err.message, 'error'); }
        return;
      }
      const payload = { content };
      if (pendingFiles.length) payload.attachments = pendingFiles.slice();
      if (MB.replyTo) payload.replies = [{ id: MB.replyTo._id, mention: false }];
      input.value = '';
      MB.saveDraft(ch._id, '');
      MB.autoGrow(input);
      pendingFiles = [];
      pending.innerHTML = '';
      pending.classList.add('hidden');
      MB.clearReply();
      const optimistic = { _id: 'tmp' + Date.now(), channel: ch._id, user: s.me._id, content: payload.content, attachments: (payload.attachments || []).map((id) => ({ _id: id, tag: 'attachments', filename: 'upload' })), replies: payload.replies, _pending: true };
      (MB.messages[ch._id] = MB.messages[ch._id] || []).push(optimistic);
      MB.renderMessages(ch);
      MB.scrollToBottom();
      try {
        const sent = await MB.api('POST', `/channels/${ch._id}/messages`, payload);
        const list = MB.messages[ch._id] || [];
        const idx = list.findIndex((x) => x._id === optimistic._id);
        const dup = sent && sent._id ? list.findIndex((x) => x._id === sent._id) : -1;
        if (dup >= 0 && idx >= 0) list.splice(idx, 1);              // gateway already delivered it
        else if (idx >= 0 && sent && sent._id) list[idx] = { ...sent, user: s.me._id, author: s.me._id };
        else if (idx >= 0) list.splice(idx, 1);
        pend[ch._id] = [];
        MB.renderMessages(ch);
        MB.scrollToBottom();
      } catch (err) {
        const list = MB.messages[ch._id] || [];
        const idx = list.findIndex((x) => x._id === optimistic._id);
        if (idx >= 0) list.splice(idx, 1);
        MB.renderMessages(ch);
        input.value = raw;
        MB.toast(err.message, 'error');
      }
    }
    MB.composerSend = send;
  };

  MB.autoGrow = function (ta) {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 320) + 'px';
  };

  // -------------------------------------------------------------- members
  MB.renderMembers = function () {
    const pane = el('#members');
    const cur = MB.current();
    const server = cur.server ? MB.server(cur.server) : null;
    if (!server) { pane.innerHTML = ''; document.body.classList.remove('show-members'); return; }
    const members = Object.values(MB.store.members[server._id] || {});
    const groups = new Map();
    const online = [], offline = [];
    for (const m of members) {
      const u = MB.store.users[m.user] || {};
      (u.online ? online : offline).push(m);
    }
    const hoist = {};
    const noRole = [];
    for (const m of online) {
      const role = MB.highestRole(server, m.user);
      if (role && role.hoist) (hoist[role._id] = hoist[role._id] || { role, list: [] }).list.push(m);
      else noRole.push(m);
    }
    const sortByName = (a, b) => MB.displayName(server._id, a.user).localeCompare(MB.displayName(server._id, b.user));
    const body = h('<div class="members-body"></div>');
    const addGroup = (title, list, color) => {
      if (!list.length) return;
      body.appendChild(h(`<div class="mem-group" ${color ? `style="color:${color}"` : ''}>${MB.esc(title)} — ${list.length}</div>`));
      for (const m of list.sort(sortByName)) {
        const u = MB.store.users[m.user] || {};
        const row = h('<div class="mem-row"></div>');
        row.appendChild(MB.avatar(m.user, 32, { status: true }));
        const right = h('<div class="mem-meta"><span class="mem-name"></span><small></small></div>');
        const nm = el('.mem-name', right);
        nm.textContent = MB.displayName(server._id, m.user);
        const c = MB.roleColor(server, m.user);
        if (c) nm.style.color = c;
        if (u.bot) right.appendChild(h('<span class="bot-tag">BOT</span>'));
        if (u.status && u.status.text) el('small', right).textContent = u.status.text;
        row.appendChild(right);
        row.onclick = () => MB.openProfile(m.user, server);
        body.appendChild(row);
      }
    };
    for (const g of Object.values(hoist).sort((a, b) => a.role.rank - b.role.rank)) addGroup(g.role.name, g.list, g.role.colour);
    addGroup('Online', noRole);
    addGroup('Offline', offline);
    pane.innerHTML = '';
    pane.appendChild(body);
  };

  // ------------------------------------------------------------- profiles
  MB.openProfile = async function (userId, server) {
    await MB.ensureUser(userId);
    const s = MB.store;
    const u = s.users[userId] || {};
    const member = server ? (s.members[server._id] || {})[userId] : null;
    const mePerms = server ? MB.permsFor(server, null, s.me._id) : null;
    const body = h(`<div class="profile">
      <div class="pf-banner"></div>
      <div class="pf-head"><div class="pf-av"></div>
        <div class="pf-names"><h3></h3><span class="pf-tag"></span></div></div>
      <div class="pf-status"></div>
      <div class="pf-bio"></div>
      ${server ? '<div class="pf-roles"></div>' : ''}
      ${member ? '<div class="pf-joined"></div>' : ''}
      <div class="pf-actions"></div></div>`);
    const av = el('.pf-av', body);
    av.appendChild(MB.avatar(userId, 72, { status: true }));
    el('h3', body).textContent = MB.displayName(server ? server._id : null, userId);
    el('.pf-tag', body).textContent = MB.tag(u);
    const statusText = (u.status && u.status.text) || '';
    el('.pf-status', body).textContent = statusText || (u.online ? 'Online' : 'Offline');
    el('.pf-status', body).classList.toggle('muted', !statusText);
    try {
      const p = await MB.api('GET', `/users/${userId}/profile`);
      if (p && p.content) el('.pf-bio', body).textContent = p.content;
      if (p && p.background && p.background._id) el('.pf-banner', body).style.backgroundImage = `url(${MB.asset('backgrounds', p.background._id)})`;
    } catch (e) { /* bots may lack profile access */ }
    if (server) {
      const roles = MB.rolesOf(server, userId);
      const wrap = el('.pf-roles', body);
      if (!roles.length) wrap.appendChild(h('<span class="muted">no roles</span>'));
      for (const r of roles) {
        const chip = h(`<span class="role-chip" style="border-color:${r.colour || '#444'}">${MB.esc(r.name)}</span>`);
        if (r.colour) chip.style.color = r.colour;
        wrap.appendChild(chip);
      }
    }
    if (member && member.joined_at) el('.pf-joined', body).textContent = 'Joined ' + MB.fmtDate(member.joined_at);
    const actions = el('.pf-actions', body);
    if (member) {
      const mbtn = h('<button class="btn primary small">Message</button>');
      mbtn.onclick = async () => {
        try {
          const dm = await MB.api('GET', `/users/${userId}/dm`);
          if (!s.dms.find((d) => d._id === dm._id)) s.dms.push(dm);
          m.close();
          MB.bus.emit('open', { server: null, channel: dm._id });
        } catch (e) { MB.toast(e.message, 'error'); }
      };
      actions.appendChild(mbtn);
    }
    if (server && mePerms) {
      if (mePerms.has('ManageNicknames')) {
        const b = h('<button class="btn ghost small">Nickname</button>');
        b.onclick = async () => {
          const val = await MB.prompt('Set nickname', (member && member.nickname) || '', 'leave empty to reset');
          if (val === null) return;
          try {
            await MB.api('PATCH', `/servers/${server._id}/members/${userId}`, { nickname: val || null });
            MB.toast('Nickname updated');
            MB.renderMembers();
          } catch (e) { MB.toast(e.message, 'error'); }
        };
        actions.appendChild(b);
      }
      if (mePerms.has('AssignRoles')) {
        const b = h('<button class="btn ghost small">Roles</button>');
        b.onclick = () => { m.close(); MB.openRoleAssign(server, userId); };
        actions.appendChild(b);
      }
      if (mePerms.has('KickMembers')) {
        const b = h('<button class="btn ghost small danger">Kick</button>');
        b.onclick = async () => {
          if (!(await MB.confirm('Kick member', `Kick ${MB.displayName(server._id, userId)} from ${server.name}?`, 'Kick'))) return;
          try {
            await MB.api('DELETE', `/servers/${server._id}/members/${userId}`);
            MB.toast('Kicked');
            m.close();
            MB.renderMembers();
          } catch (e) { MB.toast(e.message, 'error'); }
        };
        actions.appendChild(b);
      }
      if (mePerms.has('BanMembers')) {
        const b = h('<button class="btn ghost small danger">Ban</button>');
        b.onclick = async () => {
          const reason = await MB.prompt('Ban member', '', 'reason (optional)');
          if (reason === null) return;
          try {
            await MB.api('PUT', `/servers/${server._id}/bans/${userId}`, { reason });
            MB.toast('Banned');
            m.close();
            MB.renderMembers();
          } catch (e) { MB.toast(e.message, 'error'); }
        };
        actions.appendChild(b);
      }
    }
    const copy = h('<button class="btn ghost small">Copy ID</button>');
    copy.onclick = () => { navigator.clipboard.writeText(userId); MB.toast('User ID copied to console'); console.log('user id', userId); };
    actions.appendChild(copy);
    const m = MB.modal(MB.displayName(server ? server._id : null, userId), body);
  };

  MB.openRoleAssign = async function (server, userId) {
    const member = (MB.store.members[server._id] || {})[userId] || { roles: [] };
    const current = new Set(member.roles || []);
    const body = h('<div class="role-assign"></div>');
    const roles = Object.values(server.roles || {}).filter((r) => r._id !== server._id).sort((a, b) => a.rank - b.rank);
    for (const r of roles) {
      const row = h(`<label class="check"><input type="checkbox"><span class="dot" style="background:${r.colour || '#666'}"></span><span class="rl-name"></span></label>`);
      el('.rl-name', row).textContent = r.name;
      const cb = el('input', row);
      cb.checked = current.has(r._id);
      cb.dataset.rid = r._id;
      body.appendChild(row);
    }
    const row = h('<div class="row end"><button class="btn ghost">Cancel</button><button class="btn primary">Save</button></div>');
    body.appendChild(row);
    const m = MB.modal('Roles for ' + MB.displayName(server._id, userId), body);
    el('.primary', row).onclick = async () => {
      const ids = Array.from(els('input[type=checkbox]', body)).filter((c) => c.checked).map((c) => c.dataset.rid);
      try {
        await MB.api('PATCH', `/servers/${server._id}/members/${userId}`, { roles: ids });
        MB.store.members[server._id][userId] = { ...member, roles: ids };
        MB.toast('Roles updated');
        m.close();
        MB.renderMembers();
      } catch (e) { MB.toast(e.message, 'error'); }
    };
  };

  // -------------------------------------------------------------- typing
  MB.renderTyping = function (ch) {
    const box = el('#typing-bar');
    const users = (MB.store.typing[ch._id] || []).filter((u) => u !== MB.store.me._id);
    if (!users.length) { box.innerHTML = ''; return; }
    const names = users.map((u) => MB.displayName(ch.server || null, u));
    box.innerHTML = `<span class="dots"><i></i><i></i><i></i></span> ${MB.esc(names.join(', '))} ${users.length === 1 ? 'is' : 'are'} typing…`;
  };

  // --------------------------------------------------------------- title
  MB.updateTitle = function () {
    const cur = MB.current();
    let n = 0, mentions = 0;
    for (const u of Object.values(MB.store.unreads)) { n += u.count > 0 ? u.count : 1; if (u.mentions) mentions++; }
    document.title = (mentions ? `(${mentions}) ` : n ? `(${n}) ` : '') + 'magbot';
  };

  MB.jumpTo = function (mid) {
    const node = el(`.msg[data-id="${mid}"]`);
    if (node) {
      node.scrollIntoView({ block: 'center', behavior: 'smooth' });
      node.classList.add('flash');
      setTimeout(() => node.classList.remove('flash'), 1200);
    } else {
      MB.toast('that message is not loaded');
    }
  };

  MB.refreshMessage = async function (cid, mid) {
    try {
      const got = await MB.api('GET', `/channels/${cid}/messages`, undefined);
    } catch (e) {}
    const list = MB.messages[cid] || [];
    const idx = list.findIndex((m) => m._id === mid);
    if (idx >= 0) { list[idx] = { ...list[idx], edited: true }; }
    if (MB.current().channel === cid) MB.renderMessages(MB.channel(cid));
  };
})();
