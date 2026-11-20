// Deterministic animation: every visual is a pure function of t (seconds), so each frame can
// be captured exactly. Timing comes from the recorded narration (timeline.js).
const TL = window.TIMELINE, CAPS = window.CAPTIONS;
const SC = {};
TL.scenes.forEach((s, i) => { s.index = i; SC[s.id] = s; });
const $ = (q) => document.querySelector(q);
const clamp = (x) => Math.max(0, Math.min(1, x));
const ease = (x) => { x = clamp(x); return x * x * (3 - 2 * x); };
const back = (x) => { x = clamp(x); const c = 1.6; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); };
const lerp = (a, b, p) => a + (b - a) * p;
const C = { teal: '#2dd4bf', violet: '#a78bfa', blue: '#60a5fa', green: '#34d399', amber: '#fbbf24', red: '#f87171',
  orange: '#fb923c', slate: '#cbd5e1', cyan: '#22d3ee', pink: '#f472b6', muted: '#8d9abb', section: '#93c5fd' };

// "scene", "scene.end", "scene.cue", "scene.cue@fraction", each optionally "+seconds" / "-seconds"
function at(spec) {
  const m = spec.match(/^(\w+)(?:\.(\d+|end))?(?:@([\d.]+))?([+-][\d.]+)?$/);
  if (!m) throw new Error('bad time ' + spec);
  const s = SC[m[1]]; let t = s.start;
  if (m[2] === 'end') t = s.start + s.duration;
  else if (m[2] !== undefined) { const c = s.cues[+m[2]]; t = s.start + c.start + (m[3] ? +m[3] * (c.end - c.start) : 0); }
  return t + (m[4] ? +m[4] : 0);
}

const ICON = {
  file: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6',
  sheet: 'M3 3h18v18H3z M3 9h18 M3 15h18 M9 3v18',
  hash: 'M4 9h16 M4 15h16 M10 3 8 21 M16 3l-2 18',
  row: 'M3 5h18v14H3z M3 10h18 M9 10v9',
  tag: 'M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L2 12V2h10l8.6 8.6a2 2 0 0 1 0 2.8z M7 7h.01',
  shield: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z M9 12l2 2 4-4',
  check: 'M20 6 9 17l-5-5',
  x: 'M18 6 6 18 M6 6l12 12',
  alert: 'M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z M12 9v4 M12 17h.01',
  plug: 'M12 22v-5 M9 8V2 M15 8V2 M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8z',
  book: 'M4 19.5A2.5 2.5 0 0 1 6.5 17H20 M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z',
  lock: 'M5 11h14v10H5z M8 11V7a4 4 0 0 1 8 0v4',
  print: 'M12 11v5 M8.5 12a3.5 3.5 0 0 1 7 0v3 M5.5 12a6.5 6.5 0 0 1 13 0v2 M7 19c1-1 1.5-2.6 1.5-4.5',
  search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z M21 21l-4.3-4.3',
  spark: 'M12 3l1.9 5.8L20 11l-6.1 2.2L12 19l-1.9-5.8L4 11l6.1-2.2z',
  upload: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4 M17 8l-5-5-5 5 M12 3v12',
  layers: 'M12 2 2 7l10 5 10-5-10-5z M2 17l10 5 10-5 M2 12l10 5 10-5',
  code: 'M16 18l6-6-6-6 M8 6l-6 6 6 6',
  globe: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z M2 12h20 M12 2a15 15 0 0 1 0 20 M12 2a15 15 0 0 0 0 20',
  flame: 'M12 22c4 0 7-3 7-7 0-4-3-6-4-9-1 2-2 3-4 3 0-2-1-4-2-6-2 3-4 6-4 12 0 4 3 7 7 7z',
  scale: 'M12 3v18 M5 21h14 M3 7h18 M6 7l-3 7a3 3 0 0 0 6 0z M18 7l-3 7a3 3 0 0 0 6 0z',
  clock: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z M12 6v6l4 2',
  vector: 'M4 20V10 M9 20V4 M14 20v-8 M19 20V7',
  user: 'M20 21a8 8 0 0 0-16 0 M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10z',
};
const svgIcon = (name, color, size = 24, sw = 2) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round"><path d="${ICON[name]}"/></svg>`;

// ---------- registry of timed elements ----------
const ACTORS = [];
function register(root) {
  root.querySelectorAll('[data-in]').forEach((el) => {
    if (el._actor) return;
    const a = { el, tin: at(el.dataset.in), tout: el.dataset.out ? at(el.dataset.out) : Infinity,
      fx: el.dataset.fx || 'fade', dur: +(el.dataset.dur || 0.6) };
    if (a.fx === 'draw' || a.fx === 'flow') { a.len = el.getTotalLength(); el.style.strokeDasharray = a.fx === 'draw' ? `${a.len} ${a.len}` : '3 15'; }
    if (a.fx === 'type') a.full = el.textContent;
    el._actor = a; ACTORS.push(a);
  });
}
function apply(a, t) {
  const p = clamp((t - a.tin) / a.dur), q = clamp((t - a.tout) / 0.45), vis = ease(p) * (1 - ease(q));
  const el = a.el, s = el.style;
  switch (a.fx) {
    case 'fade': s.opacity = vis; s.transform = `translateY(${(1 - ease(p)) * 22}px)`; break;
    case 'left': s.opacity = vis; s.transform = `translateX(${(1 - ease(p)) * -40}px)`; break;
    case 'pop': s.opacity = clamp(p * 2) * (1 - ease(q)); s.transform = `scale(${0.6 + 0.4 * back(p)})`; break;
    case 'show': s.opacity = vis; break;
    case 'draw': s.strokeDashoffset = a.len * (1 - ease(p)); s.opacity = 1 - ease(q); break;
    case 'flow': s.strokeDashoffset = -t * 42; s.opacity = clamp((t - a.tin) / 0.4) * (1 - ease(q)); break;
    case 'type': { const n = Math.round(a.full.length * clamp(p)); el.textContent = a.full.slice(0, n); s.opacity = (t >= a.tin ? 1 : 0) * (1 - ease(q)); break; }
    case 'count': el.textContent = Math.round(+el.dataset.to * ease(p)); break;
    case 'bar': s.width = (+el.dataset.to * ease(p)) + '%'; break;
    case 'ring': { const L = +el.dataset.len; s.strokeDashoffset = L * (1 - ease(p) * +el.dataset.to / 100); s.opacity = 1 - ease(q); break; }
    case 'pulse': { const on = t >= a.tin && t < a.tout; s.opacity = on ? (0.35 + 0.35 * Math.sin((t - a.tin) * 5)) * clamp((t - a.tin) / 0.3) : 0; break; }
    case 'glow': s.opacity = vis; break;
  }
}

