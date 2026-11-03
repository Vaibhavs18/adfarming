// Mock API for ?mock=1. Replaces window.fetch and window.EventSource with in-memory shims
// that follow docs/API_CONTRACT.md. No server, no AI.
//   ?mock=1                 fictional client (fixtures in mock/fixtures.json)
//   ?mock=1&replay=<token>  replay of ONE real session, loaded from /replay-<token>/fixtures.json + media/
//                           (built by scripts/build_replay.py; the folder is private and never in git)
const STEPS = ['brief', 'ideas', 'preview', 'edit', 'check', 'ship'];
const COSTS = { brief: 0.02, ideas: 0.2, preview: 0.3, edit: 0.05, check: 0, ship: 0 };
let SPEED = 1; // multiplier on all delays
let FX, realFetch;
const S = { runs: {}, memory: [], n: 0, nextMem: 100, loggedIn: false };
const sources = new Set();

const wait = (ms) => new Promise((r) => setTimeout(r, ms * SPEED));
const now = () => new Date().toISOString();

function frameSvg(i, label) {
  const pal = [['#f3c969', '#c2410c'], ['#e9a1b8', '#7c2d52'], ['#9ad1c0', '#1f5c4d'], ['#b9b4f2', '#3730a3']][i % 4];
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="270" height="480" viewBox="0 0 270 480"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${pal[0]}"/><stop offset="1" stop-color="${pal[1]}"/></linearGradient></defs><rect width="270" height="480" fill="url(#g)"/><g fill="none" stroke="rgba(255,255,255,.35)" stroke-width="3">${[0, 1, 2, 3, 4, 5].map((k) => `<path d="M-10 ${90 + k * 60} Q 70 ${40 + k * 60} 135 ${90 + k * 60} T 280 ${90 + k * 60}"/>`).join('')}</g><text x="135" y="250" font-family="system-ui" font-size="22" font-weight="700" fill="#fff" text-anchor="middle">Frame ${i + 1}</text><text x="135" y="282" font-family="system-ui" font-size="14" fill="#fff" text-anchor="middle">${label}</text></svg>`;
  return 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
}
const FRAME_LABELS = ['Placeholder: loom', 'Placeholder: hands', 'Placeholder: drape', 'Placeholder: end card'];

function emit(runId, name, data) {
  for (const s of sources) if (s.runId === runId && !s.closed) s._dispatch(name, data);
}
function say(run, text) {
  run.commentary.push({ ts: now(), text });
  emit(run.run_id, 'commentary', { text });
}
function logEntry(run, o) {
  run.log.push({ seq: run.log.length + 1, ts: now(), step: run.current_step, kind: 'event', actor: 'agent', text: '', cost_usd: null, memory_ids: [], ...o });
}
function addCost(run, tool, usd) {
  run.cost.usd = +(run.cost.usd + usd).toFixed(2);
  run.cost.by_tool[tool] = +((run.cost.by_tool[tool] || 0) + usd).toFixed(2);
  emit(run.run_id, 'tool_done', { tool, usd });
  emit(run.run_id, 'cost_update', { usd: run.cost.usd, cap_usd: run.cost.cap_usd });
}

function stepsFor(run) {
  const cur = STEPS.indexOf(run.current_step);
  return STEPS.map((name, i) => ({
    name,
    state: run.status === 'done' ? 'done' : i < cur ? 'done' : i === cur ? 'current' : i <= run.maxIdx ? 'stale' : 'pending',
  }));
}

function chosen(run) {
  return run.concepts.find((c) => c.id === run.choice) || run.concepts[1];
}
function cardFor(run) {
  const step = run.current_step;
  if (step === 'brief') return { type: 'brief', text: run.briefText, memory_cited: FX.memory_cited_brief, cost_estimate_usd: COSTS.brief };
  if (step === 'ideas') return { type: 'ideas', concepts: run.concepts, recommended: 'c2', cost_estimate_usd: COSTS.ideas };
  if (step === 'preview') return { type: 'preview', frames: run.frames, cost_estimate_usd: COSTS.preview };
  if (step === 'edit') {
    const c = chosen(run);
    return { type: 'edit', beats: c.beats, captions: run.captions, hook: c.hook, cta: c.cta, cost_estimate_usd: COSTS.edit };
  }
  if (step === 'check') return { type: 'check', verdict: run.qc, history: run.qcHistory, proposed_fixes: run.qc.pass ? [] : run.qc.fixes };
  return { type: 'ship', cuts: run.cuts, zip: 'mock/demo.mp4', memory_proposals: S.memory.filter((m) => m.status === 'proposed') };
}
function state(run) {
  if (run.replay) return stateReplay(run);
  const cur = STEPS.indexOf(run.current_step);
  const gate = run.status === 'awaiting_human' ? { id: run.gateId, step: run.current_step, card: cardFor(run) } : null;
  const inPlay = S.memory.filter((m) => m.status === 'approved' && (run.current_step === 'brief' ? FX.memory_cited_brief : chosen(run).cites.concat(['m1'])).includes(m.id));
  return {
    run_id: run.run_id, client_id: run.client_id, status: run.status, current_step: run.current_step,
    steps: stepsFor(run), gate, memory_in_play: inPlay,
    artifacts: {
      concepts: cur >= 1 ? run.concepts : null,
      frames: cur >= 2 ? run.frames : [],
      cuts: cur >= 3 ? run.cuts : null,
      captions: cur >= 3 ? run.captions : [],
      qc: cur >= 4 ? run.qc : null,
      zip: run.status === 'done' || cur === 5 ? 'mock/demo.mp4' : null,
    },
    cost: run.cost, commentary: run.commentary.slice(-30),
  };
}

// Move the run to a step and "work" for a moment, then raise the gate.
async function goTo(run, step, lines, ms = 900, tool = null) {
  run.status = 'running';
  run.current_step = step;
  run.maxIdx = Math.max(run.maxIdx, STEPS.indexOf(step));
  emit(run.run_id, 'run_status', { status: 'running' });
  for (const l of lines) { say(run, l); await wait(ms / lines.length); }
  if (tool) addCost(run, tool[0], tool[1]);
  run.gateId += 1;
  run.status = 'awaiting_human';
  emit(run.run_id, 'card_ready', { gate: run.gateId, step });
  emit(run.run_id, 'run_status', { status: 'awaiting_human' });
}

function newRun(client_id, goal) {
  const run_id = 'demo-run-' + (++S.n);
  const run = {
    run_id, client_id, status: 'running', current_step: 'brief', maxIdx: 0, gateId: 0, created_at: now(),
    cost: { usd: 0, cap_usd: 1.5, by_tool: {} }, commentary: [], log: [],
    briefText: FX.brief_text + (goal ? '\n\nExtra from you: ' + goal : ''),
    concepts: JSON.parse(JSON.stringify(FX.concepts)), choice: null,
    frames: FRAME_LABELS.map((l, i) => frameSvg(i, l)),
    captions: JSON.parse(JSON.stringify(FX.captions)),
    cuts: { '9x16': 'mock/demo.mp4', '1x1': 'mock/demo.mp4', '16x9': 'mock/demo.mp4' },
    qc: null, qcHistory: [], steered: false,
  };
  S.runs[run_id] = run;
  logEntry(run, { actor: 'human', kind: 'run_started', text: 'You started a run for Demo Saree Brand' + (goal ? ': ' + goal : '.') });
  setTimeout(() => {
    logEntry(run, { text: 'Read the client memory (8 items) before writing the brief.', memory_ids: FX.memory_cited_brief });
    goTo(run, 'brief', ['Reading Demo Saree Brand memory, 8 items.', 'Writing the brief back to you in my own words.'], 900, ['muse-spark', 0.02]);
  }, 50);
  return run;
}

function proposeMemory(run, text, kind, quote) {
  S.memory.push({ id: 'm' + S.nextMem++, kind, text, source: 'agent_inferred', status: 'proposed', pinned: false, evidence: { quote, source_run: run.run_id }, created_at: now() });
}

async function answer(run, b) {
  if (run.replay) return answerReplay(run, b);
  const { action } = b;
  const step = run.current_step;
  logEntry(run, { actor: 'human', kind: action, text: describe(action, b), step });
  if (action === 'back') {
    const to = b.to || STEPS[Math.max(0, STEPS.indexOf(step) - 1)];
    run.choice = to === 'brief' || to === 'ideas' ? null : run.choice;
    await goTo(run, to, ['Going back to ' + to + '. Later steps are marked stale.'], 500);
    return;
  }
  if (action === 'edit') {
    const f = b.fields || {};
    if (step === 'ideas' && f.concept_id) {
      const c = run.concepts.find((x) => x.id === f.concept_id);
      if (c) { if (f.hook != null) c.hook = f.hook; if (f.cta != null) c.cta = f.cta; }
    } else if (step === 'edit') {
      const c = chosen(run);
      if (f.hook != null) c.hook = f.hook;
      if (f.cta != null) c.cta = f.cta;
      if (Array.isArray(f.captions)) f.captions.forEach((cp, i) => { if (run.captions[i] && cp && cp.text != null) run.captions[i].text = cp.text; });
    } else if (step === 'brief' && f.text != null) run.briefText = f.text;
    run.status = 'running';
    emit(run.run_id, 'run_status', { status: 'running' });
    say(run, 'Got your edits. Treating them as final.');
    await wait(500);
    run.gateId += 1; run.status = 'awaiting_human';
    emit(run.run_id, 'card_ready', { gate: run.gateId, step }); emit(run.run_id, 'run_status', { status: 'awaiting_human' });
    return;
  }
  if (action === 'steer') {
    if (step === 'ideas') {
      const target = run.concepts.find((c) => (b.text || '').toLowerCase().includes(c.id)) || run.concepts[1];
      target.hook = b.reason ? 'Ek nayi baat, ek nayi kahani.' : 'Saree shaadi mein pehni, sabne poocha.';
      target.length_s = Math.max(15, target.length_s - 4);
      proposeMemory(run, b.reason ? 'Avoid ' + b.reason + ' hooks (rejected by you).' : 'Avoid Diwali framing in hooks.', 'rejection', (b.text || '').slice(0, 120));
      await goTo(run, 'ideas', ['Rewriting ' + target.id + '.', 'Shorter hook, applied. Here is the new set.'], 1000, ['muse-spark', 0.01]);
    } else if (step === 'check') {
      run.qc = FX.qc_pass; run.qcHistory.push({ attempt: run.qcHistory.length + 1, score: 5, pass: true, summary: FX.qc_pass.summary });
      await goTo(run, 'check', ['Applying your fixes and rebuilding the cuts.', 'Re-checking the new cut.'], 1400, ['muse-wonder', 0.06]);
    } else if (step === 'edit') {
      run.captions[2].start = 3.0;
      await goTo(run, 'edit', ['Moving the caption earlier and rebuilding.'], 1000, ['ffmpeg-cuts', 0.03]);
    } else {
      await goTo(run, step, ['Revising that now.'], 800, ['muse-spark', 0.01]);
    }
    return;
  }
  // approve
  if (step === 'brief') return goTo(run, 'ideas', ['Writing three concepts.', 'Checking each against the hard rules.'], 1200, ['muse-spark', 0.03]);
  if (step === 'ideas') { run.choice = b.choice || 'c2'; return goTo(run, 'preview', ['Sketching storyboard frames for ' + run.choice + '.'], 1200, ['muse-image', 0.18]); }
  if (step === 'preview') return goTo(run, 'edit', ['Building the 9:16, 1:1 and 16:9 cuts.', 'Cropping to keep the subject in frame.'], 1500, ['ffmpeg-cuts', 0.12]);
  if (step === 'edit') {
    run.qc = FX.qc_fail; run.qcHistory = [{ attempt: 1, score: 3, pass: false, summary: FX.qc_fail.summary }];
    return goTo(run, 'check', ['Watching the cut like a client would.'], 1000, ['muse-wonder', 0.06]);
  }
  if (step === 'check') return goTo(run, 'ship', ['Packing the zip and the decision log.'], 800);
  if (step === 'ship') {
    run.status = 'done';
    say(run, 'Done. The zip is ready.');
    emit(run.run_id, 'run_status', { status: 'done' });
  }
}
function describe(action, b) {
  if (action === 'approve') return b.choice ? 'Approved concept ' + b.choice : 'Approved this step';
  if (action === 'steer') return 'Steered: ' + (b.text || '') + (b.reason ? ' (reason: ' + b.reason + ')' : '');
  if (action === 'edit') return 'Edited fields by hand';
  return 'Went back to ' + (b.to || 'the previous step');
}

const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
const err = (status, code, message) => json({ error: { code, message } }, status);

async function route(url, opts) {
  const method = (opts.method || 'GET').toUpperCase();
  const body = opts.body ? JSON.parse(opts.body) : {};
  const u = new URL(url, location.href);
  const p = u.pathname;
  await wait(120);
  if (p === '/healthz') return json({ ok: true });
  if (p === '/api/login') {
    if (!body.password) return err(401, 'bad_password', 'Wrong password.');
    S.loggedIn = true; sessionStorage.setItem('mock_login', '1');
    return json({ ok: true });
  }
  if (!S.loggedIn) return err(401, 'unauthorized', 'Please sign in.');
  if (p === '/api/me') return json({ ok: true });
  if (p === '/api/clients') return json(FX.clients);
  let m;
  if ((m = p.match(/^\/api\/clients\/([^/]+)\/memory(?:\/([^/]+)(\/decide)?)?$/))) {
    const [, , id, decide] = m;
    if (!id && method === 'GET') {
      const st = u.searchParams.get('status') || 'approved';
      return json(S.memory.filter((x) => st === 'all' || x.status === st));
    }
    if (!id && method === 'POST') {
      if (!body.text || !body.kind) return err(422, 'invalid', 'Please give a kind and some text.');
      const item = { id: 'm' + S.nextMem++, kind: body.kind, text: body.text, source: 'human', status: 'approved', pinned: !!body.pinned, evidence: null, created_at: now() };
      S.memory.push(item); return json(item);
    }
    const old = S.memory.find((x) => x.id === id);
    if (!old) return err(404, 'not_found', 'That memory item does not exist.');
    if (method === 'PATCH') {
      old.status = 'superseded';
      const item = { ...old, id: 'm' + S.nextMem++, text: body.text, status: 'approved', source: 'human', created_at: now() };
      S.memory.push(item); return json(item);
    }
    if (decide) { old.status = body.decision === 'accept' ? 'approved' : 'rejected'; if (body.decision === 'accept') old.source = 'agent_inferred'; return json(old); }
  }
  if (p === '/api/runs' && method === 'POST') {
    const run = R ? newReplayRun() : newRun(body.client_id, body.goal);
    return json({ run_id: run.run_id });
  }
  if (p === '/api/runs' && method === 'GET') {
    return json(Object.values(S.runs).reverse().map((r) => ({ run_id: r.run_id, client_id: r.client_id, status: r.status, current_step: r.current_step, created_at: r.created_at, cost_usd: r.cost.usd })));
  }
  if ((m = p.match(/^\/api\/runs\/([^/]+)(?:\/(answer|cancel|log))?$/))) {
    const run = S.runs[m[1]];
    if (!run) return err(404, 'not_found', 'Run not found.');
    if (!m[2] && method === 'GET') return json(state(run));
    if (m[2] === 'log') return json(run.log);
    if (m[2] === 'cancel') { run.status = 'cancelled'; emit(run.run_id, 'run_status', { status: 'cancelled' }); return json({ status: 'accepted' }, 202); }
    if (m[2] === 'answer') {
      if (!['approve', 'steer', 'edit', 'back'].includes(body.action)) return err(422, 'invalid_action', 'Unknown action.');
      if (run.status !== 'awaiting_human' || body.gate !== run.gateId) return err(409, 'gate_stale', 'That card has already moved on.');
      run.status = 'running'; // gate is closed immediately
      answer(run, body);
      return json({ status: 'accepted' }, 202);
    }
  }
  return err(404, 'not_found', 'No such mock route: ' + p);
}

class MockEventSource {
  constructor(url) {
    this.url = url; this.runId = url.match(/runs\/([^/]+)\/events/)[1]; this.closed = false; this.l = {};
    this.readyState = 0;
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 50);
    sources.add(this);
  }
  addEventListener(n, f) { (this.l[n] = this.l[n] || []).push(f); }
  _dispatch(n, data) { (this.l[n] || []).forEach((f) => f({ data: JSON.stringify(data), type: n })); }
  close() { this.closed = true; this.readyState = 2; sources.delete(this); }
}

