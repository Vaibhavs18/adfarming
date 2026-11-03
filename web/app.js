// AdFarming Studio UI. Preact + htm, no build. Coded against docs/API_CONTRACT.md.
import { h, render } from 'preact';
import { useState, useEffect, useRef, useCallback } from 'preact/hooks';
import htm from 'htm';
const html = htm.bind(h);

const MOCK = new URLSearchParams(location.search).get('mock') === '1';
const STEP_LABELS = { brief: 'Brief', ideas: 'Ideas', preview: 'Preview', edit: 'Edit', check: 'Check', ship: 'Ship' };
const STEP_ORDER = Object.keys(STEP_LABELS);
const KIND_LABEL = { hard_rule: 'Hard rules', brand_voice: 'Voice', preference: 'Preferences', performance: 'What worked', asset: 'Assets', rejection: 'Things to avoid' };
const KIND_ORDER = ['hard_rule', 'brand_voice', 'preference', 'performance', 'asset', 'rejection'];
const KIND_SHORT = { hard_rule: 'Hard rule', brand_voice: 'Voice', preference: 'Preference', performance: 'Performance', asset: 'Asset', rejection: 'Avoid' };
const STATUS = {
  running: ['Working', 'work'], awaiting_human: ['Awaiting you', 'await'], interrupted: ['Interrupted', 'err'],
  done: ['Done', 'done'], error: ['Error', 'err'], cancelled: ['Cancelled', ''],
};
const REJECT_REASONS = [['off-brand', 'Off-brand'], ['wrong moment', 'Wrong moment'], ['wrong tone', 'Wrong tone'], ['other', 'Other']];
const usd = (n) => '$' + Number(n || 0).toFixed(2);
const short = (s, n = 40) => (s && s.length > n ? s.slice(0, n - 1) + '…' : s || '');

