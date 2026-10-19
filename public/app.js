// ================= GenieOps — front-end =================
'use strict';
const $ = s => document.querySelector(s);
const S = { tab: 'dashboard', q: '', verdict: '', model: '', page: 1, per: 50, sort: 'verdict',
  state: null, bulkPoll: null, loginErr: '' };

// ---------------- util ----------------
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
function ago(detik) {
  if (detik == null) return '—';
  if (detik < 60) return detik + ' dtk';
  if (detik < 3600) return Math.round(detik / 60) + ' mnt';
  if (detik < 86400) return (detik / 3600).toFixed(1) + ' jam';
  return (detik / 86400).toFixed(1) + ' hari';
}
function jamUTC(s) { return s ? s.slice(11, 19) + ' UTC' : '—'; }

async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || 'GET',
    headers: opts.body ? { 'Content-Type': 'application/json' } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let d = null;
  try { d = await r.json(); } catch (e) { /* kosong */ }
  if (r.status === 401) { viewLogin(); throw new Error((d && d.error) || 'Sesi berakhir'); }
  if (!r.ok) throw new Error((d && d.error) || ('HTTP ' + r.status));
  return d;
}

// ---------------- modal & toast ----------------
function modal(html) {
  const back = document.createElement('div');
  back.className = 'mback';
  back.innerHTML = `<div class="mcard">${html}</div>`;
  back.addEventListener('click', e => { if (e.target === back) close(); });
  function close() { back.remove(); }
  back.querySelector('.mx')?.addEventListener('click', close);
  $('#modal-root').appendChild(back);
  return { back, close };
}
function confirmDlg(judul, teks, yaLabel = 'Ya, lanjut') {
  return new Promise(resolve => {
    const m = modal(`
      <div class="mhead"><b>${esc(judul)}</b><button class="mx">✕</button></div>
      <div class="small" style="margin:8px 0 14px">${teks}</div>
      <div class="row"><div class="grow"></div>
        <button class="btn gho" id="cf-no">Batal</button>
        <button class="btn dan" id="cf-ya">${esc(yaLabel)}</button></div>`);
    m.back.querySelector('#cf-no').onclick = () => { m.close(); resolve(false); };
    m.back.querySelector('#cf-ya').onclick = () => { m.close(); resolve(true); };
  });
}
function toast(msg, err) {
  const t = document.createElement('div');
  t.className = 'toast' + (err ? ' err' : '');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

// ---------------- kerangka ----------------
function shell(konten, tabAktif) {
  const nav = [['dashboard', '📡', 'Dashboard'], ['log', '🧾', 'Log Aksi'], ['atur', '⚙️', 'Pengaturan']];
  $('#app').innerHTML = `
  <header class="appbar">
    <div class="logo" style="display:flex;align-items:center;justify-content:center;font-size:18px">📡</div>
    <div class="ttl"><b>GenieOps</b><span>ONU Watchdog GenieACS${S.state ? ' · ' + esc(S.state.n_devices) + ' ONU' : ''}</span></div>
    <button class="icobtn" id="btn-out" title="Keluar">⏻</button>
  </header>
  <nav class="bottomnav">${nav.map(([h, i, t]) =>
    `<a href="#${h === 'dashboard' ? '' : h}" class="${tabAktif === h ? 'on' : ''}"><span class="ni">${i}</span>${t}</a>`).join('')}</nav>
  <main class="page">${konten}</main>`;
  $('#btn-out').onclick = async () => { try { await api('/api/logout', { method: 'POST', body: {} }); } catch (e) {} location.reload(); };
}

// ---------------- login ----------------
function viewLogin(pesan) {
  document.title = 'GenieOps — Masuk';
  $('#app').innerHTML = `
  <div class="login-wrap"><div class="login-box">
    <div class="lg"><div class="ic">📡</div>
      <h1>GenieOps</h1>
      <div class="sub">ONU Watchdog &amp; Kontrol GenieACS</div></div>
    ${pesan ? `<div class="err-box" style="display:block">${esc(pesan)}</div>` : ''}
    <label>Password</label>
    <input type="password" id="pw" autofocus placeholder="password admin">
    <button class="btn blk" id="btn-in" style="margin-top:14px">Masuk</button>
  </div></div>`;
  const masuk = async () => {
    try {
      await api('/api/login', { method: 'POST', body: { password: $('#pw').value } });
      location.hash = ''; viewDashboard();
    } catch (e) { viewLogin(e.message); }
  };
  $('#btn-in').onclick = masuk;
  $('#pw').onkeydown = e => { if (e.key === 'Enter') masuk(); };
}

// ---------------- dashboard ----------------
async function viewDashboard() {
  S.tab = 'dashboard';
  let st;
  try { st = await api('/api/state'); } catch (e) { return; }
  S.state = st;
  document.title = 'GenieOps — Dashboard';
  const v = st.verdict;
  const watchdogCard = `
    <div class="card small" style="margin-bottom:12px;padding:10px 14px">
      <div class="row" style="flex-wrap:wrap">
        <span class="status-dot ${st.watchdog_enabled ? 'dot-ok' : 'dot-down'}"></span>
        <b>Watchdog ${st.watchdog_enabled ? 'AKTIF' : 'MATI'}</b>
        <span class="mut">· tiap ${Math.round(st.watchdog_interval / 60)} mnt</span>
        <span class="grow"></span>
        <button class="btn sm gho" id="wd-run">▶ Jalankan sekarang</button>
      </div>
      <div class="tiny mut" style="margin-top:4px">
        Putaran terakhir: ${st.watchdog.ts || 'belum pernah'} ·
        ${st.watchdog.target ? `${st.watchdog.target} ONU basi diperiksa — ${st.watchdog.ok} berhasil, ${st.watchdog.fail} gagal` : 'tidak ada ONU basi yang perlu diperiksa'}
        ${st.watchdog.err ? `<b class="bad">· error: ${esc(st.watchdog.err)}</b>` : ''}
      </div>
    </div>`;
  const syncLine = `
    <div class="sync-line">
      🔄 Sinkron ACS: ${st.acs_sync.ts ? esc(st.acs_sync.ts) + ' UTC' : '—'} (${st.acs_sync.n} ONU)
      ${st.acs_sync.err ? `<b class="bad">· GAGAL: ${esc(st.acs_sync.err)}</b>` : ''}
      · 🖧 PPPoE: ${st.pppoe_sync.n} sesi
      ${st.pppoe_sync.routers.map(r => `· <span class="${r.ok ? '' : 'bad'}">${esc(r.name)} ${r.ok ? '✓' + r.n : '✗ ' + esc(r.err || '')}</span>`).join(' ')}
      ${st.probe?.running ? `· <span class="bad">🔎 pindai redaman+WiFi: ${st.probe.done}/${st.probe.total}</span>` : ''}
    </div>`;

  shell(`
    <div class="toolbar">
      <div class="grow big">Dashboard ONU</div>
      <select id="f-model"><option value="">Semua model</option>
        ${st.models.map(([m, n]) => `<option value="${esc(m)}" ${S.model === m ? 'selected' : ''}>${esc(m)} (${n})</option>`).join('')}</select>
      <button class="btn sm sec" id="btn-probe" ${st.probe?.running ? 'disabled' : ''}>🔎 Pindai Redaman+WiFi</button>
    </div>
    ${syncLine}
    <div class="kpi-row">
      <div class="kpi"><div class="k">Total ONU</div><div class="v">${st.n_devices}</div>
        <div class="s">${st.models.length} model · basi &gt; ${st.stale_minutes} mnt</div></div>
      <div class="kpi"><div class="k">✅ Normal</div><div class="v">${v.ok || 0}</div><div class="s">ACS segar + PPPoE aktif</div></div>
      <div class="kpi ${(v.basi || 0) > 0 ? 'bad' : ''}"><div class="k">🟡 ACS basi</div><div class="v">${v.basi || 0}</div>
        <div class="s">ONU hidup (PPPoE aktif) tapi ACS anggap offline</div></div>
      <div class="kpi"><div class="k">🔔 PPPoE idle</div><div class="v">${v.idle || 0}</div><div class="s">ONU hidup, pelanggan tidak dial</div></div>
      <div class="kpi ${(v.down || 0) > 0 ? 'bad' : ''}"><div class="k">🔴 Down</div><div class="v">${v.down || 0}</div>
        <div class="s">ACS basi + PPPoE mati — cek lapangan</div></div>
    </div>
    ${watchdogCard}
    <div class="toolbar">
      <input id="q" placeholder="🔍 Cari serial / IP / user PPPoE / nama pelanggan…" value="${esc(S.q)}" style="flex:2">
      <select id="f-verdict">
        <option value="">Semua status</option>
        <option value="basi" ${S.verdict === 'basi' ? 'selected' : ''}>🟡 ACS basi (ONU hidup)</option>
        <option value="down" ${S.verdict === 'down' ? 'selected' : ''}>🔴 Down</option>
        <option value="idle" ${S.verdict === 'idle' ? 'selected' : ''}>🔔 PPPoE idle</option>
        <option value="ok" ${S.verdict === 'ok' ? 'selected' : ''}>✅ Normal</option>
      </select>
      <select id="f-sort">
        <option value="verdict" ${S.sort === 'verdict' ? 'selected' : ''}>Urut: masalah dulu</option>
        <option value="inform_asc" ${S.sort === 'inform_asc' ? 'selected' : ''}>Inform terlama</option>
        <option value="serial" ${S.sort === 'serial' ? 'selected' : ''}>Serial</option>
        <option value="model" ${S.sort === 'model' ? 'selected' : ''}>Model</option>
      </select>
    </div>
    <div class="bulk-bar">
      <span class="n" id="bulk-n">…</span>
      <button class="btn sm sec" id="b-refresh">↻ Refresh massal (hasil filter)</button>
      <button class="btn sm" id="b-inform" title="Ubah PeriodicInformInterval ONU hasil filter">⏱ Interval inform</button>
      <button class="btn sm dan" id="b-reboot">⏻ Reboot massal (hasil filter)</button>
      <button class="btn sm dan" id="b-hapus" title="Hapus ONU mati/diganti dari GenieACS">🗑 Hapus massal</button>
      <span class="grow"></span>
      <span class="tiny mut" id="bulk-prog"></span>
    </div>
    <div class="card" style="padding:6px 8px" id="tabel"></div>
    <div class="pager" id="pager"></div>
  `, 'dashboard');

  $('#wd-run').onclick = async () => {
    try { await api('/api/watchdog/run', { method: 'POST', body: {} }); toast('Watchdog dijalankan — hasil terlihat beberapa detik lagi'); setTimeout(viewDashboard, 4000); }
    catch (e) { toast(e.message, true); }
  };
  let t;
  $('#q').oninput = () => { clearTimeout(t); t = setTimeout(() => { S.q = $('#q').value; S.page = 1; muatTabel(); }, 350); };
  $('#f-verdict').onchange = () => { S.verdict = $('#f-verdict').value; S.page = 1; muatTabel(); };
  $('#f-model').onchange = () => { S.model = $('#f-model').value; S.page = 1; muatTabel(); };
  $('#f-sort').onchange = () => { S.sort = $('#f-sort').value; S.page = 1; muatTabel(); };
  $('#b-refresh').onclick = () => bulk('refresh');
  $('#b-inform').onclick = () => bulk('set_inform');
  $('#b-reboot').onclick = () => bulk('reboot');
  $('#b-hapus').onclick = () => bulk('hapus');
  muatTabel();
}

async function muatTabel() {
  const el = $('#tabel');
  el.innerHTML = '<div class="mut" style="padding:14px;text-align:center">Memuat…</div>';
  let d;
  try {
    d = await api(`/api/devices?q=${encodeURIComponent(S.q)}&verdict=${S.verdict}&model=${encodeURIComponent(S.model)}&page=${S.page}&per=${S.per}&sort=${S.sort}`);
  } catch (e) { el.innerHTML = `<div class="mut" style="padding:14px">${esc(e.message)}</div>`; return; }
  $('#bulk-n').textContent = `${d.total} ONU${S.verdict || S.q || S.model ? ' (hasil filter)' : ''}`;
  const halTot = Math.max(1, Math.ceil(d.total / d.per));
  S.mobile = MOBILE();
  el.innerHTML = d.rows.length ? (S.mobile
    ? d.rows.map(r => barisKartu(r)).join('')
    : `<table class="tbl">
      <thead><tr><th>ONU</th><th>IP</th><th>PPPoE</th><th>Pelanggan</th><th>Redaman</th><th>Inform terakhir</th><th>Status</th><th></th></tr></thead>
      <tbody>${d.rows.map(r => baris(r)).join('')}</tbody>
    </table>`) : '<div class="mut" style="padding:14px;text-align:center">Tidak ada ONU yang cocok.</div>';
  $('#pager').innerHTML = d.total > d.per ? `
    <button class="btn sm gho" id="pg-prev" ${S.page <= 1 ? 'disabled' : ''}>‹ Sebelumnya</button>
    <span class="small mut">Hal. ${S.page}/${halTot} · ${(S.page - 1) * d.per + 1}–${Math.min(S.page * d.per, d.total)} dari ${d.total}</span>
    <button class="btn sm gho" id="pg-next" ${S.page >= halTot ? 'disabled' : ''}>Berikutnya ›</button>` : '';
  const bp = $('#btn-probe'); if (bp) bp.onclick = async () => {
    if (!await confirmDlg('Pindai Redaman + WiFi?',
      'GenieOps akan mengambil pohon data lengkap tiap ONU yang belum punya data redaman/WiFi (sekali jalan, di latar belakang). Lanjutkan?', 'Ya, pindai')) return;
    try {
      const r = await api('/api/probe', { method: 'POST', body: { semua: 1 } });
      toast(`🔎 Pindai dimulai: ${r.total} ONU`);
      mulaiPollProbe();
    } catch (e) { toast(e.message, true); }
  };
  if (S.state?.probe?.running) mulaiPollProbe();
  $('#pg-prev')?.addEventListener('click', () => { S.page--; muatTabel(); });
  $('#pg-next')?.addEventListener('click', () => { S.page++; muatTabel(); });
  el.querySelectorAll('[data-act]').forEach(b => b.onclick = ev => { ev.stopPropagation(); aksiSatu(b.dataset.act, b.dataset.id, b.dataset.label); });
  el.querySelectorAll('tr.klik, .kartu').forEach(x => x.onclick = ev => {
    if (ev.target.closest('button, a')) return;
    bukaDetail(x.dataset.detail);
  });
}

function baris(r) {
  const chipAcs = r.verdict === 'ok' || r.verdict === 'idle'
    ? '<span class="chip ok">ACS online</span>'
    : r.verdict === 'basi' ? '<span class="chip warn">ACS basi</span>' : '<span class="chip bad">ACS offline</span>';
  const chipPpp = r.pppoe_state === 'aktif'
    ? `<span class="chip teal">PPPoE aktif</span>` : '<span class="chip mut">PPPoE idle</span>';
  const label = r.pppoe_user || r.serial;
  return `<tr class="klik" data-detail="${esc(r.id)}" title="Klik: detail ONU (redaman & WiFi)">
    <td class="t-onu"><b>${esc(r.serial)}</b><span class="tiny mut">${esc(modelPendek(r.model))}</span></td>
    <td class="mono-ip">${esc(r.ip || '—')}</td>
    <td><b>${esc(r.pppoe_user || '—')}</b>${r.pppoe_uptime ? `<div class="tiny mut">⏱ ${esc(r.pppoe_uptime)}</div>` : ''}</td>
    <td>${r.pelanggan ? `<b>${esc(r.pelanggan)}</b>${r.kolektor ? `<div class="tiny mut">✋ ${esc(r.kolektor)}</div>` : ''}` : '<span class="mut">—</span>'}</td>
    <td class="small mono-ip">${selRx(r)}</td>
    <td class="small">${ago(r.age)}<div class="tiny mut">${jamUTC(r.last_inform)}</div></td>
    <td><div class="row" style="flex-wrap:wrap;gap:4px">${chipAcs}${chipPpp}</div>
      <div class="tiny" style="margin-top:2px;color:${{ ok: 'var(--ok)', basi: 'var(--warn)', idle: '#3b82f6', down: 'var(--bad)' }[r.verdict]};font-weight:700">${esc(r.verdict_label)}</div></td>
    <td class="t-aksi">
      <button class="btn sm sec" data-act="refresh" data-id="${esc(r.id)}" data-label="${esc(label)}" title="Refresh perangkat ini">↻</button>
      <button class="btn sm gho" data-act="reboot" data-id="${esc(r.id)}" data-label="${esc(label)}" title="Reboot ONU">⏻</button>
      ${S.state?.acs_ui_url ? `<a class="btn sm gho" href="${esc(S.state.acs_ui_url)}/#!/devices/${esc(r.id)}" target="_blank" rel="noopener" title="Buka di GenieACS">↗</a>` : ''}
    </td></tr>`;
}

async function bukaDetail(id) {
  const m = modal(`<div class="mhead"><b>Detail ONU</b><button class="mx">✕</button></div>
    <div class="mut small" style="margin:6px 0 14px">Memuat pohon data dari ACS… (WiFi dipindai otomatis — pembukaan pertama ±2-10 detik)</div>`);
  let d;
  try { d = await api('/api/device/' + encodeURIComponent(id)); }
  catch (e) { m.close(); toast(e.message, true); return; }
  const back = m.back;
  const warnaV = { ok: 'var(--ok)', basi: 'var(--warn)', idle: '#3b82f6', down: 'var(--bad)' }[d.verdict];
  const baris = (k, v) => `<div style="padding:7px 0;border-bottom:1px dashed var(--bd)"><span class="tiny mut">${k}</span><div>${v}</div></div>`;
  const wifiHtml = (d.wifi || []).map((w, i) => `
    <div style="padding:9px 0;border-bottom:1px dashed var(--bd)">
      <div class="row" style="flex-wrap:wrap">
        <b>${esc(w.ssid)}</b>${w.band ? `<span class="chip mut">${esc(w.band)}</span>` : ''}
        <span class="grow"></span>
        <span class="tiny mut">${w.key_paths.length ? 'kunci: ' + w.key_paths.length + ' path' : 'path kunci tak dikenal'}</span>
      </div>
      <div class="tiny mut" style="margin:3px 0">Password sekarang: <b class="mono-ip">${w.key ? esc(w.key) : '—'}</b></div>
      ${w.key_paths.length ? `
      <div class="row" style="gap:6px;margin-top:5px">
        <input class="grow" type="text" id="wf-${i}" placeholder="Password baru (8-63 karakter)" autocomplete="off">
        <button class="btn sm dan" data-wifi="${esc(w.ssid_path)}">💾 Ganti</button>
      </div>` : ''}
    </div>`).join('') || '<div class="mut small" style="padding:8px 0">SSID tidak ditemukan di pohon data (ONU tanpa WiFi / belum pernah melaporkan).</div>';
  back.querySelector('.mcard').innerHTML = `
    <div class="mhead"><b>${esc(d.serial || d.id)}</b><button class="mx">✕</button></div>
    <div class="tiny mut">${esc(modelUtuh(d.model))}</div>
    <div class="row" style="gap:6px;flex-wrap:wrap;margin:8px 0 4px">
      <span class="chip" style="color:${warnaV};font-weight:700">${esc(d.verdict_label)}</span>
      <span class="grow"></span>
      <button class="btn sm sec" id="d-refresh">↻ Refresh</button>
      <button class="btn sm gho" id="d-reboot">⏻ Reboot</button>
      <button class="btn sm dan" id="d-hapus" title="Hapus ONU ini dari GenieACS">🗑 Hapus</button>
      ${S.state?.acs_ui_url ? `<a class="btn sm gho" href="${esc(S.state.acs_ui_url)}/#!/devices/${esc(d.id)}" target="_blank" rel="noopener">↗ ACS</a>` : ''}
    </div>
    ${d.err_detail ? `<div class="small" style="color:var(--bad);margin:6px 0">⚠ ${esc(d.err_detail)} (menampilkan data terakhir yang tersimpan)</div>` : ''}
    ${baris('Redaman PON', d.rx !== '' && d.rx != null
      ? `<span class="status-dot ${kelasRx(d.rx)}"></span><b class="mono-ip">${esc(String(d.rx))} dBm</b> <span class="tiny mut">${kelasRx(d.rx) === 'dot-ok' ? 'baik' : (kelasRx(d.rx) === 'dot-basi' ? 'mulai rendah' : 'rendah — periksa fiber')}</span>${d.rx_path && d.rx_path !== '(tidak ada)' ? `<div class="tiny mut" style="margin-top:2px;word-break:break-all">sumber: ${esc(d.rx_path)}</div>` : ''}`
      : '<span class="mut">tidak ditemukan di pohon data</span>')}
    ${baris('IP (connection request)', `<span class="mono-ip">${esc(d.ip || '—')}</span>`)}
    ${baris('PPPoE', d.pppoe_user
      ? `<b>${esc(d.pppoe_user)}</b> · ${d.pppoe_state === 'aktif' ? '<span style="color:var(--ok)">aktif</span>' : '<span class="mut">idle</span>'}${d.pppoe_addr ? ` <span class="tiny mut mono-ip">${esc(d.pppoe_addr)}</span>` : ''}`
      : '<span class="mut">—</span>')}
    ${baris('Inform terakhir', `${ago(d.age)} lalu <span class="tiny mut mono-ip">(${jamUTC(d.last_inform)})</span>`)}
    <div style="margin-top:14px"><b>📶 WiFi</b>
      <div class="tiny mut" style="margin:2px 0 4px">Ganti password akan langsung dikirim ke ONU (setParameterValues + connection request). Perangkat WiFi yang tersambung akan diminta memasukkan password baru.</div>
      ${wifiHtml}
    </div>
    <div class="row" style="margin-top:14px"><div class="grow"></div>
      <button class="btn gho" id="d-rescan">🔄 Pindai ulang</button></div>`;
  back.querySelector('.mx')?.addEventListener('click', m.close);
  back.querySelector('#d-refresh').onclick = () => { m.close(); aksiSatu('refresh', d.id, d.pppoe_user || d.serial); };
  back.querySelector('#d-reboot').onclick = () => { m.close(); aksiSatu('reboot', d.id, d.pppoe_user || d.serial); };
  back.querySelector('#d-hapus').onclick = async () => {
    m.close();
    if (!await confirmKetik('HAPUS ONU DARI GENIEACS?',
        `ONU <b>${esc(d.serial)}</b>${d.pppoe_user ? ' (' + esc(d.pppoe_user) + ')' : ''} akan dihapus permanen dari GenieACS dan daftar GenieOps.`)) return;
    try {
      const r = await api('/api/action', { method: 'POST', body: { device_id: d.id, action: 'hapus' } });
      toast(r.ok ? '🗑 ONU dihapus dari GenieACS' : '✗ Gagal menghapus', !r.ok);
      muatTabel();
    } catch (e) { toast(e.message, true); }
  };
  back.querySelector('#d-rescan').onclick = () => { m.close(); bukaDetail(id); };
  back.querySelectorAll('[data-wifi]').forEach(btn => btn.onclick = async () => {
    const ssidPath = btn.dataset.wifi;
    const val = (btn.previousElementSibling?.value || '').trim();
    if (val.length < 8) return toast('Password minimal 8 karakter', true);
    btn.disabled = true;
    try {
      const r = await api('/api/wifi_pass', { method: 'POST', body: { device_id: d.id, ssid_path: ssidPath, pass: val } });
      toast(r.ok ? '✓ Password WiFi terkirim ke ONU' : '✗ Tugas gagal dikirim', !r.ok);
    } catch (e) { toast(e.message, true); btn.disabled = false; }
  });
}

function mulaiPollProbe() {
  if (S.probePoll) return;
  S.probePoll = setInterval(async () => {
    let st;
    try { st = await api('/api/state'); } catch (e) { return; }
    S.state = st;
    const el = $('#btn-probe');
    const p = st.probe || {};
    if (el) { el.disabled = !!p.running; el.textContent = p.running ? `🔎 ${p.done}/${p.total}` : '🔎 Pindai Redaman+WiFi'; }
    if (!p.running) {
      clearInterval(S.probePoll); S.probePoll = null;
      if (el) el.textContent = '🔎 Pindai Redaman+WiFi';
      muatTabel();
      toast(`✓ Pindai selesai${p.ketemu != null ? ` — ${p.ketemu} ONU punya data redaman/WiFi` : ''}`);
    }
  }, 3000);
}

function modelPendek(m) { const t = (m || '').split('%')[0].split('_')[0].trim(); return t || (m || ''); }
function modelUtuh(m) { return (m || '').replace(/%2D/gi, '-'); }

const MOBILE = () => (window.matchMedia ? window.matchMedia('(max-width: 760px)').matches : false);

function barisKartu(r) {
  const label = r.pppoe_user || r.serial;
  const dot = { ok: 'dot-ok', basi: 'dot-basi', idle: 'dot-idle', down: 'dot-down' }[r.verdict] || 'dot-down';
  const warna = { ok: 'var(--ok)', basi: 'var(--warn)', idle: '#3b82f6', down: 'var(--bad)' }[r.verdict];
  return `<div class="kartu" data-detail="${esc(r.id)}">
    <div class="row" style="align-items:center">
      <span class="status-dot ${dot}"></span>
      <b style="font-size:14.5px">${esc(r.serial)}</b>
      <span class="tiny mut">${esc(modelPendek(r.model))}</span>
      <span class="grow"></span>
      <button class="btn sm sec" data-act="refresh" data-id="${esc(r.id)}" data-label="${esc(label)}" title="Refresh">↻</button>
      <button class="btn sm gho" data-act="reboot" data-id="${esc(r.id)}" data-label="${esc(label)}" title="Reboot">⏻</button>
    </div>
    <div class="small" style="margin-top:4px">
      <b>${esc(r.pppoe_user || '—')}</b>
      <span class="tiny ${r.pppoe_state === 'aktif' ? '' : 'mut'}">· PPPoE ${r.pppoe_state === 'aktif' ? 'aktif' : 'idle'}</span>
      ${r.ip ? ` · <span class="mono-ip tiny">${esc(r.ip)}</span>` : ''}
    </div>
    <div class="tiny mut" style="margin-top:2px">
      ${r.rx !== '' && r.rx != null ? `📶 <b class="mono-ip" style="color:inherit">${esc(String(r.rx))} dBm</b> · ` : ''}inform ${ago(r.age)} lalu
      ${r.pelanggan ? ` · 👤 ${esc(r.pelanggan)}` : ''}
    </div>
    <div class="tiny" style="margin-top:3px;color:${warna};font-weight:700">${esc(r.verdict_label)}</div>
  </div>`;
}

function kelasRx(v) {
  const n = parseFloat(v);
  if (isNaN(n)) return '';
  return n >= -24 ? 'dot-ok' : (n >= -27 ? 'dot-basi' : 'dot-down');
}
function selRx(r) {
  if (r.rx === '' || r.rx == null) return '<span class="mut">—</span>';
  return `<span class="status-dot ${kelasRx(r.rx)}"></span>${esc(String(r.rx))} <span class="tiny mut">dBm</span>`;
}

async function aksiSatu(action, id, label) {
  if (action === 'reboot' && !await confirmDlg('Reboot ONU?', `${esc(label)} akan direboot — internet pelanggan terputus ±2 menit.`, 'Ya, reboot')) return;
  try { await api('/api/action', { method: 'POST', body: { device_id: id, action } }); toast(action === 'reboot' ? 'Perintah reboot terkirim' : 'Refresh terkirim'); }
  catch (e) { toast(e.message, true); }
}

function confirmKetik(judul, teks, kata = 'HAPUS') {
  return new Promise(resolve => {
    const m = modal(`
      <div class="mhead"><b>${esc(judul)}</b><button class="mx">✕</button></div>
      <div class="small" style="margin:8px 0 10px">${teks}</div>
      <input id="cf-ketik" placeholder="Ketik ${kata} untuk konfirmasi" autocomplete="off"
             style="width:100%;padding:9px 11px;border:1px solid var(--bd);border-radius:9px">
      <div class="row" style="margin-top:12px"><div class="grow"></div>
        <button class="btn gho" id="cf-no">Batal</button>
        <button class="btn dan" id="cf-ya" disabled>🗑 Hapus permanen</button></div>`);
    const inp = m.back.querySelector('#cf-ketik'), ya = m.back.querySelector('#cf-ya');
    inp.oninput = () => { ya.disabled = inp.value.trim().toUpperCase() !== kata; };
    m.back.querySelector('#cf-no').onclick = () => { m.close(); resolve(false); };
    ya.onclick = () => { m.close(); resolve(true); };
  });
}

function promptInterval() {
  return new Promise(resolve => {
    const m = modal(`
      <div class="mhead"><b>⏱ SET INTERVAL INFORM MASSAL</b><button class="mx">✕</button></div>
      <div class="small" style="margin:8px 0 10px">Ubah <b>PeriodicInformInterval</b> semua ONU hasil filter.
        Interval pendek (5 menit) membuat server ACS jenuh &amp; panas; 30–60 menit jauh lebih ringan
        dan status online tetap jujur lewat PPPoE.</div>
      <select id="iv-pil" style="width:100%;padding:9px 11px;border:1px solid var(--bd);border-radius:9px">
        <option value="3600">60 menit (disarankan)</option>
        <option value="1800">30 menit</option>
        <option value="600">10 menit</option>
        <option value="300">5 menit (berat — tidak disarankan)</option>
        <option value="custom">Lainnya…</option>
      </select>
      <input id="iv-custom" inputmode="numeric" placeholder="detik (60–86400)" style="display:none;width:100%;margin-top:8px;padding:9px 11px;border:1px solid var(--bd);border-radius:9px">
      <div class="row" style="margin-top:12px"><div class="grow"></div>
        <button class="btn gho" id="iv-no">Batal</button>
        <button class="btn" id="iv-ya">Terapkan</button></div>`);
    const pil = m.back.querySelector('#iv-pil'), cus = m.back.querySelector('#iv-custom');
    pil.onchange = () => { cus.style.display = pil.value === 'custom' ? 'block' : 'none'; };
    m.back.querySelector('#iv-no').onclick = () => { m.close(); resolve(0); };
    m.back.querySelector('#iv-ya').onclick = () => {
      const v = pil.value === 'custom' ? parseInt(cus.value, 10) : parseInt(pil.value, 10);
      if (!v || v < 60 || v > 86400) { toast('Interval harus 60–86400 detik', true); return; }
      m.close(); resolve(v);
    };
  });
}

async function bulk(action) {
  const nEl = $('#bulk-n').textContent;
  const jumlah = parseInt(nEl, 10) || 0;
  if (!jumlah) return toast('Tidak ada ONU pada filter ini', true);
  let interval = 0;
  if (action === 'set_inform') {
    interval = await promptInterval();
    if (!interval) return;
    const menit = interval >= 60 ? Math.round(interval / 60) + ' menit' : interval + ' detik';
    if (!await confirmDlg('UBAH INTERVAL INFORM?',
        `PeriodicInformInterval <b>${jumlah} ONU</b> (${esc(nEl)}) akan diubah menjadi <b>${menit}</b>.<br>` +
        'Interval panjang = beban server ACS jauh lebih ringan; status online tetap jujur lewat PPPoE.',
        `Ya, terapkan`)) return;
  }
  if (action === 'hapus' && !await confirmKetik('HAPUS MASSAL DARI GENIEACS?',
      `Akan <b>menghapus permanen ${jumlah} ONU</b> hasil filter dari GenieACS dan daftar GenieOps.<br>
       Cocok untuk ONU yang sudah diganti unit baru / benar-benar mati.<br>
       <b>Tidak bisa dibatalkan.</b> Pastikan filter benar (disarankan: status 🔴 Down).`)) return;
  if (action === 'reboot' && !await confirmDlg('REBOOT MASSAL?',
      `<b>${jumlah} ONU</b> (${esc(nEl)}) akan direboot sekaligus.<br>Internet pelanggan terputus ±2 menit per ONU. Yakin?`, `Ya, reboot ${jumlah} ONU`)) return;
  let d;
  try {
    d = await api('/api/bulk', { method: 'POST', body: { action, interval, q: S.q, verdict: S.verdict, model: S.model } });
  } catch (e) { return toast(e.message, true); }
  toast(`${d.total} ONU masuk antrean ${action === 'reboot' ? 'reboot' : (action === 'hapus' ? 'penghapusan' : (action === 'set_inform' ? 'set interval inform' : 'refresh'))}…`);
  pollBulk(d.bulk_id, action === 'hapus');
}

function pollBulk(bid, muatUlang) {
  if (S.bulkPoll) clearInterval(S.bulkPoll);
  const el = $('#bulk-prog');
  S.bulkPoll = setInterval(async () => {
    try {
      const b = await api('/api/bulk/' + bid);
      if (el) el.textContent = b.running ? `⏳ ${b.done}/${b.total} selesai · ✓${b.ok} ✗${b.fail}` : `✓ Selesai: ${b.ok} berhasil, ${b.fail} gagal`;
      if (!b.running) {
        clearInterval(S.bulkPoll); S.bulkPoll = null;
        setTimeout(() => { if (el) el.textContent = ''; }, 6000);
        if (muatUlang) muatTabel();
      }
    } catch (e) { clearInterval(S.bulkPoll); S.bulkPoll = null; }
  }, 2000);
}

// ---------------- log ----------------
async function viewLog() {
  S.tab = 'log';
  let d;
  try { d = await api('/api/actions'); } catch (e) { return; }
  shell(`
    <div class="toolbar"><div class="grow big">Log Aksi</div></div>
    <div class="card" style="padding:6px 8px">
      ${d.rows.length ? `<table class="tbl">
        <thead><tr><th>Waktu</th><th>ONU</th><th>Aksi</th><th>Hasil</th><th>Keterangan</th><th>Sumber</th></tr></thead>
        <tbody>${d.rows.map(r => `<tr>
          <td class="small">${esc(r.ts)}</td>
          <td><b>${esc(r.device_label || r.device_id)}</b></td>
          <td class="small">${esc(r.action)}</td>
          <td>${r.status === 'ok' ? '<span class="chip ok">ok</span>' : `<span class="chip bad">${esc(r.status)}</span>`}</td>
          <td class="small mut">${esc(r.msg || '')}</td>
          <td class="tiny mut">${esc(r.source)}</td></tr>`).join('')}</tbody></table>`
      : '<div class="mut" style="padding:14px;text-align:center">Belum ada aksi tercatat.</div>'}
    </div>`, 'log');
}

// ---------------- pengaturan ----------------
async function viewAtur() {
  S.tab = 'atur';
  let st;
  try { st = await api('/api/settings'); } catch (e) { return; }
  const mt = (st.mikrotik_list || []).map(m =>
    [m.name || '', m.host, m.port || 8728, m.user, m.pass || ''].join(',')).join('\n');
  shell(`
    <div class="toolbar"><div class="grow big">Pengaturan</div></div>
    <div class="card">
      <label>URL NBI GenieACS</label>
      <input id="s-acs" value="${esc(st.acs_nbi_url)}">
      <div class="hint">Contoh: http://10.10.10.5:7557 (port NBI, bukan 7547/3000)</div>
      <label>URL UI GenieACS (untuk tombol ↗)</label>
      <input id="s-ui" value="${esc(st.acs_ui_url)}">
      <div class="hint">Contoh: http://10.10.10.5:3000</div>
      <label>Daftar Mikrotik — satu baris per router</label>
      <textarea id="s-mt" rows="4" style="font-family:ui-monospace,monospace">${esc(mt)}</textarea>
      <div class="hint">Format: <b>nama,host,port,user,password</b> — baris tanpa nama juga boleh: host,port,user,password</div>
      <div class="grid2">
        <div><label>Batas "basi" (menit)</label><input id="s-stale" inputmode="numeric" value="${esc(st.stale_minutes)}">
      <div class="hint">Harus LEBIH BESAR dari interval inform ONU (⏱ Interval inform) — kalau lebih kecil, semua ONU terlihat "basi" dan watchdog menghajar tanpa henti</div>
          <div class="hint">ONU tanpa inform lebih lama dari ini dianggap basi/offline oleh ACS</div></div>
        <div><label>Interval sinkronisasi (detik)</label><input id="s-sync" inputmode="numeric" value="${esc(st.sync_interval)}"></div>
      </div>
    </div>
    <div class="card">
      <label class="row" style="margin-top:0"><input type="checkbox" id="s-wde" style="width:auto" ${st.watchdog_enabled === '1' ? 'checked' : ''}>
        <span><b>Watchdog otomatis</b> — ONU basi (PPPoE aktif) dipaksa inform otomatis</span></label>
      <div class="grid2">
        <div><label>Interval watchdog (detik)</label><input id="s-wdi" inputmode="numeric" value="${esc(st.watchdog_interval)}"></div>
        <div><label>Maks ONU per putaran</label><input id="s-wdm" inputmode="numeric" value="${esc(st.watchdog_max)}"></div>
      </div>
      <label>Sasaran watchdog</label>
      <select id="s-wds">
        <option value="pppoe_aktif" ${st.watchdog_scope === 'pppoe_aktif' ? 'selected' : ''}>Hanya ONU basi yang PPPoE-nya aktif (aman)</option>
        <option value="semua_stale" ${st.watchdog_scope === 'semua_stale' ? 'selected' : ''}>Semua ONU basi (termasuk yang PPPoE mati)</option>
      </select>
      <label>Paralel aksi massal</label><input id="s-bc" inputmode="numeric" value="${esc(st.bulk_concurrency)}">
      <div class="hint">Berapa banyak connection request dikirim bersamaan (jangan terlalu besar, puncak beban ACS)</div>
    </div>
    <div class="card">
      <label>Path database TagiNet (opsional, baca-saja)</label>
      <input id="s-tg" value="${esc(st.taginet_db)}" placeholder="/taginet/taginet.db">
      <div class="hint">Kalau diisi, nama pelanggan &amp; penagih dari TagiNet muncul di tabel (dicocokkan lewat user PPPoE)</div>
      <label>Password baru (kosongkan bila tidak diganti)</label>
      <input type="password" id="s-pw" placeholder="••••">
    </div>
    <button class="btn blk" id="s-save" style="margin-top:6px">💾 Simpan Pengaturan</button>
  `, 'atur');
  $('#s-save').onclick = async () => {
    const mtl = $('#s-mt').value.split('\n').map(l => l.trim()).filter(Boolean).map(l => {
      const p = l.split(',').map(x => x.trim());
      // 5 kolom: nama,host,port,user,pass — 4 kolom: host,port,user,pass — 3 kolom: host,user,pass
      if (p.length >= 5) return { name: p[0], host: p[1], port: +p[2] || 8728, user: p[3], pass: p[4] };
      if (p.length === 4) return { name: p[0], host: p[0], port: +p[1] || 8728, user: p[2], pass: p[3] };
      return { name: p[0], host: p[0], port: 8728, user: p[1] || 'admin', pass: p[2] || '' };
    });
    try {
      await api('/api/settings', { method: 'POST', body: {
        acs_nbi_url: $('#s-acs').value.trim(), acs_ui_url: $('#s-ui').value.trim(),
        mikrotik_list: JSON.stringify(mtl),
        stale_minutes: $('#s-stale').value, sync_interval: $('#s-sync').value,
        watchdog_enabled: $('#s-wde').checked ? '1' : '0',
        watchdog_interval: $('#s-wdi').value, watchdog_max: $('#s-wdm').value,
        watchdog_scope: $('#s-wds').value, bulk_concurrency: $('#s-bc').value,
        taginet_db: $('#s-tg').value.trim(),
        password_baru: $('#s-pw').value || '',
      } });
      toast('✓ Pengaturan tersimpan');
    } catch (e) { toast(e.message, true); }
  };
}

// ---------------- router ----------------
function route() {
  const h = location.hash.replace('#', '');
  if (!S.state && h !== 'log' && h !== 'atur') return viewDashboard();
  if (h === 'log') return viewLog();
  if (h === 'atur') return viewAtur();
  viewDashboard();
}
window.addEventListener('hashchange', () => { if ($('#app .appbar')) route(); });
window.addEventListener('resize', () => {
  if (S.tab !== 'dashboard' || !$('#tabel')) return;
  if (MOBILE() !== S.mobile) muatTabel();
});

(async () => {
  try { await api('/api/state'); viewDashboard(); }
  catch (e) { viewLogin(); }
})();