// ---------------------------------------------------------------------------------------------
// Replay mode: the run follows a tape of the real gates (R.gates), recorded by build_replay.py.
// Every answer moves along the tape; if the visitor clicks something other than what the real
// reviewer did, we say so and replay what actually happened. Waits are short; the real duration
// of each stretch of work is shown as "took 3m 1s in the real run".
// ---------------------------------------------------------------------------------------------
let R = null; // replay fixtures
let BASE = '';
const clone = (o) => JSON.parse(JSON.stringify(o));
function fmtDur(sec) {
  const s = Math.round(sec);
  return s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + (s % 60) + 's';
}

function showBanner(text, warn) {
  const style = document.createElement('style');
  style.textContent = ':root{--banner-h:0px}#replay-banner{position:sticky;top:0;z-index:30;background:#1b1d21;color:#fff;padding:9px 16px;font:600 14px/1.4 system-ui,-apple-system,sans-serif;text-align:center}' +
    '#replay-banner.warn{background:#b45309}.top{top:var(--banner-h)!important}@media(min-width:900px){.col-mem{top:calc(72px + var(--banner-h))!important;max-height:calc(100vh - 90px - var(--banner-h))!important}}';
  document.head.appendChild(style);
  const el = document.createElement('div');
  el.id = 'replay-banner'; el.setAttribute('role', 'note'); el.className = warn ? 'warn' : ''; el.textContent = text;
  document.body.insertBefore(el, document.body.firstChild);
  const fit = () => document.documentElement.style.setProperty('--banner-h', el.offsetHeight + 'px');
  fit(); addEventListener('resize', fit);
}

