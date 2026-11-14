/* ================= 图片查重 · 前端逻辑 ================= */
(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const DEFAULT_PAGE = 40;

  const state = {
    load: 100,
    currentView: 'image',
  threshold: Number(localStorage.getItem('pd.th') || 10),
    minSize: Number(localStorage.getItem('pd.min') || 2),
    sort: localStorage.getItem('pd.sort') || 'reclaim',
    recall: localStorage.getItem('pd.recall') || 'standard',
    autoScan: localStorage.getItem('pd.autoscan') === '1',
    dirty: false,
    loadedArchive: '',
    arScope: 'image',
    savingArchive: false,
    arNameTouched: false,
    flashId: '',
    root: '',
    roots: [],
    pending: [],
    scan: null,
    selSize: new Map(),
    groups: [],
    stats: {},
    page: { has_more: false, total_groups: 0 },
    engine: null,
    caps: null,
    byId: new Map(),
    selected: new Set(),
    best: new Map(),
    renderLimit: 40,
    poll: null,
    cmp: null,
    view: { z: 1, x: 0, y: 0 },
    split: 50,
    pendingRemove: null,
    browseCb: null,
    browsePath: '',
    browseFiles: false,
    ignoreResume: false,
    archives: [],
  };

  /* ---------------- 小工具 ---------------- */
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    const u = ['KB', 'MB', 'GB', 'TB'];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return (n >= 100 ? n.toFixed(0) : n.toFixed(1)) + ' ' + u[i];
  }
  const fmtInt = (n) => (Number(n) || 0).toLocaleString('zh-CN');
  const baseName = (p) => String(p || '').split(/[\\/]/).filter(Boolean).pop() || p;

  function fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const pad = (x) => String(x).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function popcount64(x) {
    let c = 0n;
    while (x > 0n) { c += x & 1n; x >>= 1n; }
    return Number(c);
  }
  function simPct(a, b) {
    if (!a || !b) return null;
    const d = popcount64(BigInt('0x' + a) ^ BigInt('0x' + b));
    return Math.round((1 - d / 64) * 1000) / 10;
  }

  function toast(msg, kind = '', ms = 3200) {
    const host = $('#toasts');
    while (host.children.length >= 3) host.removeChild(host.firstChild);
    const el = document.createElement('div');
    el.className = 'toast ' + kind;
    el.innerHTML = msg;
    host.appendChild(el);
    setTimeout(() => {
      el.style.transition = 'opacity .25s, transform .25s';
      el.style.opacity = '0';
      el.style.transform = 'translateX(14px)';
      setTimeout(() => el.remove(), 260);
    }, ms);
  }

  async function api(path, opts) {
    const r = await fetch(path, opts);
    const ct = r.headers.get('content-type') || '';
    if (!ct.includes('application/json')) throw new Error(`服务返回异常 (${r.status})`);
    const j = await r.json();
    if (!r.ok && j.error) throw new Error(j.error);
    return j;
  }
  const post = (path, body) => api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });

  let askResolve = null;
  function askConfirm(title, text, okLabel = '确定', danger = true) {
    $('#askTitle').textContent = title;
    $('#askText').innerHTML = text;
    $('#askStd').hidden = false;
    $('#askCustom').hidden = true;
    $('#askOk').textContent = okLabel;
    $('#askOk').className = danger ? 'danger solid' : 'primary';
    $('#ask').hidden = false;
    return new Promise((res) => { askResolve = res; });
  }

  /** 多按钮确认：buttons = [{key,label,cls}]，返回被点按钮的 key（取消=null） */
  function askButtons(title, text, buttons) {
    $('#askTitle').textContent = title;
    $('#askText').innerHTML = text;
    $('#askStd').hidden = true;
    const host = $('#askCustom');
    host.hidden = false;
    host.innerHTML = '';
    buttons.forEach((b) => {
      const el = document.createElement('button');
      el.className = b.cls || 'ghost';
      el.textContent = b.label;
      el.onclick = () => closeAsk(b.key);
      host.appendChild(el);
    });
    $('#ask').hidden = false;
    return new Promise((res) => { askResolve = res; });
  }

  function closeAsk(v) {
    $('#ask').hidden = true;
    if (askResolve) { askResolve(v); askResolve = null; }
  }


  /* 版本自检：程序更新后自动刷新，省得手动 F5 */
  let PAGE_TAG = "";
  let pageTagReady = false;
  async function watchVersion() {
    try {
      const r = await api("/api/version");
      if (!r || !r.tag) return;
      if (!pageTagReady) { PAGE_TAG = r.tag; pageTagReady = true; return; }
      if (r.tag !== PAGE_TAG) {
        PAGE_TAG = r.tag;
        toast("程序已更新，正在刷新页面…", "ok", 2200);
        setTimeout(() => location.reload(), 1200);
      }
    } catch (_) { /* 忽略 */ }
  }


  /* 调 Windows 原生文件夹选择框（服务端起 PowerShell），返回原始结果 */
  async function pickFolderNative(title, initial, key) {
    try {
      return await post('/api/pick_folder', {
        title: title || '选择文件夹', initial: initial || '', key: key || '',
      });
    } catch (e) {
      return { ok: false, error: '打开选择框失败：' + e.message };
    }
  }

  /* 选文件（原生对话框，带扩展名过滤） */
  async function pickFileNative(title, initial, filters, key) {
    try {
      return await post('/api/pick_file', {
        title: title || '选择文件', initial: initial || '', filters: filters || '',
        key: key || '',
      });
    } catch (e) {
      return { ok: false, error: '打开选择框失败：' + e.message };
    }
  }

  /* 统一入口：原生对话框优先；取消就结束，系统不支持（非 Windows）才退回内置浏览器 */
  async function pickFolder(title, initial, cb, key) {
    const r = await pickFolderNative(title, initial, key);
    if (r.ok && r.path) { cb(r.path); return; }
    if (r.cancelled) return;
    if (r.error) toast(r.error, 'err', 2600);
    openBrowser(initial || '', cb);
  }

  /* 选单个文件：原生优先，失败退回内置浏览器（只能浏览，不能过滤扩展名） */
  async function pickFile(title, initial, filters, cb, key) {
    const r = await pickFileNative(title, initial, filters, key);
    if (r.ok && r.path) { cb(r.path); return; }
    if (r.cancelled) return;
    if (r.error) toast(r.error, 'err', 2600);
    openBrowser(initial || '', cb, { files: true, title });
  }

  /* ---------------- 初始化 ---------------- */
  async function init() {
    // 界面语言：先翻译静态文案，再初始化各页（动态内容由 MutationObserver 跟上）
    if (window.I18N && window.I18N.start) { try { window.I18N.start(); } catch (_) {} }
    window.addEventListener("pd:lang", () => {
      try {
        renderStats();
        renderGroups();
        if (typeof mRenderRoots === "function") mRenderRoots();
        updateEngineBadge();
      } catch (_) {}
    });
    $('#threshold').value = state.threshold;
    $('#minSize').value = String(state.minSize);
    if (!$(`#sort option[value="${state.sort}"]`)) state.sort = 'reclaim';
    $('#sort').value = state.sort;
    $('#recall').value = state.recall;
    $('#autoScan').checked = state.autoScan;
    updateRangeFill();
    bindEvents();
    bindMedia();
    bindMp();
    bindOther();
    patchStart();
    bindTools();
    bindLoad();
    watchVersion();
    setInterval(watchVersion, 20000);
    window.addEventListener("focus", watchVersion);
    await loadRoots();
    await refreshStatus();
    loadCapabilities();
  }

  async function loadCapabilities() {
    try {
      const c = await api('/api/capabilities');
      state.caps = c;
      if (!state.engine) state.engine = c.engine;
      if (c.load) { state.load = Number(c.load); loadSync(state.load); }
      const f = c.formats || {};
      const hint = $('#formatsHint');
      if (hint) {
        const list = (f.common && f.common.length ? f.common : f.extensions) || [];
        hint.textContent = `支持 ${f.count || 0} 种格式：` +
          list.slice(0, 12).map((e) => e.slice(1).toUpperCase()).join(' / ') +
          (list.length > 12 ? ' 等' : '');
        hint.title = (f.extensions || []).map((e) => e.slice(1).toUpperCase()).join(' ');
      }
      updateEngineBadge();
    } catch (_) { /* 老版本没有这个接口也不影响 */ }
  }

  function bindEvents() {
    $('#btnAdd').onclick = () => addFolder($('#folder').value.trim());
    $('#autoScan').onchange = (e) => {
      state.autoScan = e.target.checked;
      localStorage.setItem('pd.autoscan', state.autoScan ? '1' : '0');
      toast(state.autoScan ? '已开启：添加第一个文件夹后自动扫描' : '已关闭：添加文件夹后需要手动点「开始扫描」', '', 2600);
    };
    $('#btnBrowse').onclick = () => pickFolder('选择照片文件夹', $('#folder').value || '', addFolder, 'imgAdd');
    $('#btnEmptyBrowse').onclick = () => pickFolder('选择照片文件夹', '', addFolder, 'imgAdd');
    $('#folder').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') addFolder(e.target.value.trim());
    });

    $('#btnScan').onclick = startScan;
    $('#btnPause').onclick = pauseScan;
    $('#btnResume').onclick = resumeScan;
    $('#btnStop').onclick = stopScan;
    $('#btnResumeAll').onclick = resumeScan;
    $('#btnIgnoreResume').onclick = () => {
      state.ignoreResume = true;
      $('#resumeBar').hidden = true;
    };

    $('#rootChips').addEventListener('click', onChipClick);

    let thTimer = null;
    $('#threshold').addEventListener('input', (e) => {
      state.threshold = Number(e.target.value);
      $('#thVal').textContent = state.threshold;
      updateRangeFill();
      clearTimeout(thTimer);
      thTimer = setTimeout(() => { savePrefs(); loadGroups(); }, 300);
    });
    $('#minSize').onchange = (e) => { state.minSize = Number(e.target.value); savePrefs(); loadGroups(); };
    $('#sort').onchange = (e) => { state.sort = e.target.value; savePrefs(); loadGroups(); };
    $('#recall').onchange = (e) => { state.recall = e.target.value; savePrefs(); loadGroups(); };
    $('#root').onchange = (e) => { state.root = e.target.value; loadGroups(); };

    $('#btnSmart').onclick = openAuto;
    $('#dfClose').onclick = $('#dfCancel').onclick = () => { $('#diff').hidden = true; };
    $('#dfGo').onclick = doDiffUpdate;
    $('#auClose').onclick = () => { $('#auto').hidden = true; };
    $('#auCalc').onclick = calcAuto;
    $('#auApply').onclick = applyAuto;
    $('#auKeep').onchange = toggleAutoFolder;
    $('#auAddFolder').onclick = () => pickFolder('选择文件夹', '', (p) => {
      state.autoFolders = state.autoFolders || [];
      if (!state.autoFolders.includes(p) && !state.roots.some((r) => r.path === p)) {
        state.autoFolders.push(p);
      }
      renderAutoFolders();
    });
    $('#btnExport').onclick = exportCsv;
    $('#btnClearSel').onclick = clearSelection;
    $('#btnMove').onclick = () => askRemove('move');
    $('#btnTrash').onclick = () => askRemove('trash');
    $('#btnMore').onclick = async () => {
      if (!state.page || !state.page.has_more) return;
      $('#btnMore').disabled = true;
      await loadGroups(true);
      $('#btnMore').disabled = false;
    };

    $('#groups').addEventListener('click', onGroupClick);
    $('#groups').addEventListener('dblclick', (e) => {
      const fig = e.target.closest('.thumb');
      if (fig) openCompare(fig.dataset.key, Number(fig.dataset.id));
    });

    // 对比窗口
    $('#cmpClose').onclick = closeCompare;
    $('#cmpPrev').onclick = () => stepCompare(-1);
    $('#cmpNext').onclick = () => stepCompare(1);
    $('#cmpMark').onclick = cmpMarkDelete;
    $('#cmpKeep').onclick = cmpSetKeep;
    $('#cmpReveal').onclick = () => {
      const id = state.cmp?.candId;
      if (id) post('/api/reveal', { id }).catch(() => {});
    };
    $$('.mtabs button').forEach((b) => {
      b.onclick = () => {
        $$('.mtabs button').forEach((x) => x.classList.toggle('on', x === b));
        $('#stage').dataset.mode = b.dataset.mode;
      };
    });
    $('#zoomIn').onclick = () => zoomBy(1.25);
    $('#zoomOut').onclick = () => zoomBy(0.8);
    $('#zoomReset').onclick = resetView;
    bindStage();

    // 文件夹浏览
    $('#browseClose').onclick = closeBrowser;
    $('#browseCancel').onclick = closeBrowser;
    $('#browseUp').onclick = () => {
      const p = $('#browseUp').dataset.parent;
      if (p) navigateBrowser(p);
    };
    $('#browseOk').onclick = () => {
      if (!state.browsePath) return;
      const p = state.browsePath;
      const cb = state.browseCb;
      closeBrowser();
      if (cb) cb(p);
    };
    $('#browseInput').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') navigateBrowser(e.target.value.trim());
    });
    $('#browseList').addEventListener('click', (e) => {
      const it = e.target.closest('.browse-item');
      if (!it) return;
      if (it.dataset.file === '1') {
        const p = it.dataset.path;
        const cb = state.browseCb;
        closeBrowser();
        if (cb) cb(p);
        return;
      }
      navigateBrowser(it.dataset.path);
    });

    // 存档
    $('#btnArchives').onclick = openArchives;
    $('#arClose').onclick = () => { $('#archives').hidden = true; };
    /* arscope-bind */
    $$('.ar-tab').forEach((b) => {
      b.onclick = async () => {
        if (state.arScope === b.dataset.scope) return;
        state.arScope = b.dataset.scope;
        $$('.ar-tab').forEach((x) => x.classList.toggle('on', x === b));
        $('#arHint').hidden = true;
        renderArchiveSummary();
        await loadArchives();
      };
    });
    $('#arSave').onclick = () => saveArchive();
    $('#arName').addEventListener('keydown', (e) => { if (e.key === 'Enter') saveArchive(); });
    $('#arName').addEventListener('input', () => {
      state.arNameTouched = true;
      const v = $('#arName').value.trim();
      const hint = $('#arHint');
      const dup = v && state.archives.some((a) => a.name === v);
      if (dup && !state.savingArchive) {
        hint.hidden = false;
        hint.className = 'ar-hint warn';
        hint.textContent = `已有同名存档，这次会存为「${uniqueArchiveName(v)}」（要覆盖旧的请用列表里的「覆盖」）`;
      } else if (!state.savingArchive) {
        hint.hidden = true;
      }
    });
    $('#arImport').onclick = () => {
      // 记住上次打开的位置，下次直接从那儿开始
      const last = localStorage.getItem('pd.arDir') || '';
      const start = last || (settingsCache && settingsCache.default_archive_dir) || '';
      openBrowser(start, importArchivePath, { files: true, title: '导入存档文件' });
    };
    $('#arList').addEventListener('click', onArchiveClick);
    $('#arVerify').onclick = async () => {
      try {
        const r = await post('/api/verify', {});
        toast(r.already ? '校验已经在跑了…' : '正在校验失效文件…', '', 3000);
        pollVerify();
      } catch (e) { toast('校验失败：' + esc(e.message), 'err'); }
    };

    // 设置
    $('#btnSettings').onclick = openSettings;
    $('#stClose').onclick = closeSettings;
    $('#stCancel').onclick = closeSettings;
    $('#stSave').onclick = saveSettings;


    // 确认框 / 小确认
    $('#cfClose').onclick = $('#cfCancel').onclick = () => {
      $('#confirm').hidden = true;
      state.pendingRemove = null;
    };
    $$('input[name=cfmode]').forEach((r) => r.onchange = syncConfirmMode);
    $('#cfPick').onclick = () => pickFolder('选择输出文件夹', $('#cfDest').value || '',
      (p) => { $('#cfDest').value = p; });
    $('#cfOk').onclick = doRemove;
    $('#askOk').onclick = () => closeAsk(true);
    $('#askCancel').onclick = () => closeAsk(false);
    $('#ask').addEventListener('mousedown', (e) => { if (e.target.id === 'ask') closeAsk(false); });

    document.addEventListener('keydown', onKey);
    $$('.modal').forEach((m) => {
      m.addEventListener('mousedown', (e) => {
        if (e.target === m && m.id !== 'confirm' && m.id !== 'ask' && m.id !== 'archives') closeModal(m.id);
      });
    });
  }

  function savePrefs() {
    localStorage.setItem('pd.th', String(state.threshold));
    localStorage.setItem('pd.min', String(state.minSize));
    localStorage.setItem('pd.sort', state.sort);
    localStorage.setItem('pd.recall', state.recall);
  }

  function updateRangeFill() {
    const el = $('#threshold');
    const pct = ((Number(el.value) - el.min) / (el.max - el.min)) * 100;
    el.style.setProperty('--pct', pct + '%');
    $('#thVal').textContent = el.value;
  }

  function closeModal(id) {
    const el = document.getElementById(id);
    if (!el) return;
    if (id === 'browser') closeBrowser();
    else if (id === 'compare') closeCompare();
    else el.hidden = true;
  }

  /* ---------------- 文件夹工作区 ---------------- */
  async function loadRoots() {
    try {
      const r = await api('/api/roots');
      state.roots = r.roots || [];
      state.pending = r.pending || [];
      renderChips();
      updateScanButtons();
      if (state.pending.length && !state.ignoreResume && !(state.scan && state.scan.running)) {
        $('#resumeCount').textContent = state.pending.length;
        $('#resumeBar').hidden = false;
      } else {
        $('#resumeBar').hidden = true;
      }
    } catch (e) {
      toast('读取文件夹列表失败：' + esc(e.message), 'err');
    }
  }

  function rootStatusOf(root) {
    const q = (state.scan?.queue || []).find((x) => x.path === root.path);
    if (state.scan?.running && q) {
      if (q.state === 'hashing') return 'scanning';
      if (q.state === 'pending') return 'queued';
      return q.state;
    }
    if (!root.exists) return 'error';
    if (root.status === 'paused') return 'paused';
    if (root.status === 'error') return 'error';
    if (root.last_scan > 0) return 'done';
    return 'idle';
  }

  function renderChips() {
    const host = $('#rootChips');
    if (!state.roots.length) { host.hidden = true; host.innerHTML = ''; return; }
    host.hidden = false;
    host.innerHTML = state.roots.map((r) => {
      const st = rootStatusOf(r);
      const q = (state.scan?.queue || []).find((x) => x.path === r.path);
      const count = state.scan?.running && q ? (q.total ? `${fmtInt(q.done)}/${fmtInt(q.total)}` : '…')
        : fmtInt(r.images);
      const cls = { scanning: 'st-scanning', paused: 'st-paused', error: 'st-error', done: 'st-done' }[st] || '';
      const tip = `${r.path}\n${r.last_scan ? '上次扫描：' + fmtTime(r.last_scan) : '尚未扫描'}`;
      return `<span class="chip-root ${cls}" data-path="${esc(r.path)}" title="${esc(tip)}">
        <i class="st"></i>
        <span class="nm" title="${esc(r.path)}">${esc(baseName(r.path))}</span>
        <span class="cnt">${count}</span>
        <button class="up" data-act="diff" title="对比并更新这个文件夹">⟳</button>
        <button class="x" data-act="rm" title="从工作区移除（不会删除文件）">✕</button>
      </span>`;
    }).join('');
  }

  function onChipClick(e) {
    const chip = e.target.closest('.chip-root');
    if (!chip) return;
    const path = chip.dataset.path;
    if (e.target.closest('[data-act="diff"]')) {
      openDiff(path);
      return;
    }
    if (e.target.closest('[data-act="rm"]')) {
      askConfirm('移除文件夹',
        `把 <b>${esc(path)}</b> 从工作区移除？<br>只会从索引里删掉，磁盘上的图片不会动。`,
        '移除', true).then(async (yes) => {
        if (!yes) return;
        try {
          const r = await post('/api/roots', { action: 'remove', path });
          if (!r.ok) { toast(esc(r.error || '移除失败'), 'err'); return; }
          toast(`已移除，清掉 ${fmtInt(r.removed)} 条索引`, 'ok');
          if (state.root === path) state.root = '';
          await loadRoots();
          await loadGroups();
        } catch (err) { toast('移除失败：' + esc(err.message), 'err'); }
      });
      return;
    }
    // 点名字 = 只看这个文件夹的结果
    state.root = state.root === path ? '' : path;
    $('#root').value = state.root;
    loadGroups();
  }

  async function addFolder(path) {
    if (!path) { pickFolder('选择照片文件夹', '', addFolder, 'imgAdd'); return; }
    const wasEmpty = state.roots.length === 0;
    try {
      const r = await post('/api/roots', { path, recursive: $('#recursive').checked });
      if (!r.ok) { toast(esc(r.error || '添加失败'), 'err'); return; }
      $('#folder').value = '';
      state.ignoreResume = false;
      await loadRoots();
      if (wasEmpty && state.autoScan) {
        startScan();
      } else {
        toast(`已添加：${esc(baseName(r.root))}，点「开始扫描」开始，或点标签上的 ⟳ 只更新它`, '', 4200);
        const btn = $('#btnScan');
        btn.classList.add('attention');
        setTimeout(() => btn.classList.remove('attention'), 2400);
      }
    } catch (e) {
      toast('添加失败：' + esc(e.message), 'err');
    }
  }

  /* ---------------- 扫描 ---------------- */
  function updateScanButtons() {
    const s = state.scan || {};
    const running = !!s.running;
    const paused = s.state === 'paused';
    $('#btnScan').hidden = running;
    $('#btnPause').hidden = !running || paused;
    $('#btnResume').hidden = !(running && paused);
    $('#btnStop').hidden = !running;
    $('#btnScan').disabled = running;
    const scanned = state.roots.some((r) => r.last_scan > 0 || r.images > 0);
    $('#scanLabel').textContent = scanned ? '更新扫描' : '开始扫描';
    if (!running) $('#progress').hidden = true;
  }

  async function refreshStatus() {
    try {
      const s = await api('/api/scan/status');
      state.scan = s;
      if (s.stats && s.stats.images > 0) await loadGroups();
      if (s.running) {
        $('#progress').hidden = false;
        renderProgress(s);
        pollStatus();
      } else {
        updateScanButtons();
      }
    } catch (_) { /* 忽略 */ }
  }

  async function startScan() {
    const draft = $('#folder').value.trim();
    if (draft) {
      const r = await post('/api/roots', { path: draft, recursive: $('#recursive').checked }).catch(() => null);
      if (r && r.ok) { $('#folder').value = ''; await loadRoots(); }
      else if (r && !r.ok) { toast(esc(r.error), 'err'); return; }
    }
    if (!state.roots.length) { pickFolder('选择照片文件夹', '', addFolder, 'imgAdd'); return; }
    const slow = setTimeout(() => toast('正在初始化计算引擎（首次加载 GPU 可能要几秒）…', '', 5000), 500);
    try {
      const r = await post('/api/scan', {});
      clearTimeout(slow);
      if (!r.ok) { toast(esc(r.error), 'err'); return; }
      if (r.queued) {
        toast(esc(r.message || '已加入队列'), '', 4500);
        state.poll = setTimeout(() => pollStatus(), 1500);
        return;
      }
      if (r.engine) toast('计算引擎：' + esc(r.engine), '', 2200);
      state.ignoreResume = false;
      $('#resumeBar').hidden = true;
      state.scan = { running: true, state: 'starting', total: 0, done: 0, message: '准备中…' };
      updateScanButtons();
      $('#progress').hidden = false;
      renderProgress(state.scan);
      pollStatus();
    } catch (e) {
      clearTimeout(slow);
      toast('启动扫描失败：' + esc(e.message), 'err');
    }
  }

  async function pauseScan() {
    try {
      const r = await post('/api/scan/pause');
      if (!r.ok) toast(esc(r.error), 'err');
      else toast('已暂停，随时可以继续（已分析的部分不会重复算）');
    } catch (e) { toast(esc(e.message), 'err'); }
  }

  async function resumeScan() {
    try {
      const r = await post('/api/scan/resume');
      if (!r.ok) { toast(esc(r.error), 'err'); return; }
      if (r.mode === 'restart') {
        state.scan = { running: true, state: 'starting', total: 0, done: 0, message: '继续上次未完成的扫描…' };
        $('#progress').hidden = false;
        renderProgress(state.scan);
        pollStatus();
      }
      $('#resumeBar').hidden = true;
      state.ignoreResume = true;
    } catch (e) { toast(esc(e.message), 'err'); }
  }

  async function stopScan() {
    const yes = await askConfirm('停止扫描', '停止当前扫描？<b>已分析的部分会保留</b>，之后可以继续。', '停止', true);
    if (!yes) return;
    try { await post('/api/scan/cancel'); } catch (_) {}
  }

  function pollStatus() {
    clearTimeout(state.poll);
    const tick = async () => {
      try {
        const s = await api('/api/scan/status');
        state.scan = s;
        renderProgress(s);
        renderChips();
        updateScanButtons();
        if (s.running) {
          state.poll = setTimeout(tick, 600);
        } else {
          const wasPaused = s.state === 'paused';
          if (s.state === 'done') {
            toast(`扫描完成：共 ${fmtInt(s.total)} 张，本次新分析 ${fmtInt(s.hashed)} 张`, 'ok', 4000);
            setDirty(true);
          } else if (s.state === 'error') {
            toast('扫描出错：' + esc(s.message), 'err', 6000);
          } else if (s.state === 'cancelled') {
            toast(`已停止：已分析 ${fmtInt(s.done)} / ${fmtInt(s.total)} 张，随时可以继续`, 'ok', 4500);
          } else if (wasPaused) {
            toast('已暂停，点「继续」接着扫', '', 3500);
          }
          $('#progress').hidden = !wasPaused;
          await loadRoots();
          await loadGroups();
          if (s.queued) {
            // 队列里还有任务，继续轮询等它启动
            toast(`还有 ${s.queued} 个任务在排队，稍后自动开始`, '', 3000);
            state.poll = setTimeout(tick, 1500);
            return;
          }
          if (s.pending && s.pending.length && !state.ignoreResume) {
            $('#resumeCount').textContent = s.pending.length;
            $('#resumeBar').hidden = false;
          }
        }
      } catch (e) {
        state.poll = setTimeout(tick, 1500);
      }
    };
    tick();
  }

  function renderProgress(s) {
    const total = Number(s.total) || 0;
    const done = Number(s.done) || 0;
    const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : (s.state === 'walking' ? 2 : 0);
    $('#barFill').style.width = pct + '%';
    let left = s.message || '';
    if (s.state === 'paused') {
      left = `已暂停 · ${fmtInt(done)} / ${fmtInt(total)}`;
    } else if (s.state === 'hashing' && total) {
      left = `正在分析 ${fmtInt(done)} / ${fmtInt(total)}`;
      if (s.root) left += ` · ${baseName(s.root)}`;
      if (s.current) left += ` · ${s.current}`;
    }
    $('#pText').textContent = left;
    const parts = [];
    if (pct) parts.push(pct + '%');
    if (s.queue && s.queue.length > 1 && s.qindex != null) {
      parts.push(`文件夹 ${Math.min(s.qindex + 1, s.queue.length)}/${s.queue.length}`);
    }
    if (s.elapsed) parts.push(`已用 ${Math.round(s.elapsed)}s`);
    if (s.rate > 0 && total > done && s.state === 'hashing') {
      parts.push(`约剩 ${Math.max(1, Math.round((total - done) / s.rate))}s`);
    }
    if (s.reused) parts.push(`跳过 ${fmtInt(s.reused)}`);
    if (s.writes) parts.push(`落库 ${fmtInt(s.writes)} 次`);
    if (s.errors) parts.push(`失败 ${fmtInt(s.errors)}`);
    if (s.queued) parts.push(`排队 ${fmtInt(s.queued)} 个任务`);
    if (s.gpu && s.gpu.ok) parts.push(`GPU: ${s.gpu.name}`);
    $('#pRight').textContent = parts.join(' · ');
  }

  /* ---------------- 分组 ---------------- */
  async function loadGroups(append = false) {
    if (typeof otRefreshCount === "function") otRefreshCount();
    const offset = append ? state.groups.length : 0;
    const qs = new URLSearchParams({
      threshold: state.threshold, min: state.minSize, sort: state.sort,
      root: state.root, recall: state.recall, limit: 60, offset,
    });
    try {
      const busy = setTimeout(() => $('#main').classList.add('busy'), 700);
      let res;
      try {
        res = await api('/api/groups?' + qs);
      } finally {
        clearTimeout(busy);
        $('#main').classList.remove('busy');
      }
      const groups = res.groups || [];
      state.groups = append ? state.groups.concat(groups) : groups;
      state.stats = res.stats || {};
      state.page = res.page || { has_more: false, total_groups: 0 };
      state.engine = res.engine || state.engine;
      const prevIds = state.byId;
      state.byId = new Map();
      state.groups.forEach((g) => g.images.forEach((im) => state.byId.set(im.id, im)));
      if (!append) {
        // 只清理"本来加载过、现在没了"的（自动选择出来的 id 不在当前页，要保留）
        for (const id of [...state.selected]) {
          if (prevIds.has(id) && !state.byId.has(id)) {
            state.selected.delete(id);
            state.selSize.delete(id);
          }
        }
        for (const [k] of state.best) {
          const g = state.groups.find((x) => x.key === k);
          if (!g || !g.images.some((im) => im.id === state.best.get(k))) state.best.delete(k);
        }
      }
      state.renderLimit = state.groups.length;
      renderStats();
      renderRootOptions();
      renderGroups();
      updateFooter();
      updateEngineBadge();
    } catch (e) {
      toast('读取结果失败：' + esc(e.message), 'err');
    }
    if (!$('#archives').hidden) renderArchiveSummary();
  }

  async function loadApply(pct) {
    try {
      const r = await post('/api/settings', { load: Number(pct) });
      if (r && r.ok) {
        state.load = Number(pct);
        loadSync(pct);
        updateEngineBadge();
        toast(`占用已设为 ${pct}%，下次扫描生效`, 'ok', 2200);
      } else {
        toast('设置失败', 'err');
      }
    } catch (e) { toast('设置失败：' + (e.message || e), 'err'); }
  }

  function loadSync(pct) {
    const v = String(pct || 100);
    const a = $('#loadSel');
    if (a) a.value = v;
    const b = $('#stLoad');
    if (b) b.value = v;
  }

  function bindLoad() {
    const a = $('#loadSel');
    if (a) a.onchange = () => loadApply(a.value);
    const b = $('#stLoad');
    if (b) b.onchange = () => loadApply(b.value);
  }

  /* 后台补指纹进度：只在真的在补的时候露出来 */
  let patchTimer = null;
  async function patchWatch() {
    try {
      const r = await api('/api/patch/status');
      const bar = $('#patchBar');
      const txt = $('#patchText');
      if (!bar) return;
      if (r.running) {
        bar.hidden = false;
        txt.textContent = `后台补指纹 ${fmtInt(r.done || 0)}/${fmtInt(r.missing || 0)}`;
      } else {
        bar.hidden = true;
      }
    } catch (_) { /* 老版本没有这个接口 */ }
  }
  function patchStart() {
    if (patchTimer) return;
    patchWatch();
    patchTimer = setInterval(patchWatch, 3000);
  }

  function updateEngineBadge() {
    const el = $('#engineBadge');
    const eng = state.engine || {};
    const s = state.stats || {};
    const isGpu = eng.kind === 'gpu' || eng.kind === 'hybrid';
    el.className = 'engine-badge ' + (isGpu ? 'gpu' : 'cpu');
    let txt = eng.name || (isGpu ? 'GPU' : 'CPU');
    const load = Number(state.load || 100);
    if (load < 100) txt += ` · ${load}%`;
    if (s.mode === 'lsh') txt += ' · 分桶检索';
    el.title = [eng.reason, s.mode === 'lsh' ? '图片数超过 3 万，已启用分桶候选检索' : '',
                s.recall ? `召回策略：${s.recall}` : ''].filter(Boolean).join('\n');
    $('#engineText').textContent = txt;
  }

  /** 只刷新统计数字（删除后用，成本很低，不用重算分组） */
  async function loadStats() {
    try {
      const r = await api('/api/stats');
      state.stats = Object.assign({}, state.stats || {}, {
        images: r.images, groups: (state.stats || {}).groups,
        duplicates: (state.stats || {}).duplicates, reclaim: (state.stats || {}).reclaim,
      });
      renderStats();
    } catch (_) { /* 忽略 */ }
  }

  function renderStats() {
    const s = state.stats;
    $('#sImages').textContent = fmtInt(s.images);
    $('#sGroups').textContent = fmtInt(s.groups);
    $('#sDup').textContent = fmtInt(s.duplicates);
    $('#sFree').textContent = fmtBytes(s.reclaim);
  }

  function renderRootOptions() {
    const sel = $('#root');
    const cur = state.root;
    sel.innerHTML = '<option value="">全部已扫描</option>' + state.roots.map((r) =>
      `<option value="${esc(r.path)}">${esc(baseName(r.path))}（${fmtInt(r.images)}）</option>`).join('');
    if (cur && !state.roots.some((r) => r.path === cur)) state.root = '';
    sel.value = state.root;
  }

  function renderGroups() {
    const host = $('#groups');
    const empty = $('#empty');
    if (!state.groups.length) {
      host.innerHTML = '';
      $('#more').hidden = true;
      empty.hidden = false;
      if (state.stats.images > 0) {
        empty.querySelector('h2').textContent = '没有发现重复图片';
        empty.querySelector('p').innerHTML =
          `当前范围（${esc(state.root ? baseName(state.root) : '全部')}）共 <b>${fmtInt(state.stats.images)}</b> 张图片，
           在阈值 ${state.threshold} 下没有重复组。<br>把「相似度阈值」往右调一点，可以看到更宽松的结果。`;
        empty.querySelector('.tips').hidden = true;
        empty.querySelector('button').hidden = true;
      } else {
        empty.querySelector('h2').textContent = '先添加一个照片文件夹';
        empty.querySelector('p').innerHTML =
          '可以陆续添加多个文件夹，扫描结果会累积在一起对比。<br>' +
          '中途可以暂停，关掉程序下次继续，已分析过的图片不会重复计算。';
        empty.querySelector('.tips').hidden = false;
        empty.querySelector('button').hidden = false;
      }
      return;
    }
    empty.hidden = true;
    host.innerHTML = state.groups.map(groupHTML).join('');
    const more = state.page && state.page.has_more;
    $('#more').hidden = !more;
    const total = (state.page && state.page.total_groups) || state.groups.length;
    $('#btnMore').textContent = more
      ? `显示更多（已显示 ${state.groups.length} / ${fmtInt(total)} 组）`
      : '显示更多';
    $$('#groups img').forEach((img) => {
      if (img.complete && img.naturalWidth) img.classList.add('loaded');
      else img.onload = () => img.classList.add('loaded');
      img.onerror = () => { img.style.opacity = '.25'; img.alt = '缩略图加载失败'; };
    });
  }

  function groupHTML(g) {
    const keepId = state.best.get(g.key) || g.images[0].id;
    const ordered = [...g.images].sort((a, b) => (a.id === keepId ? -1 : b.id === keepId ? 1 : a.id - b.id));
    const selCount = ordered.filter((im) => state.selected.has(im.id)).length;
    return `
    <article class="group ${selCount ? 'has-sel' : ''}" data-key="${esc(g.key)}">
      <div class="ghead">
        <span class="gnum">组 ${g.no}</span>
        <span class="pill">${g.count} 张</span>
        <span class="pill soft">相似度 ${g.min_sim}–${g.max_sim}%</span>
        <span class="pill soft">可释放 ${fmtBytes(g.reclaim)}</span>
        ${g.exact ? `<span class="pill warn">完全相同 ×${g.exact}</span>` : ''}
        <span class="pill selpill" style="display:${selCount ? '' : 'none'}">已选 ${selCount}</span>
        <div class="gactions">
          <button data-act="keepbest" class="ok">
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M5 12l4.5 4.5L19 7"/></svg>
            保留最佳
          </button>
          <button data-act="invert">反选</button>
          <button data-act="clear">取消</button>
        </div>
      </div>
      <div class="thumbs">${ordered.map((im) => thumbHTML(g, im, keepId)).join('')}</div>
    </article>`;
  }

  function thumbHTML(g, im, keepId) {
    const sel = state.selected.has(im.id);
    const isBest = im.id === keepId;
    const trim = parseTrim(im.trim);
    const sim = isBest ? null : simPct(im.phash, (state.byId.get(keepId) || {}).phash);
    return `
    <figure class="thumb ${isBest ? 'best' : ''} ${sel ? 'sel' : ''}"
            data-id="${im.id}" data-key="${esc(g.key)}" title="${esc(im.path)}">
      <div class="imgbox">
        <img loading="lazy" src="/api/thumb?id=${im.id}&w=320" alt="${esc(im.name)}">
        <div class="tagcol">
          ${isBest ? '<span class="tag best">建议保留</span>' : ''}
          ${im.same_as_best ? '<span class="tag exact">完全相同</span>' : ''}
          ${trim ? '<span class="tag trim">含留边</span>' : ''}
        </div>
        ${sim != null ? `<span class="tag sim">相似 ${sim}%</span>` : ''}
        <button class="cmpbtn" data-act="cmp" title="对比查看">⤢</button>
        <span class="pick">✓</span>
      </div>
      <figcaption>
        <div class="fname">${esc(im.name)}</div>
        <div class="fmeta">${im.w}×${im.h} · ${fmtBytes(im.size)} · 评分 <b>${im.score}</b></div>
        <div class="fpath" title="${esc(im.path)}">${esc(shortFolder(im))}</div>
      </figcaption>
    </figure>`;
  }

  /** 缩略图下面那行路径：显示"根文件夹名\子目录" */
  function shortFolder(im) {
    const root = im.root || '';
    let rel = String(im.path || '');
    if (root && rel.toLowerCase().startsWith(root.toLowerCase())) {
      rel = rel.slice(root.length).replace(/^[\\/]+/, '');
    }
    const hasSep = rel.includes('\\') || rel.includes('/');
    const dir = hasSep ? rel.replace(/[\\/][^\\/]*$/, '') : '';
    const head = baseName(root) || '';
    if (!dir) return head || '（根目录）';
    return head ? `${head}\\${dir}` : dir;
  }

  function parseTrim(t) {
    if (!t) return false;
    return String(t).split(',').some((v) => Number(v) > 0.02);
  }

  function onGroupClick(e) {
    const act = e.target.closest('[data-act]');
    const fig = e.target.closest('.thumb');
    const card = e.target.closest('.group');
    if (!card) return;
    const key = card.dataset.key;

    if (act && act.dataset.act === 'cmp' && fig) {
      e.stopPropagation();
      openCompare(key, Number(fig.dataset.id));
      return;
    }
    if (act && act.dataset.act && !fig) {
      const g = state.groups.find((x) => x.key === key);
      if (!g) return;
      if (act.dataset.act === 'keepbest') keepBest(g);
      if (act.dataset.act === 'clear') clearGroup(g);
      if (act.dataset.act === 'invert') invertGroup(g);
      return;
    }
    if (fig) toggleSelect(Number(fig.dataset.id));
  }

  /* ---------------- 自动选择 ---------------- */
  let autoResult = null;

  function openAuto() {
    if (!state.stats.images) { toast('还没有扫描结果'); return; }
    renderAutoFolders();
    $('#auPreview').textContent = '选好规则后点「计算」，看看会选中多少张。';
    $('#auApply').disabled = true;
    autoResult = null;
    $('#auto').hidden = false;
  }

  function renderAutoFolders() {
    const host = $('#auFolders');
    const list = [];
    state.roots.forEach((r) => list.push(r.path));
    (state.autoFolders || []).forEach((p) => { if (!list.includes(p)) list.push(p); });
    if (!list.length) {
      host.innerHTML = '<div class="au-empty">还没有扫描文件夹，可以先用「添加文件夹…」选一个</div>';
      return;
    }
    host.innerHTML = list.map((p) => `
      <label><input type="checkbox" value="${esc(p)}">
        <span title="${esc(p)}">${esc(p)}</span></label>`).join('');
  }

  function toggleAutoFolder() {
    const keep = $('#auKeep').value;
    $('#auFolderWrap').hidden = keep !== 'folders';
  }

  async function calcAuto() {
    const scope = $('input[name=auscope]:checked')?.value || 'groups';
    const keep = $('#auKeep').value;
    const folders = $$('#auFolders input:checked').map((i) => i.value);
    if (keep === 'folders' && !folders.length) {
      toast('请至少勾选一个文件夹', 'err');
      return;
    }
    const folder_mode = $('input[name=aufmode]:checked')?.value || 'drop';
    $('#auCalc').disabled = true;
    $('#auPreview').textContent = '正在计算…';
    try {
      const r = await post('/api/autoselect', {
        scope, keep, folder_mode, folders,
        threshold: state.threshold, min: state.minSize, root: state.root, recall: state.recall,
      });
      if (!r.ok) {
        $('#auPreview').textContent = '计算失败：' + (r.error || '');
        return;
      }
      autoResult = r;
      $('#auPreview').innerHTML = `将选中 <b>${fmtInt(r.count)}</b> 张，约 <b>${fmtBytes(r.bytes)}</b>` +
        (r.groups ? `（涉及 ${fmtInt(r.groups)} 组）` : '') +
        `<br><span class="muted">保留规则：${esc(r.keep_label || '')}</span>`;
      $('#auApply').disabled = !r.count;
    } catch (e) {
      $('#auPreview').textContent = '计算失败：' + e.message;
    } finally {
      $('#auCalc').disabled = false;
    }
  }

  function applyAuto() {
    if (!autoResult || !autoResult.ids.length) return;
    autoResult.ids.forEach((id, i) => {
      state.selected.add(id);
      state.selSize.set(id, Number((autoResult.sizes || [])[i] || 0));
    });
    paintSelection();
    $('#auto').hidden = true;
    toast(`已勾选 ${fmtInt(autoResult.count)} 张，共 ${fmtBytes(autoResult.bytes)}`, 'ok', 4000);
  }

  /* ---------------- 选择 ---------------- */
  function toggleSelect(id) {
    if (state.selected.has(id)) {
      state.selected.delete(id);
      state.selSize.delete(id);
    } else {
      state.selected.add(id);
      const im = state.byId.get(id);
      state.selSize.set(id, Number(im ? im.size : 0) || 0);
    }
    const fig = $(`.thumb[data-id="${id}"]`);
    if (fig) {
      fig.classList.toggle('sel', state.selected.has(id));
      refreshGroupSel(fig.closest('.group'));
    }
    updateFooter();
  }

  function setSelected(id, on) {
    if (on) {
      if (!state.selected.has(id)) {
        state.selected.add(id);
        const im = state.byId.get(id);
        state.selSize.set(id, Number(im ? im.size : 0) || 0);
      }
    } else {
      state.selected.delete(id);
      state.selSize.delete(id);
    }
  }

  function refreshGroupSel(card) {
    if (!card) return;
    const figs = $$('.thumb', card);
    const n = figs.filter((f) => f.classList.contains('sel')).length;
    card.classList.toggle('has-sel', n > 0);
    const pill = $('.selpill', card);
    if (pill) {
      pill.style.display = n ? '' : 'none';
      pill.textContent = `已选 ${n}`;
    }
  }

  function keepBest(g) {
    const keepId = state.best.get(g.key) || g.images[0].id;
    g.images.forEach((im) => setSelected(im.id, im.id !== keepId));
    paintSelection();
  }

  function clearGroup(g) {
    g.images.forEach((im) => state.selected.delete(im.id));
    paintSelection();
  }

  function invertGroup(g) {
    g.images.forEach((im) => {
      if (state.selected.has(im.id)) state.selected.delete(im.id);
      else state.selected.add(im.id);
    });
    paintSelection();
  }

  function smartSelect() {
    openAuto();
  }

  function clearSelection() {
    state.selected.clear();
    state.selSize.clear();
    paintSelection();
  }

  function paintSelection() {
    $$('#groups .thumb').forEach((f) => f.classList.toggle('sel', state.selected.has(Number(f.dataset.id))));
    $$('#groups .group').forEach(refreshGroupSel);
    updateFooter();
  }

  function selBytes() {
    let n = 0;
    state.selected.forEach((id) => {
      const v = state.selSize.get(id);
      n += Number(v != null ? v : (state.byId.get(id)?.size || 0));
    });
    return n;
  }

  function updateFooter() {
    const n = state.selected.size;
    $('#selCount').textContent = fmtInt(n);
    $('#selSize').textContent = fmtBytes(selBytes());
    $('#actionbar').classList.toggle('has', n > 0);
    $('#btnTrash').disabled = n === 0;
    $('#btnMove').disabled = n === 0;
    $('#btnClearSel').disabled = n === 0;
  }

  /* ---------------- 对比 ---------------- */
  function openCompare(key, id) {
    const g = state.groups.find((x) => x.key === key);
    if (!g) return;
    const keepId = state.best.get(key) || g.images[0].id;
    const cands = g.images.filter((im) => im.id !== keepId);
    let idx = cands.findIndex((im) => im.id === id);
    if (idx < 0) idx = 0;
    state.cmp = { g, keepId, cands, idx };
    if (!cands.length) return;
    $('#compare').hidden = false;
    renderCompare();
  }

  function renderCompare() {
    const c = state.cmp;
    if (!c) return;
    const keep = state.byId.get(c.keepId) || c.g.images.find((i) => i.id === c.keepId);
    const cand = c.cands[c.idx];
    $('#cmpTitle').textContent = `组 ${c.g.no} · 对比`;
    $('#cmpSub').textContent = `${c.idx + 1} / ${c.cands.length} · ${c.cands.length + 1} 张候选`;

    const maxPx = 1600;
    $('#imgA').src = `/api/preview?id=${keep.id}&max=${maxPx}`;
    $('#imgB').src = `/api/preview?id=${cand.id}&max=${maxPx}`;
    $('#tagB').textContent = state.selected.has(cand.id) ? '已标记删除' : '对比';
    $('#tagB').style.background = state.selected.has(cand.id) ? 'rgba(248,81,73,.85)' : '';

    $('#cmpPos').textContent = `${c.idx + 1} / ${c.cands.length}`;
    $('#cmpInfo').innerHTML = cmpInfoHTML(keep, cand);
    $('#cmpMark').textContent = state.selected.has(cand.id) ? '取消标记' : '标记删除';
    resetView();
  }

  function cmpInfoHTML(a, b) {
    const rows = [];
    const row = (k, va, vb, ca = '', cb = '') =>
      rows.push(`<span class="k">${k}</span><span class="v ${ca}">${va}</span><span class="v ${cb}">${vb}</span>`);

    const aRes = a.w * a.h, bRes = b.w * b.h;
    row('文件名', esc(a.name), esc(b.name));
    rows.push(`<span class="k">路径</span><span class="v" title="${esc(a.path)}">${esc(shortFolder(a))}</span>` +
      `<span class="v" title="${esc(b.path)}">${esc(shortFolder(b))}</span>`);
    row('分辨率', `${a.w}×${a.h}`, `${b.w}×${b.h}`, aRes >= bRes ? 'win' : 'lose', bRes >= aRes ? 'win' : 'lose');
    row('大小', fmtBytes(a.size), fmtBytes(b.size), a.size >= b.size ? 'win' : 'lose', b.size >= a.size ? 'win' : 'lose');
    row('清晰度', Math.round(a.sharp), Math.round(b.sharp), a.sharp >= b.sharp ? 'win' : 'lose', b.sharp >= a.sharp ? 'win' : 'lose');
    row('画质评分', a.score, b.score, a.score >= b.score ? 'win' : 'lose', b.score >= a.score ? 'win' : 'lose');
    const s = simPct(a.phash, b.phash);
    row('像素相似度', s == null ? '—' : s + '%', s == null ? '—' : s + '%');
    row('文件内容', a.sha256 && a.sha256 === b.sha256 ? '完全相同' : '不同',
        a.sha256 && a.sha256 === b.sha256 ? '完全相同' : '不同');
    row('格式', a.fmt || '—', b.fmt || '—');
    return rows.join('');
  }

  function stepCompare(d) {
    const c = state.cmp;
    if (!c) return;
    c.idx = (c.idx + d + c.cands.length) % c.cands.length;
    renderCompare();
  }

  function closeCompare() {
    $('#compare').hidden = true;
    state.cmp = null;
    $('#imgA').src = '';
    $('#imgB').src = '';
  }

  function cmpMarkDelete() {
    const c = state.cmp;
    if (!c) return;
    const id = c.cands[c.idx].id;
    if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
    paintSelection();
    renderCompare();
  }

  function cmpSetKeep() {
    const c = state.cmp;
    if (!c) return;
    const id = c.cands[c.idx].id;
    state.best.set(c.g.key, id);
    c.g.images.forEach((im) => setSelected(im.id, im.id !== id));
    paintSelection();
    renderGroups();
    closeCompare();
    toast('已把这张设为该组保留，其余已勾选', 'ok');
  }

  /* --------- 舞台：缩放 / 平移 / 分割线 --------- */
  function bindStage() {
    const stage = $('#stage');
    let dragging = false, mode = null, sx = 0, sy = 0, vx = 0, vy = 0;

    stage.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      const rect = stage.getBoundingClientRect();
      const nearHandle = Math.abs(e.clientX - rect.left - (state.split / 100) * rect.width) < 22;
      if (stage.dataset.mode === 'overlay' && (nearHandle || state.view.z <= 1.01)) mode = 'split';
      else if (state.view.z > 1.01) mode = 'pan';
      else mode = null;
      if (!mode) return;
      dragging = true;
      sx = e.clientX; sy = e.clientY; vx = state.view.x; vy = state.view.y;
      stage.setPointerCapture(e.pointerId);
      stage.classList.add('dragging');
      if (mode === 'split') updateSplit(e.clientX);
    });

    stage.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      if (mode === 'split') updateSplit(e.clientX);
      else {
        state.view.x = vx + (e.clientX - sx);
        state.view.y = vy + (e.clientY - sy);
        applyView();
      }
    });

    const end = (e) => {
      if (!dragging) return;
      dragging = false;
      stage.classList.remove('dragging');
      try { stage.releasePointerCapture(e.pointerId); } catch (_) {}
    };
    stage.addEventListener('pointerup', end);
    stage.addEventListener('pointercancel', end);

    stage.addEventListener('wheel', (e) => {
      if (stage.dataset.mode === 'side') return;
      e.preventDefault();
      zoomBy(e.deltaY < 0 ? 1.12 : 1 / 1.12);
    }, { passive: false });

    stage.addEventListener('dblclick', resetView);
  }

  function updateSplit(clientX) {
    const rect = $('#stage').getBoundingClientRect();
    let pct = ((clientX - rect.left) / rect.width) * 100;
    pct = Math.max(2, Math.min(98, pct));
    state.split = pct;
    const st = $('#stage');
    st.style.setProperty('--split', pct + '%');
    st.style.setProperty('--right', (100 - pct) + '%');
  }

  function zoomBy(f) {
    const z = Math.max(1, Math.min(8, state.view.z * f));
    if (z === 1) { state.view.x = state.view.y = 0; }
    state.view.z = z;
    applyView();
  }

  function resetView() {
    state.view = { z: 1, x: 0, y: 0 };
    applyView();
  }

  function applyView() {
    const t = `translate(${state.view.x}px, ${state.view.y}px) scale(${state.view.z})`;
    $('#imgA').style.transform = t;
    $('#imgB').style.transform = t;
    $('#zoomReset').textContent = Math.round(state.view.z * 100) + '%';
  }

  /* ---------------- 删除 / 移动 ---------------- */
  function askRemove(mode) {
    if (!state.selected.size) return;
    const ids = [...state.selected];
    const bytes = selBytes();
    const names = ids.slice(0, 8).map((id) => state.byId.get(id)?.name || '').filter(Boolean);
    $('#cfText').innerHTML =
      `将处理 <b>${fmtInt(ids.length)}</b> 个文件，共 <b>${fmtBytes(bytes)}</b>。` +
      (mode === 'move' ? '文件会被移动到指定文件夹，不会丢失。' : '删除后可在系统回收站还原。');
    $('#cfList').textContent = names.join('\n') + (ids.length > 8 ? `\n… 还有 ${ids.length - 8} 个` : '');
    state.pendingRemove = ids;

    const destEl = $('#cfDest');
    if (!destEl.value) {
      const first = state.byId.get(ids[0]);
      const base = (first && first.root) || state.roots[0]?.path || '';
      destEl.value = defaultDupDir(base);
    }
    const radio = $(`input[name=cfmode][value="${mode}"]`);
    if (radio) radio.checked = true;
    syncConfirmMode();
    $('#confirm').hidden = false;
  }

  function syncConfirmMode() {
    const v = $('input[name=cfmode]:checked')?.value || 'trash';
    $('#cfDestWrap').hidden = v !== 'move';
    $('#cfOk').textContent = v === 'move' ? '确认移动' : '确认删除';
    $('#cfOk').className = v === 'move' ? 'primary' : 'danger solid';
  }

  /** 从当前列表里就地摘掉这些 id（不重新请求分组，几十万张也不会卡） */
  function dropIdsFromGroups(ids) {
    const gone = new Set(ids.map(Number));
    let droppedGroups = 0;
    const kept = [];
    for (const g of state.groups) {
      const before = g.images.length;
      g.images = g.images.filter((im) => !gone.has(Number(im.id)));
      if (g.images.length === before) { kept.push(g); continue; }
      if (g.images.length < Math.max(2, state.minSize)) { droppedGroups++; continue; }
      g.count = g.images.length;
      if (!g.images.some((im) => im.id === (state.best.get(g.key) || 0))) {
        state.best.set(g.key, g.images[0].id);
      }
      kept.push(g);
    }
    state.groups = kept;
    return droppedGroups;
  }

  function applyLocalRemoval(ids, removed, freed) {
    const dropped = dropIdsFromGroups(ids);
    state.selected = new Set([...state.selected].filter((id) => !ids.map(Number).includes(Number(id))));
    state.stats = state.stats || {};
    state.stats.images = Math.max(0, Number(state.stats.images || 0) - (removed || 0));
    state.stats.duplicates = Math.max(0, Number(state.stats.duplicates || 0) - (removed || 0));
    state.stats.reclaim = Math.max(0, Number(state.stats.reclaim || 0) - (freed || 0));
    state.stats.groups = Math.max(0, Number(state.stats.groups || 0) - dropped);
    renderStats();
    renderGroups();
    if (typeof paintSelection === "function") paintSelection();   // 底部"已选"跟着归零
  }

  async function doRemove() {
    const ids = state.pendingRemove;
    if (!ids || !ids.length) return;
    const mode = $('input[name=cfmode]:checked')?.value || 'trash';
    const dest = $('#cfDest').value.trim();
    if (mode === 'move' && !dest) { toast('请填写目标文件夹', 'err'); return; }
    const btn = $('#cfOk');
    const total = ids.length;
    const CHUNK = 40;                       // 一批 40 个：每批回来就更新列表，长列表也不用干等
    btn.disabled = true;
    $('#confirm').hidden = true;
    const futs = [];
    let done = 0, removed = 0, freed = 0;
    const failed = [];
    try {
      for (let i = 0; i < total; i += CHUNK) {
        const part = ids.slice(i, i + CHUNK);
        btn.textContent = `处理中… ${done}/${total}`;
        const r = await post('/api/remove', { ids: part, mode, dest });
        if (!r.ok) { failed.push(...part.map((id) => ({ id, error: r.error }))); }
        else {
          removed += r.removed || 0;
          freed += r.freed || 0;
          if (r.failed && r.failed.length) failed.push(...r.failed);
          // 成功删除的那些立刻从界面上消失
          const goneIds = part.filter((id) => !(r.failed || []).some((f) => String(f.id) === String(id)));
          applyLocalRemoval(goneIds, goneIds.length, (r.freed || 0) * (goneIds.length / Math.max(1, part.length)));
        }
        done += part.length;
        futs.push(part);
        await new Promise((res) => setTimeout(res, 0));   // 让浏览器有机会重绘
      }
      state.pendingRemove = null;
      if (removed) setDirty(true);
      let msg = `${mode === 'move' ? '已移动' : '已删除'} ${fmtInt(removed)} 个文件，释放 ${fmtBytes(freed)}`;
      if (failed.length) msg += `；${failed.length} 个失败`;
      toast(msg, failed.length ? 'err' : 'ok', 4200);
      await loadRoots();
      loadStats();                                       // 后台校准数字，不阻塞界面
    } catch (e) {
      toast('操作失败：' + esc(e.message), 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = '开始处理';
      syncConfirmMode();
    }
  }

  const dirOf = (p) => String(p).replace(/[\\/][^\\/]*$/, '');

  /** 默认存放重复图的目录：放在扫描目录的"隔壁"，免得下次扫描又扫回来 */
  function defaultDupDir(base) {
    let p = String(base || '').replace(/[\\/]+$/, '');
    if (!p) return '';
    if (/^[A-Za-z]:$/.test(p)) return p + '\\_重复图片';
    const parts = p.split(/[\\/]/);
    const name = parts.pop() || '照片';
    const parent = parts.join('\\');
    if (!parent) return name + '_重复图片';
    return parent + '\\' + name + '_重复图片';
  }

  function exportCsv() {
    const qs = new URLSearchParams({ threshold: state.threshold, min: state.minSize, root: state.root });
    window.location.href = '/api/export?' + qs;
    toast('正在导出 CSV 清单…');
  }

  /* ---------------- 对比更新文件夹 ---------------- */
  let diffPath = '';

  async function openDiff(path) {
    diffPath = path;
    $('#dfRoot').textContent = path;
    $('#dfBody').textContent = '正在统计…';
    $('#dfGo').disabled = true;
    $('#dfGo').textContent = '只更新这个文件夹';
    $('#diff').hidden = false;
    try {
      const r = await post('/api/diff', { path });
      if (!r.ok) { $('#dfBody').textContent = r.error || '统计失败'; return; }
      if (!r.known) {
        $('#dfBody').innerHTML =
          `这个文件夹还没有扫描过（共 <b>${fmtInt(r.total)}</b> 张），点下面按钮开始第一次扫描。`;
        $('#dfGo').textContent = '扫描这个文件夹';
        $('#dfGo').disabled = false;
        return;
      }
      if (!r.need_update) {
        $('#dfBody').innerHTML = `<b>没有变化</b>：${fmtInt(r.unchanged)} 张和上次扫描时一模一样。`;
        return;
      }
      const s = r.samples || {};
      $('#dfBody').innerHTML = `
        <div class="df-row">
          <div><div class="df-num new">+${fmtInt(r.new)}</div><div class="df-label">新增</div></div>
          <div><div class="df-num changed">~${fmtInt(r.changed)}</div><div class="df-label">有变化</div></div>
          <div><div class="df-num removed">−${fmtInt(r.removed)}</div><div class="df-label">已删除</div></div>
          <div><div class="df-num same">${fmtInt(r.unchanged)}</div><div class="df-label">未变化</div></div>
        </div>
        ${(s.new || []).length ? `<div class="df-samples">新增：${esc(s.new.join('、'))}</div>` : ''}
        ${(s.changed || []).length ? `<div class="df-samples">变化：${esc(s.changed.join('、'))}</div>` : ''}
        ${(s.removed || []).length ? `<div class="df-samples">删除：${esc(s.removed.join('、'))}</div>` : ''}
        <div class="st-hint">只影响这个文件夹：新增/变化的重新分析，已删除的从索引移除，其它文件夹不动。</div>`;
      $('#dfGo').disabled = false;
    } catch (e) {
      $('#dfBody').textContent = '统计失败：' + e.message;
    }
  }

  async function doDiffUpdate() {
    $('#diff').hidden = true;
    try {
      const r = await post('/api/scan', { paths: [diffPath] });
      if (!r.ok) { toast(esc(r.error), 'err'); return; }
      if (r.queued) toast(esc(r.message || '已排队等待'), '', 4500);
      state.scan = { running: true, state: 'starting', total: 0, done: 0, message: '准备中…' };
      updateScanButtons();
      $('#progress').hidden = false;
      renderProgress(state.scan);
      pollStatus();
    } catch (e) {
      toast('更新失败：' + esc(e.message), 'err');
    }
  }

  async function pollVerify() {
    try {
      const s = await api('/api/verify/status');
      if (s.running) {
        setTimeout(pollVerify, 800);
        return;
      }
      if (s.removed) {
        toast(`校验完成：清理了 ${fmtInt(s.removed)} 条失效记录`, 'ok', 4500);
        setDirty(true);
        await loadRoots();
        await loadGroups();
      } else {
        toast('校验完成：没有失效文件', 'ok');
      }
    } catch (_) { /* 忽略 */ }
  }

  /* ---------------- 设置：存档位置 / 保存频率 ---------------- */
  const FLUSH_PRESETS = {
    safe: [100, 1],
    balanced: [500, 4],
    disk: [2000, 15],
    max: [10000, 60],
  };
  let settingsCache = null;

  async function openSettings() {
    try {
      const r = await api('/api/settings');
      settingsCache = r;
      const s = r.settings || {};
      const mode = s.archive_mode || 'app';
    if ($('#stPath')) $('#stPath').value = (settingsCache && settingsCache.settings.archive_path) || '';
      $('#stEngine').value = s.engine || 'auto';
      $('#stWorkers').value = s.workers || 0;
      if (typeof loadSync === 'function') loadSync(r.load || s.load || 100);
      renderBench(s.bench || {});
      let best = 'balanced';
      let bestD = 1e18;
      Object.entries(FLUSH_PRESETS).forEach(([k, [rows, secs]]) => {
        const d = Math.abs(Math.log((Number(s.flush_rows) || 500) / rows))
          + Math.abs((Number(s.flush_secs) || 4) - secs) / 20;
        if (d < bestD) { bestD = d; best = k; }
      });
      $('#stFlush').value = best;
      if (typeof syncArchiveMode === 'function') syncArchiveMode();
      $('#settings').hidden = false;
    } catch (e) {
      toast('读取设置失败：' + esc(e.message), 'err');
    }
  }

  function closeSettings() {
    $('#settings').hidden = true;
  }

  function syncArchiveMode() {
    const row = $('#stPathRow');
    if (row) row.hidden = true;
    const res = $('#stResolved');
    if (res) res.textContent = settingsCache ? settingsCache.archive_dir : '';
  }

  function previewArchiveDir(mode) {
    const r = settingsCache || {};
    if (mode === 'custom') {
    return '';
    }
    if (mode === 'images') {
      const roots = r.roots || [];
      if (!roots.length) return '（还没有添加文件夹，会暂时存在软件目录）';
      return roots[0].replace(/[\\/]+$/, '') + '\\_查重存档';
    }
    return (r.data_dir || 'data') + '\\archives';
  }

  function renderBench(b) {
    const el = $('#stBench');
    if (!b || !b.at) {
      el.innerHTML = '还没有测速记录。运行「重新测速.bat」可以测出这台电脑最适合的配置并写进这里。';
      return;
    }
    const cpuBest = Math.max(...Object.values(b.cpu || { 0: 0 }).map(Number));
    const cpuW = Object.entries(b.cpu || {}).sort((a, c) => Number(c[1]) - Number(a[1]))[0];
    const gpu = Number(b.gpu || 0);
    const parts = [`上次测速 ${esc(b.at)}（${fmtInt(b.samples)} 张样本）`];
    if (gpu > 0) parts.push(`GPU ${gpu.toFixed(0)} 张/秒`);
    parts.push(`CPU ${cpuBest.toFixed(0)} 张/秒${cpuW ? `（${cpuW[0]} 线程）` : ''}`);
    parts.push(`推荐 <b>${b.recommended === 'gpu' ? 'GPU' : 'CPU'}</b>`);
    el.innerHTML = parts.join(' · ');
  }

  async function saveSettings() {
    const mode = 'app';
    const preset = FLUSH_PRESETS[$('#stFlush').value] || FLUSH_PRESETS.balanced;
    const body = {
      archive_mode: mode,
      flush_rows: preset[0],
      flush_secs: preset[1],
      engine: $('#stEngine').value || 'auto',
      workers: Math.max(0, Math.min(128, parseInt($('#stWorkers').value, 10) || 0)),
      load: Number(($('#stLoad') || {}).value || 100),
    };
    if (mode === 'custom') {
        if (!body.archive_path) { toast('请填写存档文件夹', 'err'); return; }
    }
    try {
      const r = await post('/api/settings', body);
      if (!r.ok) { toast(esc(r.error || '保存失败'), 'err'); return; }
      settingsCache = r;
      $('#arDir').textContent = r.archive_dir;
      closeSettings();
      toast(`设置已保存 · 存档位置：${esc(r.archive_dir)}`, 'ok', 4000);
      if (!$('#archives').hidden) await loadArchives();
    } catch (e) {
      toast('保存设置失败：' + esc(e.message), 'err');
    }
  }

  /* ---------------- 存档 ---------------- */
  function setDirty(v) {
    state.dirty = !!v;
    $('#btnArchives').classList.toggle('dirty', state.dirty);
  }

  /** "3 天前" 这种相对时间 */
  function fmtAgo(ts) {
    const t = Number(ts) || 0;
    if (!t) return '';
    const d = (Date.now() / 1000 - t);
    if (d < 60) return '刚刚';
    if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
    if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
    if (d < 86400 * 30) return `${Math.floor(d / 86400)} 天前`;
    return '';
  }

  /** 存档 JSON 大约多大（实测每张约 550 字节：路径 + 哈希 + 元数据） */
  function estArchiveBytes(n) { return Number(n || 0) * 560; }

  function suggestArchiveName() {
    if (!$('#arName').value || !state.arNameTouched) {
      const head = state.roots.length ? baseName(state.roots[0].path) : '扫描结果';
      const d = new Date();
      const p = (v) => String(v).padStart(2, '0');
      $('#arName').value = `${head} ${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    }
  }

  function renderArchiveSummary() {
    const n = Number(state.stats.images || 0);
    const ms0 = state.mediaStats || {};
    const scope = state.arScope || 'image';
    const isMedia = scope !== 'image';
    const KIND_NAME = { image: "图片", video: "视频", audio: "音频", novel: "小说" };
    const cnt = isMedia ? Number(ms0[scope] || 0) : n;

    if (!cnt) {
      $('#arNow').textContent = isMedia
        ? `还没有扫描到${KIND_NAME[scope]}`
        : '还没有扫描结果';
      $('#arEst').textContent = `先到「${KIND_NAME[scope]}」页扫描一个文件夹，再来保存`;
      $('#arSave').disabled = true;
      return;
    }
    $('#arSave').disabled = false;

    // 每种类型的存档体积差很多：视频每段 8 帧特征最占地方
    const PER_ITEM = { video: 36 * 1024, audio: 22 * 1024, novel: 12 * 1024 };
    let est;
    if (isMedia) {
      est = cnt * (PER_ITEM[scope] || 20000);
      $('#arNow').innerHTML = `当前${KIND_NAME[scope]}：<b>${fmtInt(cnt)}</b> 个`;
    } else {
      const groups = state.stats.groups || 0;
      est = estArchiveBytes(n);
      $('#arNow').innerHTML = `当前结果：<b>${fmtInt(n)}</b> 张图片 · ${fmtInt(groups)} 组`;
    }
    if (state.dirty) {
      $('#arNow').innerHTML += ' <span class="ar-badge">未保存</span>';
    }

    const cur = state.archives.find((a) => a.id === state.loadedArchive);
    const tail = cur ? `已载入「${esc(cur.name)}」` : '还没有载入任何存档';
    $('#arEst').textContent = `预计存档 ≈ ${fmtBytes(est)} · ${tail}`;
  }

  async function openArchives() {
    $('#archives').hidden = false;
    try {
      const ms = await api('/api/media/status');
      state.mediaStats = { video: ms.stats.video.items, audio: ms.stats.audio.items,
                           novel: ms.stats.novel.items };
    } catch (_) { state.mediaStats = {}; }
    state.arScope = M.tab || 'image';
    $$('.ar-tab').forEach((b) => b.classList.toggle('on', b.dataset.scope === state.arScope));
    suggestArchiveName();
    $('#arHint').hidden = true;
    renderArchiveSummary();
    try {
      const s = await api('/api/settings');
      settingsCache = s;
      $('#arDir').textContent = s.archive_dir;
    } catch (_) { /* 忽略 */ }
    await loadArchives();
  }

  async function loadArchives() {
    try {
      const r = await api('/api/archives?scope=' + encodeURIComponent(state.arScope));
      state.archives = r.archives || [];
      renderArchives();
    } catch (e) {
      toast('读取存档失败：' + esc(e.message), 'err');
    }
  }

  function renderArchives() {
    const host = $('#arList');
    renderArchiveSummary();
    const tip = $('#arCountTip');
    if (tip) tip.textContent = state.archives.length ? `共 ${state.archives.length} 个` : '';
    if (!state.archives.length) {
      host.innerHTML = `<div class="ar-empty">
        <div class="ar-empty-title">还没有存档</div>
        <div class="muted">扫描出结果后，点第 1 步的「保存当前结果」，这里就会出现一条记录，
        之后可以随时载入、合并或覆盖。</div></div>`;
      return;
    }
    host.innerHTML = state.archives.map((a) => {
      const cur = a.id === state.loadedArchive;
      const ago = fmtAgo(a.created);
      const md = a.media || {};
      const mediaTxt = [md.video ? `${md.video} 视频` : "", md.audio ? `${md.audio} 音频` : "",
                        md.novel ? `${md.novel} 小说` : ""].filter(Boolean).join(" · ");
      const facts = [
        a.images ? `${fmtInt(a.images)} 张图片` : "",
        mediaTxt,
        `${fmtInt(a.roots)} 个文件夹`,
        fmtBytes(a.size),
      ].filter(Boolean).join('<span class="sep">·</span>');
      const when = ago ? `${fmtTime(a.created)}（${ago}）` : fmtTime(a.created);
      return `<div class="ar-item ${cur ? 'current' : ''} ${a.id === state.flashId ? 'flash' : ''}" data-id="${esc(a.id)}">
        <div class="ar-info">
          <div class="ar-name" title="${esc(a.name)}">${esc(a.name)}${cur ? '<span class="ar-badge">已载入</span>' : ''}</div>
          <div class="ar-facts">${facts}</div>
          <div class="ar-when">保存于 ${when}<span class="ar-file">${esc(a.id)}.json</span></div>
        </div>
        <div class="ar-acts">
          <button data-act="load" class="${cur ? '' : 'primary'}"
            ${cur ? 'disabled title="这就是当前载入的存档"' : 'title="用这个存档替换当前索引（照片文件不动）"'}>${cur ? '已载入' : '载入'}</button>
          <button data-act="merge" title="把这个存档的记录并进当前结果，重复的自动跳过">合并</button>
          <button data-act="update" title="用当前结果重写这个存档（旧内容会被替换）">覆盖</button>
          <button data-act="export" title="把存档另存为 .json，可以拷到别的电脑用">导出</button>
          <button data-act="del" class="danger" title="删除这个存档文件">删除</button>
        </div>
      </div>`;
    }).join("");
  }

  function defaultArchiveName() {
    const head = state.roots.length ? baseName(state.roots[0].path) : '扫描结果';
    const d = new Date();
    const p = (v) => String(v).padStart(2, '0');
    return `${head} ${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  /** 重名时自动加序号：名字（2）、名字（3）…… */
  function uniqueArchiveName(base) {
    const used = new Set(state.archives.map((a) => a.name));
    if (!used.has(base)) return base;
    for (let i = 2; i < 200; i++) {
      const cand = `${base}（${i}）`;
      if (!used.has(cand)) return cand;
    }
    return `${base} ${Date.now()}`;
  }

  async function saveArchive(opts = {}) {
    if (state.savingArchive) return false;
    const _ms = state.mediaStats || {};
    const _mediaTotal = (_ms.video || 0) + (_ms.audio || 0) + (_ms.novel || 0);
    const _okToSave = state.arScope === 'media' ? _mediaTotal > 0 : Number(state.stats.images || 0) > 0;
    if (!_okToSave) {
      if (!opts.silent) toast('当前没有扫描结果，先扫描再保存', 'err');
      return false;
    }
    const name = uniqueArchiveName($('#arName').value.trim() || defaultArchiveName());
    const btn = $('#arSave');
    const label = btn.textContent;
    state.savingArchive = true;
    btn.disabled = true;
    btn.textContent = '保存中…';
    const t0 = performance.now();
    const ticker = setInterval(() => {
      btn.textContent = `保存中… ${((performance.now() - t0) / 1000).toFixed(1)}s`;
    }, 200);
    try {
      const r = await post('/api/archives', {
        name, scope: state.arScope,
        settings: { threshold: state.threshold, min: state.minSize, sort: state.sort },
      });
      if (!r.ok) {
        toast(esc(r.error || '保存失败'), 'err');
        return false;
      }
      setDirty(false);
      state.loadedArchive = r.archive.id;
      state.flashId = r.archive.id;
      state.arNameTouched = false;
      await loadArchives();
      const hint = $('#arHint');
      hint.hidden = false;
      hint.className = 'ar-hint ok';
      hint.textContent = `已保存到 ${r.archive.id}.json · ${fmtBytes(r.archive.size)} · ${fmtTime(r.archive.created)}`;
      if (!opts.silent) {
        toast(`已保存「${esc(r.archive.name)}」· ${fmtInt(r.archive.images)} 张 · ${fmtBytes(r.archive.size)}`, 'ok', 4500);
      }
      return true;
    } catch (e) {
      toast('保存失败：' + esc(e.message), 'err');
      return false;
    } finally {
      clearInterval(ticker);
      state.savingArchive = false;
      btn.disabled = false;
      btn.textContent = label;
      setTimeout(() => { state.flashId = ''; }, 1800);
    }
  }

  async function onArchiveClick(e) {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const item = btn.closest('.ar-item');
    const id = item.dataset.id;
    const meta = state.archives.find((a) => a.id === id) || {};

    if (btn.dataset.act === 'export') {
      window.location.href = '/api/archives/export?scope=' + encodeURIComponent(state.arScope) +
        '&id=' + encodeURIComponent(id);
      return;
    }
    if (btn.dataset.act === 'del') {
      const yes = await askConfirm('删除存档', `删除存档「${esc(meta.name || '')}」？<br>只删存档文件，不影响照片。`, '删除', true);
      if (!yes) return;
      await post('/api/archives/delete', { id, scope: state.arScope });
      await loadArchives();
      return;
    }
    if (btn.dataset.act === 'load') {
      if (state.dirty && Number(state.stats.images || 0)) {
        const pick = await askButtons('载入前先保存吗？',
          `当前结果（<b>${fmtInt(state.stats.images)}</b> 张）还没保存，` +
          `载入「${esc(meta.name || '')}」会用它替换现在的索引。<br>` +
          `<span class="muted">照片文件不受影响，只影响软件里的扫描结果。</span>`,
          [
            { key: 'save', label: '保存并载入', cls: 'primary' },
            { key: 'load', label: '直接载入（丢弃）', cls: 'danger solid' },
            { key: 'cancel', label: '取消', cls: 'ghost' },
          ]);
        if (!pick || pick === 'cancel') return;
        if (pick === 'save') {
          const ok = await saveArchive({ silent: true });
          if (!ok) return;
        }
      } else {
        const yes = await askConfirm('载入存档',
          `载入「${esc(meta.name || '')}」会<b>替换当前索引</b>（照片文件不受影响）。确定继续？`,
          '载入', false);
        if (!yes) return;
      }
      try {
        const r = await post('/api/archives/load', { id, scope: state.arScope });
        if (!r.ok) { toast(esc(r.error), 'err'); return; }
        if (r.settings) {
          if (r.settings.threshold) { state.threshold = Number(r.settings.threshold); $('#threshold').value = state.threshold; updateRangeFill(); }
          if (r.settings.min) { state.minSize = Number(r.settings.min); $('#minSize').value = String(state.minSize); }
          if (r.settings.sort) { state.sort = r.settings.sort; $('#sort').value = state.sort; }
          savePrefs();
        }
        state.selected.clear(); state.best.clear(); state.root = '';
        state.loadedArchive = id;
        setDirty(false);
        $('#archives').hidden = true;
        const extra = r.missing ? `，其中 ${fmtInt(r.missing)} 个文件已不存在、已清理` : '';
        toast(`已载入「${esc(r.name)}」，共 ${fmtInt(r.images)} 张${extra}`, r.missing ? '' : 'ok', 5000);
        if (r.verifying) {
          toast('存档较大，正在后台校验失效文件…', '', 3000);
          pollVerify();
        }
        await loadRoots();
        await loadGroups();
        if (M.tab !== 'image') { await mLoadRoots(); await mLoadGroups(); }
      } catch (err) { toast('载入失败：' + esc(err.message), 'err'); }
      return;
    }
    if (btn.dataset.act === 'update') {
      const oldN = Number(meta.images || 0);
      const newN = Number(state.stats.images || 0);
      if (!newN) { toast('当前没有扫描结果，无法覆盖', 'err'); return; }
      const diff = newN - oldN;
      const yes = await askConfirm('用当前结果覆盖存档',
        `「${esc(meta.name || '')}」<br>` +
        `存档里 <b>${fmtInt(oldN)}</b> 张 → 当前 <b>${fmtInt(newN)}</b> 张` +
        `（${diff >= 0 ? '+' : ''}${fmtInt(diff)}）<br>` +
        `<span class="muted">会覆盖这个存档文件，旧内容不能恢复。</span>`,
        '覆盖', false);
      if (!yes) return;
      try {
        const r = await post('/api/archives/update', {
          id, scope: state.arScope,
          settings: { threshold: state.threshold, min: state.minSize, sort: state.sort },
        });
        if (!r.ok) { toast(esc(r.error), 'err'); return; }
        setDirty(false);
        state.loadedArchive = id;
        state.flashId = id;
        await loadArchives();
        const hint = $('#arHint');
        hint.hidden = false;
        hint.className = 'ar-hint ok';
        hint.textContent = `已覆盖 ${r.archive.id}.json · ${fmtInt(r.archive.images)} 张 · ${fmtBytes(r.archive.size)}`;
        toast(`已覆盖「${esc(r.archive.name)}」· ${fmtInt(r.archive.images)} 张 · ${fmtBytes(r.archive.size)}`, 'ok', 4500);
        setTimeout(() => { state.flashId = ''; }, 1800);
      } catch (err) { toast('更新失败：' + esc(err.message), 'err'); }
      return;
    }
    if (btn.dataset.act === 'merge') {
      const yes = await askConfirm('合并存档',
        `把「${esc(meta.name || '')}」里的 <b>${fmtInt(meta.images || 0)}</b> 张合并到当前索引？<br>` +
        `<span class="muted">已有的不会重复，只补新的。</span>`,
        '合并', false);
      if (!yes) return;
      try {
        const r = await post('/api/archives/merge', { id, scope: state.arScope });
        if (!r.ok) { toast(esc(r.error), 'err'); return; }
        toast(`已合并，新增 ${fmtInt(r.added)} 张`, 'ok');
        setDirty(true);
        await loadRoots();
        await loadGroups();
      } catch (err) { toast('合并失败：' + esc(err.message), 'err'); }
    }
  }

  async function importArchivePath(path) {
    try {
      const r = await post('/api/archives/import_path', { path });
      if (!r.ok) { toast(esc(r.error || '导入失败'), 'err', 4500); return; }
      localStorage.setItem('pd.arDir', String(path).replace(/[\\/][^\\/]*$/, '') || '');
      toast(`已导入「${esc(r.archive.name)}」· ${fmtInt(r.archive.images)} 张`, 'ok', 4200);
      await loadArchives();
    } catch (err) {
      toast('导入失败：' + esc(err.message), 'err');
    }
  }

  /* ---------------- 文件夹选择 ---------------- */
  function openBrowser(path, cb, opts = {}) {
    state.browseCb = cb || null;
    state.browseFiles = !!opts.files;
    $('#browseTitle').textContent = opts.title || (state.browseFiles ? '选择存档文件' : '选择文件夹');
    $('#browseOk').hidden = state.browseFiles;
    $('#browseOk').textContent = opts.okLabel || '添加此文件夹';
    $('#browser').hidden = false;
    navigateBrowser(path || '');
  }
  function closeBrowser() {
    $('#browser').hidden = true;
    state.browseCb = null;
    state.browseFiles = false;
  }
  async function navigateBrowser(path) {
    $('#browseHint').textContent = '正在读取…';
    try {
      const qs = '/api/fs?path=' + encodeURIComponent(path || '') +
        (state.browseFiles ? '&files=1' : '');
      const r = await api(qs);
      if (r.error) { $('#browseHint').textContent = r.error; return; }
      state.browsePath = r.path;
      $('#browseInput').value = r.path;
      const items = [];
      (r.quick || []).forEach((q) => items.push({ name: q.name + '（常用）', path: q.path }));
      (r.dirs || []).forEach((d) => items.push({ name: d.name, path: d.path }));
      (r.files || []).forEach((f) => items.push({
        name: f.name, path: f.path, file: true, size: f.size,
      }));
      $('#browseList').innerHTML = items.length ? items.map((d) => `
        <div class="browse-item ${d.file ? 'is-file' : ''}" data-path="${esc(d.path)}"
             ${d.file ? 'data-file="1"' : ''}>
          <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor">
            ${d.file
              ? '<path d="M6 2h7l5 5v15H6z" opacity=".75"/><path d="M13 2v5h5" opacity=".45"/>'
              : '<path d="M3 6a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" opacity=".75"/>'}
          </svg>
          <span>${esc(d.name)}</span>
          ${d.file ? `<em>${fmtBytes(d.size)}</em>` : ''}
        </div>`).join('') : '<div class="browse-item muted">（这里是空的）</div>';
      const nFiles = (r.files || []).length;
      $('#browseHint').textContent = !r.path
        ? '选择一个磁盘或常用位置'
        : (state.browseFiles
          ? `${fmtInt(nFiles)} 个存档文件${nFiles ? ' · 点文件名直接导入' : '（换个文件夹看看）'}`
          : `当前文件夹约 ${fmtInt(r.images)} 张图片`);
      $('#browseOk').disabled = !r.path;
      $('#browseUp').disabled = !r.parent;
      if (r.parent) $('#browseUp').dataset.parent = r.parent;
    } catch (e) {
      $('#browseHint').textContent = '读取失败：' + e.message;
    }
  }

  /* ---------------- 键盘 ---------------- */
  function onKey(e) {
    if (e.key === 'Escape') {
      if (!$('#ask').hidden) { closeAsk(false); return; }
      if (!$('#compare').hidden) { closeCompare(); return; }
      if (!$('#browser').hidden) { closeBrowser(); return; }
      if (!$('#preview').hidden) { $('#preview').hidden = true; return; }
      if (!$('#archives').hidden) { $('#archives').hidden = true; return; }
      if (!$('#settings').hidden) { closeSettings(); return; }
      if (!$('#confirm').hidden) { $('#confirm').hidden = true; state.pendingRemove = null; return; }
      return;
    }
    if (!$('#compare').hidden) {
      if (e.key === 'ArrowLeft') { e.preventDefault(); stepCompare(-1); }
      if (e.key === 'ArrowRight') { e.preventDefault(); stepCompare(1); }
      return;
    }
    if (e.key === 'Enter' && e.ctrlKey) askRemove('trash');
  }

  /* ================= 媒体页签（视频 / 音频 / 小说） ================= */
  const MT = {
    image: null,
    video: { th: 0.88, min: 2, label: "画面相似度", icon: "🎬", hint: "ffmpeg 抽 8 帧 + AI 画面特征" },
    audio: { th: 0.35, min: 2, label: "指纹重合度", icon: "🎵", hint: "频谱指纹，能认不同码率/格式" },
    novel: { th: 0.82, min: 2, label: "书名相似度", icon: "📖", hint: "只比书名 + 显示章节数" },
  };
  const M = { tab: "image", roots: {}, groups: {}, sel: new Map(), poll: null, job: null };

  function mCfg() { return MT[M.tab] || MT.video; }

  /* ---------------------------------------------------------------- 视图切换
     左边栏每个按钮 = 一个视图；图片视图额外带扫描控制，其他视图只显示自己的面板。 */
  const VIEWS = {
    image: { title: "图片查重", hint: "多文件夹累积对比 · 随时暂停继续",
             el: "#view-image", panel: "#imagePanel", scan: true },
    video: { title: "视频查重", hint: "按画面抽帧比对",
             el: "#view-media", panel: "#mediaPanel", media: true },
    audio: { title: "音频查重", hint: "按频谱指纹比对",
             el: "#view-media", panel: "#mediaPanel", media: true },
    novel: { title: "小说查重", hint: "按书名 + 章节数比对",
             el: "#view-media", panel: "#mediaPanel", media: true },
    clean: { title: "广告过滤", hint: "去掉小说里的广告行（不改原文件）",
             el: "#view-clean", panel: "#cleanPanel" },
    conv: { title: "繁简 / 编码", hint: "繁体转简体、任意编码转 UTF-8 / GBK / BIG5",
            el: "#view-conv", panel: "#convPanel" },
    pack: { title: "打包", hint: "流程可编辑：格式 / 加密 / 拆分 / 改名",
            el: "#view-pack", panel: "#packPanel" },
    unzip: { title: "解压", hint: "多层 / 分卷在内存里逐层解开，只写最终文件",
             el: "#view-unzip", panel: "#unzipPanel" },
    brute: { title: "暴力破解", hint: "只破解不解压，结果进入单独的密码本",
             el: "#view-brute", panel: "#brutePanel" },
  };

  function setView(view) {
    const cfg = VIEWS[view];
    if (!cfg) return;
    state.currentView = view;
    if (cfg.media) M.tab = view;

    document.body.dataset.view = view;
    $$(".nav-item").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
    Object.entries(VIEWS).forEach(([name, c]) => {
      const el = $(c.el);
      if (!el) return;
      const on = (name === view) || (cfg.media && c.media);
      el.hidden = !on;
      el.classList.toggle("on", on);
      const inner = $(c.panel);
      if (inner) inner.hidden = !on;
    });
    $("#viewTitle").textContent = cfg.title || "";
    $("#viewHint").textContent = cfg.hint || "";
    $("#headActs").hidden = !cfg.scan;

    if (view === "brute") { brLoadFound(); brLoadHashcat(); brPoll(); return; }
    if (view === "pack") { pkPoll(); return; }
    if (view === "conv") { cvPoll(); return; }
    if (view === "unzip") { uzLoadFound(); uzPoll(); return; }
    if (view === "clean") { clPoll(); return; }
    if (typeof otRefreshCount === "function") otRefreshCount();
    if (cfg.media) {
      const tab = view;
      const c = mCfg();
      $("#mThreshold").min = tab === "video" ? 0.70 : (tab === "audio" ? 0.10 : 0.50);
      $("#mThreshold").max = tab === "video" ? 0.99 : (tab === "audio" ? 0.90 : 0.99);
      $("#mThreshold").step = 0.01;
      $("#mThreshold").value = c.th;
      $("#mThVal").textContent = Number(c.th).toFixed(2);
      $("#mThLabel").textContent = c.label;
      $("#mMin").value = String(c.min);
      $("#mAvWrap").hidden = tab !== "audio";
      $("#mContentWrap").hidden = tab !== "novel";
      $("#mContent").checked = localStorage.getItem("pd.content") !== "0";
      $("#mAudioVideo").checked = localStorage.getItem("pd.audvid") === "1";
      mLoadRoots();
      mLoadGroups();
    }
  }

  async function mLoadRoots() {
    if (M.tab === "image") return;
    try {
      const r = await api("/api/media/roots?kind=" + M.tab);
      M.roots[M.tab] = r;
      mRenderRoots();
      $("#mEngine").textContent = r.ffmpeg === false ? "缺少 ffmpeg" : "";
      const st = r.stats || {};
      $("#mItemsLabel").textContent = M.tab === "novel" ? "本小说" : (M.tab === "audio" ? "个音频" : "个视频");
    } catch (e) { /* ignore */ }
  }

  function mRenderRoots() {
    const r = M.roots[M.tab];
    const host = $("#mChips");
    if (!r || !r.roots.length) {
      host.innerHTML = "";
      $("#mRoot").innerHTML = '<option value="">全部已扫描</option>';
      return;
    }
    host.innerHTML = r.roots.map((x) => {
      const cls = x.status === "scanning" ? "scanning" : (x.status === "paused" ? "paused" : (x.status === "error" ? "err" : "ok"));
      const n = x.status === "scanning" ? `${x.done || 0}/${x.total || 0}` : fmtInt(x.items || 0);
      return `<span class="chip-root ${cls}" data-path="${esc(x.path)}" title="${esc(x.path)}">
        <i class="st"></i><span class="nm">${esc(baseName(x.path))}</span>
        <span class="cnt">${n}</span>
        <button class="up" data-act="refresh" title="重新扫描这个文件夹">⟳</button>
        <button class="x" data-act="rm" title="从工作区移除（不会删文件）">✕</button>
      </span>`;
    }).join("");
    const cur = $("#mRoot").value;
    $("#mRoot").innerHTML = '<option value="">全部已扫描</option>' +
      r.roots.map((x) => `<option value="${esc(x.path)}">${esc(baseName(x.path))}</option>`).join("");
    $("#mRoot").value = cur;
  }

  async function mAddFolder() {
    const p = $("#mFolder").value.trim();
    if (!p) { pickFolder("选择文件夹", "", (x) => { $("#mFolder").value = x; mAddFolder(); }, "mAdd"); return; }
    const r = await post("/api/media/roots", { kind: M.tab, path: p, recursive: $("#mRecursive").checked });
    if (!r.ok) { toast(esc(r.error || "添加失败"), "err"); return; }
    $("#mFolder").value = "";
    toast("已添加：" + esc(baseName(r.root)) + "，点「开始扫描」开始", "", 3500);
    await mLoadRoots();
  }

  async function mStart() {
    try {
      const r = await post("/api/media/scan", {
        kind: M.tab,
        include_video: M.tab === "audio" && $("#mAudioVideo").checked,
      });
      if (!r.ok) { toast(esc(r.error || "启动失败"), "err"); return; }
      if (r.queued) { toast(esc(r.message || "已排队"), "", 4000); return; }
      toast("开始扫描（" + esc(r.engine || "") + "）", "", 2600);
      mPoll();
    } catch (e) { toast("启动失败：" + esc(e.message), "err"); }
  }

  function mPoll() {
    clearTimeout(M.poll);
    const tick = async () => {
      try {
        const s = await api("/api/media/status");
        M.job = s;
        mRenderProgress(s);
        if (s.running) {
          M.poll = setTimeout(tick, 700);
          // 扫描中也让人看到已扫到的结果
          const now = Date.now();
          if (!M._lastPeek || now - M._lastPeek > 4000) {
            M._lastPeek = now;
            mLoadGroups(true);
          }
        } else {
          $("#mScan").hidden = false;
          $("#mPause").hidden = true;
          $("#mStop").hidden = true;
          if (s.state === "done") toast("扫描完成：" + fmtInt(s.total) + " 个，新分析 " + fmtInt(s.hashed), "ok", 4000);
          else if (s.state === "error") toast("扫描出错：" + esc(s.message), "err", 6000);
          else if (s.state === "paused") toast("已暂停，点「继续」接着扫", "", 3000);
          await mLoadRoots();
          await mLoadGroups();
        }
      } catch (_) { /* ignore */ }
    };
    tick();
  }

  function mRenderProgress(s) {
    const el = $("#mProgress");
    if (!s.running && !s.total) { el.hidden = true; return; }
    el.hidden = false;
    const pct = s.total ? Math.round(100 * s.done / s.total) : 0;
    el.innerHTML = `扫描中 ${fmtInt(s.done)}/${fmtInt(s.total)}（${pct}%）· ${s.rate || 0}/秒 · ` +
      `新分析 ${fmtInt(s.hashed)} · 跳过 ${fmtInt(s.reused)} · 失败 ${fmtInt(s.errors)} · ` +
      fmtInt(Math.round(s.elapsed || 0)) + "s" +
      `<div class="bar"><i style="width:${pct}%"></i></div>`;
    $("#mScan").hidden = s.running;
    $("#mPause").hidden = !s.running || s.paused;
    $("#mPause").textContent = s.paused ? "继续" : "暂停";
    $("#mStop").hidden = !s.running;
  }

  async function mLoadGroups(quick) {
    if (M.tab === "image") return;
    if (typeof otRefreshCount === "function") otRefreshCount();
    const th = Number($("#mThreshold").value);
    const min = Number($("#mMin").value);
    const root = $("#mRoot").value;
    try {
      const qs = `/api/media/groups?kind=${M.tab}&threshold=${th}&min=${min}&limit=200` +
        `&root=${encodeURIComponent(root)}${quick ? "&quick=1" : ""}` +
        (M.tab === "novel" && !$("#mContent").checked ? "&content=0" : "");
      const r = await api(qs);
      M.groups[M.tab] = r;
      $("#mItems").textContent = fmtInt(r.stats.items);
      $("#mGroups").textContent = fmtInt(r.stats.groups);
      $("#mDup").textContent = fmtInt(r.stats.duplicates);
      $("#mFree").textContent = fmtBytes(r.stats.reclaim);
      M.partial = !!quick;
      mRenderGroups(r);
      mUpdateFooter();
    } catch (e) { toast("读取结果失败：" + esc(e.message), "err"); }
  }

  function mRowMeta(row) {
    const m = row.meta || {};
    if (M.tab === "novel") {
      const chap = row.chap ? `（对比 ${row.chap.b} 章）` : "";
      return `${m.chapters || 0} 章${chap} · ${fmtInt(m.words || 0)} 字 · ${fmtBytes(row.size)}`;
    }
    if (M.tab === "audio") {
      const tag = m.from_video ? "（视频音轨）" : "";
      return `${row.dur_txt || ""} · ${m.bitrate || 0} kbps · ${fmtBytes(row.size)}${tag}`;
    }
    return `${row.w}×${row.h} · ${row.dur_txt || ""} · ${fmtBytes(row.size)}`;
  }

  function mRowHtml(row, groupNo, isRep) {
    const sel = M.sel.has(row.id);
    const icon = mCfg().icon;
    M.byId = M.byId || {};
    M.byId[row.id] = row;
    let thumb;
    if (M.tab === "video") {
      thumb = `<div class="m-thumbbox">
        <img class="m-thumb" loading="lazy" src="/api/media/thumb?id=${row.id}" alt=""
             onerror="this.parentNode.classList.add('noimg')">
        <button class="m-play" data-pv="${row.id}" title="预览视频">▶</button>
      </div>`;
    } else if (M.tab === "audio") {
      thumb = `<div class="m-thumbbox audio">
        <div class="m-thumb icon">${icon}</div>
        <button class="m-play" data-pv="${row.id}" title="试听">▶</button>
      </div>`;
    } else {
      thumb = `<div class="m-thumb icon">${icon}</div>`;
    }
    const simTxt = isRep ? "保留" : `${row.sim}%`;
    let tag = "";
    if (M.tab === "novel" && !isRep) {
      const ch = row.chap;
      if (ch && ch.matched) {
        const total = Math.max(ch.a || 0, ch.b || 0);
        const pct = Math.round((ch.ratio || 0) * 100);
        tag = `<span class="m-tag">正文重合 ${ch.matched}/${total} 章 · ${pct}%</span>`;
      } else if (row.by_content) {
        tag = '<span class="m-tag">正文重复</span>';
      } else {
        tag = '<span class="m-tag" style="background:rgba(227,182,91,.16);color:#e3b65b">仅书名相似</span>';
      }
    }
    return `<div class="m-row ${sel ? "sel" : ""}" data-id="${row.id}">
      ${thumb}
      <div class="m-main">
        <div class="m-name" title="${esc(row.path)}">${esc(row.name)}${isRep ? '<span class="m-tag">建议保留</span>' : ""}${tag}</div>
        <div class="m-meta" title="${esc(row.path)}">${esc(mRowMeta(row))}</div>
      </div>
      <div class="m-sim">${esc(simTxt)}</div>
      ${M.tab === "novel" ? `<button class="m-pv" data-pv="${row.id}" title="预览正文">👁</button>` : ""}
    </div>`;
  }

  function mRenderGroups(r) {
    const host = $("#mList");
    if (!r.groups.length) {
      host.innerHTML = "";
      $("#mEmpty").hidden = false;            // 没有结果时必须给个说法，别留一片空白
      const scanning = M.job && M.job.running;
      $("#mEmpty").innerHTML = r.stats.items
        ? (M.partial
          ? `<p>已扫到的 ${fmtInt(r.stats.items)} 个文件里没有发现同名/同大小的重复</p><small>扫描还在继续，结果会随时刷新</small>`
          : "<p>没有发现重复</p><small>可以调低阈值再试试</small>")
        : "<p>还没有扫描结果</p><small>添加一个文件夹，然后点「开始扫描」</small>";
      return;
    }
    $("#mEmpty").hidden = true;
    host.innerHTML = r.groups.map((g) => `
      <div class="m-group" data-key="g${g.no}">
        <div class="m-ghead">
          <b>组 ${g.no}</b>
          <span>${g.count} 个 · 相似 ${(g.max_sim * 100).toFixed(1)}–${(g.min_sim * 100).toFixed(1)}%</span>
          <span>可释放 ${fmtBytes(g.reclaim)}</span>
          <div class="m-gacts">
            <button data-act="keep" data-no="${g.no}">只留建议</button>
            <button data-act="inv" data-no="${g.no}">反选</button>
            <button data-act="clear" data-no="${g.no}">取消</button>
          </div>
        </div>
        ${mRowHtml(g.rep, g.no, true)}
        ${g.members.map((x) => mRowHtml(x, g.no, false)).join("")}
      </div>`).join("");
  }

  function mToggle(id) {
    id = Number(id);
    if (M.sel.has(id)) M.sel.delete(id); else M.sel.set(id, 1);
    const row = $(`.m-row[data-id="${id}"]`);
    if (row) row.classList.toggle("sel", M.sel.has(id));
    mUpdateFooter();
  }

  function mUpdateFooter() {
    const n = M.sel.size;
    $("#mFooter").hidden = n === 0 && !(M.groups[M.tab] && M.groups[M.tab].groups.length);
    $("#mSelInfo").textContent = n ? `${fmtInt(n)} 项已选` : "未选择";
    $("#mTrash").disabled = !n;
    $("#mMove").disabled = !n;
  }

  async function mSmart() {
    const r = await post("/api/media/autoselect", {
      kind: M.tab, keep: "best", min: Number($("#mMin").value),
      threshold: Number($("#mThreshold").value), root: $("#mRoot").value,
    });
    if (!r.ok) { toast(esc(r.error || "失败"), "err"); return; }
    M.sel.clear();
    r.ids.forEach((id) => M.sel.set(id, 1));
    mRenderGroups(M.groups[M.tab] || { groups: [] });
    mUpdateFooter();
    toast(`已勾选 ${fmtInt(r.count)} 项（${r.groups} 组）`, "ok", 3500);
  }

  /** 媒体页：从当前列表里就地摘掉这些 id，不重新拉整页 */
  function mDropIds(ids) {
    const gone = new Set(ids.map(Number));
    const pack = M.groups[M.tab];
    if (!pack) return 0;
    let dropped = 0;
    const keep = [];
    for (const g of pack.groups || []) {
      const before = (g.members || []).length + 1;
      g.members = (g.members || []).filter((m) => !gone.has(Number(m.id)));
      const left = (g.members || []).length + 1;      // 代表还在，除非代表本身被删
      const repGone = gone.has(Number((g.rep || {}).id));
      if (repGone) {
        if (g.members && g.members.length) {
          const newRep = g.members.shift();
          g.rep = Object.assign({}, newRep, { sim: 100, rep: true });
        } else {
          dropped++; continue;
        }
      }
      if (left < 2) { dropped++; continue; }
      keep.push(g);
    }
    pack.groups = keep;
    if (pack.stats) {
      pack.stats.groups = keep.length;
      pack.stats.duplicates = Math.max(0, Number(pack.stats.duplicates || 0) - (ids.length - dropped));
      pack.stats.items = Math.max(0, Number(pack.stats.items || 0) - ids.length);
    }
    mRenderGroups(pack);
    return dropped;
  }

  async function mRemove(mode) {
    const ids = [...M.sel.keys()];
    if (!ids.length) return;
    const yes = await askConfirm(mode === "move" ? "移到文件夹" : "移到回收站",
      `处理 <b>${fmtInt(ids.length)}</b> 个文件？<br><span class="muted">${
        mode === "move" ? "会移动到扫描目录旁边的「重复媒体」文件夹" : "会放进系统回收站，可以还原"}</span>`,
      mode === "move" ? "移动" : "移到回收站", mode !== "move");
    if (!yes) return;

    const CHUNK = 20;                  // 媒体文件大，一批少一点；每批回来就更新列表
    const total = ids.length;
    let removed = 0, freed = 0, done = 0;
    const failed = [];
    try {
      for (let i = 0; i < total; i += CHUNK) {
        const part = ids.slice(i, i + CHUNK);
        const r = await post("/api/media/remove", { ids: part, mode });
        if (!r.ok) { failed.push(...part.map((id) => ({ id, error: r.error }))); }
        else {
          removed += r.removed || 0;
          freed += r.freed || 0;
          if (r.failed && r.failed.length) failed.push(...r.failed);
          const bad = new Set((r.failed || []).map((f) => String(f.id)));
          const gone = part.filter((id) => !bad.has(String(id)));
          gone.forEach((id) => M.sel.delete(id));
          mDropIds(gone);
        }
        done += part.length;
        mUpdateFooter();
        await new Promise((res) => setTimeout(res, 0));
      }
      let msg = `${mode === "move" ? "已移动" : "已删除"} ${fmtInt(removed)} 个，释放 ${fmtBytes(freed)}`;
      if (failed.length) msg += `；${failed.length} 个失败`;
      toast(msg, failed.length ? "err" : "ok", 4200);
      mLoadRoots();
    } catch (e) { toast("处理失败：" + esc(e.message), "err"); }
  }

  function mExport() {
    const th = Number($("#mThreshold").value);
    window.location.href = `/api/media/export?kind=${M.tab}&threshold=${th}&min=${$("#mMin").value}`;
  }



  /* ---------------- 小说预览 ---------------- */
  let pvId = 0;
  let pvFile = '';
  /* ---------------- 其他文件（没参与查重的） ----------------
     图片页看 status='error' 的记录（读不出来 / 后缀不对），
     视频 / 音频 / 小说页看 status='skipped' 的（太小 / 没音轨 / 动图）。 */
  const OT = { kind: "image", items: [], at: -1 };

  async function otRefreshCount() {
    try {
      const r = await api('/api/other/count');
      const c = r.counts || {};
      const imgN = Number(c.image || 0);
      const b1 = $('#btnOther'), b2 = $('#mOther');
      if (b1) {
        b1.hidden = !imgN;
        const n = $('#btnOtherNum');
        if (n) n.textContent = imgN ? `(${fmtInt(imgN)})` : '';
      }
      if (b2) {
        const kn = M.tab === 'image' ? 0 : Number(c[M.tab] || 0);
        b2.hidden = !kn;
        const n2 = $('#mOtherNum');
        if (n2) n2.textContent = kn ? `(${fmtInt(kn)})` : '';
      }
    } catch (_) { /* 老版本没有这个接口 */ }
  }

  async function otOpen(kind) {
    OT.kind = kind;
    OT.at = -1;
    const box = $('#other');
    box.hidden = false;
    $('#otTitle').textContent = `其他文件 · ${({ image: '图片', video: '视频', audio: '音频', novel: '小说' })[kind] || kind}`;
    $('#otList').innerHTML = '<div class="ot-empty">读取中…</div>';
    $('#otFoot').textContent = '';
    try {
      const url = kind === 'image' ? '/api/images/other' : `/api/media/other?kind=${kind}`;
      const r = await api(url);
      OT.items = r.items || [];
    } catch (e) {
      OT.items = [];
      $('#otList').innerHTML = `<div class="ot-empty">读取失败：${esc(e.message || e)}</div>`;
      return;
    }
    otRender();
  }

  function otRender() {
    const host = $('#otList');
    if (!OT.items.length) {
      host.innerHTML = `<div class="ot-empty">这一类里没有跳过或失败的文件。</div>`;
      $('#otFoot').textContent = '';
      return;
    }
    host.innerHTML = OT.items.map((it, i) => {
      const isImg = OT.kind === 'image';
      const size = isImg ? Number(it.size || 0) : Number(it.size || 0);
      const reason = isImg ? (it.err || '分析失败') : (it.reason || '跳过');
      return `<div class="ot-row" data-i="${i}">
        <div class="ot-info">
          <div class="ot-name" title="${esc(it.path || '')}">${esc(it.name || '')}</div>
          <div class="ot-path" title="${esc(it.path || '')}">${esc(it.path || '')}</div>
          <div class="ot-reason">${esc(reason)}<span class="sep">·</span>${fmtBytes(size)}</div>
        </div>
        <div class="ot-acts">
          <button data-otact="view" class="primary" data-i="${i}">预览</button>
          <button data-otact="open" data-i="${i}" title="用系统默认程序打开（视频编码放不了时用）">系统程序</button>
          <button data-otact="reveal" data-i="${i}">定位</button>
        </div>
      </div>`;
    }).join("");
    $('#otFoot').textContent = `共 ${OT.items.length} 个 · 这里只列没有参与查重的文件`;
  }

  async function otAct(act, i) {
    const it = OT.items[i];
    if (!it) return;
    if (act === 'open') {
      const r = await post('/api/open_path', { path: it.path });
      if (!r.ok) toast(esc(r.error || '打不开'), 'err');
      return;
    }
    if (act === 'reveal') {
      await post('/api/reveal', { path: it.path });
      return;
    }
    // 预览
    if (OT.kind === 'image') {
      openImageFile(it);
      return;
    }
    mpOpenMedia(it, OT.items.map((x) => ({ id: x.id, name: x.name, path: x.path })));
  }

  /* ---------------- 视频 / 音频预览 ----------------
     只认库里的条目 id：/api/media/file?id=N 边读边发，支持 Range，能拖进度条。 */
  const MP = { ids: [], idx: 0, kind: "video" };

  function mpRows() {
    return $$("#mList .m-row").map((el) => Number(el.dataset.id));
  }

  function mpOpen(id) {
    const ids = mpRows();
    const idx = ids.indexOf(Number(id));
    if (idx < 0) return;
    MP.ids = ids;
    MP.idx = idx;
    MP.kind = M.tab;
    M.byId = M.byId || {};
    $("#mplay").hidden = false;
    mpShow();
  }

  /** 从"其他文件"列表里预览：不依赖 .m-row，直接给 id 列表 */
  function mpOpenMedia(item, list) {
    const ids = (list || []).map((x) => Number(x.id));
    const idx = ids.indexOf(Number(item.id));
    if (idx < 0) return;
    MP.ids = ids;
    MP.idx = idx;
    MP.kind = OT.kind;
    M.byId = M.byId || {};
    (list || []).forEach((x) => { M.byId[x.id] = Object.assign({}, M.byId[x.id], x); });
    $("#mplay").hidden = false;
    mpShow();
  }

  const VIDEO_EXT = /\.(mp4|m4v|mov|mkv|webm|avi|wmv|flv|ts|mpg|mpeg|rmvb|rm|3gp|ogm|vob|asf|m2ts|mts|f4v|divx|m1v|m2v|dat)$/i;
  const IMG_EXT = /\.(gif|webp|apng|png|jpe?g|bmp|avif)$/i;

  function mpShow() {
    const id = MP.ids[MP.idx];
    const row = (M.byId || {})[id] || {};
    const name = row.path || row.name || "";
    // 按**文件后缀**决定用哪个播放器（音频页里可能有带音轨的 mp4，反之亦然）
    const isImgFile = IMG_EXT.test(name);
    const isVideoFile = !isImgFile && VIDEO_EXT.test(name);
    const v = $("#mpVideo");
    const a = $("#mpAudio");
    const im = $("#mpImage");
    const missing = $("#mpMissing");
    const src = `/api/media/file?id=${id}`;
    im.hidden = true;

    $("#mpTitle").textContent = row.name || `#${id}`;
    const meta = [];
    if (row.dur_txt) meta.push(row.dur_txt);
    if (isVideoFile && row.w) meta.push(`${row.w}×${row.h}`);
    if (row.size) meta.push(fmtBytes(row.size));
    if (row.sim != null && !row.rep) meta.push(`组内相似 ${row.sim}%`);
    $("#mpMeta").textContent = meta.join(" · ");
    $("#mpFoot").textContent = row.path || "";

    missing.hidden = true;
    v.onerror = null;
    a.onerror = null;

    if (isImgFile) {
      v.hidden = true; a.hidden = true;
      v.removeAttribute("src"); a.removeAttribute("src");
      im.hidden = false;
      im.src = src;
      im.onerror = () => { im.hidden = true; missing.hidden = false; };
      waveClear();
    } else {
      im.removeAttribute("src");
      if (isVideoFile) {
        a.pause(); a.hidden = true; a.removeAttribute("src");
        v.hidden = false;
        v.src = src;
        v.poster = `/api/media/thumb?id=${id}`;
        v.onerror = () => {
          // 后缀是视频、内容其实是图片/gif → 退回图片
          im.hidden = false;
          im.src = src;
          im.onerror = () => { im.hidden = true; v.hidden = true; missing.hidden = false; };
          v.hidden = true;
          waveClear();
        };
        v.load();
      } else {
        v.pause(); v.hidden = true; v.removeAttribute("src");
        a.hidden = false;
        a.src = src;
        a.onerror = () => {
          a.hidden = true;
          missing.hidden = false;
          missing.textContent = "文件读不到，可能已经被移动或删除";
          // 区分两种原因：文件确实没了 / 服务端还是旧版本（没有这个接口）
          fetch(src, { method: "GET", headers: { Range: "bytes=0-1" } })
            .then((r) => r.json().catch(() => null).then((j) => ({ status: r.status, j })))
            .then(({ status, j }) => {
              if (status === 404) {
                const msg = (j && j.error) || "";
                missing.textContent = /文件不存在|读不到/.test(msg)
                  ? "文件不存在了（可能被移动或删除）"
                  : "预览接口不可用：程序可能还是旧版本，关掉重开一次试试";
              } else if (status === 200) {
                missing.textContent = "浏览器播放不了这个编码（试试下面的「系统播放器」）";
              }
            })
            .catch(() => {});
        };
        a.load();
      }
      waveLoad(id);
    }

    // 组内其它条目：只显示前 12 个 + 计数，避免大组把弹窗撑爆
    const strip = $("#mpStrip");
    const MAX_CHIPS = 12;
    let chips = MP.ids.map((rid, i) => ({ rid, i, r: (M.byId || {})[rid] || {} }));
    if (chips.length > MAX_CHIPS) {
      const cur = chips[MP.idx];
      const rest = chips.filter((c) => c.i !== MP.idx);
      const head = rest.slice(0, MAX_CHIPS - 1);
      chips = [cur, ...head].sort((a, b) => a.i - b.i);
      strip.innerHTML = chips.map((c) => `
        <button class="mp-chip ${c.i === MP.idx ? "on" : ""}" data-mp="${c.rid}"
          title="${esc(c.r.path || "")}">${esc(c.r.name || c.rid)}</button>`).join("")
        + `<span class="mp-more" title="这一组共 ${MP.ids.length} 个，用「上一个 / 下一个」翻">还有 ${MP.ids.length - chips.length} 个</span>`;
    } else {
      strip.innerHTML = chips.map((c) => `
        <button class="mp-chip ${c.i === MP.idx ? "on" : ""}" data-mp="${c.rid}"
          title="${esc(c.r.path || "")}">${esc(c.r.name || c.rid)}</button>`).join("");
    }

    $("#mpPrev").disabled = MP.idx <= 0;
    $("#mpNext").disabled = MP.idx >= MP.ids.length - 1;
  }

  function mpStep(delta) {
    const n = MP.idx + delta;
    if (n < 0 || n >= MP.ids.length) return;
    MP.idx = n;
    mpShow();
  }

  /* ---------------- 波形图 ----------------
     后端把整段音频压成 N 个 (min,max) 对，这里画成竖条；
     点/拖波形可以跳转，播放时画一条进度线。 */
  const WAVE = { peaks: null, secs: 0, id: 0, dragging: false };

  function waveSetVisible(on) {
    const box = $("#mpWaveBox");
    if (box) box.hidden = !on;
  }

  function waveClear() {
    WAVE.peaks = null;
    WAVE.secs = 0;
    const cv = $("#mpWave");
    if (!cv) return;
    const g = cv.getContext("2d");
    g.clearRect(0, 0, cv.width, cv.height);
    waveSetVisible(false);
  }

  function wavePlayer() {
    const v = $("#mpVideo");
    const a = $("#mpAudio");
    if (v && !v.hidden) return v;
    if (a && !a.hidden) return a;
    return null;
  }

  function waveDraw(progress) {
    const cv = $("#mpWave");
    if (!cv || !WAVE.peaks) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = cv.clientWidth || 600;
    const cssH = cv.clientHeight || 84;
    if (cv.width !== Math.round(cssW * dpr) || cv.height !== Math.round(cssH * dpr)) {
      cv.width = Math.round(cssW * dpr);
      cv.height = Math.round(cssH * dpr);
    }
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, cssW, cssH);
    const pk = WAVE.peaks;
    const bars = pk.length / 2;
    const mid = cssH / 2;
    const step = cssW / bars;
    const bw = Math.max(1, step * 0.7);
    const played = Math.max(0, Math.min(1, progress || 0));
    // 归一化：安静的文件也能把波形画满
    let amp = 1;
    for (let i = 0; i < pk.length; i++) amp = Math.max(amp, Math.abs(pk[i]));
    const scale = 1 / amp;
    // 中轴线
    g.fillStyle = "rgba(120,150,190,.25)";
    g.fillRect(0, mid, cssW, 1);
    for (let i = 0; i < bars; i++) {
      const lo = pk[i * 2] * scale;
      const hi = pk[i * 2 + 1] * scale;
      const y1 = mid - hi * (mid - 3);
      const y2 = mid - lo * (mid - 3);
      const h = Math.max(1, y2 - y1);
      const on = (i / bars) <= played;
      g.fillStyle = on ? "#5fb0ff" : "rgba(120, 150, 190, .45)";
      g.fillRect(i * step, y1, bw, h);
    }
    if (played > 0) {
      g.fillStyle = "rgba(255,255,255,.85)";
      g.fillRect(Math.min(cssW - 1, played * cssW), 0, 1, cssH);
    }
  }

  async function waveLoad(id) {
    WAVE.id = id;
    waveClear();
    try {
      const r = await api(`/api/media/waveform?id=${id}&buckets=900`);
      if (!r.ok || !r.peaks || !r.peaks.length) { waveSetVisible(false); return; }
      if (WAVE.id !== id) return;                 // 期间又切了别的文件
      WAVE.peaks = r.peaks;
      WAVE.secs = r.duration || 0;
      waveSetVisible(true);
      // 刚取消 hidden 时布局还没算出来，等一帧再画
      requestAnimationFrame(() => {
        waveDraw(0);
        requestAnimationFrame(() => waveDraw(0));
      });
    } catch (_) {
      waveSetVisible(false);
    }
  }

  function waveSeek(e) {
    const cv = $("#mpWave");
    const pl = wavePlayer();
    if (!cv || !pl || !pl.duration) return;
    const rect = cv.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    try { pl.currentTime = ratio * pl.duration; } catch (_) {}
    waveDraw(ratio);
  }

  function waveBind() {
    const cv = $("#mpWave");
    if (!cv || cv.dataset.bound) return;
    window.addEventListener("resize", () => {
      if (WAVE.peaks && !$("#mplay").hidden) waveDraw(0);
    });
    cv.dataset.bound = "1";
    cv.addEventListener("pointerdown", (e) => {
      WAVE.dragging = true;
      cv.setPointerCapture(e.pointerId);
      waveSeek(e);
    });
    cv.addEventListener("pointermove", (e) => { if (WAVE.dragging) waveSeek(e); });
    cv.addEventListener("pointerup", () => { WAVE.dragging = false; });
    cv.addEventListener("pointercancel", () => { WAVE.dragging = false; });
    // 播放进度
    const tick = () => {
      const pl = wavePlayer();
      if (pl && WAVE.peaks && !WAVE.dragging && pl.duration) {
        waveDraw(pl.currentTime / pl.duration);
      }
    };
    const v = $("#mpVideo"), a = $("#mpAudio");
    [v, a].forEach((el) => {
      if (!el || el.dataset.waveBound) return;
      el.dataset.waveBound = "1";
      el.addEventListener("timeupdate", tick);
      el.addEventListener("seeked", tick);
      el.addEventListener("loadedmetadata", tick);
    });
  }

  function bindMp() {
    const close = $("#mpClose");
    if (!close) return;
    close.onclick = mpClose;
    $("#mpPrev").onclick = () => mpStep(-1);
    $("#mpNext").onclick = () => mpStep(1);
    $("#mplay").addEventListener("click", (e) => {
      if (e.target.id === "mplay") mpClose();
    });
    $("#mpStrip").addEventListener("click", (e) => {
      const b = e.target.closest("[data-mp]");
      if (!b) return;
      const i = MP.ids.indexOf(Number(b.dataset.mp));
      if (i >= 0) { MP.idx = i; mpShow(); }
    });
    $("#mpOpen").onclick = async () => {
      const row = (M.byId || {})[MP.ids[MP.idx]] || {};
      if (!row.path) return;
      const r = await post("/api/open_path", { path: row.path });
      if (!r.ok) toast(esc(r.error || "打不开"), "err");
    };
    $("#mpPath").onclick = async () => {
      const row = (M.byId || {})[MP.ids[MP.idx]] || {};
      if (!row.path) return;
      try { await post("/api/reveal", { path: row.path }); } catch (_) {}
    };
    waveBind();
    document.addEventListener("keydown", (e) => {
      if ($("#mplay").hidden) return;
      if (e.key === "Escape") mpClose();
      else if (e.key === "ArrowLeft") mpStep(-1);
      else if (e.key === "ArrowRight") mpStep(1);
    });
  }

  function mpClose() {
    $("#mplay").hidden = true;
    waveClear();
    const v = $("#mpVideo");
    const a = $("#mpAudio");
    try { v.pause(); a.pause(); } catch (_) {}
    v.removeAttribute("src");
    a.removeAttribute("src");
    MP.ids = [];
  }

  /** 图片类"其他文件"的预览：能读出来就显示图，读不出来给提示 */
  async function openImageFile(item) {
    pvId = item.id || 0;
    pvFile = item.path || '';
    $("#preview").hidden = false;
    $("#pvTitle").textContent = item.name || "预览";
    $("#pvMeta").textContent = item.err || "";
    $("#pvFoot").textContent = item.path || "";
    const body = $("#pvBody");
    body.innerHTML = '<div class="muted" style="padding:20px">加载中…</div>';
    if (!item.id) {
      body.innerHTML = '<div class="muted" style="padding:20px">这个文件没有索引 id，只能用「系统程序」打开。</div>';
      return;
    }
    const src = `/api/preview?id=${item.id}&max=1600`;
    const probe = new Image();
    probe.onload = () => {
      body.innerHTML = "";
      body.appendChild(probe);
      probe.style.cssText = "max-width:100%;max-height:78vh;display:block;margin:0 auto;border-radius:8px";
    };
    probe.onerror = () => {
      body.innerHTML = `<div class="muted" style="padding:20px">这张图读不出来（文件可能损坏，或后缀和内容不符）。<br>
        可以点下面的「打开所在文件夹」，或者用系统程序打开。</div>`;
    };
    probe.src = src;
  }

  async function openPreview(id) {
    pvId = id;
    pvFile = '';
    $("#pvTitle").textContent = "读取中…";
    $("#pvMeta").textContent = "";
    $("#pvBody").textContent = "";
    $("#preview").hidden = false;
    try {
      const r = await api(`/api/media/text?id=${id}&limit=8000`);
      if (!r.ok) { $("#pvTitle").textContent = "打不开"; $("#pvBody").textContent = r.error || ""; return; }
      $("#pvTitle").textContent = r.name;
      $("#pvMeta").textContent =
        `${fmtInt(r.chapters)} 章 · ${fmtInt(r.words)} 字 · 编码 ${r.enc}` +
        (r.removed_lines ? ` · 已忽略 ${fmtInt(r.removed_lines)} 行广告` : "");
      $("#pvBody").textContent = r.text + (r.truncated ? "\n\n……（仅显示前 8000 字）" : "");
      pvFile = r.path || '';
      $("#pvFoot").textContent = r.path || '';
    } catch (e) {
      $("#pvTitle").textContent = "读取失败";
      $("#pvBody").textContent = e.message;
    }
  }



  /* ---------------- 暴力破解（独立子页） ---------------- */
  const BR = { poll: null };

  async function brLoadHashcat() {
    try {
      const r = await api('/api/brute/hashcat');
      state.hashcatOk = !!r.available;
      state.gpuSpeed = Number(r.speed) || 0;
      const el = $('#brHashcat');
      if (!el) return;
      if (r.available) {
        const sp = state.gpuSpeed ? ` · 约 ${(state.gpuSpeed / 1e6).toFixed(0)} MH/s` : '';
        el.textContent = `⚡ GPU：${r.devices[0] || 'hashcat'}（v${r.version}${sp}）`;
        el.style.color = '#6ede7f';
      } else {
        el.textContent = `CPU 模式：${r.error || 'hashcat 未就绪'}`;
        el.style.color = '#e3b65b';
      }
      el.title = r.exe || '';
      brEstimate();
    } catch (_) { state.hashcatOk = false; }
  }

  async function brLoadFound() {
    try {
      const r = await api('/api/unzip/found');
      const list = r.found || [];
      if (!list.length) {
        $('#brFoundN').textContent = '0';
        $('#brFound').innerHTML = '<span class="muted">还没有破解记录</span>';
        return;
      }
      $('#brFoundN').textContent = `共 ${list.length} 条`;
      $('#brFound').innerHTML = list
        .map((f) => `<span class="uz-pw-chip" title="${esc(f.src || '')} ${esc(f.at || '')}">${esc(f.pwd)}</span>`)
        .join('');
    } catch (_) { /* 忽略 */ }
  }

  /* --------- 破解页：配置 / 预估 / 启动 / 进度 --------- */

  function brWords() {
    return $('#brPwd').value.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
  }

  function brBruteCfg() {
    const mode = $('#brMode').value;
    const pre = $('#brPre').value;
    const suf = $('#brSuf').value;
    const cfg = {
      workers: 4,
      prefix: pre,
      suffix: suf,
      max_minutes: Number($('#brMaxMin').value) || 0,
      rules: {
        case: $('#brRuleCase').checked,
        leet: $('#brRuleLeet').checked,
        digits: $('#brRuleDigits').checked ? (Number($('#brRuleDigitN').value) || 0) : 0,
      },
    };
    const els = [];
    if (mode !== 'dict') {
      if (pre) els.push({ type: 'literal', value: pre });
      if (mode === 'mask') {
        const lo = Math.max(1, Number($('#brMinLen').value) || 1);
        const hi = Math.max(lo, Number($('#brMaxLen').value) || lo);
        els.push({ type: 'charset', charset: $('#brCharset').value, len_min: lo, len_max: hi });
      } else {
        const n = Math.max(1, Number($('#brLen').value) || 6);
        els.push({ type: 'num', from: 0, to: Math.pow(10, n) - 1, width: 0 });
      }
      if (suf) els.push({ type: 'literal', value: suf });
      cfg.elements = els;
      cfg.auto = true;
    } else {
      cfg.auto = false;
    }
    return cfg;
  }

  function fmtDur(sec) {
    const n = Number(sec) || 0;
    if (n <= 0) return '不到 1 秒';
    if (n < 1) return '不到 1 秒';
    if (n < 90) return `${Math.round(n)} 秒`;
    if (n < 5400) return `${Math.round(n / 60)} 分钟`;
    return `${(n / 3600).toFixed(1)} 小时`;
  }

  function brModeRows() {
    const mode = $('#brMode').value;
    $('#brDigitsRow').hidden = !(mode === 'auto' || mode === 'digits');
    $('#brMaskRow').hidden = mode !== 'mask';
    $('#brAffixRow').hidden = mode === 'dict';
    $('#brDictRow').hidden = false;
    const tips = {
      auto: '先试密码本（含变形），再按位数穷举 —— 不知道密码长什么样就选这个',
      dict: '只试密码本，最快；适合知道大概是什么词',
      digits: '纯数字，1 位到 N 位都试',
      mask: '自选字符集和长度，组合会爆炸，长度别开太大',
    };
    $('#brTip').textContent = tips[mode] || '';
    const nSel = $('#brRuleDigitN');
    if (nSel) nSel.disabled = !$('#brRuleDigits').checked;
  }

  let brEstTimer = null;
  function brEstimate() {
    clearTimeout(brEstTimer);
    brEstTimer = setTimeout(async () => {
      const box = $('#brSpace');
      if (!box) return;
      const cfg = brBruteCfg();
      const pws = brWords();
      try {
        const r = await post('/api/brute/plan',
          { passwords: pws, brute: cfg, src: $('#brSrc').value.trim() });
        if (!r.ok) { box.textContent = '预估失败'; return; }
        const dictPart = r.dict_count
          ? `密码本 ${fmtInt(r.dict_count)} 条` +
            (r.dict_variants > r.dict_count ? `（变形后 ${fmtInt(r.dict_variants)}）` : '')
          : '';
        const brutePart = r.brute_count
          ? `穷举 <b>${fmtInt(r.brute_count)}</b> 个${r.label ? '（' + esc(r.label) + '）' : ''}`
          : '';
        const parts = [dictPart, brutePart].filter(Boolean);
        if (!parts.length) {
          box.innerHTML = '还没有要试的候选：填密码本，或把「怎么试」换成穷举方式';
          return;
        }
        let tail = '';
        if (r.total > 0) {
          const eta = r.gpu ? r.eta_gpu : r.eta_cpu;
          tail = r.gpu
            ? ` · 走显卡约 <b>${fmtDur(eta)}</b>`
            : ` · 纯 CPU 约 ${fmtDur(eta)}`;
        }
        box.innerHTML = parts.join(' ＋ ') +
          (r.total ? ` ＝ 共 <b>${fmtInt(r.total)}</b> 个候选` : '') + tail;
      } catch (_) { box.textContent = '预估失败（服务没起？）'; }
    }, 260);
  }

  async function brStart() {
    const src = $('#brSrc').value.trim();
    if (!src) { toast('请先选目标文件夹或压缩包', 'err'); return; }
    const pws = brWords();
    const cfg = brBruteCfg();
    if (!pws.length && !cfg.elements) {
      toast('密码本和穷举都是空的：先在「怎么试」里选个方式', 'err', 3200);
      return;
    }
    try {
      const r = await post('/api/brute/start', {
        src, passwords: pws, brute: cfg, recursive: $('#brRecursive').checked,
        dict_files: dictsSelected('br'),
      });
      if (!r.ok) { toast(esc(r.error || '启动失败'), 'err', 4000); return; }
      toast('开始破解…', '', 2200);
      $('#brLog').textContent = '';
      const box = $('#brLogBox');
      if (box) box.open = true;
      $('#brProgress').hidden = false;
      brPoll();
    } catch (e) { toast('启动失败：' + esc(e.message), 'err'); }
  }

  function brRenderResults(list) {
    const ok = (list || []).filter((x) => x && x.ok);
    if (!ok.length) {
      $('#brFoundN').textContent = '0';
      $('#brFound').innerHTML = '<span class="muted">本次还没有破解出密码</span>';
      return;
    }
    $('#brFoundN').textContent = `本次成功 ${ok.length} 个`;
    $('#brFound').innerHTML = ok.map((x) => {
      const meta = [x.name, x.engine, x.seconds ? `${x.seconds}s` : '']
        .filter(Boolean).map((v) => esc(String(v))).join(' · ');
      return `<div class="br-row-res"><span class="pw">${esc(x.pwd)}</span>
        <span class="muted">${meta}</span>
        <button class="br-copy" data-pw="${esc(x.pwd)}">复制</button></div>`;
    }).join('');
  }

  let brPaused = false;

  async function brPauseToggle() {
    try {
      const want = brPaused ? 'resume' : 'pause';
      const r = await post(`/api/brute/${want}`, {});
      if (!r.ok) { toast(esc(r.error || '操作失败'), 'err'); return; }
      brPaused = !brPaused;
      $('#brPause').textContent = brPaused ? '继续' : '暂停';
      toast(brPaused ? '已暂停（进度已保存）' : '继续破解', 'ok', 2500);
      if (!brPaused) brPoll();
    } catch (e) { toast('操作失败：' + esc(e.message), 'err'); }
  }

  function brPoll() {
    clearTimeout(BR.poll);
    const tick = async () => {
      try {
        const s = await api('/api/brute/status');
        if (s.state === 'idle') return;
        const el = $('#brProgress');
        el.hidden = false;
        const pct = s.total ? Math.round(100 * s.done / s.total) : 0;
        const paused = !!(s.running && s.paused);
        brPaused = paused;
        el.innerHTML = (paused ? '已暂停' : (s.running ? '破解中' : '已结束')) +
          ` ${fmtInt(s.done)}/${fmtInt(s.total)} 个包（${pct}%）· ` +
          `成功 ${fmtInt(s.found)} · 失败 ${fmtInt(s.failed)} · 用时 ${fmtInt(Math.round(s.elapsed))}s` +
          `<div class="bar"><i style="width:${pct}%"></i></div>`;
        $('#brStart').hidden = s.running;
        $('#brPause').hidden = !s.running;
        $('#brPause').textContent = paused ? '继续' : '暂停';
        $('#brStop').hidden = !s.running;
        if (s.log && s.log.length) {
          const box = $('#brLog');
          box.textContent = s.log.join('\n');
          box.scrollTop = box.scrollHeight;
        }
        if (s.results && s.results.length) brRenderResults(s.results);
        if (s.running) BR.poll = setTimeout(tick, 700);
        else { toast(`破解结束：成功 ${fmtInt(s.found)} 个`, 'ok', 4000); brLoadFound(); }
      } catch (_) { /* 忽略 */ }
    };
    tick();
  }

  /* ---------------- 已知明文攻击（bkcrack） ---------------- */
  const PL = { poll: null };

  async function plStart() {
    const zip = $('#plZip').value.trim();
    if (!zip) { toast('先选要攻的 zip 压缩包', 'err'); return; }
    const auto = $('#plAuto').checked;
    const text = $('#plText').value;
    const file = $('#plFile').value.trim();
    if (!auto && !file && text.trim().length < 12) {
      toast('已知明文至少要 12 字节（粘贴文字、选个文件，或勾上「自动猜文件头」）', 'err', 3800);
      return;
    }
    try {
      const r = await post('/api/plain/start', {
        zip, plain_text: text, plain_file: file, auto,
        offset: Number($('#plOffset').value) || 0,
        recover_password: $('#plRecover').checked,
      });
      if (!r.ok) { toast(esc(r.error || '启动失败'), 'err', 4000); return; }
      $('#plStart').hidden = true;
      $('#plStop').hidden = false;
      $('#plResult').hidden = true;
      $('#plLog').hidden = false;
      $('#plLog').textContent = '';
      plPoll();
    } catch (e) { toast('启动失败：' + esc(e.message), 'err'); }
  }

  function plPoll() {
    clearTimeout(PL.poll);
    const tick = async () => {
      try {
        const s = await api('/api/plain/status');
        if (s.state === 'idle') return;
        const box = $('#plLog');
        if (s.log && s.log.length) {
          box.textContent = s.log.join('\n');
          box.scrollTop = box.scrollHeight;
        }
        const pct = s.total ? Math.round(100 * s.done / s.total) : 0;
        $('#plStatus').textContent = s.running
          ? `跑着：第 ${fmtInt(s.done + 1)}/${fmtInt(s.total)} 个条目（${pct}%）· ${fmtInt(Math.round(s.elapsed))}s`
          : (s.message || '结束');
        if (s.running) {
          PL.poll = setTimeout(tick, 800);
          return;
        }
        $('#plStart').hidden = false;
        $('#plStop').hidden = true;
        const r = s.result || {};
        const el = $('#plResult');
        if (r.keys) {
          el.hidden = false;
          el.innerHTML = `✅ 找到密钥 <b>${esc(r.keys.join(' '))}</b>` +
            (r.password ? ` · 原密码 <b>${esc(r.password)}</b>` : '') +
            `<br>已生成无密码副本：<span class="muted">${esc(r.out || '')}</span>` +
            (r.verified ? '' : ' <span class="muted">（校验没过，只解对了一部分）</span>');
          toast('明文攻击成功', 'ok', 4000);
        } else {
          el.hidden = true;
          toast('明文攻击没成功：' + esc(s.message || ''), 'err', 5000);
        }
      } catch (_) { /* 忽略 */ }
    };
    tick();
  }

  /* ---------------- 打包流程（步骤可编辑） ---------------- */
  const PK = {
    poll: null,
    steps: [],
    editIndex: -1,
    drafts: {},
  };

  const PK_FMT_LABELS = {
    zip: 'zip（最通用）', '7z': '7z（压缩率高，需 7-Zip）', tar: 'tar（只打包不压缩）',
    'tar.gz': 'tar.gz', 'tar.bz2': 'tar.bz2', 'tar.xz': 'tar.xz',
  };
  const PK_LEVEL_LABELS = {
    0: '不压缩（最快）', 1: '快（压缩率低）', 6: '标准', 9: '更小（压缩率高，慢）',
  };

  function pkDefaultSteps() {
    return [{ type: 'pack', fmt: 'zip', enc: 'none', password: '', multi: false, level: 6, each_file: false }];
  }

  function pkFmtName(fmt) { return PK_FMT_LABELS[fmt] || fmt || 'zip'; }
  function pkLevelName(lv) { return PK_LEVEL_LABELS[lv] || ('级别 ' + lv); }

  function pkLabel(type) {
    return ({
      pack: '压缩', merge: '合并再压一次', split: '拆分',
      rename: '改输出名', flatten: '拍平嵌套',
    })[type] || type;
  }

  function pkDescribe(step) {
    if (step.type === 'pack') {
      const encMap = { none: '不加密', zipcrypto: 'ZipCrypto', aes: 'AES-256' };
      const bits = [pkFmtName(step.fmt || 'zip')];
      bits.push(encMap[step.enc] || '不加密');
      if (step.enc !== 'none') bits.push(step.password ? '有密码' : '缺密码');
      if (step.multi) bits.push('多层');
      bits.push(pkLevelName(step.level));
      if (step.each_file) bits.push('每个子项各一包');
      return `压缩（${bits.join('，')}）`;
    }
    if (step.type === 'merge') {
      const encMap = { none: '不加密', zipcrypto: 'ZipCrypto', aes: 'AES-256' };
      const bits = [pkFmtName(step.fmt || 'zip'), encMap[step.enc] || '不加密'];
      if (step.enc && step.enc !== 'none') bits.push(step.password ? '有密码' : '缺密码');
      bits.push(pkLevelName(step.level));
      return `合并再压一次` + (step.name ? `（${step.name}）` : '') +
        `：${bits.join('，')}`;
    }
    if (step.type === 'split') {
      if ((step.mode || 'count') === 'count') {
        const bits = [`按数量 ${step.parts || 2} 份`];
        if (step.min_size_mb) bits.push(`小于 ${step.min_size_mb}MB 不拆`);
        if (step.max_size_mb) bits.push(`每卷超 ${step.max_size_mb}MB 额外拆`);
        return `拆分（${bits.join('，')}）`;
      }
      const bits = [`按体积每卷 ${step.size_mb || 700} MB`];
      if (step.min_parts) bits.push(`至少 ${step.min_parts} 份`);
      if (step.max_parts) bits.push(`不超过 ${step.max_parts} 份`);
      return `拆分（${bits.join('，')}）`;
    }
    if (step.type === 'rename') return `改输出名（${step.pattern}）`;
    return pkLabel(step.type);
  }

  function pkRenderSteps() {
    const box = $('#pkSteps');
    if (!box) return;
    if (!PK.steps.length) {
      box.innerHTML = '<li class="muted">流程是空的，点下面「+ 添加步骤」</li>';
    } else {
      box.innerHTML = PK.steps.map((s, i) => `
        <li data-i="${i}">
          <span class="desc">${i + 1}. ${esc(pkDescribe(s))}</span>
          <button class="ghost mini" data-act="up" ${i === 0 ? 'disabled' : ''}>↑</button>
          <button class="ghost mini" data-act="down" ${i === PK.steps.length - 1 ? 'disabled' : ''}>↓</button>
          <button class="ghost mini" data-act="edit">编辑</button>
          <button class="ghost mini" data-act="del">删除</button>
        </li>`).join('');
    }
    box.querySelectorAll('button[data-act]').forEach((b) => {
      b.onclick = () => {
        const i = Number(b.closest('li').dataset.i);
        const act = b.dataset.act;
        if (act === 'del') { PK.steps.splice(i, 1); pkRenderSteps(); }
        else if (act === 'up' && i > 0) {
          [PK.steps[i - 1], PK.steps[i]] = [PK.steps[i], PK.steps[i - 1]];
          pkRenderSteps();
        } else if (act === 'down' && i < PK.steps.length - 1) {
          [PK.steps[i + 1], PK.steps[i]] = [PK.steps[i], PK.steps[i + 1]];
          pkRenderSteps();
        } else if (act === 'edit') { pkOpenForm(PK.steps[i].type, i); }
      };
    });
    pkSummary();
  }

  function pkSummary() {
    const el = $('#pkSummary');
    if (!el) return;
    if (!PK.steps.length) { el.textContent = '流程为空'; return; }
    el.textContent = '共 ' + PK.steps.length + ' 步：' +
      PK.steps.map((s) => pkLabel(s.type)).join(' → ');
  }

  function pkFormHtml(type, draft) {
    const d = draft || {};
    if (type === 'pack') {
      const fmts = Object.entries(PK_FMT_LABELS)
        .map(([k, v]) => `<option value="${k}">${v}</option>`).join('');
      const levels = Object.entries(PK_LEVEL_LABELS)
        .map(([k, v]) => `<option value="${k}">${v}</option>`).join('');
      return `
        <label class="chk"><span>格式</span><select id="fFmt">${fmts}</select></label>
        <label class="chk"><span>压缩级别</span><select id="fLevel">${levels}</select></label>
        <label class="chk"><span>加密</span>
          <select id="fEnc">
            <option value="none">不加密</option>
            <option value="zipcrypto">ZipCrypto（兼容性好）</option>
            <option value="aes">AES-256（更安全）</option>
          </select></label>
        <input id="fPwd" type="text" placeholder="密码" autocomplete="off">
        <label class="chk"><input type="checkbox" id="fMulti"><span>多层压缩（子文件夹各成一包再套进来）</span></label>
        <label class="chk"><input type="checkbox" id="fEachFile"><span>每个子项各打一包（配合"合并"使用）</span></label>
        <span class="muted" id="fHint"></span>`;
    }
    if (type === 'merge') {
      const fmts = Object.entries(PK_FMT_LABELS)
        .map(([k, v]) => `<option value="${k}">${v}</option>`).join('');
      const levels = Object.entries(PK_LEVEL_LABELS)
        .map(([k, v]) => `<option value="${k}">${v}</option>`).join('');
      return `
        <label class="chk"><span>包名</span>
          <input id="fName" type="text" placeholder="留空就是「合并包」"></label>
        <label class="chk"><span>格式</span><select id="mFmt">${fmts}</select></label>
        <label class="chk"><span>压缩级别</span><select id="mLevel">${levels}</select></label>
        <label class="chk"><span>加密</span>
          <select id="mEnc">
            <option value="none">不加密</option>
            <option value="zipcrypto">ZipCrypto（兼容性好）</option>
            <option value="aes">AES-256（更安全）</option>
          </select></label>
        <input id="mPwd" type="text" placeholder="密码" autocomplete="off">
        <span class="muted" id="mHint">合并时会用这里的格式和密码重新压一次</span>`;
    }
    if (type === 'split') {
      return `
        <label class="chk"><span>拆分方式</span>
          <select id="fSplitMode">
            <option value="count">按数量拆（分成几份）</option>
            <option value="size">按体积拆（每卷多大）</option>
          </select></label>
        <span id="fCountBox" class="pk-inline">
          <label class="chk"><span>份数</span>
            <input id="fParts" type="number" min="2" step="1" value="3"></label>
          <label class="chk"><input type="checkbox" id="fMinSizeOn"><span>小于</span>
            <input id="fMinSize" type="number" min="0" step="1" value="500" disabled> <span class="muted">MB 就不拆</span></label>
          <label class="chk"><input type="checkbox" id="fMaxSizeOn"><span>每卷超过</span>
            <input id="fMaxSize" type="number" min="0" step="1" value="700" disabled> <span class="muted">MB 再额外拆</span></label>
        </span>
        <span id="fSizeBox" class="pk-inline" hidden>
          <label class="chk"><span>每卷</span>
            <input id="fSize" type="number" min="1" step="1" value="700"> <span class="muted">MB</span></label>
          <label class="chk"><input type="checkbox" id="fMinPartsOn"><span>至少</span>
            <input id="fMinParts" type="number" min="2" step="1" value="2" disabled> <span class="muted">份</span></label>
          <label class="chk"><input type="checkbox" id="fMaxPartsOn"><span>不超过</span>
            <input id="fMaxParts" type="number" min="2" step="1" value="10" disabled> <span class="muted">份</span></label>
        </span>
        <span class="muted" id="fSplitHint">按份数平均切；包小于 1KB 不拆</span>`;
    }
    return '<span class="muted">这一步没有参数</span>';
  }

  function pkOpenForm(type, index) {
    PK.editIndex = typeof index === 'number' ? index : -1;
    const draft = PK.editIndex >= 0 ? PK.steps[PK.editIndex] : (PK.drafts[type] || {});
    const box = $('#pkForm');
    box.hidden = false;
    box.innerHTML = pkFormHtml(type, draft) +
      `<button id="fSave" class="primary mini">${PK.editIndex >= 0 ? '保存' : '添加'}</button>
       <button id="fCancel" class="ghost mini">取消</button>`;
    const enc = $('#fEnc');
    if (enc) {
      $('#fFmt').value = draft.fmt || 'zip';
      $('#fLevel').value = String(draft.level == null ? 6 : draft.level);
      enc.value = draft.enc || 'none';
      $('#fPwd').value = draft.password || '';
      $('#fMulti').checked = !!draft.multi;
      $('#fEachFile').checked = !!draft.each_file;
      pkSyncFmt();
      $('#fFmt').addEventListener('change', pkSyncFmt);
    }
    if ($('#fName')) $('#fName').value = draft.name || '';
    if ($('#mFmt')) {
      const prev = pkLastPack();
      $('#mFmt').value = draft.fmt || prev.fmt || 'zip';
      $('#mLevel').value = String(draft.level == null ? (prev.level == null ? 6 : prev.level) : draft.level);
      $('#mEnc').value = draft.enc || prev.enc || 'none';
      $('#mPwd').value = draft.password || prev.password || '';
      pkSyncEnc($('#mFmt'), $('#mEnc'), $('#mPwd'), $('#mHint'));
      $('#mFmt').addEventListener('change',
        () => pkSyncEnc($('#mFmt'), $('#mEnc'), $('#mPwd'), $('#mHint')));
    }
    if ($('#fSize')) $('#fSize').value = draft.size_mb || 700;
    if ($('#fParts')) $('#fParts').value = draft.parts || 3;
    if ($('#fMinSize')) {
      const on = !!draft.min_size_mb;
      $('#fMinSizeOn').checked = on;
      $('#fMinSize').value = draft.min_size_mb || 500;
      $('#fMinSize').disabled = !on;
    }
    if ($('#fMaxSize')) {
      const on = !!draft.max_size_mb;
      $('#fMaxSizeOn').checked = on;
      $('#fMaxSize').value = draft.max_size_mb || 700;
      $('#fMaxSize').disabled = !on;
    }
    if ($('#fMinParts')) {
      const on = !!draft.min_parts;
      $('#fMinPartsOn').checked = on;
      $('#fMinParts').value = draft.min_parts || 2;
      $('#fMinParts').disabled = !on;
    }
    if ($('#fMaxParts')) {
      const on = !!draft.max_parts;
      $('#fMaxPartsOn').checked = on;
      $('#fMaxParts').value = draft.max_parts || 10;
      $('#fMaxParts').disabled = !on;
    }
    if ($('#fSplitMode')) {
      $('#fSplitMode').value = draft.mode || (draft.parts ? 'count' : 'size');
      pkSyncSplit();
      pkSyncSplitToggles();
      $('#fSplitMode').addEventListener('change', pkSyncSplit);
    }
    if ($('#fPattern')) $('#fPattern').value = draft.pattern || '{name}_{date}';
    $('#fSave').onclick = () => {
      const step = pkReadForm(type);
      if (!step) return;
      if (PK.editIndex >= 0) PK.steps[PK.editIndex] = step;
      else PK.steps.push(step);
      box.hidden = true;
      pkRenderSteps();
    };
    $('#fCancel').onclick = () => { box.hidden = true; };
  }

  function pkSyncSplitToggles() {
    const pairs = [['#fMinSizeOn', '#fMinSize'], ['#fMaxSizeOn', '#fMaxSize'],
                   ['#fMinPartsOn', '#fMinParts'], ['#fMaxPartsOn', '#fMaxParts']];
    pairs.forEach(([cb, inp]) => {
      const c = $(cb);
      const i = $(inp);
      if (!c || !i) return;
      const sync = () => { i.disabled = !c.checked; };
      c.onchange = sync;
      sync();
    });
  }

  function pkSyncSplit() {
    const mode = $('#fSplitMode').value;
    const countMode = mode === 'count';
    const cbox = $('#fCountBox');
    const sbox = $('#fSizeBox');
    if (cbox) cbox.hidden = !countMode;
    if (sbox) sbox.hidden = countMode;
    const hint = $('#fSplitHint');
    if (hint) {
      hint.textContent = countMode
        ? '按份数平均切；包小于 1KB 不拆'
        : '按每卷大小切；至少/不超过用来卡份数，0 表示不限';
    }
  }

  function pkLastPack() {
    for (let i = PK.steps.length - 1; i >= 0; i -= 1) {
      if (PK.steps[i].type === 'pack') return PK.steps[i];
    }
    return {};
  }

  /* 格式 → 加密选项联动（压缩步骤和合并步骤共用） */
  function pkSyncEnc(fmtSel, encSel, pwdEl, hintEl) {
    if (!fmtSel || !encSel) return;
    const fmt = fmtSel.value;
    const isTar = fmt.startsWith('tar');
    const is7z = fmt === '7z';
    const want = encSel.value || 'none';
    if (isTar) {
      encSel.innerHTML = '<option value="none">tar 不支持加密</option>';
      encSel.value = 'none';
      encSel.disabled = true;
      if (pwdEl) { pwdEl.value = ''; pwdEl.disabled = true; }
    } else {
      encSel.disabled = false;
      if (is7z) {
        encSel.innerHTML = '<option value="none">不加密</option>' +
          '<option value="aes">有密码（AES-256，含文件名加密）</option>';
        encSel.value = (want === 'aes' || want === 'zipcrypto') ? 'aes' : 'none';
      } else {
        encSel.innerHTML = '<option value="none">不加密</option>' +
          '<option value="zipcrypto">ZipCrypto（兼容性好）</option>' +
          '<option value="aes">AES-256（更安全）</option>';
        encSel.value = ['none', 'zipcrypto', 'aes'].indexOf(want) >= 0 ? want : 'none';
      }
      if (pwdEl) pwdEl.disabled = encSel.value === 'none';
      encSel.onchange = () => { if (pwdEl) pwdEl.disabled = encSel.value === 'none'; };
    }
    if (hintEl) {
      hintEl.textContent = is7z ? '7z 走系统 7-Zip；不支持多层压缩'
        : (isTar ? 'tar 只是打包，不压缩也不加密' : '');
    }
  }

  function pkSyncFmt() {
    const fmt = $('#fFmt').value;
    const multi = $('#fMulti');
    pkSyncEnc($('#fFmt'), $('#fEnc'), $('#fPwd'), $('#fHint'));
    if (multi) {
      multi.disabled = fmt === '7z';
      if (multi.disabled) multi.checked = false;
    }
  }

  function pkReadForm(type) {
    if (type === 'pack') {
      const fmt = $('#fFmt').value;
      const enc = $('#fEnc').value;
      const password = $('#fPwd').value;
      if (enc !== 'none' && !password) { toast('加密要先填密码', 'err'); return null; }
      if (fmt.startsWith('tar') && enc !== 'none') {
        toast('tar 系列不支持加密，换 zip 或 7z', 'err'); return null;
      }
      if (fmt === '7z' && $('#fMulti').checked) {
        toast('7z 暂不支持多层压缩，多层请用 zip', 'err'); return null;
      }
      return { type, fmt, enc, password, multi: $('#fMulti').checked,
               each_file: $('#fEachFile').checked,
               level: Number($('#fLevel').value) };
    }
    if (type === 'merge') {
      const fmt = $('#mFmt').value;
      const enc = $('#mEnc').value;
      const password = $('#mPwd').value;
      if (enc !== 'none' && !password) { toast('加密要先填密码', 'err'); return null; }
      if (fmt.startsWith('tar') && enc !== 'none') {
        toast('tar 系列不支持加密，换 zip 或 7z', 'err'); return null;
      }
      return { type, name: $('#fName').value.trim(), fmt, enc, password,
               level: Number($('#mLevel').value) };
    }
    if (type === 'split') {
      const mode = $('#fSplitMode').value;
      if (mode === 'count') {
        const k = Number($('#fParts').value) || 0;
        if (k < 2) { toast('份数至少 2', 'err'); return null; }
        const step = { type, mode, parts: Math.round(k) };
        if ($('#fMinSizeOn').checked) {
          const v = Number($('#fMinSize').value) || 0;
          if (v > 0) step.min_size_mb = v;
        }
        if ($('#fMaxSizeOn').checked) {
          const v = Number($('#fMaxSize').value) || 0;
          if (v > 0) step.max_size_mb = v;
        }
        return step;
      }
      const n = Number($('#fSize').value) || 0;
      if (n <= 0) { toast('每卷大小要大于 0', 'err'); return null; }
      const step = { type, mode, size_mb: n };
      if ($('#fMinPartsOn').checked) {
        const v = Math.round(Number($('#fMinParts').value) || 0);
        if (v >= 2) step.min_parts = v;
      }
      if ($('#fMaxPartsOn').checked) {
        const v = Math.round(Number($('#fMaxParts').value) || 0);
        if (v >= 2) step.max_parts = v;
      }
      return step;
    }
    if (type === 'rename') {
      const p = $('#fPattern').value.trim() || '{name}';
      return { type, pattern: p };
    }
    return { type };
  }

  async function pkRun() {
    const src = $('#pkSrc').value.trim();
    const dst = $('#pkDst').value.trim();
    if (!src || !dst) { toast('先选输入和输出文件夹', 'err'); return; }
    if (!PK.steps.length) { toast('流程是空的，先加一步「压缩」', 'err'); return; }
    try {
      const r = await post('/api/pack/start', {
        src, dst, steps: PK.steps, mode: $('#pkMode').value,
      });
      if (!r.ok) { toast(esc(r.error || '启动失败'), 'err', 4200); return; }
      $('#pkStart').hidden = true;
      $('#pkStop').hidden = false;
      $('#pkProgress').hidden = false;
      $('#pkLog').textContent = '';
      pkPoll();
    } catch (e) { toast('启动失败：' + esc(e.message), 'err'); }
  }

  function pkPoll() {
    clearTimeout(PK.poll);
    const tick = async () => {
      try {
        const s = await api('/api/pack/status');
        if (s.state === 'idle') return;
        const pct = s.total ? Math.round(100 * s.done / s.total) : 0;
        const el = $('#pkProgress');
        el.hidden = false;
        el.innerHTML = (s.running ? '运行中' : '已结束') +
          `${s.stage ? ' · ' + esc(s.stage) : ''} · 包 ${fmtInt(s.archives)} 个 · ` +
          `文件 ${fmtInt(s.parts)} 个 · ${fmtBytes(s.bytes)} · 失败 ${fmtInt(s.failed)} · ` +
          `用时 ${fmtInt(Math.round(s.elapsed))}s` +
          `<div class="bar"><i style="width:${pct}%"></i></div>`;
        if (s.log && s.log.length) {
          const box = $('#pkLog');
          box.textContent = s.log.join('\n');
          box.scrollTop = box.scrollHeight;
        }
        if (s.running) { PK.poll = setTimeout(tick, 700); return; }
        $('#pkStart').hidden = false;
        $('#pkStop').hidden = true;
        toast(`流程结束：${fmtInt(s.archives)} 个包`, 'ok', 4000);
      } catch (_) { /* 忽略 */ }
    };
    tick();
  }

  /* ---------------- 繁简 / 编码转换 ---------------- */
  const CV = { poll: null };

  async function cvStart() {
    const src = $('#cvSrc').value.trim();
    const dst = $('#cvDst').value.trim();
    if (!src || !dst) { toast('先选输入和输出文件夹', 'err'); return; }
    try {
      const r = await post('/api/conv/start', {
        src, dst,
        t2s: $('#cvT2S').checked,
        out_enc: $('#cvEnc').value,
        newline: $('#cvNewline').value,
        recursive: $('#cvRecursive').checked,
      });
      if (!r.ok) { toast(esc(r.error || '启动失败'), 'err', 4000); return; }
      $('#cvStart').hidden = true;
      $('#cvStop').hidden = false;
      $('#cvProgress').hidden = false;
      $('#cvLog').textContent = '';
      cvPoll();
    } catch (e) { toast('启动失败：' + esc(e.message), 'err'); }
  }

  function cvPoll() {
    clearTimeout(CV.poll);
    const tick = async () => {
      try {
        const s = await api('/api/conv/status');
        if (s.state === 'idle') return;
        const pct = s.total ? Math.round(100 * s.done / s.total) : 0;
        const el = $('#cvProgress');
        el.hidden = false;
        el.innerHTML = (s.running ? '转换中' : '已结束') +
          ` ${fmtInt(s.done)}/${fmtInt(s.total)} 个（${pct}%）· 已写 ${fmtInt(s.files)} 个 · ` +
          `内容有变化 ${fmtInt(s.changed)} · 失败 ${fmtInt(s.failed)} · 用时 ${fmtInt(Math.round(s.elapsed))}s` +
          `<div class="bar"><i style="width:${pct}%"></i></div>`;
        if (s.log && s.log.length) {
          const box = $('#cvLog');
          box.textContent = s.log.join('\n');
          box.scrollTop = box.scrollHeight;
        }
        if (s.running) { CV.poll = setTimeout(tick, 700); return; }
        $('#cvStart').hidden = false;
        $('#cvStop').hidden = true;
        toast(`转换结束：${fmtInt(s.files)} 个文件`, 'ok', 4000);
      } catch (_) { /* 忽略 */ }
    };
    tick();
  }

  async function cvPaste() {
    // 先试浏览器剪贴板（本机 http://127.0.0.1 属于安全上下文），失败就找后台用 PowerShell 读
    let text = "";
    try {
      if (navigator.clipboard && navigator.clipboard.readText) {
        text = await navigator.clipboard.readText();
      }
    } catch (_) { text = ""; }
    if (!text) {
      try {
        const r = await post('/api/clipboard/read', {});
        if (r.ok) text = r.text || '';
        else { toast(esc(r.error || '读不到剪贴板'), 'err', 3500); return; }
      } catch (e) { toast('读剪贴板失败：' + esc(e.message), 'err'); return; }
    }
    if (!text.trim()) { toast('剪贴板里没有文字', 'err'); return; }
    const ta = $('#cvSample');
    ta.value = text;
    toast(`已粘贴 ${fmtInt(text.length)} 个字`, 'ok', 2200);
    cvPreview();
  }

  async function cvCopyOut() {
    const out = $('#cvOut');
    if (out.hidden || !out.textContent) { toast('先点「试一下」再复制', 'err'); return; }
    const txt = out.textContent.split('现在：').pop() || '';
    let done = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(txt);
        done = true;
      }
    } catch (_) { done = false; }
    if (!done) {
      try {
        const r = await post('/api/clipboard/write', { text: txt });
        done = !!r.ok;
      } catch (_) { done = false; }
    }
    toast(done ? '转换结果已复制' : '复制失败', done ? 'ok' : 'err', 2500);
  }

  async function cvPreview() {
    const text = $('#cvSample').value;
    if (!text.trim()) { toast('先粘一段文字', 'err'); return; }
    try {
      const r = await post('/api/conv/preview', { text });
      const out = $('#cvOut');
      out.hidden = false;
      if (!r.ok) { out.textContent = '转换失败：' + (r.error || ''); return; }
      out.textContent = '原来：' + r.before.slice(0, 400) + '\n\n现在：' + r.after.slice(0, 400);
    } catch (e) { toast('预览失败：' + esc(e.message), 'err'); }
  }

  /* ---------------- 字典（解压页 + 破解页共用） ---------------- */
  const Dicts = { local: [], catalog: [], loadedAt: 0 };
  window.__dicts = Dicts;      // 调试用：控制台里能看字典列表

  function fmtSize(n) {
    const b = Number(n) || 0;
    if (b >= 1048576) return (b / 1048576).toFixed(1) + ' MB';
    if (b >= 1024) return Math.round(b / 1024) + ' KB';
    return b + ' B';
  }

  async function dictsRefresh() {
    try {
      const r = await api('/api/dicts');
      window.__dictsRaw = r;
      if (!r.ok) return;
      Dicts.local = r.local || [];
      Dicts.catalog = r.catalog || [];
      dictsRender();
    } catch (e) { window.__dictErr = String((e && e.message) || e); }
  }

  function dictsRender() {
    for (const p of ['uz', 'br']) {
      const sel = document.querySelector('#' + p + 'DictSel');
      if (!sel) continue;
      const keep = new Set(Array.from(sel.selectedOptions).map((o) => o.value));
      sel.innerHTML = Dicts.local.map((d) =>
        `<option value="${esc(d.name)}"${keep.has(d.name) ? ' selected' : ''}>` +
        `${esc(d.name)}（${fmtSize(d.size)}）</option>`).join('');
      const msg = document.querySelector('#' + p + 'DictMsg');
      if (msg && !keep.size && Dicts.local.length) {
        msg.textContent = `${Dicts.local.length} 个可用，选中的会一起参与`;
      }
    }
  }

  function dictsCatalogHtml() {
    if (!Dicts.catalog.length) return '<div class="muted">拿不到字典清单（检查网络）</div>';
    return Dicts.catalog.map((c) => `
      <div class="row">
        <label class="chk"><input type="checkbox" class="dlg-id" value="${esc(c.id)}"
          ${c.downloaded ? 'checked disabled' : ''}><span>${esc(c.name)}</span></label>
        <span class="muted">${fmtSize(c.size)}${c.downloaded ? ' · 已下载' : ''}</span>
        <span class="grow muted">${esc(c.desc || '')}</span>
      </div>`).join('') +
      '<div class="row"><button id="dlgGo" class="primary mini">开始下载</button>' +
      '<span class="muted" id="dlgMsg"></span></div>';
  }

  async function dictsPickExternal() {
    try {
      const r = await post('/api/pick_files', {
        title: '选字典文件（可多选）', initial: Dicts.local[0] ? '' : '',
        filters: '字典|*.txt;*.dic;*.lst;*.wordlist;*.pass;*.csv;*.gz;*.zip;*.bz2;*.xz|所有文件|*.*',
      });
      if (!r.ok) return;
      const paths = r.paths || [r.path];
      const msg = document.querySelector('#uzDictMsg');
      if (msg) msg.textContent = `正在导入 ${paths.length} 个文件…`;
      const imp = await post('/api/dicts/import', { paths });
      await dictsRefresh();
      toast(`已导入 ${fmtInt((imp.copied || []).length)} 个字典` +
        ((imp.skipped || []).length ? `，跳过 ${imp.skipped.length} 个（已存在）` : ''), 'ok', 4000);
    } catch (e) { toast('导入失败：' + esc(e.message), 'err'); }
  }

  async function dictsDeleteSelected() {
    const sel = document.querySelector('#uzDictSel');
    const names = sel ? Array.from(sel.selectedOptions).map((o) => o.value) : [];
    if (!names.length) { toast('先选中要删除的字典', 'err'); return; }
    if (!confirm(`确定从软件目录删除 ${names.length} 个字典文件？`)) return;
    const r = await post('/api/dicts/delete', { names });
    await dictsRefresh();
    toast(`已删除 ${fmtInt((r.deleted || []).length)} 个`, 'ok', 3000);
  }

  async function dictsDownloadStart() {
    const ids = Array.from(document.querySelectorAll('#uzDictCat .dlg-id'))
      .filter((el) => el.checked && !el.disabled).map((el) => el.value);
    if (!ids.length) { toast('先勾选要下载的字典', 'err'); return; }
    const r = await post('/api/dicts/download', { ids });
    if (!r.ok) { toast(esc(r.error || '启动失败'), 'err'); return; }
    const msg = document.querySelector('#dlgMsg');
    if (msg) msg.textContent = '下载中…';
    const t0 = Date.now();
    const tick = async () => {
      try {
        const st = await api('/api/dicts/status');
        if (msg && st.log && st.log.length) msg.textContent = st.log[st.log.length - 1];
        if (st.state === 'running' && Date.now() - t0 < 600000) {
          setTimeout(tick, 1500);
          return;
        }
        await dictsRefresh();
        if (msg) msg.textContent = st.state === 'done' ? '下载完成' : (st.log || []).slice(-1)[0] || '结束';
        toast('字典下载' + (st.state === 'done' ? '完成' : '结束'), 'ok', 3500);
      } catch (_) { /* 忽略 */ }
    };
    setTimeout(tick, 800);
  }

  function dictsSelected(prefix) {
    const sel = document.querySelector('#' + prefix + 'DictSel');
    return sel ? Array.from(sel.selectedOptions).map((o) => o.value) : [];
  }

  async function dictsRefreshHint(prefix) {
    const names = dictsSelected(prefix);
    const msg = document.querySelector('#' + prefix + 'DictMsg');
    if (!msg) return;
    if (!names.length) {
      msg.textContent = Dicts.local.length
        ? `${Dicts.local.length} 个可用，选中的会一起参与` : '还没有字典，可以「读取外部…」或「下载…」';
      return;
    }
    msg.textContent = '正在统计条数…';
    try {
      const r = await post('/api/dicts/load', { entries: names, limit: 200000 });
      if (r.ok) {
        msg.textContent = `已选 ${names.length} 个 · 合计 ${fmtInt(r.total)} 条` +
          (r.truncated ? '（预览上限 20 万）' : '');
      } else {
        msg.textContent = r.error || '统计失败';
      }
    } catch (_) { msg.textContent = '统计失败'; }
  }

  function bindDicts() {
    const sel = $('#uzDictSel');
    if (sel) sel.addEventListener('change', () => dictsRefreshHint('uz'));
    const pick = $('#uzDictPick');
    if (pick) pick.onclick = dictsPickExternal;
    const del = $('#uzDictDel');
    if (del) del.onclick = dictsDeleteSelected;
    const dl = $('#uzDictDl');
    if (dl) {
      dl.onclick = () => {
        const box = $('#uzDictCat');
        if (!box) return;
        box.hidden = !box.hidden;
        if (!box.hidden) {
          box.innerHTML = dictsCatalogHtml();
          const go = $('#dlgGo');
          if (go) go.onclick = dictsDownloadStart;
        }
      };
    }
  }

  /* ---------------- 解压 ---------------- */
  const UZ = { poll: null };

  async function uzStart() {
    const src = $('#uzSrc').value.trim();
    const dst = $('#uzDst').value.trim();
    if (!src || !dst) { toast('请先选好输入和输出文件夹', 'err'); return; }
    const pws = $('#uzPwd').value.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
    try {
      const r = await post('/api/unzip/start', {
        src, dst, passwords: pws, dict_files: dictsSelected('uz'), recursive: $('#uzRecursive').checked,
        auto_brute: false,      // 暴力破解已挪到独立子页
      });
      if (!r.ok) { toast(esc(r.error || '启动失败'), 'err', 4000); return; }
      toast(`开始解压（密码本 ${r.passwords} 条）`, '', 2600);
      $('#uzLog').textContent = '';
      uzPoll();
    } catch (e) { toast('启动失败：' + esc(e.message), 'err'); }
  }

  function uzPoll() {
    clearTimeout(UZ.poll);
    const tick = async () => {
      try {
        const s = await api('/api/unzip/status');
        const el = $('#uzProgress');
        if (s.state === 'idle') { el.hidden = true; return; }
        el.hidden = false;
        const pct = s.total ? Math.round(100 * s.done / s.total) : 0;
        el.innerHTML = `解压中 ${fmtInt(s.done)}/${fmtInt(s.total)}（${pct}%）· ` +
          `包 ${fmtInt(s.archives)} · 文件 ${fmtInt(s.files)} · ` +
          `写出 ${fmtBytes(s.bytes)} · 失败 ${fmtInt(s.failed)} · ${fmtInt(Math.round(s.elapsed))}s` +
          `<div class="bar"><i style="width:${pct}%"></i></div>`;
        $('#uzStart').hidden = s.running;
        $('#uzStop').hidden = !s.running;
        if (s.log && s.log.length) {
          const box = $('#uzLog');
          box.textContent = s.log.join('\n');
          box.scrollTop = box.scrollHeight;
        }
        if (s.running) {
          UZ.poll = setTimeout(tick, 700);
        } else {
          toast(`解压完成：${fmtInt(s.files)} 个文件`, 'ok', 4000);
        }
      } catch (_) { /* 忽略 */ }
    };
    tick();
  }


  async function uzLoadFound() {
    try {
      const r = await api('/api/unzip/found');
      const list = (r.found || []);
      $('#uzFoundN').textContent = list.length ? `共 ${list.length} 条` : '0';
      $('#uzFound').innerHTML = list.length
        ? list.map((f) => `<span class="uz-pw-chip" title="${esc(f.src || '')} ${esc(f.at || '')}">${esc(f.pwd)}</span>`).join('')
        : '<span class="muted">还没有破解记录</span>';
    } catch (_) { /* 忽略 */ }
  }

  /* ---------------- 小说广告过滤 ---------------- */
  const CL = { poll: null };

  async function clStart() {
    const src = $('#clSrc').value.trim();
    const dst = $('#clDst').value.trim();
    if (!src || !dst) { toast('请先选好输入和输出文件夹', 'err'); return; }
    try {
      const r = await post('/api/clean/start', { src, dst, recursive: $('#clRecursive').checked });
      if (!r.ok) { toast(esc(r.error || '启动失败'), 'err', 4000); return; }
      toast('开始过滤…', '', 2500);
      clPoll();
    } catch (e) { toast('启动失败：' + esc(e.message), 'err'); }
  }

  function clPoll() {
    clearTimeout(CL.poll);
    const tick = async () => {
      try {
        const s = await api('/api/clean/status');
        const el = $('#clProgress');
        if (s.state === 'idle') { el.hidden = true; return; }
        el.hidden = false;
        const pct = s.total ? Math.round(100 * s.done / s.total) : 0;
        el.innerHTML = `过滤中 ${fmtInt(s.done)}/${fmtInt(s.total)}（${pct}%）· ` +
          `已写出 ${fmtInt(s.written)} · 删除广告行 ${fmtInt(s.removed_lines)} · ` +
          `跳过 ${fmtInt(s.skipped)} · 失败 ${fmtInt(s.errors)}` +
          `<div class="bar"><i style="width:${pct}%"></i></div>`;
        $('#clStart').hidden = s.running;
        $('#clStop').hidden = !s.running;
        if (s.running) {
          CL.poll = setTimeout(tick, 600);
        } else {
          $('#clEmpty').hidden = true;
          $('#clResult').innerHTML = `<div class="m-group"><div class="m-ghead">
            <b>完成</b><span>写出 ${fmtInt(s.written)} 个文件</span>
            <span>删除广告行 ${fmtInt(s.removed_lines)}</span>
            <span>省下 ${fmtBytes(s.saved_bytes)}</span>
            <span>跳过 ${fmtInt(s.skipped)} / 失败 ${fmtInt(s.errors)}</span></div>
            <div class="m-row" style="cursor:default"><div class="m-main">
              <div class="m-name">输出目录：${esc(s.dst || '')}</div>
              <div class="m-meta">原文件没有改动</div></div></div></div>`;
          toast(`过滤完成：${fmtInt(s.written)} 个文件`, 'ok', 4000);
        }
      } catch (_) { /* 忽略 */ }
    };
    tick();
  }

  /* ---------------- 过滤 / 解压 / 破解 面板事件 ---------------- */
  function brSpaceHint() {
    const box = $('#brSpace');
    if (!box) return;
    const n = Number($('#brLen').value) || 0;
    if (n <= 0) { box.textContent = '空格数会在开始后显示'; return; }
    let total = 0;
    for (let i = 1; i <= n; i++) total += Math.pow(10, i);
    box.textContent = `约 ${fmtInt(total)} 个候选（前缀 + ${n} 位数字 + 后缀）` +
      (state.hashcatOk ? ' · 走 GPU 掩码' : ' · 走 CPU 穷举');
  }

  /* 把「已破解密码本」倒进某个 textarea（解压页和破解页共用） */
  async function fillPasswordBook(boxSel) {
    const r = await api('/api/unzip/found').catch(() => null);
    const list = (r && r.found) || [];
    const box = $(boxSel);
    if (!box) return;
    const have = new Set(box.value.split(/\r?\n/).map((x) => x.trim()).filter(Boolean));
    let added = 0;
    list.forEach((f) => {
      const pw = (f && f.pwd) || '';
      if (!pw || have.has(pw)) return;
      have.add(pw);
      box.value += (box.value.trim() ? '\n' : '') + pw;
      added += 1;
    });
    toast(added ? `已载入 ${added} 条已破解密码` : '没有新的已破解密码', added ? 'ok' : '', 2600);
  }

  function bindOther() {
    const close = $("#otClose");
    if (!close) return;
    close.onclick = () => { $("#other").hidden = true; };
    $("#other").addEventListener("click", (e) => {
      if (e.target.id === "other") $("#other").hidden = true;
    });
    $("#btnOther").onclick = () => otOpen("image");
    $("#mOther").onclick = () => otOpen(M.tab === "image" ? "video" : M.tab);
    $("#otList").addEventListener("click", (e) => {
      const b = e.target.closest("[data-otact]");
      if (!b) return;
      otAct(b.dataset.otact, Number(b.dataset.i));
    });
    document.addEventListener("keydown", (e) => {
      if (!$("#other").hidden && e.key === "Escape") $("#other").hidden = true;
    });
  }

  function bindTools() {
    const on = (sel, fn) => { const el = $(sel); if (el) el.onclick = fn; };
    const pickInto = (sel, title) => {
      const el = $(sel);
      if (!el) return;
      pickFolder(title, el.value || '', (p) => { el.value = p; },
                 sel.replace(/^#/, ''));
    };

    // 小说广告过滤
    on('#clSrcPick', () => pickInto('#clSrc', '选择要清洗的小说文件夹'));
    on('#clDstPick', () => pickInto('#clDst', '选择输出文件夹'));
    on('#clStart', clStart);
    on('#clStop', async () => { await post('/api/clean/cancel', {}); });

    // 解压
    on('#uzSrcPick', () => pickInto('#uzSrc', '选择含压缩包的文件夹'));
    on('#uzDstPick', () => pickInto('#uzDst', '选择解压输出文件夹'));
    on('#uzStart', uzStart);
    on('#uzStop', async () => { await post('/api/unzip/cancel', {}); });

    // 暴力破解
    on('#brSrcPick', () => pickInto('#brSrc', '选择压缩包所在文件夹'));
    on('#brFilePick', () => pickFile('选择压缩包', $('#brSrc').value || '',
      '压缩包|*.zip;*.7z;*.rar;*.tar;*.gz;*.001|所有文件|*.*',
      (p) => { $('#brSrc').value = p; }));
    on('#brStart', brStart);
    on('#brPause', brPauseToggle);
    on('#brStop', async () => { await post('/api/brute/cancel', {}); });
    on('#brFoundLoad', () => fillPasswordBook('#brPwd'));
    on('#uzFoundLoad', () => fillPasswordBook('#uzPwd'));

    // 破解页：方式切换、预估、复制
    const modeSel = $('#brMode');
    if (modeSel) {
      modeSel.addEventListener('change', () => { brModeRows(); brEstimate(); });
      ['#brPre', '#brSuf', '#brLen', '#brMinLen', '#brMaxLen', '#brMaxMin', '#brPwd']
        .forEach((sel) => { const el = $(sel); if (el) el.addEventListener('input', brEstimate); });
      ['#brCharset', '#brRuleDigitN']
        .forEach((sel) => { const el = $(sel); if (el) el.addEventListener('change', brEstimate); });
      ['#brRuleCase', '#brRuleLeet', '#brRuleDigits']
        .forEach((sel) => { const el = $(sel); if (el) el.addEventListener('change', () => { brModeRows(); brEstimate(); }); });
      const found = $('#brFound');
      if (found) {
        found.addEventListener('click', (e) => {
          const b = e.target.closest('[data-pw]');
          if (!b) return;
          const pw = b.dataset.pw || '';
          if (navigator.clipboard && pw) {
            navigator.clipboard.writeText(pw).then(() => toast('已复制密码', 'ok', 1600)).catch(() => {});
          }
        });
      }
      brModeRows();
      brEstimate();

      // 明文攻击
      on('#plPickZip', () => pickFile('选要攻的压缩包', $('#plZip').value || '',
        '压缩包|*.zip|所有文件|*.*', (p) => { $('#plZip').value = p; }));
      on('#plPickFile', () => pickFile('选已知明文文件', $('#plFile').value || '',
        '所有文件|*.*', (p) => { $('#plFile').value = p; }));
      on('#plAuto', () => {
        const on = $('#plAuto').checked;
        $('#plText').disabled = on;
        $('#plFile').disabled = on;
      });
      on('#plStart', plStart);
      on('#plStop', async () => { await post('/api/plain/cancel', {}); });
    }
    ['#brLen', '#brPre', '#brSuf'].forEach((sel) => {
      const el = $(sel);
      if (el) el.addEventListener('input', brSpaceHint);
    });
    brSpaceHint();

    // 打包流程
    on('#pkSrcPick', () => pickInto('#pkSrc', '选择要打包的文件夹'));
    on('#pkDstPick', () => pickInto('#pkDst', '选择输出文件夹'));
    on('#pkStart', pkRun);
    on('#pkStop', async () => { await post('/api/pack/cancel', {}); });
    on('#pkAdd', () => pkOpenForm($('#pkNewType').value, -1));
    const resetBtn = $('#pkReset');
    if (resetBtn) resetBtn.onclick = () => { PK.steps = pkDefaultSteps(); pkRenderSteps(); };
    if (!PK.steps.length) PK.steps = pkDefaultSteps();
    pkRenderSteps();

    // 繁简 / 编码
    on('#cvSrcPick', () => pickInto('#cvSrc', '选择要转换的文件夹'));
    on('#cvDstPick', () => pickInto('#cvDst', '选择输出文件夹'));
    on('#cvStart', cvStart);
    on('#cvStop', async () => { await post('/api/conv/cancel', {}); });
    on('#cvPreview', cvPreview);
    on('#cvPaste', cvPaste);
    on('#cvCopyOut', cvCopyOut);

    // 字典（放最后，出问题也不影响别的按钮）
    try { bindDicts(); } catch (e) { console.error(e); }
    setTimeout(dictsRefresh, 300);
  }

  /* ---------------- 媒体面板事件 ---------------- */
  function bindMedia() {
    $$(".nav-item").forEach((b) => { b.onclick = () => setView(b.dataset.view); });
    $("#mScan").onclick = mStart;
    $("#mPause").onclick = async () => {
      const paused = M.job && M.job.paused;
      await post(paused ? "/api/media/resume" : "/api/media/pause", {});
      mPoll();
      if (!paused) setTimeout(() => mLoadGroups(true), 500);   // 暂停后马上看结果
    };
    $("#mStop").onclick = async () => {
      await post("/api/media/cancel", {});
      toast("已停止，已完成的结果保留", "", 3000);
      setTimeout(mPoll, 400);
    };
    $("#mSmart").onclick = mSmart;
    $("#mExport").onclick = mExport;
    let tmr = null;
    $("#mThreshold").oninput = (e) => {
      $("#mThVal").textContent = Number(e.target.value).toFixed(2);
      clearTimeout(tmr);
      tmr = setTimeout(mLoadGroups, 350);
    };
    $("#mMin").onchange = mLoadGroups;
    $("#mAudioVideo").onchange = (e) => {
      localStorage.setItem("pd.audvid", e.target.checked ? "1" : "0");
      toast(e.target.checked ? "已开启：扫描时会一起分析视频里的音轨" : "已关闭视频音轨", "", 2800);
    };
    $("#mRoot").onchange = mLoadGroups;
    $("#mAdd").onclick = mAddFolder;
    $("#mBrowse").onclick = () => pickFolder("选择文件夹", $("#mFolder").value || "", (p) => {
      $("#mFolder").value = p;
      mAddFolder();
    });
    $("#mFolder").addEventListener("keydown", (e) => { if (e.key === "Enter") mAddFolder(); });
    $("#mChips").addEventListener("click", async (e) => {
      const chip = e.target.closest(".chip-root");
      if (!chip) return;
      if (e.target.closest('[data-act="refresh"]')) {
        const yes = await askConfirm("重新扫描",
          `重新扫描「${esc(baseName(chip.dataset.path))}」？<br>` +
          `<span class="muted">没变的跳过，新加的会分析，删掉的从索引移除。</span>`,
          "开始扫描", false);
        if (!yes) return;
        try {
          const r = await post("/api/media/scan", {
            kind: M.tab, paths: [chip.dataset.path],
            include_video: M.tab === "audio" && $('#mAudioVideo').checked,
          });
          if (!r.ok) { toast(esc(r.error || "启动失败"), "err"); return; }
          toast("正在更新这个文件夹…", "", 2600);
          mPoll();
        } catch (err) { toast("启动失败：" + esc(err.message), "err"); }
        return;
      }
      if (e.target.closest('[data-act="rm"]')) {
        const yes = await askConfirm("移出工作区",
          `把「${esc(baseName(chip.dataset.path))}」移出工作区？<br><span class="muted">只删索引，不动磁盘文件</span>`,
          "移出", true);
        if (!yes) return;
        await post("/api/media/roots/remove", { kind: M.tab, path: chip.dataset.path });
        await mLoadRoots();
        await mLoadGroups();
      }
    });
    $("#mList").addEventListener("click", (e) => {
      const pv = e.target.closest("[data-pv]");
      if (pv) {
        e.stopPropagation();
        const id = Number(pv.dataset.pv);
        if (M.tab === "novel") openPreview(id); else mpOpen(id);
        return;
      }
      const act = e.target.closest("[data-act]");
      const row = e.target.closest(".m-row");
      if (act) {
        const no = act.dataset.no;
        const g = (M.groups[M.tab]?.groups || []).find((x) => String(x.no) === String(no));
        if (!g) return;
        const ids = [g.rep.id, ...g.members.map((m) => m.id)];
        if (act.dataset.act === "keep") {
          ids.forEach((id) => M.sel.delete(id));
          g.members.forEach((m) => M.sel.set(m.id, 1));
        } else if (act.dataset.act === "inv") {
          ids.forEach((id) => { if (M.sel.has(id)) M.sel.delete(id); else M.sel.set(id, 1); });
        } else {
          ids.forEach((id) => M.sel.delete(id));
        }
        mRenderGroups(M.groups[M.tab]);
        mUpdateFooter();
        e.stopPropagation();
        return;
      }
      if (row) mToggle(row.dataset.id);
    });
    $("#mClear").onclick = () => {
      M.sel.clear();
      mRenderGroups(M.groups[M.tab] || { groups: [] });
      mUpdateFooter();
    };
    $("#mTrash").onclick = () => mRemove("trash");
    $("#mMove").onclick = () => mRemove("move");
    $("#pvClose").onclick = () => { $("#preview").hidden = true; };
    $("#pvPath").onclick = async () => {
      if (!pvFile) return;
      try {
        const r = await post("/api/reveal", { path: pvFile });
        if (!r.ok) toast("打不开所在文件夹", "err", 2400);
      } catch (_) { toast("打不开所在文件夹", "err", 2400); }
    };
  }
  init();
})();