// ---------- chrome ----------
function chrome(t) {
  const i = TL.scenes.findIndex((s) => t >= s.start && t < s.start + s.duration);
  const s = TL.scenes[i < 0 ? TL.scenes.length - 1 : i];
  $('#progress').style.width = (1920 * t / TL.total) + 'px';
  if ($('#chapter').dataset.id !== s.id) {
    $('#chapter').dataset.id = s.id;
    $('#chapter').innerHTML = `<span>${String(s.index + 1).padStart(2, '0')}</span>${s.title}`;
    $('#tech').innerHTML = s.tech.map((x, k) => `<span class="chip" data-k="${k}"><i style="color:${[C.teal, C.violet, C.blue, C.amber, C.pink][k % 5]};background:${[C.teal, C.violet, C.blue, C.amber, C.pink][k % 5]}"></i>${x}</span>`).join('');
    $('#foot').innerHTML = s.footnote ? svgIcon('alert', C.muted, 18) + s.footnote : '';
  }
  const local = t - s.start, out = s.start + s.duration - t;
  const head = ease(local / 0.5) * ease(out / 0.35);
  $('#chapter').style.opacity = head; $('#chapter').style.transform = `translateY(${(1 - ease(local / 0.5)) * 14}px)`;
  document.querySelectorAll('#tech .chip').forEach((c) => { const k = +c.dataset.k; const p = ease((local - 0.25 - k * 0.18) / 0.45); c.style.opacity = p * ease(out / 0.35); c.style.transform = `translateY(${(1 - p) * 10}px)`; });
  $('#foot').style.opacity = ease((local - 1.2) / 0.6) * ease(out / 0.35);
  const cap = CAPS.find((c) => t >= c.a && t < c.b + 0.12);
  const el = $('#caption');
  if (cap) { if (el.textContent !== cap.text) el.textContent = cap.text; el.style.opacity = ease((t - cap.a) / 0.15); }
  else el.style.opacity = 0;
  $('#blob1').style.transform = `translate(${-200 + 120 * Math.sin(t / 9)}px, ${-250 + 80 * Math.cos(t / 11)}px)`;
  $('#blob2').style.transform = `translate(${1400 + 100 * Math.cos(t / 10)}px, ${600 + 90 * Math.sin(t / 8)}px)`;
  document.querySelectorAll('.scene').forEach((el) => {
    const sc = SC[el.dataset.scene], a = (t - sc.start) / 0.35, b = (sc.start + sc.duration + (+el.dataset.linger || 0) - t) / 0.35;
    const o = ease(a) * ease(b); el.style.opacity = o; el.style.display = o > 0 ? '' : 'none';
  });
}

// ---------- scene builders (HTML) ----------
const scenesEl = $('#scenes'), overlays = $('#overlays');
function scene(id, html, linger = 0, layer = scenesEl) {
  const d = document.createElement('div'); d.className = 'scene'; d.dataset.scene = id; d.dataset.linger = linger; d.innerHTML = html; layer.appendChild(d);
}
const FT = { xls: ['XLS', C.green], md: ['MD', C.slate], html: ['WEB', C.orange], doc: ['DOC', C.blue] };
const ftype = (k, size = 54) => `<span class="ftype" style="background:${FT[k][1]};width:${size}px;height:${size}px;font-size:${size < 40 ? 11 : 15}px;border-radius:${size < 40 ? 9 : 14}px">${FT[k][0]}</span>`;

// 1 · the problem
(function () {
  const cards = [
    [120, 210, 'xls', 'Requirements', 'Spreadsheet', 'Every list API must cap its page size on the server.', 'problem.1@0.02'],
    [770, 175, 'md', 'Coding standard', 'Markdown', 'A limit that is accepted and passed through is not a limit.', 'problem.1@0.22'],
    [1410, 250, 'html', 'API specification', 'Web page', 'Patient search takes a caller-supplied limit.', 'problem.1@0.4'],
    [330, 560, 'xls', 'Traceability matrix', 'Spreadsheet', 'Patient search: no upper limit, and no test.', 'problem.1@0.55'],
    [1060, 590, 'doc', 'Outage postmortem', 'Word document', '45 minutes. 60% of requests failed.', 'problem.1@0.82'],
  ];
  let h = '';
  cards.forEach(([x, y, k, title, type, quote, tin], n) => {
    h += `<div class="glass" style="left:${x}px;top:${y}px;width:440px;height:200px;padding:26px 28px" data-in="${tin}" data-fx="pop">
      <div style="display:flex;gap:16px;align-items:center">${ftype(k)}<div><div class="title">${title}</div><div class="sub">${type}</div></div></div>
      <div class="quote" style="margin-top:18px">“${quote}”</div>
      <div class="abs" style="inset:-2px;border-radius:24px;border:2px solid ${n === 4 ? C.red : C.teal};opacity:0" data-in="${n === 4 ? 'problem.3@0.55' : 'problem.2'}" data-out="${n === 4 ? 'problem.end' : 'problem.3@0.5'}" data-fx="show"></div>
    </div>`;
  });
  h += `<div class="badge bad" style="left:1380px;top:568px" data-in="problem.3@0.6" data-fx="pop">${svgIcon('alert', '#3b0a0a', 20)} Something broke</div>`;
  [[650, 420], [1300, 420], [880, 470], [1580, 520]].forEach(([x, y], n) =>
    h += `<div class="abs" style="left:${x}px;top:${y}px;font-size:46px;font-weight:800;color:rgba(141,154,187,.45)" data-in="problem.2@${0.1 + n * 0.15}" data-fx="pop">?</div>`);
  scene('problem', h);
})();

// 2 · upload
(function () {
  const hash = '11a6933f 9fc5c91b 82cca2e7 …';
  let h = `<div class="glass" style="left:120px;top:190px;width:760px;height:660px;border:2px dashed rgba(45,212,191,.45)" data-in="upload" data-fx="show">
      <div class="abs" style="left:0;right:0;top:44px;text-align:center" class="kicker"><span class="kicker" style="color:${C.teal}">${svgIcon('upload', C.teal, 22)}&nbsp; Upload</span></div>
      <div class="abs" style="left:255px;top:150px;width:250px;height:310px;border-radius:24px;background:linear-gradient(160deg,#123a2e,#0c1a1a);border:2px solid ${C.green};box-shadow:0 30px 80px rgba(52,211,153,.25)" data-in="upload.0@0.25" data-fx="fade" data-dur="0.9">
        <div style="height:64px;border-radius:22px 22px 0 0;background:${C.green};display:flex;align-items:center;justify-content:center;font:800 26px var(--sans);color:#052e22">XLSX</div>
        <div style="margin:22px;display:grid;grid-template-columns:repeat(3,1fr);gap:8px">${'<i style="height:26px;border-radius:5px;background:rgba(52,211,153,.18)"></i>'.repeat(15)}</div>
        <div class="abs" style="left:0;right:0;height:4px;top:70px;background:linear-gradient(90deg,transparent,${C.teal},transparent);box-shadow:0 0 22px ${C.teal}" data-in="upload.1@0.05" data-out="upload.1@0.55" data-fx="show" id="scanline"></div>
      </div>
      <div class="abs" style="left:0;right:0;top:500px;text-align:center" data-in="upload.0@0.45">
        <div class="title" style="font-size:30px">Non-functional requirements</div>
        <div class="sub" style="font-size:20px;margin-top:6px">Excel workbook · 7 sheets</div></div>
    </div>`;
  const steps = [
    ['shield', C.green, 'Virus scan', 'ClamAV', '<span class="badge ok" style="position:static">' + svgIcon('check', '#052e22', 18, 3) + ' Clean</span>', 'upload.1@0.15'],
    ['print', C.violet, 'Fingerprint', 'SHA-256 of the file', `<span class="mono" style="font-size:21px;color:${C.violet}" data-in="upload.1@0.6" data-fx="type" data-dur="1.6">${hash}</span>`, 'upload.1@0.5'],
    ['lock', C.teal, 'Stays on this server', 'No outside service involved yet', '<span class="badge ok" style="position:static;background:#2dd4bf">' + svgIcon('check', '#052e22', 18, 3) + ' Local</span>', 'upload.2'],
  ];
  steps.forEach(([ic, col, title, sub, right, tin], n) => {
    h += `<div class="glass" style="left:960px;top:${215 + n * 210}px;width:860px;height:180px;padding:30px 34px;display:flex;align-items:center;gap:26px" data-in="${tin}" data-fx="left">
      <div style="width:84px;height:84px;border-radius:24px;display:flex;align-items:center;justify-content:center;background:${col}22;border:1px solid ${col}66">${svgIcon(ic, col, 42)}</div>
      <div style="flex:1"><div class="title" style="font-size:30px">${title}</div><div class="sub" style="font-size:20px">${sub}</div></div>
      <div style="max-width:360px;text-align:right">${right}</div></div>`;
  });
  scene('upload', h);
})();