function newReplayRun() {
  const run_id = 'replay-run-' + (++S.n);
  const run = {
    run_id, client_id: R.client.id, replay: true, ptr: -1, status: 'running', current_step: 'brief', maxIdx: 0, gateId: 0,
    created_at: now(), cost: { usd: 0, cap_usd: R.run.cap_usd, by_tool: {} }, commentary: [], log: [],
    cards: R.gates.map((g) => clone(g.card)), visited: new Set(), answered: new Set(),
  };
  S.runs[run_id] = run;
  setTimeout(() => raiseGate(run, 0), 50);
  return run;
}

function replayLog(run, rows) {
  for (const r of rows) run.log.push({ ...r, seq: run.log.length + 1, memory_ids: r.memory_ids || [] });
}
function replayCost(run, tool, usd) {
  run.cost.usd = +(run.cost.usd + usd).toFixed(4);
  run.cost.by_tool[tool] = +((run.cost.by_tool[tool] || 0) + usd).toFixed(4);
  emit(run.run_id, 'tool_done', { tool, usd });
  emit(run.run_id, 'cost_update', { usd: run.cost.usd, cap_usd: run.cost.cap_usd });
}

// Move to gate k, "work" for a moment (real commentary + the honest real duration), then raise it.
async function raiseGate(run, k, opts = {}) {
  const g = R.gates[k];
  run.ptr = k; run.status = 'running'; run.current_step = g.step;
  run.maxIdx = Math.max(run.maxIdx, STEPS.indexOf(g.step));
  emit(run.run_id, 'run_status', { status: 'running' });
  const first = !run.visited.has(k);
  const lines = opts.lines || [...(opts.pre || []), ...g.commentary.slice(-2), 'That took ' + fmtDur(g.work_s) + ' in the real run.'];
  const ms = Math.min(3200, Math.max(1100, g.work_s * 25));
  for (const l of lines) { say(run, l); await wait(ms / lines.length); }
  if (first) {
    run.visited.add(k);
    for (const [tool, usd] of Object.entries(g.cost)) replayCost(run, tool, usd);
    replayLog(run, g.log_q);
    for (const m of R.memory) if (m.appears_at_gate === k && !S.memory.some((x) => x.id === m.id)) S.memory.push(clone(m));
  }
  run.gateId += 1;
  run.status = 'awaiting_human';
  emit(run.run_id, 'card_ready', { gate: run.gateId, step: g.step });
  emit(run.run_id, 'run_status', { status: 'awaiting_human' });
}