// ---------- API ----------
class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}
let onUnauth = () => {};
async function api(path, { method = 'GET', body } = {}) {
  let r;
  try {
    r = await fetch(path, { method, credentials: 'same-origin', headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  } catch (e) {
    throw new ApiError(0, 'network', 'Cannot reach the Studio server. Check your connection and try again.');
  }
  let data = null;
  try { data = await r.json(); } catch (e) { /* no body */ }
  if (r.status === 401 && !path.endsWith('/login')) { onUnauth(); throw new ApiError(401, 'unauthorized', 'Please sign in again.'); }
  if (!r.ok) {
    const e = (data && data.error) || {};
    const friendly = {
      gate_blocked: 'That paid step is waiting for your approval first.',
      cost_cap: 'This run has hit its spending cap, so I stopped before spending more.',
    };
    const fallback = r.status === 409 ? 'That card has already moved on. I refreshed it.' : r.status === 422 ? 'That did not look right. Check what you entered.' : r.status >= 500 ? 'Something went wrong on the server. Try again in a moment.' : 'Something went wrong.';
    throw new ApiError(r.status, e.code || 'error', friendly[e.code] || e.message || fallback);
  }
  return data;
}

// ---------- small bits ----------
function useToast() {
  const [t, setT] = useState(null);
  const timer = useRef();
  const show = useCallback((message, kind = 'err') => {
    setT({ message, kind });
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setT(null), 7000);
  }, []);
  const view = t && html`<div class=${'toast ' + (t.kind === 'info' ? 'info' : '')} role=${t.kind === 'info' ? 'status' : 'alert'}><span>${t.message}</span><button aria-label="Dismiss" onClick=${() => setT(null)}>×</button></div>`;
  return [show, view];
}

function Cites({ ids, memById }) {
  if (!ids || !ids.length) return null;
  return html`<div class="cites" aria-label="Memory rules this obeyed">${ids.map((id) => {
    const m = memById[id];
    return html`<span class="cite" title=${m ? m.text : id}>${m ? short(m.text, 34) : id}</span>`;
  })}</div>`;
}

function Dots() { return html`<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>`; }

function approveLabel(base, est) {
  return est > 0 ? `${base} and spend ~${usd(est)}` : base;
}

// ---------- login ----------
function Login({ onDone }) {
  const [pw, setPw] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  async function go(e) {
    e.preventDefault();
    setBusy(true); setErr('');
    try { await api('/api/login', { method: 'POST', body: { password: pw } }); onDone(); }
    catch (x) { setErr(x.status === 401 ? 'That password did not work. Try again.' : x.message); }
    setBusy(false);
  }
  return html`<main class="center"><form class="card" onSubmit=${go}>
    <h1 style="display:flex;align-items:center;gap:10px;font-size:22px"><span class="logo"></span>AdFarming Studio</h1>
    <p class="lead">Sign in to review ideas, steer them, and approve each step before anything is spent.</p>
    <label class="f" for="pw">Password</label>
    <input id="pw" type="password" autocomplete="current-password" value=${pw} onInput=${(e) => setPw(e.target.value)} required autofocus />
    <p class="bad sm" role="alert" style="min-height:22px;margin:8px 0 0">${err}</p>
    <div class="actions" style="margin-top:8px"><button class="btn primary" disabled=${busy || !pw} type="submit">${busy ? 'Signing in...' : 'Sign in'}</button></div>
    ${MOCK && html`<p class="mut sm">Demo mode: any password works.</p>`}
  </form></main>`;
}

// ---------- header ----------
function CostBar({ cost }) {
  const c = cost || { usd: 0, cap_usd: 0 };
  const pct = c.cap_usd ? Math.min(100, (c.usd / c.cap_usd) * 100) : 0;
  const tools = Object.entries(c.by_tool || {});
  return html`<div class="cost" title=${tools.map(([k, v]) => k + ' ' + usd(v)).join(', ')}>
    <div class="nums"><span>Spent <b style="color:var(--fg)">${usd(c.usd)}</b></span><span>Cap ${usd(c.cap_usd)}</span></div>
    <div class=${'meter ' + (pct > 80 ? 'hot' : '')} role="progressbar" aria-label="Spend against cap" aria-valuemin="0" aria-valuemax=${c.cap_usd} aria-valuenow=${c.usd}><b style=${'width:' + pct + '%'}></b></div>
  </div>`;
}

function StatusPill({ status }) {
  const [label, cls] = STATUS[status] || [status, ''];
  return html`<span class=${'status ' + cls} role="status">${label}${status === 'running' && html`<${Dots} />`}</span>`;
}

// ---------- memory ----------
function MemItem({ item, inPlay, cid, onChange, toast }) {
  const [edit, setEdit] = useState(false);
  const [text, setText] = useState(item.text);
  const [rej, setRej] = useState(false);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const proposed = item.status === 'proposed';
  async function run(fn) {
    setBusy(true);
    try { await fn(); await onChange(); } catch (e) { toast(e.message); }
    setBusy(false);
  }
  const save = () => run(async () => { await api(`/api/clients/${cid}/memory/${item.id}`, { method: 'PATCH', body: { text } }); setEdit(false); });
  const decide = (decision) => run(async () => { await api(`/api/clients/${cid}/memory/${item.id}/decide`, { method: 'POST', body: { decision, note: note || undefined } }); setRej(false); });
  const ev = item.evidence && (item.evidence.quote || item.evidence.text);
  return html`<div class=${'mem ' + (proposed ? 'proposed ' : '') + (inPlay ? 'inplay' : '')}>
    <div class="meta">
      <span class=${'pill ' + (item.source === 'human' ? '' : 'acc')}>${item.source === 'human' ? 'Human' : 'Learned by agent'}</span>
      ${item.pinned && html`<span class="pill">Pinned</span>`}
      ${proposed && html`<span class="pill warn">Proposed</span>`}
      ${inPlay && html`<span class="pill ok">In play</span>`}
    </div>
    ${edit
      ? html`<textarea aria-label="Edit memory text" value=${text} onInput=${(e) => setText(e.target.value)}></textarea>
          <div class="acts"><button class="btn sm primary" disabled=${busy || !text.trim()} onClick=${save}>Save</button><button class="btn sm ghost" disabled=${busy} onClick=${() => { setEdit(false); setText(item.text); }}>Cancel</button></div>`
      : html`<button class="txt" title="Click to edit" onClick=${() => setEdit(true)}>${item.text}</button>`}
    ${ev && html`<p class="evid">“${ev}”</p>`}
    ${proposed && !rej && html`<div class="acts"><button class="btn sm primary" disabled=${busy} onClick=${() => decide('accept')}>Accept</button><button class="btn sm" disabled=${busy} onClick=${() => setRej(true)}>Reject</button></div>`}
    ${proposed && rej && html`<div class="acts" style="flex-direction:column;align-items:stretch">
      <input type="text" aria-label="Reason for rejecting (optional)" placeholder="Why not? (one line, optional)" value=${note} onInput=${(e) => setNote(e.target.value)} />
      <div class="acts" style="margin-top:0"><button class="btn sm" disabled=${busy} onClick=${() => decide('reject')}>Reject it</button><button class="btn sm ghost" onClick=${() => setRej(false)}>Cancel</button></div></div>`}
  </div>`;
}

function MemoryPanel({ cid, items, inPlayIds, onChange, toast }) {
  const [kind, setKind] = useState('hard_rule');
  const [text, setText] = useState('');
  const [pin, setPin] = useState(false);
  const [busy, setBusy] = useState(false);
  const live = items.filter((m) => m.status === 'approved' || m.status === 'proposed');
  const proposed = live.filter((m) => m.status === 'proposed');
  const approved = live.filter((m) => m.status === 'approved');
  async function add(e) {
    e.preventDefault();
    setBusy(true);
    try { await api(`/api/clients/${cid}/memory`, { method: 'POST', body: { kind, text: text.trim(), pinned: pin } }); setText(''); setPin(false); await onChange(); }
    catch (x) { toast(x.message); }
    setBusy(false);
  }
  const item = (m) => html`<${MemItem} key=${m.id + m.text} item=${m} inPlay=${inPlayIds.has(m.id)} cid=${cid} onChange=${onChange} toast=${toast} />`;
  return html`<section class="panel" aria-label="Client memory">
    <div class="memhead"><h2 style="margin:0">Client memory</h2><span class="pill">${approved.length} rules</span></div>
    <p class="mut sm">Everything here is used on every run. Nothing is learned silently.</p>
    ${proposed.length > 0 && html`<div class="mgroup"><h4>Proposed (${proposed.length})</h4>${proposed.map(item)}</div>`}
    ${KIND_ORDER.map((k) => {
      const g = approved.filter((m) => m.kind === k).sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));
      return g.length ? html`<div class="mgroup" key=${k}><h4>${KIND_LABEL[k]}</h4>${g.map(item)}</div>` : null;
    })}
    <form class="addrule" onSubmit=${add}>
      <h4>Add a rule</h4>
      <label class="f" for="nk">Type</label>
      <select id="nk" value=${kind} onChange=${(e) => setKind(e.target.value)}>${KIND_ORDER.map((k) => html`<option value=${k}>${KIND_SHORT[k]}</option>`)}</select>
      <label class="f" for="nt">Rule</label>
      <textarea id="nt" placeholder="e.g. Never show the competitor's name" value=${text} onInput=${(e) => setText(e.target.value)}></textarea>
      <div class="row"><label style="display:flex;align-items:center;gap:8px;min-height:44px"><input type="checkbox" checked=${pin} onChange=${(e) => setPin(e.target.checked)} style="width:20px;height:20px" /> Pin</label>
      <button class="btn primary" style="margin-left:auto" type="submit" disabled=${busy || !text.trim()}>Add rule</button></div>
    </form>
  </section>`;
}

// ---------- stepper + strip ----------
function Stepper({ steps }) {
  const stateText = { done: 'Done', current: 'Now', pending: 'Later', stale: 'Redo' };
  return html`<ol class="stepper" aria-label="Progress">${steps.map((s, i) => html`<li class=${s.state} data-n=${i + 1} aria-current=${s.state === 'current' ? 'step' : undefined}>${STEP_LABELS[s.name]}<span class="st">${stateText[s.state] || s.state}</span></li>`)}</ol>`;
}
function MemoryStrip({ items }) {
  return html`<div class="strip" role="list" aria-label="Memory in play"><span class="lbl">Memory in play</span>
    ${items.length ? items.map((m) => html`<span role="listitem" class="cite" title=${m.text}>${short(m.text, 44)}</span>`) : html`<span class="mut sm">Nothing yet</span>`}</div>`;
}

// ---------- gate cards ----------
function Actions({ children }) { return html`<div class="actions">${children}</div>`; }
function BackBtn({ step, run, act, busy }) {
  const i = STEP_ORDER.indexOf(step);
  if (i <= 0) return null;
  const to = STEP_ORDER[i - 1];
  return html`<button class="btn ghost" disabled=${busy} onClick=${() => act({ action: 'back', to })}>Back to ${STEP_LABELS[to]}</button>`;
}

function BriefCard({ card, run, act, busy, memById }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(card.text);
  useEffect(() => setText(card.text), [card.text]);
  return html`<article class="card"><h2><span class="step">1</span>Here is how I read the brief</h2>
    <p class="lead">Check this matches what you want. I will only carry on when you approve.</p>
    ${editing
      ? html`<label class="f" for="bt">Brief (your edit is final)</label><textarea id="bt" style="min-height:140px" value=${text} onInput=${(e) => setText(e.target.value)}></textarea>`
      : html`<div class="said">${card.text}</div>`}
    <h4 style="margin-top:14px">Rules I will obey</h4>
    <${Cites} ids=${card.memory_cited} memById=${memById} />
    <${Actions}>
      <button class="btn primary" disabled=${busy || editing} onClick=${() => act({ action: 'approve' })}>${approveLabel('Approve', card.cost_estimate_usd)}</button>
      ${editing
        ? html`<button class="btn" disabled=${busy || !text.trim()} onClick=${async () => { await act({ action: 'edit', fields: { text } }); setEditing(false); }}>Save edit</button><button class="btn ghost" onClick=${() => { setEditing(false); setText(card.text); }}>Cancel</button>`
        : html`<button class="btn" disabled=${busy} onClick=${() => setEditing(true)}>Edit</button>`}
    <//>
    <p class="mut sm">Want it different? Use the Steer box and say what to change.</p></article>`;
}

function ConceptCard({ c, rec, card, act, busy, memById }) {
  const [editing, setEditing] = useState(false);
  const [hook, setHook] = useState(c.hook);
  const [cta, setCta] = useState(c.cta);
  const [rejecting, setRejecting] = useState(false);
  const [other, setOther] = useState('');
  useEffect(() => { setHook(c.hook); setCta(c.cta); }, [c.hook, c.cta]);
  const reject = (reason, extra) => act({ action: 'steer', reason, text: `Not ${c.id} (${c.title}): ${reason}${extra ? '. ' + extra : ''}` });
  return html`<div class=${'concept ' + (rec ? 'rec' : '')}>
    <div class="tags"><span class="pill">${c.id.toUpperCase()}</span> <span class="pill">${c.angle}</span> <span class="pill">${c.length_s}s</span> <span class="pill">${c.beats.length} clips</span> ${rec && html`<span class="pill acc">Recommended</span>`}</div>
    <h3 style="margin-top:6px">${c.title}</h3>
    ${editing
      ? html`<label class="f" for=${'h' + c.id}>Hook</label><textarea id=${'h' + c.id} value=${hook} onInput=${(e) => setHook(e.target.value)}></textarea>
          <label class="f" for=${'c' + c.id}>Call to action</label><input id=${'c' + c.id} type="text" value=${cta} onInput=${(e) => setCta(e.target.value)} />
          <div class="actions" style="margin-top:10px"><button class="btn sm primary" disabled=${busy || !hook.trim()} onClick=${async () => { await act({ action: 'edit', fields: { concept_id: c.id, hook, cta } }); setEditing(false); }}>Save</button><button class="btn sm ghost" onClick=${() => setEditing(false)}>Cancel</button></div>`
      : html`<p class="hook">“${c.hook}”</p>`}
    <dl>
      <dt>Why it fits</dt><dd>${c.why_it_fits}</dd>
      ${c.risks && html`<dt>Watch out</dt><dd>${c.risks}</dd>`}
      <dt>Ends with</dt><dd>${c.cta}</dd>
      <dt>Obeys</dt><dd><${Cites} ids=${c.cites} memById=${memById} /></dd>
    </dl>
    <div class="foot">
      <button class=${'btn ' + (rec ? 'primary' : '')} style="width:100%" disabled=${busy || editing} onClick=${() => act({ action: 'approve', choice: c.id })}>${approveLabel('Approve ' + c.id.toUpperCase(), card.cost_estimate_usd)}</button>
      <div class="actions" style="margin-top:8px"><button class="btn sm" disabled=${busy} onClick=${() => setEditing(!editing)}>Edit hook</button><button class="btn sm ghost" disabled=${busy} onClick=${() => setRejecting(!rejecting)} aria-expanded=${rejecting}>Not this one</button></div>
      ${rejecting && html`<div class="rejrow"><span class="lbl">Why not? This teaches the memory.</span>
        ${REJECT_REASONS.filter(([k]) => k !== 'other').map(([k, l]) => html`<button class="chip" disabled=${busy} onClick=${() => reject(k)}>${l}</button>`)}
        <input type="text" aria-label="Other reason" placeholder="Other: say why" value=${other} onInput=${(e) => setOther(e.target.value)} />
        <button class="chip" disabled=${busy || !other.trim()} onClick=${() => reject('other', other.trim())}>Send other reason</button></div>`}
    </div></div>`;
}

function IdeasCard({ card, act, busy, memById }) {
  return html`<article class="card"><h2><span class="step">2</span>Pick the idea to build</h2>
    <p class="lead">Approving one idea unlocks the paid storyboard. ${card.cost_estimate_usd > 0 && `That step costs about ${usd(card.cost_estimate_usd)}.`} Steer to change one first, for example “c2 but a shorter hook”.</p>
    <div class="grid c3">${card.concepts.map((c) => html`<${ConceptCard} key=${c.id} c=${c} rec=${card.recommended === c.id} card=${card} act=${act} busy=${busy} memById=${memById} />`)}</div>
    <${Actions}><${BackBtn} step="ideas" act=${act} busy=${busy} /><//></article>`;
}

function PreviewCard({ card, act, busy }) {
  return html`<article class="card"><h2><span class="step">3</span>Storyboard preview</h2>
    <p class="lead">Rough frames for the chosen idea. If it looks off, steer before we pay for the real cuts.</p>
    <div class="frames">${card.frames.map((f, i) => html`<figure><img src=${f} alt=${'Storyboard frame ' + (i + 1)} loading="lazy" /><figcaption>Frame ${i + 1}</figcaption></figure>`)}</div>
    <${Actions}>
      <button class="btn primary" disabled=${busy} onClick=${() => act({ action: 'approve' })}>${approveLabel('Approve', card.cost_estimate_usd)}</button>
      <${BackBtn} step="preview" act=${act} busy=${busy} /><//>
    ${card.cost_estimate_usd > 0 && html`<p class="mut sm">Next step builds all three video sizes and checks them: about ${usd(card.cost_estimate_usd)}.</p>`}</article>`;
}

function CutTabs({ cuts }) {
  const keys = ['9x16', '1x1', '16x9'].filter((k) => cuts && cuts[k]);
  const [k, setK] = useState(keys[0]);
  if (!keys.length) return null;
  const cur = keys.includes(k) ? k : keys[0];
  return html`<div><div class="tabs" role="tablist">${keys.map((x) => html`<button role="tab" class="tab" aria-selected=${x === cur} onClick=${() => setK(x)}>${x.replace('x', ':')}</button>`)}</div>
    <video key=${cur} class=${'vid r' + cur.replace('x', '')} controls playsinline preload="metadata" src=${cuts[cur]} aria-label=${'Cut at ' + cur.replace('x', ':')}></video></div>`;
}

function EditCard({ card, run, act, busy }) {
  const [hook, setHook] = useState(card.hook);
  const [cta, setCta] = useState(card.cta);
  useEffect(() => { setHook(card.hook); setCta(card.cta); }, [card.hook, card.cta]);
  const dirty = hook !== card.hook || cta !== card.cta;
  const banned = /\b(sale|hurry|only today|limited time|last chance)\b/i.exec(hook + ' ' + card.captions.map((c) => c.text).join(' '));
  // Caption text is generated from speech and cannot be overridden by the backend: only hook/cta edits are sent.
  const save = () => act({ action: 'edit', fields: { hook, cta } });
  const cuts = run.artifacts && run.artifacts.cuts;
  return html`<article class="card"><h2><span class="step">4</span>Edit the cut</h2>
    <p class="lead">Edit the hook or call to action by hand; your edits are final. Caption words come from the speech, so to change one, use Steer instead of typing over it.</p>
    ${cuts && html`<${CutTabs} cuts=${cuts} />`}
    <div class="field-row"><div><label class="f" for="eh">Hook</label><input id="eh" type="text" value=${hook} onInput=${(e) => setHook(e.target.value)} /></div>
    <div><label class="f" for="ec">Call to action</label><input id="ec" type="text" value=${cta} onInput=${(e) => setCta(e.target.value)} /></div></div>
    ${banned && html`<p class="warnbox" role="alert">Heads up: “${banned[0]}” breaks the client's no-hard-sell rule.</p>`}
    <h4 style="margin-top:16px">Captions</h4>
    <div class="scroll"><table class="caps"><thead><tr><th>#</th><th>Time</th><th>Text</th><th></th></tr></thead><tbody>
      ${card.captions.map((c, i) => html`<tr key=${i}><td class="n">${i + 1}</td><td class="t" style="white-space:nowrap">${c.start.toFixed(1)}–${c.end.toFixed(1)}s</td>
        <td class="x">${c.text}</td>
        <td class="s"><button class="btn sm ghost" onClick=${() => { window.dispatchEvent(new CustomEvent('prefill-steer', { detail: `Caption ${i + 1}: ` })); }}>Steer</button></td></tr>`)}
    </tbody></table></div>
    <h4 style="margin-top:16px">Clips</h4>
    <div class="scroll"><table><thead><tr><th>Clip</th><th>In–out</th><th>Purpose</th><th>On screen</th></tr></thead><tbody>
      ${card.beats.map((b) => html`<tr><td>${b.clip}</td><td style="white-space:nowrap">${b.src_in}–${b.src_out}s</td><td>${b.purpose}</td><td>${b.on_screen_text}</td></tr>`)}</tbody></table></div>
    <${Actions}>
      <button class="btn primary" disabled=${busy || dirty} title=${dirty ? 'Save your edits first' : ''} onClick=${() => act({ action: 'approve' })}>${approveLabel('Approve', card.cost_estimate_usd)}</button>
      <button class="btn" disabled=${busy || !dirty} onClick=${save}>Save edits</button>
      <${BackBtn} step="edit" act=${act} busy=${busy} /><//>
    ${dirty && html`<p class="mut sm">You have unsaved edits. Save them before approving.</p>`}</article>`;
}

function Verdict({ v }) {
  if (!v) return null;
  return html`<div class=${'verdict ' + (v.pass ? '' : 'fail')}>
    <div style="display:flex;gap:12px;align-items:baseline;flex-wrap:wrap"><span class="score">${v.score}/5</span><b class=${v.pass ? 'ok' : 'bad'}>${v.pass ? 'Passes the check' : 'Needs fixes'}</b></div>
    <p style="margin:6px 0 0">${v.summary}</p>
    ${v.issues && v.issues.length > 0 && html`<ul style="margin:8px 0 0;padding-left:20px">${v.issues.map((i) => html`<li>${i}</li>`)}</ul>`}</div>`;
}

function CheckCard({ card, run, act, busy }) {
  const fixes = card.proposed_fixes || [];
  const [sel, setSel] = useState(fixes.map(() => true));
  useEffect(() => setSel(fixes.map(() => true)), [fixes.join('|')]);
  const chosen = fixes.filter((_, i) => sel[i]);
  const cuts = run.artifacts && run.artifacts.cuts;
  return html`<article class="card"><h2><span class="step">5</span>Quality check</h2>
    <p class="lead">I watched the cut the way a client would.</p>
    <${Verdict} v=${card.verdict} />
    ${card.history && card.history.length > 1 && html`<div class="hist">${card.history.map((hh) => html`<span class=${'pill ' + (hh.pass ? 'ok' : 'bad')}>Try ${hh.attempt}: ${hh.score}/5</span>`)}</div>`}
    ${cuts && html`<details><summary class="btn sm ghost" style="display:inline-flex">Watch the cut</summary><${CutTabs} cuts=${cuts} /></details>`}
    ${fixes.length > 0 && html`<h4 style="margin-top:14px">Suggested fixes (tick the ones to apply)</h4><div class="fixes">${fixes.map((f, i) => html`<label><input type="checkbox" checked=${sel[i]} onChange=${(e) => setSel(sel.map((x, j) => (j === i ? e.target.checked : x)))} /><span>${f}</span></label>`)}</div>`}
    <${Actions}>
      ${fixes.length > 0 && html`<button class="btn primary" disabled=${busy || !chosen.length} onClick=${() => act({ action: 'steer', text: 'Apply these fixes: ' + chosen.join(' ') })}>Steer: apply ${chosen.length} fix${chosen.length === 1 ? '' : 'es'}</button>`}
      <button class=${'btn ' + (fixes.length ? '' : 'primary')} disabled=${busy} onClick=${() => act({ action: 'approve' })}>${card.verdict && card.verdict.pass ? 'Approve' : 'Approve anyway'}</button>
      <${BackBtn} step="check" act=${act} busy=${busy} /><//></article>`;
}

function ShipCard({ card, run, act, busy, cid, memChanged, toast }) {
  const qc = run.artifacts && run.artifacts.qc;
  const cuts = card.cuts || {};
  const [dec, setDec] = useState({});
  async function decide(m, decision) {
    setDec({ ...dec, [m.id]: 'busy' });
    try { await api(`/api/clients/${cid}/memory/${m.id}/decide`, { method: 'POST', body: { decision } }); setDec((d) => ({ ...d, [m.id]: decision })); memChanged(); }
    catch (e) { toast(e.message); setDec((d) => ({ ...d, [m.id]: null })); }
  }
  const done = run.status === 'done';
  return html`<article class="card"><h2><span class="step">6</span>${done ? 'Shipped' : 'Ready to ship'}</h2>
    <p class="lead">Three sizes, ready for the client.</p>
    <div class="cutgrid">${['9x16', '1x1', '16x9'].filter((k) => cuts[k]).map((k) => html`<figure key=${k}><video class=${'vid r' + k.replace('x', '')} controls playsinline preload="metadata" src=${cuts[k]} aria-label=${'Final cut ' + k.replace('x', ':')}></video><figcaption><b>${k.replace('x', ':')}</b> ${{ '9x16': 'Reels, Shorts, Stories', '1x1': 'Feed', '16x9': 'YouTube, web' }[k]}</figcaption></figure>`)}</div>
    ${qc && html`<h4 style="margin-top:16px">Quality check</h4><${Verdict} v=${qc} />`}
    ${card.memory_proposals && card.memory_proposals.length > 0 && html`<h4 style="margin-top:16px">What I learned this run (your call)</h4>
      ${card.memory_proposals.map((m) => html`<div class="mem proposed" key=${m.id}><p style="margin:0">${m.text}</p>${m.evidence && m.evidence.quote && html`<p class="evid">“${m.evidence.quote}”</p>`}
        <div class="acts">${dec[m.id] === 'accept' ? html`<span class="pill ok">Added to memory</span>` : dec[m.id] === 'reject' ? html`<span class="pill">Dismissed</span>` : html`<button class="btn sm primary" disabled=${dec[m.id] === 'busy'} onClick=${() => decide(m, 'accept')}>Accept</button><button class="btn sm" disabled=${dec[m.id] === 'busy'} onClick=${() => decide(m, 'reject')}>Reject</button>`}</div></div>`)}`}
    <${Actions}>
      ${card.zip && html`<a class="btn" href=${card.zip} download>Download zip</a>`}
      ${!done && html`<button class="btn primary" disabled=${busy} onClick=${() => act({ action: 'approve' })}>Approve and finish</button><${BackBtn} step="ship" act=${act} busy=${busy} />`}
    <//></article>`;
}

function WorkingCard({ run, latest, onResume }) {
  const label = STATUS[run.status] ? STATUS[run.status][0] : run.status;
  const hint = { interrupted: 'The server restarted while this run was working. Nothing you approved is lost: resume to continue.', error: 'Something went wrong in this run. Check the decision log, then resume to try again.', cancelled: 'You stopped this run.', done: 'This run is finished.' }[run.status];
  const busy = run.status === 'running';
  return html`<article class="card working" aria-live="polite">
    ${busy ? html`<div class="spinner" aria-hidden="true"></div>` : null}
    <h2>${busy ? `Working on ${STEP_LABELS[run.current_step]}` : label}</h2>
    <p class="mut">${hint || (latest ? latest.text : 'Getting started.')}</p>
    ${(run.status === 'interrupted' || run.status === 'error') && onResume && html`<div class="actions"><button class="btn primary" onClick=${onResume}>Resume</button></div>`}
    ${busy && html`<p class="mut sm">Nothing to do right now. This screen updates itself when it needs you.</p>`}</article>`;
}

function GateCard(props) {
  const { run } = props;
  const g = run.gate;
  if (!g && run.status === 'done' && run.artifacts && run.artifacts.cuts) {
    const card = { type: 'ship', cuts: run.artifacts.cuts, zip: run.artifacts.zip, memory_proposals: props.memory.filter((m) => m.status === 'proposed') };
    return html`<${ShipCard} ...${props} card=${card} />`;
  }
  if (!g) return html`<${WorkingCard} run=${run} latest=${props.latest} onResume=${props.onResume} />`;
  const card = g.card;
  const C = { brief: BriefCard, ideas: IdeasCard, preview: PreviewCard, edit: EditCard, check: CheckCard, ship: ShipCard }[card.type];
  if (!C) return html`<article class="card"><p>Unknown card type: ${card.type}</p></article>`;
  return html`<${C} key=${g.id} ...${props} card=${card} />`;
}

// ---------- side column ----------
function SteerBox({ run, act, busy }) {
  const [text, setText] = useState('');
  const ref = useRef();
  const canSteer = run.status === 'awaiting_human' && run.gate && run.gate.card.type !== 'ship';
  useEffect(() => {
    const f = (e) => { setText(e.detail); ref.current && ref.current.focus(); };
    window.addEventListener('prefill-steer', f);
    return () => window.removeEventListener('prefill-steer', f);
  }, []);
  async function send(e) {
    e.preventDefault();
    if (!text.trim()) return;
    const ok = await act({ action: 'steer', text: text.trim() });
    if (ok) setText('');
  }
  return html`<form class="panel steer" onSubmit=${send}><h2 style="font-size:17px">Steer</h2>
    <label class="f" for="steer-text" style="margin-top:0">Tell Wonder what to change, in your own words</label>
    <textarea id="steer-text" ref=${ref} value=${text} placeholder=${canSteer ? 'e.g. c2 but a shorter hook, and no Diwali' : run.gate ? 'Nothing to steer on this step' : 'Available when a card is waiting for you'} disabled=${!canSteer || busy} onInput=${(e) => setText(e.target.value)}></textarea>
    <div class="actions" style="margin-top:10px"><button class="btn" type="submit" disabled=${!canSteer || busy || !text.trim()}>Steer</button>
      ${run.status === 'running' && html`<span class="mut sm">Working. Steer opens when I ask.</span>`}</div></form>`;
}

function Commentary({ items }) {
  const ref = useRef();
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [items.length]);
  return html`<section class="panel" aria-label="Commentary"><h2 style="font-size:17px">What Wonder is doing</h2>
    ${items.length ? html`<ul class="comm" ref=${ref} aria-live="polite">${items.map((c) => html`<li><span class="ts">${new Date(c.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>${c.text}</li>`)}</ul>` : html`<p class="mut sm">Nothing yet.</p>`}</section>`;
}