// 3 · markdown
(function () {
  const rows = [
    ['', 'A', 'B', 'C', 'h'],
    ['1', '<b>Requirement</b>', '<b>Verification</b>', '<b>On breach</b>'],
    ['2', 'p95 latency under 300ms for reads, 800ms…', 'Load test before each release…', 'Sev-3 incident, capacity…'],
    ['3', 'No unbounded queries. Every endpoint returning a collection takes a limit…', 'Code review and automated test…', 'Blocks release'],
    ['4', '', '', ''], ['5', '', '', ''], ['6', '', '', ''],
  ];
  let grid = '';
  rows.forEach((r) => r.slice(0, 4).forEach((c, k) => grid += `<div class="${r[4] ? 'h' : k === 0 ? 'n' : ''}">${c}</div>`));
  const tabs = ['Overview', 'Narrative', '1. Availability', '2. Performance', '3. Security', '4. Observability', '5. Maintain…'];
  const P = (s) => `<span class="md-p">${s}</span>`;
  const lines = [
    `<span class="md-h">## Overview</span>`,
    `${P('|')} Field ${P('|')} Value ${P('|')}`,
    `${P('|')} Title ${P('|')} Non-functional requirements — CarePath ${P('|')}`,
    ``,
    `<span class="md-h">## 2. Performance</span>`,
    `${P('|')} Requirement ${P('|')} Verification ${P('|')} On breach ${P('|')}`,
    `${P('| --- | --- | --- |')}`,
    `${P('|')} p95 latency under 300ms … ${P('|')} Load test … ${P('|')} Sev-3 … ${P('|')}`,
    `${P('|')} No unbounded queries … ${P('|')} Code review … ${P('|')} Blocks release ${P('|')}`,
    ``,
    `<span class="md-h">## 3. Security and privacy</span>`,
    `${P('|')} Requirement ${P('|')} Verification ${P('|')} On breach ${P('|')}`,
  ];
  let code = '';
  lines.forEach((l, n) => code += `<div data-in="markdown.0@${(0.15 + n * 0.07).toFixed(2)}" data-fx="show" data-dur="0.25"><b>${n + 1}</b>${l || ' '}</div>`);
  let h = `<div class="glass sheet" style="left:80px;top:180px;width:760px;height:470px" data-in="markdown" data-fx="fade">
      <div style="height:54px;background:linear-gradient(90deg,#0f5132,#13603c);display:flex;align-items:center;gap:12px;padding:0 20px;font-size:18px;font-weight:700;color:#d1fae5">${svgIcon('sheet', '#d1fae5', 22)} Non-functional requirements · Excel</div>
      <div style="height:50px;display:flex;align-items:center;padding:0 16px;gap:12px;color:var(--muted);font-size:16px;border-bottom:1px solid rgba(255,255,255,.07)"><i class="mono" style="font-style:normal;color:${C.green}">fx</i> No unbounded queries. Every endpoint returning a collection…</div>
      <div class="grid">${grid}</div>
      <div class="abs" style="left:44px;right:0;top:${104 + 30 + 52 * 2}px;height:52px;background:rgba(167,139,250,.16);border:2px solid ${C.violet};border-radius:6px" data-in="markdown.2" data-fx="show"></div>
      <div class="tabs">${tabs.map((x) => `<span class="${x.startsWith('2.') ? 'on' : ''}">${x}</span>`).join('')}</div>
      <div class="abs" style="left:300px;bottom:2px;width:132px;height:40px;border-radius:8px;border:2px solid ${C.violet};box-shadow:0 0 18px ${C.violet}" data-in="markdown.1" data-out="markdown.2" data-fx="show"></div>
    </div>
    <div class="glass editor" style="left:1010px;top:180px;width:830px;height:470px" data-in="markdown.0" data-fx="fade">
      <div class="bar"><i style="background:#f87171"></i><i style="background:#fbbf24"></i><i style="background:#34d399"></i><span style="margin-left:10px">Markdown · converted text</span>
        <span class="badge" style="position:absolute;right:14px;top:8px;background:rgba(167,139,250,.2);color:${C.violet};border:1px solid ${C.violet}" data-in="markdown.7@0.1" data-fx="pop">${svgIcon('print', C.violet, 18)} SHA-256 fingerprint ✓</span>
        <span class="badge" style="position:absolute;right:14px;top:8px;background:rgba(52,211,153,.16);color:${C.green};border:1px solid ${C.green}" data-in="markdown.5@0.1" data-out="markdown.7" data-fx="pop">${svgIcon('shield', C.green, 18)} Sealed process · no network</span></div>
      <div class="hl" style="top:${50 + 14 + 34 * 8}px;background:rgba(167,139,250,.16);border:2px solid ${C.violet};box-shadow:0 0 24px rgba(167,139,250,.35)" data-in="markdown.2" data-fx="show"></div>
      ${[0, 4, 10].map((n) => `<div class="hl" style="top:${50 + 14 + 34 * n}px;background:rgba(167,139,250,.14)" data-in="markdown.1@0.05" data-out="markdown.2" data-fx="show"></div>`).join('')}
      ${[7, 8].map((n) => `<div class="hl" style="top:${50 + 14 + 34 * n}px;background:rgba(45,212,191,.12)" data-in="markdown.1@0.5" data-out="markdown.2" data-fx="show"></div>`).join('')}
      <div class="code">${code}</div>
    </div>
    <svg class="abs" style="left:840px;top:380px" width="170" height="60"><path d="M10 30 H158" stroke="${C.teal}" stroke-width="4" fill="none" stroke-linecap="round" data-in="markdown.0@0.05" data-fx="draw"/>
      <path d="M10 30 H158" stroke="#fff" stroke-width="4" fill="none" stroke-linecap="round" data-in="markdown.0@0.5" data-fx="flow"/><path d="M146 18 L160 30 L146 42" stroke="${C.teal}" stroke-width="4" fill="none" stroke-linecap="round" data-in="markdown.0@0.4" data-fx="show"/></svg>
    <div class="badge" style="left:842px;top:322px;background:rgba(45,212,191,.16);color:${C.teal};border:1px solid ${C.teal}" data-in="markdown.0" data-fx="pop">MarkItDown</div>
    <div class="glass" style="left:1010px;top:680px;width:830px;height:180px;padding:24px 30px;border-color:rgba(167,139,250,.5)" data-in="markdown.3" data-fx="fade">
      <div class="kicker" style="color:${C.violet}">The rule we will follow</div>
      <div style="font-size:31px;font-weight:700;margin-top:12px;line-height:1.3">Every list API must cap its page size on the server.</div></div>
    <div class="glass" style="left:80px;top:680px;width:760px;height:180px;padding:24px 30px" data-in="markdown.6" data-fx="fade">
      <div style="display:flex;justify-content:space-between;align-items:center"><div class="kicker" style="color:${C.amber}">Scanned page? Tesseract OCR</div><span class="sub" style="font-size:15px;border:1px solid var(--line);border-radius:99px;padding:3px 10px">example</span></div>
      <div class="mono" style="font-size:22px;margin-top:16px;color:#ffe7a8">## Page 3 (read by OCR, 91% confidence)</div>
      <div class="sub" style="margin-top:10px">Low confidence is flagged by the source check.</div></div>`;
  scene('markdown', h);
})();