async function answerReplay(run, b) {
  const k = run.ptr;
  const g = R.gates[k];
  const rec = g.answer;
  const act = b.action;
  if (act === 'back') {
    const to = b.to || STEPS[Math.max(0, STEPS.indexOf(g.step) - 1)];
    let j = -1;
    R.gates.forEach((x, i) => { if (x.step === to && i <= k) j = i; });
    if (j < 0) j = Math.max(0, R.gates.findIndex((x) => x.step === to));
    logEntry(run, { actor: 'human', kind: 'back', text: 'Went back to ' + to, step: g.step });
    return raiseGate(run, j, { lines: ['Going back to ' + to + '. Later steps are marked stale.'] });
  }
  if (act === 'edit' && rec.kind !== 'edit') {
    // The visitor edits by hand where the real reviewer did not: apply it to what they see, nothing else changes.
    const f = b.fields || {};
    const c = run.cards[k];
    if (c.type === 'ideas') {
      const t = c.concepts.find((x) => x.id === (f.concept_id || c.recommended)) || c.concepts[0];
      if (f.hook != null) t.hook = f.hook;
      if (f.cta != null) t.cta = f.cta;
    } else if (c.type === 'edit') {
      if (f.hook != null) c.hook = f.hook;
      if (f.cta != null) c.cta = f.cta;
    } else if (c.type === 'brief' && f.text != null) c.text = f.text;
    logEntry(run, { actor: 'human', kind: 'edit', text: 'You edited fields by hand (replay: only changes what you see here)', step: g.step });
    say(run, 'Got your edits. In this replay they only change what you see on this screen.');
    await wait(500);
    run.gateId += 1; run.status = 'awaiting_human';
    emit(run.run_id, 'card_ready', { gate: run.gateId, step: g.step }); emit(run.run_id, 'run_status', { status: 'awaiting_human' });
    return;
  }
  // approve / steer (or edit where the real reviewer edited): follow the tape.
  if (!run.answered.has(k)) { run.answered.add(k); replayLog(run, g.log_a); }
  else logEntry(run, { actor: 'human', kind: act, text: describe(act, b), step: g.step });
  const pre = [];
  if (act !== rec.kind) {
    pre.push({
      steer: 'In the real session the reviewer steered here: “' + (rec.text || '') + '”. Replaying what actually happened.',
      approve: 'In the real session this was approved as it stood. Continuing.',
      edit: 'In the real session the reviewer edited the hook and the call to action by hand here. Replaying that.',
    }[rec.kind] || 'Replaying what actually happened.');
  }
  if (k + 1 < R.gates.length) return raiseGate(run, k + 1, { pre });
  // last gate approved: wrap up
  run.status = 'running';
  emit(run.run_id, 'run_status', { status: 'running' });
  const t = R.tail;
  for (const l of [...t.commentary.slice(-1), 'Wrapping up took ' + fmtDur(t.work_s) + ' in the real run.']) { say(run, l); await wait(500); }
  if (!run.visited.has('tail')) {
    run.visited.add('tail');
    for (const [tool, usd] of Object.entries(t.cost)) replayCost(run, tool, usd);
    replayLog(run, t.log);
  }
  run.status = 'done';
  say(run, 'Done. In the real session this took ' + fmtDur(R.run.total_real_s) + ' end to end and cost $' + R.run.total_cost_usd.toFixed(2) + '.');
  emit(run.run_id, 'run_status', { status: 'done' });
}

