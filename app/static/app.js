/* 脱敏文稿工作台 —— 原生 JS 单页应用 */
'use strict';

const state = { me: null };

const $ = (sel, el = document) => el.querySelector(sel);

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

let toastTimer = null;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 4000);
}

/* ---------------------------------------------------------------- API */

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: 'same-origin', ...opts });
  let data = null;
  if ((res.headers.get('content-type') || '').includes('application/json')) {
    data = await res.json();
  }
  if (!res.ok) {
    const msg = (data && data.detail) || `请求失败（${res.status}）`;
    if (res.status === 409) {
      toast(msg + '，正在刷新…');
      setTimeout(render, 800);
    } else if (res.status !== 401 || !opts.quiet401) {
      toast(msg);
    }
    throw new Error(msg);
  }
  return data;
}

function post(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

/* ------------------------------------------------- Unicode 选区与渲染 */

/* 浏览器选区偏移 = UTF-16 码元数，与服务端 u16 偏移一致 */
function selectionOffsets(container) {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
  const r = sel.getRangeAt(0);
  if (!container.contains(r.startContainer) || !container.contains(r.endContainer)) {
    return null;
  }
  const pre = document.createRange();
  pre.selectNodeContents(container);
  pre.setEnd(r.startContainer, r.startOffset);
  const start = pre.toString().length;
  const len = r.toString().length;
  if (len === 0) return null;
  return { start, end: start + len };
}

/* 按码元偏移把正文切成高亮片段；for...of 按码点迭代，绝不拆散表情 */
function segments(text, props) {
  const prio = { accepted: 3, pending: 2, rejected: 1 };
  const cps = [];
  let off = 0;
  for (const ch of text) { cps.push({ ch, off }); off += ch.length; }
  const mark = new Array(cps.length).fill(null);
  for (const p of props) {
    const cls = p.status;
    if (!(cls in prio)) continue;
    for (let i = 0; i < cps.length; i++) {
      const cpEnd = cps[i].off + cps[i].ch.length;
      if (cps[i].off < p.end && cpEnd > p.start) {
        if (mark[i] === null || prio[cls] > prio[mark[i]]) mark[i] = cls;
      }
    }
  }
  const segs = [];
  let cur = null;
  for (let i = 0; i < cps.length; i++) {
    if (cur && cur.m === mark[i]) {
      cur.text += cps[i].ch;
    } else {
      cur = { m: mark[i], text: cps[i].ch };
      segs.push(cur);
    }
  }
  return segs;
}

function renderContent(container, text, props) {
  container.textContent = '';
  for (const seg of segments(text, props)) {
    if (seg.m === null) {
      container.appendChild(document.createTextNode(seg.text));
    } else {
      container.appendChild(el('span', 'hl-' + seg.m, seg.text));
    }
  }
}

/* ---------------------------------------------------------------- 视图 */

const STATUS_LABEL = {
  pending: '待裁决', accepted: '已接受', rejected: '已驳回', withdrawn: '已撤回',
};

function renderTopbar() {
  const info = $('#userinfo');
  info.textContent = '';
  if (!state.me) return;
  const roleLabel = state.me.role === 'author' ? '作者' : '审阅人';
  info.appendChild(el('span', 'who', `${state.me.username}（${roleLabel}）`));
  const home = el('a', 'toplink', '首页');
  home.href = '#/';
  info.appendChild(home);
  const out = el('button', 'linkbtn', '退出登录');
  out.onclick = async () => {
    await post('/api/logout').catch(() => {});
    state.me = null;
    location.hash = '#/';
    render();
  };
  info.appendChild(out);
}

function renderLogin(app) {
  renderTopbar();
  app.textContent = '';
  const box = el('div', 'card login');
  box.appendChild(el('h2', null, '登录'));
  const form = el('form');
  const u = el('input'); u.placeholder = '用户名'; u.required = true;
  const p = el('input'); p.type = 'password'; p.placeholder = '密码'; p.required = true;
  const btn = el('button', 'primary', '登录');
  btn.type = 'submit';
  form.append(u, p, btn);
  form.onsubmit = async (e) => {
    e.preventDefault();
    try {
      state.me = await post('/api/login', { username: u.value.trim(), password: p.value });
      location.hash = '#/';
      render();
    } catch (_) { /* toast 已提示 */ }
  };
  box.appendChild(form);
  box.appendChild(el('p', 'hint',
    '默认账号：作者 alice / alice123；审阅人 bob / bob123、carol / carol123'));
  app.appendChild(box);
}

async function renderHome(app) {
  app.textContent = '';
  const [todo, docs] = await Promise.all([api('/api/todo'), api('/api/documents')]);

  /* 待办 */
  const todoCard = el('div', 'card');
  todoCard.appendChild(el('h2', null, '待办'));
  const list = el('ul', 'todo');
  const addTodo = (text, docId) => {
    const li = el('li');
    const a = el('a', null, text);
    a.href = `#/doc/${docId}`;
    li.appendChild(a);
    list.appendChild(li);
  };
  if (todo.role === 'reviewer') {
    (todo.adjudicate || []).forEach(t => addTodo(`待裁决：《${t.title}》有 ${t.n} 条他人提议待你裁决`, t.doc_id));
    (todo.confirm || []).forEach(t => addTodo(`待确认：《${t.title}》提议已全部裁决，请确认最终预览`, t.doc_id));
  } else {
    (todo.waiting || []).forEach(t => addTodo(
      t.pending
        ? `进行中：《${t.title}》还有 ${t.pending} 条提议未裁决`
        : `进行中：《${t.title}》已获 ${t.confirmations}/2 名审阅人确认`, t.doc_id));
    (todo.ready || []).forEach(t => addTodo(`可发布：《${t.title}》已满足发布条件`, t.doc_id));
    (todo.published || []).forEach(t => addTodo(`已发布：《${t.title}》`, t.doc_id));
  }
  if (!list.children.length) list.appendChild(el('li', 'empty', '暂无待办事项'));
  todoCard.appendChild(list);
  app.appendChild(todoCard);

  /* 新建文稿（作者） */
  if (state.me.role === 'author') {
    const card = el('div', 'card');
    card.appendChild(el('h2', null, '新建文稿'));
    const title = el('input'); title.placeholder = '标题';
    const content = el('textarea'); content.rows = 6;
    content.placeholder = '粘贴正文（创建后正文不可变，修改须另建版本）';
    const btn = el('button', 'primary', '创建文稿');
    btn.onclick = async () => {
      try {
        const r = await post('/api/documents', {
          title: title.value, content: content.value,
        });
        location.hash = `#/doc/${r.id}`;
      } catch (_) {}
    };
    card.append(title, content, btn);
    app.appendChild(card);
  }

  /* 文稿列表 */
  const card = el('div', 'card');
  card.appendChild(el('h2', null, '全部文稿'));
  const table = el('table');
  table.innerHTML = '<tr><th>标题</th><th>作者</th><th>状态</th><th>当前版本</th><th>创建时间</th></tr>';
  docs.forEach(d => {
    const tr = el('tr');
    const a = el('a', null, d.title);
    a.href = `#/doc/${d.id}`;
    const td0 = el('td'); td0.appendChild(a);
    tr.appendChild(td0);
    tr.appendChild(el('td', null, d.author));
    tr.appendChild(el('td', null, d.status === 'published' ? '已发布' : '进行中'));
    tr.appendChild(el('td', null, 'v' + d.current_seq));
    tr.appendChild(el('td', null, d.created_at));
    table.appendChild(tr);
  });
  if (!docs.length) card.appendChild(el('p', 'empty', '暂无文稿'));
  else card.appendChild(table);
  app.appendChild(card);
}

function proposalItem(d, p, reload) {
  const li = el('li', 'prop prop-' + p.status);
  const head = el('div', 'prop-head');
  head.appendChild(el('span', 'badge badge-' + p.status, STATUS_LABEL[p.status]));
  head.appendChild(el('span', 'excerpt', `“${p.excerpt}”`));
  li.appendChild(head);
  const meta = el('div', 'meta',
    `${p.reviewer} 提议 · 区间 [${p.start}, ${p.end})` +
    (p.decider ? ` · 由 ${p.decider} 裁决` : ''));
  li.appendChild(meta);

  const mine = p.reviewer_id === state.me.id;
  const canAct = d.status === 'open' && p.status === 'pending' && state.me.role === 'reviewer';
  if (canAct && mine) {
    const btn = el('button', 'small', '撤回');
    btn.onclick = async () => {
      try {
        await post(`/api/proposals/${p.id}/withdraw`, { expected_state_version: d.state_version });
        reload();
      } catch (_) {}
    };
    li.appendChild(btn);
  }
  if (canAct && !mine) {
    const ok = el('button', 'small primary', '接受');
    ok.onclick = async () => {
      try {
        await post(`/api/proposals/${p.id}/decide`,
          { decision: 'accept', expected_state_version: d.state_version });
        reload();
      } catch (_) {}
    };
    const no = el('button', 'small danger', '驳回');
    no.onclick = async () => {
      try {
        await post(`/api/proposals/${p.id}/decide`,
          { decision: 'reject', expected_state_version: d.state_version });
        reload();
      } catch (_) {}
    };
    li.append(ok, no);
  }
  return li;
}

async function renderDoc(app, id) {
  app.textContent = '';
  const d = await api(`/api/documents/${id}`);
  const reload = () => render();

  const head = el('div', 'card');
  const h2 = el('h2', null, d.title);
  head.appendChild(h2);
  const badge = el('span', 'doc-status ' + d.status, d.status === 'published' ? '已发布' : '进行中');
  h2.appendChild(badge);
  head.appendChild(el('div', 'meta',
    `作者 ${d.author} · 当前版本 v${d.current_version.seq} · 创建于 ${d.created_at}` +
    (d.published_at ? ` · 发布于 ${d.published_at}` : '')));

  const actions = el('div', 'actions');
  const exp = el('a', 'button', '导出遮盖文本');
  exp.href = `/api/documents/${d.id}/export`;
  exp.setAttribute('download', '');
  actions.appendChild(exp);

  const isOwner = state.me.role === 'author' && state.me.id === d.author_id;
  if (isOwner && d.status === 'open') {
    const pub = el('button', 'primary', '发布');
    pub.disabled = !d.can_publish;
    pub.onclick = async () => {
      try {
        await post(`/api/documents/${d.id}/publish`, { expected_state_version: d.state_version });
        toast('发布成功');
        reload();
      } catch (_) {}
    };
    actions.appendChild(pub);
  }
  head.appendChild(actions);

  if (isOwner && d.status === 'open') {
    const box = el('div', 'publish-check');
    box.appendChild(el('b', null, '发布条件检查：'));
    const ul = el('ul');
    const ok1 = d.pending_count === 0;
    const ok2 = d.confirmations.length >= 2;
    ul.appendChild(el('li', ok1 ? 'ok' : 'bad',
      (ok1 ? '✓ ' : '✗ ') + '全部提议已裁决' + (ok1 ? '' : `（剩余 ${d.pending_count} 条）`)));
    ul.appendChild(el('li', ok2 ? 'ok' : 'bad',
      (ok2 ? '✓ ' : '✗ ') + `两名不同且非作者的审阅人已确认同一份最终预览（当前 ${d.confirmations.length}/2）`));
    box.appendChild(ul);
    head.appendChild(box);

    /* 新建版本 */
    const nv = el('details', 'newver');
    nv.appendChild(el('summary', null, '修改正文（另建不可变新版本，旧版批注不沿用）'));
    const ta = el('textarea'); ta.rows = 8; ta.value = d.current_version.content;
    const btn = el('button', 'primary', '保存为新版本');
    btn.onclick = async () => {
      try {
        await post(`/api/documents/${d.id}/versions`,
          { content: ta.value, expected_state_version: d.state_version });
        toast('已创建新版本');
        reload();
      } catch (_) {}
    };
    nv.append(ta, btn);
    head.appendChild(nv);
  }
  app.appendChild(head);

  const layout = el('div', 'layout');

  /* 左栏：正文 + 预览 */
  const left = el('div', 'card');
  left.appendChild(el('h3', null, `当前版本正文（v${d.current_version.seq}）`));
  const legend = el('div', 'legend');
  legend.innerHTML = '<span class="hl-pending">待裁决</span> ' +
    '<span class="hl-accepted">已接受（将被遮盖）</span> ' +
    '<span class="hl-rejected">已驳回</span>';
  left.appendChild(legend);
  const content = el('div', 'content');
  renderContent(content, d.current_version.content,
    d.proposals.filter(p => p.status !== 'withdrawn'));
  left.appendChild(content);

  /* 审阅人：选中文字提议遮盖 */
  if (state.me.role === 'reviewer' && d.status === 'open') {
    const bar = el('div', 'selbar hidden');
    content.addEventListener('mouseup', () => {
      bar.textContent = '';
      const r = selectionOffsets(content);
      if (!r) { bar.classList.add('hidden'); return; }
      bar.classList.remove('hidden');
      bar.appendChild(el('span', null, `已选择区间 [${r.start}, ${r.end})`));
      const go = el('button', 'small primary', '提议遮盖该范围');
      go.onclick = async () => {
        try {
          await post(`/api/documents/${d.id}/proposals`,
            { start: r.start, end: r.end, expected_state_version: d.state_version });
          toast('提议已提交');
          reload();
        } catch (_) {}
      };
      const cancel = el('button', 'small', '取消');
      cancel.onclick = () => { bar.classList.add('hidden'); window.getSelection().removeAllRanges(); };
      bar.append(go, cancel);
    });
    left.appendChild(bar);
  }

  left.appendChild(el('h3', null, '遮盖预览（导出即此内容，仅已接受提议的并集）'));
  const preview = el('pre', 'preview');
  preview.textContent = d.preview;
  left.appendChild(preview);
  layout.appendChild(left);

  /* 右栏：提议 / 确认 / 版本 */
  const right = el('div');

  const propCard = el('div', 'card');
  propCard.appendChild(el('h3', null, `遮盖提议（${d.proposals.length}）`));
  const plist = el('ul', 'props');
  d.proposals.forEach(p => plist.appendChild(proposalItem(d, p, reload)));
  if (!d.proposals.length) plist.appendChild(el('li', 'empty', '当前版本暂无提议'));
  propCard.appendChild(plist);
  right.appendChild(propCard);

  const confCard = el('div', 'card');
  confCard.appendChild(el('h3', null, '最终预览确认'));
  const names = d.confirmations.map(c => c.reviewer).join('、');
  confCard.appendChild(el('p', null,
    `有效确认：${d.confirmations.length}/2` + (names ? `（${names}）` : '')));
  if (d.stale_confirmations > 0) {
    confCard.appendChild(el('p', 'warn',
      `有 ${d.stale_confirmations} 条确认因裁决变化已失效，需重新确认`));
  }
  if (state.me.role === 'reviewer' && d.status === 'open') {
    const mine = d.confirmations.some(c => c.reviewer_id === state.me.id);
    const btn = el('button', 'primary', mine ? '已确认（可重新确认）' : '确认当前最终预览');
    if (d.pending_count > 0) {
      btn.disabled = true;
      confCard.appendChild(el('p', 'warn', `还有 ${d.pending_count} 条提议未裁决，全部裁决后才能确认`));
    }
    btn.onclick = async () => {
      try {
        await post(`/api/documents/${d.id}/confirm`, { expected_state_version: d.state_version });
        toast('已确认当前最终预览');
        reload();
      } catch (_) {}
    };
    confCard.appendChild(btn);
  }
  right.appendChild(confCard);

  const verCard = el('div', 'card');
  verCard.appendChild(el('h3', null, '历史版本'));
  const vlist = el('ul', 'versions');
  d.versions.forEach(v => {
    const li = el('li');
    if (v.seq === d.current_version.seq) {
      li.appendChild(el('b', null, `v${v.seq}（当前）`));
    } else {
      const a = el('a', null, `v${v.seq}`);
      a.href = `#/doc/${d.id}/v/${v.seq}`;
      li.appendChild(a);
    }
    li.appendChild(el('span', 'meta', ` ${v.created_at} · ${v.proposals} 条提议`));
    vlist.appendChild(li);
  });
  verCard.appendChild(vlist);
  right.appendChild(verCard);

  layout.appendChild(right);
  app.appendChild(layout);
}

async function renderVersionView(app, id, seq) {
  app.textContent = '';
  const v = await api(`/api/documents/${id}/versions/${seq}`);
  const card = el('div', 'card');
  const back = el('a', 'button', '← 返回文稿');
  back.href = `#/doc/${id}`;
  card.appendChild(back);
  card.appendChild(el('h2', null, `${v.title} — 历史版本 v${v.seq}`));
  card.appendChild(el('p', 'warn',
    '该版本为不可变历史版本；其批注不会沿用到新版本。'));
  card.appendChild(el('div', 'meta', `创建于 ${v.created_at} · 文稿状态：${v.doc_status === 'published' ? '已发布' : '进行中'}`));
  const content = el('div', 'content');
  renderContent(content, v.content, v.proposals.filter(p => p.status !== 'withdrawn'));
  card.appendChild(content);
  card.appendChild(el('h3', null, '该版本的遮盖预览'));
  const preview = el('pre', 'preview');
  preview.textContent = v.preview;
  card.appendChild(preview);
  card.appendChild(el('h3', null, `该版本的提议（${v.proposals.length}）`));
  const plist = el('ul', 'props');
  v.proposals.forEach(p => {
    const li = el('li', 'prop prop-' + p.status);
    const head = el('div', 'prop-head');
    head.appendChild(el('span', 'badge badge-' + p.status, STATUS_LABEL[p.status]));
    head.appendChild(el('span', 'excerpt', `“${p.excerpt}”`));
    li.appendChild(head);
    li.appendChild(el('div', 'meta',
      `${p.reviewer} 提议 · 区间 [${p.start}, ${p.end})` +
      (p.decider ? ` · 由 ${p.decider} 裁决` : '')));
    plist.appendChild(li);
  });
  if (!v.proposals.length) plist.appendChild(el('li', 'empty', '该版本无提议'));
  card.appendChild(plist);
  app.appendChild(card);
}

/* ---------------------------------------------------------------- 路由 */

async function render() {
  const app = $('#app');
  if (!state.me) { renderLogin(app); return; }
  renderTopbar();
  const h = location.hash || '#/';
  const m = h.match(/^#\/doc\/(\d+)(?:\/v\/(\d+))?$/);
  try {
    if (m && m[2]) await renderVersionView(app, +m[1], +m[2]);
    else if (m) await renderDoc(app, +m[1]);
    else await renderHome(app);
  } catch (_) { /* toast 已提示 */ }
}

window.addEventListener('hashchange', render);

(async function init() {
  try {
    state.me = await api('/api/me', { quiet401: true });
  } catch (_) {
    state.me = null;
  }
  render();
})();