// 4 · quality
(function () {
  const checks = [
    ['Readable text', '4,734 characters of text were read.', 20],
    ['Tables the graph can read', '7 tables and 24 rows, every column named.', 25],
    ['Structure', '7 headings and 7 tables.', 15],
    ['Says what it is', 'Gives its document number, version, owner and status.', 15],
    ['Finished', 'Nothing is left as TBD or TODO.', 15],
    ['Within limits', '31 rows and lines, all within what the graph reads.', 10],
  ];
  let h = `<div class="glass" style="left:80px;top:180px;width:960px;height:690px" data-in="quality" data-fx="fade">
    <div style="padding:28px 34px 0"><div class="kicker" style="color:${C.teal}">Source check</div>
    <div class="title" style="font-size:30px;margin-top:8px">Non-functional requirements · Excel</div></div>`;
  checks.forEach(([lab, fin, pts], n) => {
    const tin = `quality.2@${(0.02 + n * 0.15).toFixed(2)}`;
    h += `<div class="qrow" style="top:${120 + n * 94}px" data-in="${tin}" data-fx="left">
      <div class="ic">${svgIcon('check', C.green, 24, 3)}</div>
      <div><div class="lab">${lab}</div><div class="fin">${fin}</div></div>
      <div><div class="pts"><span data-in="${tin}" data-fx="count" data-to="${pts}" data-dur="0.9">0</span> / ${pts}</div><div class="track"><i data-in="${tin}" data-fx="bar" data-to="100" data-dur="0.9"></i></div></div></div>`;
  });
  h += `</div>`;
  const ring = (cx, val, col, tin, size) => {
    const r = size / 2 - 14, L = 2 * Math.PI * r;
    return `<svg width="${size}" height="${size}" style="transform:rotate(-90deg)"><circle cx="${size / 2}" cy="${size / 2}" r="${r}" stroke="rgba(255,255,255,.08)" stroke-width="16" fill="none"/>
      <circle cx="${size / 2}" cy="${size / 2}" r="${r}" stroke="${col}" stroke-width="16" fill="none" stroke-linecap="round" stroke-dasharray="${L} ${L}" style="stroke-dashoffset:${L};filter:drop-shadow(0 0 10px ${col})" data-in="${tin}" data-fx="ring" data-len="${L}" data-to="${val}" data-dur="1.4"/></svg>`;
  };
  h += `<div class="glass" style="left:1080px;top:180px;width:760px;height:330px;padding:30px" data-in="quality.1" data-fx="fade">
      <div class="abs" style="left:40px;top:40px">${ring(0, 100, C.green, 'quality.4', 250)}
        <div class="abs" style="left:0;top:0;width:250px;height:250px;display:flex;flex-direction:column;align-items:center;justify-content:center">
          <div style="font-size:76px;font-weight:800;line-height:1"><span data-in="quality.4" data-fx="count" data-to="100" data-dur="1.4">0</span></div><div class="sub">out of 100</div></div></div>
      <div class="abs" style="left:330px;top:58px;width:400px">
        <div class="kicker" style="color:${C.muted}">Six fixed checks</div>
        <div class="title" style="font-size:28px;margin-top:10px;line-height:1.3">Same file, same score. No AI involved.</div>
        <span class="badge ok" style="position:relative;margin-top:22px" data-in="quality.4@0.5" data-fx="pop">${svgIcon('check', '#052e22', 20, 3)} Ready</span></div>
    </div>
    <div class="glass" style="left:1080px;top:540px;width:760px;height:330px;padding:30px;border-color:rgba(251,191,36,.4)" data-in="quality.5" data-fx="fade">
      <div class="abs" style="left:40px;top:58px">${ring(0, 60, C.amber, 'quality.5@0.2', 200)}
        <div class="abs" style="left:0;top:0;width:200px;height:200px;display:flex;flex-direction:column;align-items:center;justify-content:center">
          <div style="font-size:60px;font-weight:800;line-height:1;color:${C.amber}"><span data-in="quality.5@0.2" data-fx="count" data-to="60" data-dur="1.4">0</span></div><div class="sub">Needs work</div></div></div>
      <div class="abs" style="left:280px;top:40px;width:450px">
        <div class="kicker" style="color:${C.amber}">A raw export · Excel</div>
        <div style="margin-top:14px;font-size:20px;line-height:1.4"><b style="color:${C.amber}">0 / 25</b> Tables: placeholder column names, repeated rows</div>
        <div style="margin-top:12px;font-size:20px;line-height:1.4"><b style="color:${C.amber}">0 / 15</b> No document number, version, owner, date or status</div></div>
    </div>
    <div class="badge" style="left:1590px;top:448px;background:rgba(45,212,191,.16);color:${C.teal};border:1px solid ${C.teal}" data-in="quality.6" data-fx="pop">Suggests · never blocks</div>`;
  scene('quality', h);
})();