function stateReplay(run) {
  const k = Math.max(run.ptr, 0);
  const g = R.gates[k];
  const cur = STEPS.indexOf(run.current_step);
  let gate = null;
  if (run.status === 'awaiting_human') {
    const card = clone(run.cards[k]);
    if (card.type === 'ship') { card.memory_proposals = S.memory.filter((m) => m.status === 'proposed'); card.zip = null; }
    gate = { id: run.gateId, step: g.step, card };
  }
  let concepts = null;
  for (let j = k; j >= 0 && cur >= 1; j--) if (R.gates[j].step === 'ideas') { concepts = run.cards[j].concepts; break; }
  const A = R.artifacts;
  const inPlay = S.memory.filter((m) => m.status === 'approved' && g.in_play.includes(m.id));
  return {
    run_id: run.run_id, client_id: run.client_id, status: run.status, current_step: run.current_step,
    steps: stepsFor(run), gate, memory_in_play: inPlay,
    artifacts: {
      concepts, frames: cur >= 2 ? A.frames : [], cuts: cur >= 3 ? A.cuts : null,
      captions: cur >= 3 ? A.captions : [], qc: cur >= 4 ? A.qc : null, zip: null,
    },
    cost: run.cost, commentary: run.commentary.slice(-30),
  };
}