// ---------- log drawer ----------
function LogDrawer({ runId, onClose, toast }) {
  const [rows, setRows] = useState(null);
  const ref = useRef();
  useEffect(() => {
    api(`/api/runs/${runId}/log`).then(setRows).catch((e) => { toast(e.message); setRows([]); });
    ref.current && ref.current.focus();
    const k = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [runId]);
  const who = { human: 'You', agent: 'Wonder', system: 'System' };
  return html`<div class="scrim" onClick=${onClose}></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-label="Decision log" tabindex="-1" ref=${ref}>
      <div class="memhead"><h2 style="margin:0">Decision log</h2><button class="btn sm" onClick=${onClose}>Close</button></div>
      <p class="mut sm">Everything that happened in this run, in plain words.</p>
      ${rows === null ? html`<p class="mut">Loading...</p>` : rows.length === 0 ? html`<p class="mut">Nothing logged yet.</p>` : html`<ol class="log">${rows.map((r) => html`<li key=${r.seq}><span class="who">#${r.seq} · ${who[r.actor] || r.actor}${r.step ? ' · ' + STEP_LABELS[r.step] : ''}${r.cost_usd ? ' · ' + usd(r.cost_usd) : ''}</span>${r.text}${r.memory_ids && r.memory_ids.length > 0 && html`<div style="margin-top:4px">${r.memory_ids.map((id) => html`<span class="cite" title=${id}>rule ${id}</span>`)}</div>`}</li>`)}</ol>`}
    </aside>`;
}

// ---------- runs list ----------
function RunsList({ clients, clientId, setClientId, onOpen, toast, startOpen }) {
  const [runs, setRuns] = useState(null);
  const [goal, setGoal] = useState('');
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(!!startOpen);
  useEffect(() => { setOpen(!!startOpen); }, [startOpen]);
  useEffect(() => { api('/api/runs').then(setRuns).catch((e) => { toast(e.message); setRuns([]); }); }, []);
  async function create(e) {
    e.preventDefault();
    setBusy(true);
    try { const r = await api('/api/runs', { method: 'POST', body: { client_id: clientId, goal: goal.trim() || undefined } }); onOpen(r.run_id); }
    catch (x) { toast(x.message); setBusy(false); }
  }
  const client = (id) => (clients.find((c) => c.id === id) || {}).name || id;
  return html`<main class="runs-wrap">
    <div class="memhead" style="margin-bottom:12px"><h1 style="margin:0;font-size:22px">Your runs</h1><button class="btn primary" onClick=${() => setOpen(!open)} aria-expanded=${open}>New run</button></div>
    ${open && html`<form class="card" style="margin-bottom:16px" onSubmit=${create}>
      <h2>Start a new run</h2>
      <label class="f" for="cl">Client</label>
      <select id="cl" value=${clientId} onChange=${(e) => setClientId(e.target.value)}>${clients.map((c) => html`<option value=${c.id}>${c.name}</option>`)}</select>
      <label class="f" for="goal">Anything to add to the brief? (optional)</label>
      <textarea id="goal" placeholder="e.g. This is for the Karwa Chauth push" value=${goal} onInput=${(e) => setGoal(e.target.value)}></textarea>
      <div class="actions"><button class="btn primary" type="submit" disabled=${busy || !clientId}>${busy ? 'Starting...' : 'Start'}</button><span class="mut sm">Starting costs a few cents. Paid steps wait for your approval.</span></div></form>`}
    ${runs === null ? html`<p class="mut">Loading...</p>` : runs.length === 0 ? html`<div class="empty"><p><b>No runs yet.</b></p><p>Press New run to start your first one.</p></div>` :
      runs.map((r) => { const [l, c] = STATUS[r.status] || [r.status, '']; return html`<button class="runrow" key=${r.run_id} onClick=${() => onOpen(r.run_id)}>
        <span><b>${client(r.client_id)}</b><br /><span class="mut sm">${STEP_LABELS[r.current_step] || r.current_step} · ${new Date(r.created_at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}</span></span>
        <span style="text-align:right"><span class=${'status ' + c}>${l}</span><br /><span class="mut sm">${usd(r.cost_usd)}</span></span></button>`; })}
  </main>`;
}

// ---------- studio (one run) ----------
function Studio({ runId, cid, memory, memChanged, toast, openLog, memOpen }) {
  const [run, setRun] = useState(null);
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState([]);
  const runRef = useRef(null);

  const refetch = useCallback(async () => {
    try { const r = await api(`/api/runs/${runId}`); runRef.current = r; setRun(r); setLive([]); memChanged(); return r; }
    catch (e) { toast(e.message); }
  }, [runId]);

  useEffect(() => {
    setRun(null); refetch();
    let es, closed = false, delay = 1000, timer;
    const connect = () => {
      es = new EventSource(`/api/runs/${runId}/events`);
      es.onopen = () => { delay = 1000; };
      const re = () => refetch();
      es.addEventListener('card_ready', re);
      es.addEventListener('run_status', re);
      es.addEventListener('commentary', (e) => { try { const d = JSON.parse(e.data); setLive((l) => [...l, { ts: new Date().toISOString(), text: d.text }]); } catch (x) { /* ignore */ } });
      es.addEventListener('cost_update', (e) => { try { const d = JSON.parse(e.data); setRun((r) => r && { ...r, cost: { ...r.cost, usd: d.usd, cap_usd: d.cap_usd } }); } catch (x) { /* ignore */ } });
      es.addEventListener('error', (e) => { if (e && e.data) { try { toast(JSON.parse(e.data).message); } catch (x) { /* ignore */ } } });
      es.onerror = () => {
        if (closed) return;
        if (es.readyState === 2 || MOCK) { es.close(); timer = setTimeout(() => { connect(); refetch(); }, delay); delay = Math.min(delay * 2, 15000); }
      };
    };
    connect();
    return () => { closed = true; clearTimeout(timer); es && es.close(); };
  }, [runId]);

  const act = useCallback(async (body) => {
    const r = runRef.current;
    if (!r || !r.gate) return false;
    setBusy(true);
    try { await api(`/api/runs/${runId}/answer`, { method: 'POST', body: { gate: r.gate.id, ...body } }); await refetch(); setBusy(false); return true; }
    catch (e) { toast(e.message); if (e.status === 409) await refetch(); setBusy(false); return false; }
  }, [runId]);

  if (!run) return html`<p class="boot">Opening run...</p>`;
  const memById = {};
  memory.forEach((m) => { memById[m.id] = m; });
  run.memory_in_play.forEach((m) => { memById[m.id] = m; });
  const comm = [...(run.commentary || []), ...live];
  const inPlayIds = new Set(run.memory_in_play.map((m) => m.id));
  return html`<div>
    <${StatusRow} run=${run} openLog=${openLog} onCancel=${async () => { try { await api(`/api/runs/${runId}/cancel`, { method: 'POST' }); await refetch(); } catch (e) { toast(e.message); } }} />
    <div class="studio">
      <aside class=${'col-mem ' + (memOpen ? 'open' : '')}><${MemoryPanel} cid=${cid} items=${memory} inPlayIds=${inPlayIds} onChange=${memChanged} toast=${toast} /></aside>
      <main style="min-width:0"><${MemoryStrip} items=${run.memory_in_play} /><${Stepper} steps=${run.steps} />
        <${GateCard} memory=${memory} run=${run} act=${act} busy=${busy} memById=${memById} cid=${cid} memChanged=${memChanged} toast=${toast} latest=${comm[comm.length - 1]} onResume=${async () => { try { await api(`/api/runs/${runId}/resume`, { method: 'POST' }); await refetch(); } catch (e) { toast(e.message); await refetch(); } }} /></main>
      <div class="col-side"><${SteerBox} run=${run} act=${act} busy=${busy} /><div style="height:16px"></div><${Commentary} items=${comm} /></div>
    </div></div>`;
}

function StatusRow({ run, openLog, onCancel }) {
  return html`<div class="statusrow"><${StatusPill} status=${run.status} /><${CostBar} cost=${run.cost} />
    <button class="btn sm" onClick=${openLog}>Decision log</button>
    ${run.status === 'running' || run.status === 'awaiting_human' ? html`<button class="btn sm ghost" onClick=${onCancel}>Stop</button>` : null}</div>`;
}

// ---------- app ----------
function App() {
  const [auth, setAuth] = useState('checking');
  const [clients, setClients] = useState([]);
  const [clientId, setClientId] = useState('');
  const [memory, setMemory] = useState([]);
  const [hash, setHash] = useState(location.hash);
  const [logOpen, setLogOpen] = useState(false);
  const [memOpen, setMemOpen] = useState(false);
  const [toast, toastView] = useToast();

  useEffect(() => {
    onUnauth = () => setAuth('login');
    const hc = () => setHash(location.hash);
    window.addEventListener('hashchange', hc);
    api('/api/me').then(() => setAuth('ok')).catch(() => setAuth('login'));
    return () => window.removeEventListener('hashchange', hc);
  }, []);
  useEffect(() => {
    if (auth !== 'ok') return;
    api('/api/clients').then((cs) => { setClients(cs); setClientId((c) => c || (cs[0] && cs[0].id) || ''); }).catch((e) => toast(e.message));
  }, [auth]);
  const loadMem = useCallback(async () => {
    if (!clientId) return;
    try { setMemory(await api(`/api/clients/${clientId}/memory?status=all`)); } catch (e) { toast(e.message); }
  }, [clientId]);
  useEffect(() => { loadMem(); }, [loadMem]);

  const m = hash.match(/^#\/run\/(.+)$/);
  const runId = m ? decodeURIComponent(m[1]) : null;
  const open = (id) => { location.hash = id ? '#/run/' + encodeURIComponent(id) : '#/runs'; };
  useEffect(() => { setLogOpen(false); }, [runId]);

  if (auth === 'checking') return html`<p class="boot">Loading Studio...</p>`;
  if (auth === 'login') return html`<${Login} onDone=${() => setAuth('ok')} />`;
  const cname = (clients.find((c) => c.id === clientId) || {}).name || '';
  return html`<div>
    <header class="top"><div class="bar">
      <button class="brand" onClick=${() => open(null)} aria-label="AdFarming Studio, all runs"><span class="logo"></span>AdFarming Studio</button>
      ${cname && html`<span class="pill hide-sm">${cname}</span>`}
      ${runId && html`<button class="btn sm memtoggle" aria-expanded=${memOpen} onClick=${() => setMemOpen(!memOpen)}>Memory (${memory.filter((x) => x.status === 'approved').length})</button>`}
      <button class="btn sm" onClick=${() => open(null)}>All runs</button>
      <button class="btn sm primary hide-sm" onClick=${() => { location.hash = '#/new'; }}>New run</button>
    </div></header>
    ${runId
      ? html`<${Studio} key=${runId} runId=${runId} cid=${clientId} memory=${memory} memChanged=${loadMem} toast=${toast} openLog=${() => setLogOpen(true)} memOpen=${memOpen} />`
      : html`<${RunsList} clients=${clients} clientId=${clientId} setClientId=${setClientId} onOpen=${open} toast=${toast} startOpen=${hash === '#/new'} />`}
    ${logOpen && runId && html`<${LogDrawer} runId=${runId} onClose=${() => setLogOpen(false)} toast=${toast} />`}
    ${toastView}
  </div>`;
}

(async () => {
  if (MOCK) { const m = await import('./mock/mock.js'); await m.install(); }
  render(html`<${App} />`, document.getElementById('app'));
})();