// ---------- the graph ----------
const NS = 'http://www.w3.org/2000/svg';
const POS = {};
function el(tag, attrs, parent) { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; }
function textWidth(str, size, weight = 600) { const c = textWidth.c || (textWidth.c = document.createElement('canvas').getContext('2d')); c.font = `${weight} ${size}px "Segoe UI Variable Display", "Segoe UI"`; return c.measureText(str).width; }
// kind: 'circle' (r) or 'pill' (value). type: small caption under label.
function node(group, id, x, y, r, label, color, icon, tin, opts = {}) {
  POS[id] = { x, y, r, group };
  const g = el('g', { 'data-in': tin, 'data-fx': 'pop', 'data-dur': '0.7' }, $(group));
  g.style.transformBox = 'fill-box'; g.style.transformOrigin = 'center'; g.style.opacity = 0;
  if (opts.out) g.dataset.out = opts.out;
  if (opts.pill) {
    const w = Math.max(textWidth(label, 19) + 66, 120), h = 54;
    POS[id].r = h / 2 + 4; POS[id].w = w;
    el('rect', { x: x - w / 2 - 6, y: y - h / 2 - 6, width: w + 12, height: h + 12, rx: 33, fill: color, opacity: 0.16, filter: 'url(#glow)' }, g);
    el('rect', { x: x - w / 2, y: y - h / 2, width: w, height: h, rx: 27, fill: 'url(#nodeFill)', stroke: color, 'stroke-width': 2.5 }, g);
    const ic = el('g', { transform: `translate(${x - w / 2 + 16},${y - 11}) scale(.92)` }, g); ic.innerHTML = `<path d="${ICON[icon]}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
    const t = el('text', { x: x - w / 2 + 48, y: y + 7, 'font-size': 19, 'font-weight': 600, fill: '#f1f5ff' }, g); t.textContent = label;
    return g;
  }
  el('circle', { cx: x, cy: y, r: r + 12, fill: color, opacity: 0.22, filter: 'url(#glow)' }, g);
  el('circle', { cx: x, cy: y, r, fill: 'url(#nodeFill)', stroke: color, 'stroke-width': 3 }, g);
  const s = r / 24 * 0.85;
  const ic = el('g', { transform: `translate(${x - 12 * s},${y - 12 * s}) scale(${s})` }, g);
  ic.innerHTML = `<path d="${ICON[icon]}" fill="none" stroke="${color}" stroke-width="${2 / Math.max(s, 1) + 0.4}" stroke-linecap="round" stroke-linejoin="round"/>`;
  const lines = label.split('\n'), size = opts.size || 20;
  const top = opts.above ? y - r - 22 - (lines.length - 1) * (size + 4) - (opts.type ? 20 : 0) : y + r + 28;
  lines.forEach((ln, k) => { const t = el('text', { x, y: top + k * (size + 4), 'text-anchor': 'middle', 'font-size': size, 'font-weight': 650, fill: '#f1f5ff' }, g); t.textContent = ln; });
  if (opts.type) { const t = el('text', { x, y: top + lines.length * (size + 4) + 1, 'text-anchor': 'middle', 'font-size': 13.5, 'font-weight': 700, fill: color, 'letter-spacing': '0.14em' }, g); t.textContent = opts.type.toUpperCase(); }
  if (opts.ring) { const ring = el('circle', { cx: x, cy: y, r: r + 9, fill: 'none', stroke: color, 'stroke-width': 2.5, 'stroke-dasharray': '6 7', 'data-in': opts.ring[0], 'data-out': opts.ring[1], 'data-fx': 'pulse' }, g); ring.style.opacity = 0; }
  return g;
}
function trim(a, b, ra, rb, bend) {
  const dx = b.x - a.x, dy = b.y - a.y, L = Math.hypot(dx, dy), ux = dx / L, uy = dy / L;
  const s = { x: a.x + ux * ra, y: a.y + uy * ra }, e = { x: b.x - ux * rb, y: b.y - uy * rb };
  const mx = (s.x + e.x) / 2 - uy * bend * L, my = (s.y + e.y) / 2 + ux * bend * L;
  const at_ = (u) => ({ x: (1 - u) * (1 - u) * s.x + 2 * (1 - u) * u * mx + u * u * e.x, y: (1 - u) * (1 - u) * s.y + 2 * (1 - u) * u * my + u * u * e.y });
  return { d: `M${s.x},${s.y} Q${mx},${my} ${e.x},${e.y}`, mid: at_(0.5), at_, e, ux, uy, m: { x: mx, y: my } };
}
// screen position of a node, accounting for the group transform it will have at the time an edge is drawn
const XF = { '#g-struct': { x: 150, y: 0, s: 1 } };
function scr(id) { const p = POS[id], f = XF[p.group] || { x: 0, y: 0, s: 1 }; return { x: f.x + p.x * f.s, y: f.y + p.y * f.s, r: (p.r + 6) * f.s }; }
function edge(group, a, b, label, color, tin, opts = {}) {
  const A = opts.screen ? scr(a) : { ...POS[a], r: POS[a].r + 6 }, B = opts.screen ? scr(b) : { ...POS[b], r: POS[b].r + 6 };
  const g = el('g', {}, $(group));
  const reach = (id, P, dx, dy, f) => { const q = POS[id]; if (!q.w) return P.r; const L = Math.hypot(dx, dy); return Math.min((q.w / 2 + 6) * f / Math.abs(dx / L || 1e-6), (q.r) * f / Math.abs(dy / L || 1e-6)); };
  const fa = opts.screen ? (XF[POS[a].group] || { s: 1 }).s : 1, fb = opts.screen ? (XF[POS[b].group] || { s: 1 }).s : 1;
  const geo = trim(A, B, reach(a, A, B.x - A.x, B.y - A.y, fa), reach(b, B, B.x - A.x, B.y - A.y, fb) + 4, opts.bend ?? 0.08);
  const w = opts.dashed ? 2 : 3;
  const base = el('path', { d: geo.d, fill: 'none', stroke: color, 'stroke-width': w, 'stroke-linecap': 'round', opacity: opts.dashed ? 0.55 : 0.9, 'data-in': tin, 'data-fx': 'draw', 'data-dur': '0.9' }, g);
  if (opts.out) base.dataset.out = opts.out;
  if (opts.dashed) { base.setAttribute('stroke-dasharray', '5 8'); base.dataset.fx = 'show'; base.style.opacity = 0; }
  if (!opts.dashed) {
    const fl = el('path', { d: geo.d, fill: 'none', stroke: '#ffffff', 'stroke-opacity': 0.7, 'stroke-width': 3, 'stroke-linecap': 'round', 'data-in': tin + '+0.9', 'data-fx': 'flow' }, g);
    if (opts.out) fl.dataset.out = opts.out;
    const ah = el('path', { d: `M${geo.e.x - geo.ux * 13 - geo.uy * 7},${geo.e.y - geo.uy * 13 + geo.ux * 7} L${geo.e.x},${geo.e.y} L${geo.e.x - geo.ux * 13 + geo.uy * 7},${geo.e.y - geo.uy * 13 - geo.ux * 7}`, fill: 'none', stroke: color, 'stroke-width': 3, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'data-in': tin + '+0.75', 'data-fx': 'show' }, g);
    if (opts.out) ah.dataset.out = opts.out; ah.style.opacity = 0;
  }
  if (opts.hot) {
    const hot = el('path', { d: geo.d, fill: 'none', stroke: color, 'stroke-width': 10, 'stroke-linecap': 'round', filter: 'url(#glow)', 'data-in': opts.hot[0], 'data-out': opts.hot[1], 'data-fx': 'glow' }, g); hot.style.opacity = 0;
  }
  if (label) {
    const size = opts.size || 17, tw = textWidth(label, size, 700) + 22;
    if (opts.at) geo.mid = geo.at_(opts.at);
    const lg = el('g', { 'data-in': tin + '+0.6', 'data-fx': 'show' }, g); lg.style.opacity = 0; if (opts.out) lg.dataset.out = opts.out;
    el('rect', { x: geo.mid.x - tw / 2, y: geo.mid.y - 15, width: tw, height: 30, rx: 15, fill: '#0b1226', stroke: color, 'stroke-opacity': 0.6 }, lg);
    const t = el('text', { x: geo.mid.x, y: geo.mid.y + 6, 'text-anchor': 'middle', 'font-size': size, 'font-weight': 700, fill: color }, lg); t.textContent = label;
  }
  return g;
}

// structure graph of the requirements spreadsheet (graph space, shifted by #g-struct's transform)
(function () {
  const G = '#g-struct';
  node(G, 'D', 330, 520, 56, 'Non-functional\nrequirements', C.green, 'sheet', 'structure.1', { type: 'document' });
  node(G, 'S1', 150, 330, 36, 'Availability', C.section, 'hash', 'structure.1@0.45', { type: 'section', size: 18, above: true });
  node(G, 'S2', 360, 270, 36, 'Performance', C.section, 'hash', 'structure.1@0.6', { type: 'section', size: 18, above: true });
  node(G, 'S3', 570, 330, 36, 'Security & privacy', C.section, 'hash', 'structure.1@0.75', { type: 'section', size: 18, above: true });
  ['S1', 'S2', 'S3'].forEach((s, k) => edge(G, 'D', s, k === 1 ? 'contains section' : '', C.section, `structure.1@${0.5 + k * 0.15}`, { bend: 0 }));
  node(G, 'R', 760, 520, 50, 'No unbounded\nqueries', C.teal, 'row', 'structure.2@0.25', { type: 'record', ring: ['structure.3', 'structure.4'] });
  edge(G, 'D', 'R', 'contains record', C.teal, 'structure.2@0.3', { bend: 0 });
  node(G, 'VV', 1130, 330, 0, 'Verification: code review + automated test', C.amber, 'tag', 'structure.3@0.15', { pill: true });
  node(G, 'VB', 1180, 590, 0, 'On breach: Blocks release', C.amber, 'tag', 'structure.3@0.6', { pill: true });
  edge(G, 'R', 'VV', 'Verification', C.amber, 'structure.3@0.2', { bend: -0.05 });
  edge(G, 'R', 'VB', 'On breach', C.amber, 'structure.3@0.65', { bend: 0.04 });
  const others = [['RA', 700, 780, 'Authentication\neverywhere'], ['RS', 950, 800, 'Secrets as\nfiles'], ['RC', 1300, 770, '85% line\ncoverage']];
  others.forEach(([id, x, y, label], k) => {
    node(G, id, x, y, 34, label, C.teal, 'row', `structure.4@${k * 0.12}`, { type: 'record', size: 17 });
    edge(G, id, 'VB', k === 0 ? 'On breach' : '', C.amber, `structure.4@${0.1 + k * 0.12}`, { bend: 0.06 });
  });
  // the shared value pulses once three other rules arrive
  const vb = POS.VB; const halo = el('rect', { x: vb.x - vb.w / 2 - 12, y: vb.y - 39, width: vb.w + 24, height: 78, rx: 39, fill: 'none', stroke: C.amber, 'stroke-width': 2.5, 'stroke-dasharray': '6 7', 'data-in': 'structure.4@0.5', 'data-out': 'structure.5', 'data-fx': 'pulse' }, $(G)); halo.style.opacity = 0;
  // provenance tag
  const tag = el('g', { 'data-in': 'structure.5@0.1', 'data-out': 'files', 'data-fx': 'show' }, $(G)); tag.style.opacity = 0;
  el('rect', { x: 500, y: 395, width: 330, height: 44, rx: 12, fill: '#0b1226', stroke: C.teal, 'stroke-opacity': 0.7 }, tag);
  const tt = el('text', { x: 520, y: 424, 'font-size': 19, 'font-weight': 600, fill: '#d6fff8' }, tag); tt.textContent = 'line 30 · source fingerprint ✓';
})();
// structure-scene banner (HTML)
scene('structure', `<div class="badge" style="left:1500px;top:190px;background:rgba(45,212,191,.16);color:${C.teal};border:1px solid ${C.teal}" data-in="structure.6" data-fx="pop">${svgIcon('check', C.teal, 18, 3)} No AI · no cost</div>
  <div class="badge" style="left:1492px;top:572px;background:rgba(251,191,36,.14);color:${C.amber};border:1px solid ${C.amber}" data-in="structure.4@0.55" data-fx="pop">Shared value · linked for free</div>`);

// four more documents (screen space)
XF['#g-struct'] = { x: 40, y: 150, s: 0.55 };
(function () {
  const docs = [['CS', 880, 'Coding\nstandard', C.slate, 'code', 'files.0@0.3'], ['SPEC', 1130, 'API\nspecification', C.orange, 'globe', 'files.0@0.45'],
    ['TM', 1380, 'Traceability\nmatrix', C.green, 'sheet', 'files.0@0.62'], ['PM', 1630, 'Outage\npostmortem', C.blue, 'file', 'files.0@0.82']];
  docs.forEach(([id, x, label, col, ic, tin]) => {
    node('#g-docs', id, x, 250, 44, label, col, ic, tin, { size: 18 });
    for (let k = 0; k < 3; k++) {
      const dx = (k - 1) * 46, dot = el('circle', { cx: x + dx, cy: 395, r: 8, fill: C.section, opacity: 0, 'data-in': `files.1@${0.2 + k * 0.15}`, 'data-out': 'ai', 'data-fx': 'show' }, $('#g-docs'));
      const ln = el('path', { d: `M${x},${300 + 52} L${x + dx},${387}`, stroke: C.section, 'stroke-width': 1.5, opacity: 0, 'data-in': `files.1@${0.2 + k * 0.15}`, 'data-out': 'ai', 'data-fx': 'show' }, $('#g-docs'));
    }
    const b = el('g', { 'data-in': `files.2@${0.1 + docs.findIndex((d) => d[0] === id) * 0.15}`, 'data-fx': 'pop' }, $('#g-docs')); b.style.opacity = 0; b.style.transformBox = 'fill-box'; b.style.transformOrigin = 'center';
    el('rect', { x: x + 26, y: 196, width: 62, height: 28, rx: 14, fill: C.green }, b);
    const t = el('text', { x: x + 57, y: 216, 'text-anchor': 'middle', 'font-size': 17, 'font-weight': 800, fill: '#052e22' }, b); t.textContent = '100';
  });
  // the source check: three files point to the rule (requirements sheet defines it; two mention it)
  edge('#g-ev', 'CS', 'R', '', C.violet, 'files.3@0.15', { screen: true, dashed: true, bend: 0.12, out: 'ai' });
  edge('#g-ev', 'TM', 'R', '', C.violet, 'files.3@0.3', { screen: true, dashed: true, bend: 0.18, out: 'ai' });
})();
XF['#g-docs'] = { x: 0, y: 0, s: 1 }; XF['#g-biz'] = { x: 0, y: 0, s: 1 };
scene('files', `<div class="badge" style="left:470px;top:160px;background:rgba(167,139,250,.16);color:${C.violet};border:1px solid ${C.violet}" data-in="files.3@0.35" data-fx="pop">${svgIcon('search', C.violet, 18)} 3 files point to the same rule</div>
  <div class="glass" style="left:830px;top:470px;width:1030px;height:400px;padding:30px 36px" data-in="files.4@0.2" data-fx="fade">
    <div class="kicker" style="color:${C.amber}">Read together</div>
    <div class="abs" style="left:60px;right:60px;top:150px;height:4px;border-radius:4px;background:linear-gradient(90deg,${C.amber},${C.red})"></div>
    ${[['28 May', 'Test review', 'Patient search has no upper limit, and no test.', 'Traceability matrix', C.amber, 'files.5'],
      ['2 June', 'Release review', 'Waived for the release.', 'Outage postmortem', C.orange, 'files.6'],
      ['14 Aug', 'Outage', 'A search asks for one lakh records. The whole table is read.', 'Outage postmortem', C.red, 'files.7']].map(([d, k, txt, src, col, tin], n) =>
      `<div class="abs" style="left:${36 + n * 330}px;top:100px;width:305px" data-in="${tin}" data-fx="pop">
        <div style="width:28px;height:28px;border-radius:50%;background:${col};box-shadow:0 0 22px ${col};margin:24px 0 0 20px"></div>
        <div style="font-size:30px;font-weight:800;margin-top:18px;color:${col}">${d}</div>
        <div style="font-size:21px;font-weight:700;margin-top:4px">${k}</div>
        <div style="font-size:19px;color:#cfd8f5;margin-top:6px;line-height:1.35">${txt}</div>
        <div class="sub" style="font-size:15px;margin-top:8px">${svgIcon('file', C.muted, 14)} ${src}</div></div>`).join('')}
    <div class="badge bad" style="right:36px;top:24px;font-size:22px;padding:10px 18px" data-in="files.8@0.2" data-fx="pop">${svgIcon('flame', '#3b0a0a', 22)} 45 min · 60% of requests failed</div>
  </div>`);

// AI business layer (screen space)
(function () {
  const B = '#g-biz';
  node(B, 'RULE', 1000, 640, 52, 'Page-size rule', C.violet, 'book', 'ai.2@0.55', { type: 'requirement' });
  node(B, 'API', 1300, 480, 52, 'Patient search API', C.cyan, 'plug', 'ai.2@0.1', { type: 'component' });
  node(B, 'OUT', 1600, 560, 52, 'August outage', C.red, 'alert', 'ai.2@0.05', { type: 'incident' });
  node(B, 'STD', 700, 700, 44, 'Bound every list', C.pink, 'scale', 'ai.3@0.05', { type: 'control', size: 18 });
  node(B, 'TX', 1790, 780, 40, 'Two-second\ntransaction rule', C.violet, 'clock', 'ai.3@0.45', { type: 'requirement', size: 17 });
  const hot = ['search.3@0.3', 'publish.6'];
  edge(B, 'OUT', 'API', 'affects', C.red, 'ai.2@0.3', { bend: 0.1, hot });
  edge(B, 'API', 'RULE', 'violates', C.cyan, 'ai.2@0.7', { bend: -0.08, hot });
  edge(B, 'STD', 'RULE', 'references', C.pink, 'ai.3@0.2', { bend: 0.06 });
  edge(B, 'OUT', 'TX', 'violates', C.red, 'ai.3@0.6', { bend: -0.1, at: 0.7 });
  // evidence back to the sources (dashed)
  [['RULE', 'R', 'ai.2@0.8'], ['API', 'SPEC', 'ai.5@0.3'], ['OUT', 'PM', 'ai.2@0.4'], ['STD', 'CS', 'ai.3@0.3']]
    .forEach(([a, b, tin]) => edge('#g-ev', a, b, '', C.muted, tin, { screen: true, dashed: true, bend: 0.05 }));
  // rejected proposal
  edge(B, 'OUT', 'RULE', 'caused by', C.red, 'ai.4@0.05', { bend: -0.25, dashed: true, out: 'ai.5' });
  // merge: two mentions of the same API travel into one node
  [['SPEC', 1130, 300], ['PM', 1630, 300]].forEach(([src, x, y]) => {
    const g = el('g', {}, $(B)); g.dataset.merge = `${x},${y}`;
    el('circle', { cx: 0, cy: 0, r: 22, fill: 'url(#nodeFill)', stroke: C.cyan, 'stroke-width': 2.5 }, g);
    g.style.opacity = 0;
  });
})();
function mergeFx(t) {
  const t0 = at('ai.5@0.05'), t1 = at('ai.5@0.6');
  document.querySelectorAll('[data-merge]').forEach((g) => {
    const [x, y] = g.dataset.merge.split(',').map(Number), p = ease((t - t0) / (t1 - t0));
    g.setAttribute('transform', `translate(${lerp(x, 1300, p)},${lerp(y, 480, p)})`);
    g.style.opacity = t < t0 ? 0 : t < t1 ? 1 : clamp(1 - (t - t1) / 0.3);
  });
}
const types = ['Ticket', 'Requirement', 'Control', 'Component', 'Code', 'Runbook', 'Incident', 'Change'];
const rels = ['depends on', 'affects', 'violates', 'required by', 'implemented in', 'covers', 'is on', 'modifies', 'fixes', 'deployed to', 'verifies', 'references'];
const tcol = [C.amber, C.violet, C.pink, C.cyan, C.blue, C.green, C.red, C.orange];
scene('ai', `<div class="glass" style="left:820px;top:350px;width:1030px;height:500px;padding:34px 40px" data-in="ai.1@0.02" data-out="ai.2" data-fx="fade">
    <div class="kicker" style="color:${C.violet}">${svgIcon('spark', C.violet, 18)} Fixed vocabulary</div>
    <div class="title" style="font-size:24px;margin-top:22px">8 types</div>
    <div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:12px">${types.map((x, k) => `<span class="chip" style="border-color:${tcol[k]}88;color:${tcol[k]}">${x}</span>`).join('')}</div>
    <div class="title" style="font-size:24px;margin-top:26px">12 relationships</div>
    <div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:12px">${rels.map((x) => `<span class="chip" style="${['affects', 'violates', 'verifies'].includes(x) ? 'background:rgba(167,139,250,.22);border-color:#a78bfa' : ''}">${x}</span>`).join('')}</div>
  </div>
  <div class="glass" style="left:80px;top:680px;width:540px;height:160px;padding:20px 24px;border-color:rgba(52,211,153,.5)" data-in="ai.3@0.1" data-out="ai.4" data-fx="fade">
    <div class="kicker" style="color:${C.green}">${svgIcon('check', C.green, 16, 3)} Quote found word for word</div>
    <div style="font-size:18px;line-height:1.4;margin-top:10px;font-style:italic;color:#d9e2ff">“GET /patients accepts a limit query parameter and passes it to the database unchanged.”</div></div>
  <div class="glass" style="left:80px;top:680px;width:540px;height:160px;padding:20px 24px;border-color:rgba(248,113,113,.6)" data-in="ai.4@0.1" data-out="ai.5" data-fx="fade">
    <div class="kicker" style="color:${C.red}">${svgIcon('x', C.red, 16, 3)} Rejected</div>
    <div style="font-size:21px;line-height:1.4;margin-top:10px">“caused by” is not in the vocabulary, and no quote supports it.</div></div>
  <div class="glass" style="left:80px;top:680px;width:540px;height:160px;padding:20px 24px;border-color:rgba(34,211,238,.6)" data-in="ai.5@0.4" data-out="ai.6" data-fx="fade">
    <div class="kicker" style="color:${C.cyan}">${svgIcon('layers', C.cyan, 16)} Merged</div>
    <div style="font-size:21px;line-height:1.4;margin-top:10px">Same type, same words, two files: one node. Each keeps its own quote.</div></div>
  <div class="glass" style="left:80px;top:680px;width:540px;height:160px;padding:20px 24px" data-in="ai.6@0.1" data-fx="fade">
    <div class="kicker" style="color:${C.amber}">${svgIcon('scale', C.amber, 16)} Spend control</div>
    <div style="font-size:21px;line-height:1.4;margin-top:10px">Only new or changed files go to the model. Monthly budget checked first.</div></div>`, 0);

// 8 · search panel
scene('search', `
  <div class="glass" style="left:1260px;top:175px;width:600px;height:225px;padding:24px 28px" data-in="search.1" data-fx="left">
    <div class="kicker" style="color:${C.teal}">${svgIcon('search', C.teal, 16)} Keyword index · BM25</div>
    <div class="sub" style="margin-top:6px">Always built · fast · free</div>
    ${[['page', 4], ['limit', 6], ['outage', 3]].map(([w, n], k) => `<div style="display:flex;align-items:center;gap:14px;margin-top:${k ? 8 : 16}px"><span class="mono" style="width:90px;font-size:19px;color:#d6fff8">${w}</span>
      <div class="track" style="flex:1;margin:0;height:12px"><i data-in="search.1@${0.3 + k * 0.1}" data-fx="bar" data-to="${n * 15}"></i></div><span class="sub" style="width:96px">${n} passages</span></div>`).join('')}
  </div>
  <div class="glass" style="left:1260px;top:420px;width:600px;height:240px;padding:24px 28px" data-in="search.2" data-fx="left">
    <div style="display:flex;justify-content:space-between"><div class="kicker" style="color:${C.violet}">${svgIcon('vector', C.violet, 16)} Embeddings · optional</div><span class="sub" style="font-size:15px">off by default</span></div>
    <div class="sub" style="margin-top:6px">OpenAI vectors · NumPy similarity · match by meaning</div>
    <div style="display:flex;align-items:flex-end;gap:7px;height:70px;margin-top:16px">${[.3, .8, .45, .65, .9, .3, .55, .7, .25, .6, .4, .85, .5, .35, .75, .2].map((v, k) => `<i data-in="search.2@${0.3 + k * 0.02}" data-fx="pop" style="display:block;width:24px;height:${v * 70}px;border-radius:5px;background:linear-gradient(180deg,${C.violet},#6d4fd6)"></i>`).join('')}</div>
    <div class="mono" style="font-size:18px;color:${C.violet};margin-top:10px">[0.12, −0.34, 0.81, …]</div>
  </div>
  <div class="glass" style="left:1260px;top:680px;width:600px;height:190px;padding:24px 28px" data-in="search.3" data-fx="left">
    <div class="kicker" style="color:${C.amber}">${svgIcon('layers', C.amber, 16)} Combine, then walk</div>
    <div style="font-size:21px;line-height:1.45;margin-top:12px">Both rankings fused. From the best matches, walk up to <b style="color:${C.amber}">two hops</b> along typed relationships.</div>
    <div class="sub" style="margin-top:8px">outage → affects → API → violates → rule</div></div>`);

// 9 · draft panel + themes
scene('draft', `
  <div class="glass" style="left:1260px;top:175px;width:600px;height:150px;padding:24px 28px" data-in="draft.0" data-fx="left">
    <div class="kicker" style="color:${C.blue}">Numbered drafts</div>
    <div style="display:flex;gap:12px;margin-top:18px">${['v1', 'v2', 'v3'].map((v) => `<span class="chip" style="color:var(--muted)">${v}</span>`).join('')}<span class="chip" style="border-color:${C.blue};color:${C.blue};background:rgba(96,165,250,.15)" data-in="draft.0@0.4" data-fx="pop">v4 · draft</span></div></div>
  <div class="glass" style="left:1260px;top:345px;width:600px;height:150px;padding:24px 28px" data-in="draft.1" data-fx="left">
    <div class="kicker" style="color:${C.pink}">Graphify · NetworkX · Leiden</div>
    <div style="font-size:21px;margin-top:14px;line-height:1.4">Groups what exists into themes. Adds no facts.</div></div>
  <div class="glass" style="left:1260px;top:515px;width:600px;height:355px;padding:24px 28px" data-in="draft.2" data-fx="left">
    <div class="kicker" style="color:${C.green}">Benchmark · runs on its own</div>
    <div style="display:flex;align-items:center;gap:30px;margin-top:20px">
      <div style="width:330px">${Array.from({ length: 16 }, (_, k) => `<span class="cell" ${k < 15 ? `data-in="draft.2@${(0.35 + k * 0.03).toFixed(2)}" data-fx="pop" style="background:${C.green};box-shadow:0 0 12px rgba(52,211,153,.5)"` : ''}></span>`).join('')}</div>
      <div><div style="font-size:72px;font-weight:800;line-height:1"><span data-in="draft.2@0.35" data-fx="count" data-to="15" data-dur="1.6">0</span><span style="color:var(--muted);font-size:40px"> / 16</span></div>
      <div class="sub" style="margin-top:8px">test questions: right<br>document found</div></div></div></div>`);
(function () {
  const H = $('#g-halo');
  [[420, 430, 360, 210, C.teal, 'Performance rules'], [1000, 610, 420, 200, C.cyan, 'Patient search'], [1700, 680, 190, 220, C.red, 'August outage']].forEach(([x, y, rx, ry, col, label], k) => {
    const g = el('g', { 'data-in': `draft.1@${0.15 + k * 0.15}`, 'data-out': 'publish', 'data-fx': 'show' }, H); g.style.opacity = 0;
    el('ellipse', { cx: x, cy: y, rx, ry, fill: col, opacity: 0.16, filter: 'url(#soft)' }, g);
    const t = el('text', { x, y: k === 0 ? y + ry + 50 : y - ry + 18, 'text-anchor': 'middle', 'font-size': 26, 'font-weight': 800, fill: col, 'letter-spacing': '0.1em' }, g); t.textContent = label.toUpperCase();
  });
})();

// 10 · publish and ask
scene('publish', `
  <div class="glass" style="left:1260px;top:175px;width:600px;height:120px;padding:22px 28px;display:flex;align-items:center;justify-content:space-between" data-in="publish" data-out="publish.6" data-fx="left">
    <div><div class="kicker" style="color:${C.muted}">${svgIcon('user', C.muted, 16)} Reviewed by a person</div><div class="title" style="margin-top:6px">Version 4</div></div>
    <span class="badge" style="position:relative;background:${C.teal};color:#052e22;font-size:20px" data-in="publish.0@0.1" data-out="publish.0@0.7" data-fx="show">Publish</span>
    <span class="badge ok" style="position:absolute;right:28px;font-size:20px" data-in="publish.0@0.7" data-fx="pop">${svgIcon('check', '#052e22', 18, 3)} Published</span></div>
  <div class="glass" style="left:1260px;top:315px;width:600px;height:90px;padding:0 26px;display:flex;align-items:center;gap:14px;border-color:rgba(96,165,250,.5)" data-in="publish.2" data-out="publish.6" data-fx="left">
    ${svgIcon('search', C.blue, 24)}<span style="font-size:22px;font-weight:600" data-in="publish.2@0.15" data-fx="type" data-dur="2.4">Why did the service go down on 14 August?</span></div>
  <div class="glass" style="left:1260px;top:425px;width:600px;height:445px;padding:24px 28px" data-in="publish.3" data-out="publish.6" data-fx="fade">
    <div class="kicker" style="color:${C.teal}">Answer</div>
    <div style="font-size:20px;line-height:1.45;margin-top:10px;color:#e3e9ff" data-in="publish.3@0.05" data-fx="type" data-dur="3">Patient search passed a limit of one lakh straight to the database and read the whole table. The gap was found at test review in May and waived in June.</div>
    ${[['doc', 'Outage postmortem'], ['xls', 'Traceability matrix · test-review finding'], ['xls', 'Requirements · page-size rule']].map(([k, label], n) =>
      `<div style="display:flex;align-items:center;gap:12px;margin-top:${n ? 10 : 22}px;padding:10px 14px;border-radius:14px;background:rgba(255,255,255,.04);border:1px solid var(--line)" data-in="publish.3@${0.55 + n * 0.12}" data-fx="left">
        ${ftype(k, 34)}<span style="flex:1;font-size:18px;font-weight:600">${label}</span>
        <span style="display:flex;gap:6px" data-in="publish.4@${0.3 + n * 0.15}" data-fx="pop">${['active', 'fingerprint', 'quote'].map((x) => `<span style="font-size:13px;font-weight:700;color:#052e22;background:${C.green};border-radius:99px;padding:3px 8px">${x} ✓</span>`).join('')}</span></div>`).join('')}
    <div style="display:flex;align-items:center;gap:10px;margin-top:16px;color:var(--muted);font-size:18px" data-in="publish.5" data-fx="fade">${svgIcon('lock', C.muted, 18)} Restricted documents: never used</div>
  </div>
  <div class="abs" style="left:0;right:0;top:770px;display:flex;justify-content:center;gap:16px" data-in="publish.7" data-fx="fade">
    ${[['The rule', C.violet], ['The gap', C.amber], ['The waiver', C.orange], ['The outage', C.red]].map(([x, col], n) => `<span class="chip" style="font-size:24px;padding:12px 24px;border-color:${col};color:${col};background:${col}1f" data-in="publish.7@${0.15 + n * 0.15}" data-fx="pop">${x}</span>${n < 3 ? '<span style="color:var(--muted);font-size:28px;align-self:center">→</span>' : ''}`).join('')}</div>
  <div class="abs" style="left:0;right:0;top:845px;text-align:center;font-size:24px;font-weight:600;color:#cfe0ff" data-in="publish.8" data-fx="fade">Every line points back to its source.</div>`);

// ---------- graph camera ----------
// structure group: full size in its own scene, then tucked to the left; whole graph shrinks for side panels.
const CAM = {
  struct: [['structure', 150, 0, 1], ['files', 150, 0, 1], ['files+1.2', 40, 150, 0.55]],
  all: [['ai', 0, 0, 1], ['search', 0, 0, 1], ['search+1.2', -25, 80, 0.68], ['publish.6', -25, 80, 0.68], ['publish.6+1.4', 184, 22, 0.8]],
};
function cam(keys, t) {
  let prev = keys[0];
  for (const k of keys) { const kt = at(k[0]); if (t < kt) { const pt = at(prev[0]); const p = ease((t - pt) / Math.max(kt - pt, 1e-6)); return [1, 2, 3].map((i) => lerp(prev[i], k[i], p)); } prev = k; }
  return prev.slice(1);
}
function graphFx(t) {
  const s = cam(CAM.struct, t), a = cam(CAM.all, t);
  $('#g-struct').setAttribute('transform', `translate(${s[0]},${s[1]}) scale(${s[2]})`);
  $('#g-all').setAttribute('transform', `translate(${a[0]},${a[1]}) scale(${a[2]})`);
  const show = ease((t - SC.structure.start) / 0.4);
  $('#graph').style.opacity = show; $('#graph').style.display = show > 0 ? '' : 'none';
  // dim the structure layer while the business layer is the subject
  const dim = 1 - 0.45 * ease((t - at('ai.2')) / 0.8) * (1 - ease((t - at('publish.6')) / 1));
  $('#g-struct').style.opacity = dim; $('#g-docs').style.opacity = 1 - 0.3 * ease((t - at('ai.2')) / 0.8) * (1 - ease((t - at('publish.6')) / 1));
  mergeFx(t);
}

register(document);
window.seek = function (t) {
  chrome(t);
  graphFx(t);
  for (const a of ACTORS) apply(a, t);
  return true;
};
window.READY = document.fonts ? document.fonts.ready.then(() => true) : Promise.resolve(true);
const hash = new URLSearchParams(location.search).get('t');
seek(hash ? +hash : 0);
// tick marks for chapters
$('#ticks').innerHTML = TL.scenes.map((s) => `<i style="left:${1920 * s.start / TL.total}px"></i>`).join('');