async function loadReplay(token) {
  if (!/^[A-Za-z0-9_-]{4,64}$/.test(token)) return null;
  try {
    const base = '/replay-' + token + '/';
    const r = await realFetch(base + 'fixtures.json');
    if (!r.ok) return null;
    const txt = await r.text();
    const fx = JSON.parse(txt.replace(/"media\//g, '"' + base + 'media/'));
    if (fx.schema !== 'adfarming-replay/1' || !Array.isArray(fx.gates) || !fx.gates.length) return null;
    BASE = base;
    return fx;
  } catch (e) { return null; }
}

export async function install() {
  realFetch = window.fetch.bind(window);
  FX = await (await realFetch('mock/fixtures.json')).json();
  const token = new URLSearchParams(location.search).get('replay');
  if (token) {
    R = await loadReplay(token);
    if (R) {
      FX = { clients: R.clients, memory: R.memory.filter((m) => m.appears_at_gate == null), memory_cited_brief: R.gates[0].in_play };
      showBanner(R.banner);
    } else showBanner('That replay link did not load, so this is the fictional demo (Demo Saree Brand) instead.', true);
  }
  S.memory = JSON.parse(JSON.stringify(FX.memory));
  S.loggedIn = sessionStorage.getItem('mock_login') === '1';
  window.fetch = (input, opts = {}) => {
    const url = typeof input === 'string' ? input : input.url;
    if (/^\/(api\/|healthz)/.test(new URL(url, location.href).pathname) && new URL(url, location.href).origin === location.origin) return route(url, opts);
    return realFetch(input, opts);
  };
  window.EventSource = MockEventSource;
  window.__mock = S;
  // ?at=ideas|preview|edit|check|ship fast-forwards a demo run (demo-run-1) to that step, for screenshots.
  const at = new URLSearchParams(location.search).get('at');
  if (at && STEPS.includes(at) && at !== 'brief') {
    S.loggedIn = true; sessionStorage.setItem('mock_login', '1');
    SPEED = 0.01;
    if (R) {
      const run = newReplayRun();
      (async () => {
        while (STEPS.indexOf(run.current_step) < STEPS.indexOf(at) || run.status !== 'awaiting_human') {
          if (run.status === 'awaiting_human') answer(run, { action: 'approve', gate: run.gateId });
          await wait(40);
        }
        SPEED = 1;
      })();
      return;
    }
    const run = newRun('demo_saree', '');
    const settle = async (step) => { while (!(run.status === 'awaiting_human' && run.current_step === step)) await wait(40); };
    (async () => {
      await settle('brief');
      const plan = [[{}, 'ideas'], [{ choice: 'c2' }, 'preview'], [{}, 'edit'], [{}, 'check'], [{}, 'ship']];
      for (const [extra, next] of plan) {
        if (STEPS.indexOf(run.current_step) >= STEPS.indexOf(at)) break;
        answer(run, { action: 'approve', gate: run.gateId, ...extra });
        await wait(30);
        await settle(next);
      }
      SPEED = 1;
    })();
  }
}
