// Responsibility: Render everything inside the stage - the conversation, the brief, the timeline, the result.
// Owns: the lane registry, the shared mutable DOM state every event card attaches to.
// Boundaries: it renders; it does not fetch, open a socket, read an event's meaning, or open the viewer.

/* THE STAGE: everything that renders inside #stage - the conversation, the structured brief,
 * the process timeline and its event cards, and the final result.
 *
 * One module because it is one surface with one reason to change ("what the timeline shows"),
 * and because the lane registry is shared mutable DOM state that would have to be passed
 * around if the cards were split into separate files for their own sake.
 *
 * It renders. It does not fetch, does not open sockets, and does not decide what an event
 * means - `core/events.js` does that and calls in. When the run finishes it ANNOUNCES the
 * result through `onResult`; it does not open the viewer itself, which is what removed the old
 * stage <-> viewer cycle.
 */
import { esc, fmtDur, mdBlock } from "../core/format.js";
import { prettyText } from "../core/engineering_text.js";
import { SENT_BACK, attemptsLabel, laneLabel, reasoningHeader } from "../core/events.js";
import { applyFlow, bindAxis, bindUnit, displayText, followAxis, followSuggestion, followUnit, formHtml, markConfirmed,
  readForm, shown, unitChoiceHint, unitChoiceNeeded, unitOf } from "./geometry_form.js";
// the card keeps the user's answers through a re-draw and says back what they changed
import { changedAnswers, editsLine, noteDrawn, servedProposal } from "./geometry_form.js";

/* The lightbox is its own DOM region (#lb) but too small to be its own module. */
export function openLightbox(src) {
  document.getElementById("lb-img").src = src;
  document.getElementById("lb").classList.add("open");
}
export function closeLightbox() {
  document.getElementById("lb").classList.remove("open");
}

/* Installed by the entrypoint. The stage tells the application a run finished and hands over
   the data; the application decides whether that means opening a viewer. */
let onResult = () => {};
export function setResultHandler(fn) { onResult = fn || (() => {}); }

/* Installed by the entrypoint. The card's Cancel control only says the user asked to stop the
   run; asking them to confirm and calling the API belong to the application, not the renderer. */
let onCancel = () => {};
export function setCancelHandler(fn) { onCancel = fn || (() => {}); }

/* What each lane mark means, in words: the dot's title and its name for a screen reader. The
   marks themselves are drawn by css/theme.css; `Stage.markOf` decides which one a lane earns. */
const MARKS = {
  done: "finished", bad: "did not pass", back: "sent back for a rebuild",
  warn: "finished with a warning", nomesh: "no mesh was built here", ended: "stopped",
};

export const Stage = {
  // The empty state names no formats. It used to say "(STL or STEP)" while the server accepted
  // four, so a user holding a .vtp or .iges read that their file was unsupported and never tried
  // it - the picker offered it, the server accepted it, and only this sentence disagreed. The
  // list now comes from the capability response through `setSupportedCopy`, and until that
  // arrives the copy stays format-neutral rather than guessing.
  mount(){document.getElementById('stage').innerHTML='<div class="scroll"><div class="chat-col" id="cc"><div id="empty"><p id="empty-lead">Upload geometry to begin.</p><p id="empty-formats"></p><p class="empty-sub">Hexera will guide you through the simulation setup.</p></div></div></div>';
    this.nodes={};this.proc=null;this.tl=null;this._briefEl=null;this._briefSig='';
    // A NEW RUN STARTS CLEAN. The attempt the last run ended on, the attempts that built and a
    // mesh bar a run left open when it ended mid-mesh all belonged to that run: kept, the next
    // run's "Attempt 1" header went missing and its mesh bar never appeared (the old, detached
    // one still counted as open).
    this._att=null;this._attSep=null;this._built=new Set();this._seq=0;this.meshBar=null;this._ended=false;
    // ...and so did the stopped clock and the last event's time
    this.finished=false;this._evt=0;},
  // Fill the empty state's format line from the SERVER's capability description. Advisory and
  // best-effort: when capabilities are unavailable the line stays empty and the neutral lead
  // sentence still invites an upload, because the server is the authority on what it accepts.
  setSupportedCopy(text){const el=document.getElementById('empty-formats');
    if(el)el.innerHTML=text?String(text).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])):'';},
  col(){return document.getElementById('cc');},
  scrollBottom(){const s=document.querySelector('#stage .scroll');if(s)s.scrollTop=s.scrollHeight;},
  clearEmpty(){document.getElementById('empty')?.remove();},
  // There is ONE assistant identity in this product and it is Hexera - the same
  // one that took the request also delivers the result, so the default names it
  // rather than leaving a generic "Assistant" to sign off the run. `who` only
  // overrides that when some other party is speaking.
  // Assistant text is shown as an engineer prints it (prettyText): y⁺ = 30–300 rather than the
  // \(y^+=30\text{–}300\) an older stored reply may carry, and 40 m/s never split across a line.
  // The user's own words are shown exactly as they typed them.
  chat(role,text,who){this.clearEmpty();const g=document.createElement('div');g.className='im '+(role==='user'?'user':'assistant');
    if(role==='user')g.innerHTML=`<div class="who">${esc(who||'You')}</div><div class="bub">${mdBlock(text)}</div>`;
    else g.innerHTML=`<div class="who">${esc(who||'Hexera')}</div><div class="txt">${mdBlock(prettyText(displayText(text)))}</div>`;
    this.col().appendChild(g);this.scrollBottom();},
  // THE FINALIZED BRIEF, as the application settled it - not the model's prose
  // re-read here. Rendered once and then updated in place, because a later turn
  // re-sends the same brief and a conversation should not accumulate copies of it.
  brief(b){
    if(!b)return;
    const sig=JSON.stringify(b);
    if(this._briefSig===sig)return;
    this._briefSig=sig;
    const ROWS=[['purpose','Purpose'],['engine','Engine'],['input_kind','Input'],
                ['dimensionality','Dimensionality'],['mesh_fidelity','Mesh detail preference'],
                ['label','Case']];
    // Prefer the server's display label. A field may be label-ONLY: the fidelity tier always
    // applies, even when the user stated no preference and there is no raw value.
    let rows=ROWS.filter(([k])=>b[k]||b[k+'_label']).map(([k,l])=>
      `<div class="rc-row"><div class="rc-k">${esc(l)}</div><div class="rc-v">${esc(b[k+'_label']||b[k])}</div></div>`).join('');
    if(b.engine_params&&Object.keys(b.engine_params).length)
      rows+=`<div class="rc-row"><div class="rc-k">Engine parameters</div>`
           +`<div class="rc-v">${esc(JSON.stringify(b.engine_params))}</div></div>`;
    (b.boundary_assignments||[]).forEach(p=>{
      rows+=`<div class="rc-row"><div class="rc-k">${esc(p.name)}</div>`
           +`<div class="rc-v">${esc(p.role||'')}</div></div>`;});
    const ck=(b.checks||[]).map(c=>{
      const good=c.status==='pass',unknown=c.status==='unknown';
      return `<div class="rc-ck${good?'':(unknown?' unk':' bad')}">`
        +`<span class="ic">${good?'✓':(unknown?'?':'✕')}</span>`
        +`<span>${esc(c.label)}${c.detail?` <span class="rc-d">${esc(c.detail)}</span>`:''}</span></div>`;}).join('');
    const html=`<div class="who">Requirements · submitted by Hexera</div>
      <div class="req-card${(b.checks||[]).every(c=>c.status==='pass')?' ok':''}">
        <div class="rc-h">Structured brief</div>${rows}
        ${ck?`<div class="rc-h rc-h2">Validated by the application</div>${ck}`:''}
      </div>`;
    if(this._briefEl){this._briefEl.innerHTML=html;}
    else{this.clearEmpty();
      const g=document.createElement('div');g.className='im assistant';g.innerHTML=html;
      this.col().appendChild(g);this._briefEl=g;}
    this.scrollBottom();},

  /* THE ENGINE CHOICE, under the one engine question. Every engine that can take the uploaded file,
     best measured fit first: its fit, the plain-words reason (how it did in the lab on shapes measured
     to be like this one) and a button that picks it. The recommendation is marked, never pre-picked:
     the user chooses, and the click is sent as their own words ("Use cfMesh") so the conversation
     records the choice. One card per open question; a turn that closes the question (a reply with
     no choice) retires the old card's buttons. */
  engineChoice(c,pick){
    const old=this._ecEl;
    const engines=(c&&c.engines)||[];
    if(!engines.length){
      if(old){old.querySelectorAll('button').forEach(b=>{b.disabled=true;});
        old.querySelector('.eng-choice')?.classList.add('done');}
      return;}
    if(old)old.remove();
    this.clearEmpty();
    const rows=engines.map((e,i)=>{
      const cls=['ec-row'];if(e.recommended)cls.push('rec');if(e.outside)cls.push('out');
      const badge=e.recommended?'<span class="ec-badge">Recommended</span>':'';
      return `<div class="${cls.join(' ')}" data-engine="${esc(e.engine)}">`
        +`<div class="ec-top"><span class="ec-name">${esc(e.label||e.engine)}</span>${badge}`
        +`<span class="ec-fit">${esc(e.fit||'')}</span></div>`
        +`<div class="ec-why">${esc(e.reason||'')}</div>`
        +(e.heads_up?`<div class="ec-note">Heads-up: ${esc(e.heads_up)}</div>`:'')
        +`<button type="button" class="ec-pick" data-i="${i}">Use ${esc(e.label||e.engine)}</button></div>`;}).join('');
    const shape=c.shape?`<div class="ec-shape">This looks like ${esc(c.shape)}.</div>`:'';
    const g=document.createElement('div');g.className='im assistant';
    g.innerHTML=`<div class="eng-choice" role="group" aria-label="Choose the mesh engine">`
      +`<div class="ec-h">Pick the mesh engine</div>${shape}${rows}`
      +`<div class="ec-foot">Ranked by how each engine did in our lab on shapes measured to be like this one. You choose.</div></div>`;
    g.querySelectorAll('.ec-pick').forEach(b=>b.addEventListener('click',()=>{
      const e=engines[Number(b.dataset.i)]||{};
      g.querySelectorAll('.ec-pick').forEach(x=>{x.disabled=true;});
      b.closest('.ec-row').classList.add('picked');
      g.querySelector('.eng-choice').classList.add('done');
      if(pick)pick(e.reply||('Use '+(e.label||e.engine)));}));
    this.col().appendChild(g);this._ecEl=g;this.scrollBottom();},

  /* THE GEOMETRY CHECK AS A CARD: the fallback when the 3D stage cannot open (no skin stored,
     no WebGL). The hero is the solid angled view - the translucent overview is drawn for the
     vision model, not for people - and the form is the same one the stage shows. One card per
     session, replaced if the check is shown again. */
  geometryCheck(d,confirm){
    const p=d.proposal||{},pics=d.pictures||[];
    // what the check proposed, kept apart from the card the user edits: Proceed says back what changed
    this._gcServed=servedProposal(null,p);
    // WHAT WAS TYPED ON THE CARD BEFORE outlives its re-draw (the model's labels arriving): a
    // name or role the user set, a typed diameter, the kind, the flow, the far-field box, the
    // ground, the unit box - read before the new form is built, so it is drawn with the user's
    // answers. Only what the user CHANGED is carried: an answer they left as drawn takes the
    // model's word, where it used to put the measuring step's back over it.
    const was=this._gcEl&&!this._gcEl.querySelector('.gc-done')?this._gcTyped(this._gcEl,this._gcP):null;
    if(was&&was.unit){p.unit=was.unit;p.unit_basis='chosen';p.unit_touched=true;}
    if(was&&Object.keys(was.answers).length){p.user_set=was.answers;Object.assign(p,was.answers);}
    const hero=pics.find(x=>x.name==='iso')||pics.find(x=>x.name!=='overview')||pics[0];
    const thumbs=pics.filter(x=>x!==hero&&x.name!=='overview').slice(0,8).map(x=>
      `<img class="gc-thumb" src="${x.url}" alt="${esc(x.name)}" title="${esc(x.name)}" loading="lazy">`).join('');
    const size=(p.size_mm||[]).map(v=>shown(v,p)).join(' x ');
    const html=`<div class="who">Geometry check · what Hexera sees</div>
      <div class="req-card gc-card">
        <div class="rc-h">${esc(p.part||'the part')}${size?` · ${esc(size)} <span class="gc-u">${unitOf(p)}</span>`:''}</div>
        ${hero?`<img class="gc-overview" src="${hero.url}" alt="the part, with a numbered sticker on each opening" title="click to enlarge">`:''}
        ${thumbs?`<div class="gc-thumbs">${thumbs}</div>`:''}
        ${formHtml(p)}
      </div>`;
    this.clearEmpty();
    if(this._gcEl)this._gcEl.remove();
    const g=document.createElement('div');g.className='im assistant';g.innerHTML=html;
    this.col().appendChild(g);this._gcEl=g;this._gcP=p;
    if(was)this._gcRestore(g,was,p);
    applyFlow(g);bindUnit(g,p);bindAxis(g,p);noteDrawn(g);
    g.querySelectorAll('.gc-overview,.gc-thumb').forEach(im=>{im.onclick=()=>openLightbox(im.src);});
    const btn=g.querySelector('.gc-proceed');
    btn.onclick=async()=>{
      // the unit is picked, never passed, while the size makes it doubtful
      if(unitChoiceNeeded(p)){g.querySelector('.gc-hint').textContent=unitChoiceHint(p);return;}
      const body=readForm(g,p);
      btn.disabled=true;btn.textContent='Confirming…';
      try{
        await confirm(body,editsLine(this._gcServed,body,p));     // the chat says back what the user changed
        markConfirmed(g);
        g.querySelector('.gc-card').classList.add('ok');
      }catch(e){
        btn.disabled=false;btn.textContent='Looks right, proceed';
        g.querySelector('.gc-hint').textContent='Could not confirm: '+(e&&e.message||'try again');
      }
      this.scrollBottom();};
    this.scrollBottom();},

  _gcTyped(g,p){
    const v=(sel)=>{const el=g.querySelector(sel);return el?el.value:undefined;};
    const rows={};
    g.querySelectorAll('.gc-table tbody tr').forEach(tr=>{const id=Number(tr.dataset.id),o=((p&&p.openings)||[]).find(x=>Number(x.id)===id)||{};
      const name=(tr.querySelector('.gc-name')||{}).value,role=(tr.querySelector('.gc-role')||{}).value,dia=(tr.querySelector('.gc-dia')||{}).value;
      // each field on its own: a row whose role was changed keeps the model's name when it lands
      const r={};if(name!==undefined&&name!==(o.name||''))r.name=name;if(role!==undefined&&role!==o.role)r.role=role;if(dia)r.dia=dia;
      if(Object.keys(r).length)rows[id]=r;});
    // the kind, the flow, the far field and the ground: only those the user changed, on top of
    // what they changed before an earlier re-draw
    return {answers:{...((p&&p.user_set)||{}),...changedAnswers(g)},unit:p&&p.unit_touched?p.unit:undefined,rows,
            // the flow axis the user turned, and a reference length they typed (as typed, in the file's units)
            axis:p&&p.flow_axis_touched?p.flow_axis:undefined,ref:p&&p.reference_length_typed?v('.gc-ref'):undefined};},
  _gcRestore(g,was,p){
    const set=(sel,val)=>{const el=g.querySelector(sel);if(el&&val!==undefined)el.value=val;};
    if(was.ref!==undefined){set('.gc-ref',was.ref);p.reference_length_typed=true;}
    if(was.axis)followAxis(g,p,was.axis);        // an untyped reference length follows the user's axis
    Object.entries(was.rows).forEach(([id,r])=>{const tr=g.querySelector(`.gc-table tr[data-id="${id}"]`);if(!tr)return;
      const name=tr.querySelector('.gc-name'),role=tr.querySelector('.gc-role'),dia=tr.querySelector('.gc-dia');
      if(name&&r.name!==undefined)name.value=r.name;if(role&&r.role!==undefined)role.value=r.role;if(dia&&r.dia)dia.value=r.dia;});},

  /* THE CARD FOLLOWS THE CHECK IN PLACE: a unit settled in the chat reaches its unit box when the
     user has not set it themselves. The card is never drawn again for that, so what the user
     typed on it stays. A confirmed card is left as it is. */
  geometryCheckUpdate(d){
    const g=this._gcEl,p=this._gcP;if(!g||!p||g.querySelector('.gc-done'))return;
    const q=(d&&d.proposal)||{};
    if(d&&d.proposal)this._gcServed=servedProposal(this._gcServed,d.proposal);
    followUnit(g,p,q.unit,q.unit_basis);followSuggestion(g,p,q.unit_suggestion);},

  ensureProc(){if(this.proc)return;this.clearEmpty();
    const p=document.createElement('div');p.className='proc';
    p.innerHTML=`<div class="proc-head"><svg class="proc-orb" viewBox="0 0 24 24" aria-hidden="true"><circle class="sp-arm" cx="12" cy="12" r="7.2"/><path class="sp-hd" d="M19.20 8.16L22.40 14.56L16.00 14.56Z"/><path class="sp-hd" d="M4.80 15.84L1.60 9.44L8.00 9.44Z"/></svg>
      <span class="proc-title">Generating mesh</span>
      <span class="proc-sub" id="proc-sub">working…</span>
      <span class="proc-clock" id="proc-clock">0:00</span>
      <button class="proc-cancel" id="proc-cancel" type="button" hidden>Cancel run</button></div>
      <div class="tl" id="tl"></div>`;
    this.col().appendChild(p);this.proc=p;this.tl=p.querySelector('#tl');
    p.querySelector('#proc-cancel').onclick=()=>onCancel();
    this.t0=Date.now();
    // one ticker drives every live duration - the run's total and the active stage's
    clearInterval(this._tick);
    this._tick=setInterval(()=>{
      const c=document.getElementById('proc-clock');
      if(c&&!this.finished)c.textContent=fmtDur(Date.now()-this.t0);
      Object.values(this.nodes).forEach(n=>{
        if(n.t0&&!n.tEnd&&n.dur)n.dur.textContent=fmtDur(Date.now()-n.t0);});
      if(this.meshBar)this.tickMesh();
    },500);},

  // WAITING FOR A WORKER. The sub-line under "Generating mesh" said "working…" from the
  // moment a job was created, including the five to ten minutes a job can sit `pending`
  // while the fleet wakes from zero - a silent spinner that read as a hang. The poller hands
  // every status here; a non-empty text is the wait (with the backend's estimate), an empty one
  // means a worker has it and the line returns to "working…". A finished run is left alone.
  waiting(text){if(!this.proc||this.proc.classList.contains('done'))return;
    const s=this.proc.querySelector('#proc-sub');if(!s)return;
    s.textContent=text||'working…';},
  // The Cancel control is shown ONLY for a run this page is attached to live. A replayed run
  // renders the same card, and a control that could stop nothing must not be on it.
  showCancel(){const b=this.proc?.querySelector('#proc-cancel');if(b)b.hidden=false;},

  // THE RUN'S CLOCK IS THE SERVER'S. It counts from when the job was created, not from when this
  // page opened it - a reload, or a run opened from a link, used to start it again at 0:00 - and it
  // stops at the time the job ended. Before, it never stopped at all: a finished run kept counting.
  // A start ahead of this machine's own clock is held at now, so the clock never runs backwards.
  clock(createdAt,endedAt){
    const t0=Date.parse(createdAt||''),t1=Date.parse(endedAt||'');
    if(this.finished&&!(t1>0))return;
    if(t0>0)this.t0=Math.min(t0,Date.now());
    if(t1>0)this.finished=true;
    const c=this.proc&&this.proc.querySelector('.proc-clock');
    if(c&&this.t0)c.textContent=fmtDur((t1>0?t1:Date.now())-this.t0);},
  // WHEN, BY THE SERVER'S CLOCK. Every event says when it happened; a lane opens and closes at
  // those times, so a replayed run's stages last as long as they did - not the instant the replay
  // took, which read "0:00" on every stage after a reload. Live, it is the moment the event left.
  at(ts){const t=Date.parse(ts||'');this._evt=t>0?Math.min(t,Date.now()):0;},
  now(){return this._evt||Date.now();},

  // MESH RUN - bounded by the engine's DECLARED budget (published by the backend).
  // We bar elapsed against that real cap; we do not invent a percentage.
  startMesh(engine,budget,history){
    const n=this.node('builder');   // meshing IS the builder's run_mesh call
    // A MESHER RAN, in this lane and in this attempt: what the lane's mark and the attempt count
    // are about. The event is published just before the mesher starts, after every refusal.
    n.built=true;this._built.add(this._att||1);
    if(this.meshBar)return;
    // What we bar elapsed against. When we have MEASURED history for this engine+purpose
    // we bar against the TYPICAL time and say the honest range; otherwise we bar against
    // the engine's kill-deadline budget and say so. Never a fabricated percentage.
    const hasHist=history&&history.typical_s>0;
    const ref=hasHist?history.typical_s*1000:budget*1000;
    const sub=hasHist
      ?`similar runs ~${fmtDur(history.typical_s*1000)} (${fmtDur(history.p10_s*1000)}-${fmtDur(history.p90_s*1000)}, n=${history.n})`
      :`no history yet · will run up to ${fmtDur(budget*1000)}`;
    const w=document.createElement('div');w.className='meshbar';
    w.innerHTML=`<div class="mb-top"><span class="mb-lbl">meshing · ${esc(engine||'')}</span>
        <span class="mb-num">0:00</span></div>
      <div class="mb-track"><div class="mb-fill"></div></div>
      <div class="mb-sub">${esc(sub)}</div>`;
    n.body.appendChild(w);this.scroll(n);
    this.meshBar={el:w,t0:Date.now(),ref:ref,budget:budget*1000,hasHist:hasHist};},
  tickMesh(){const m=this.meshBar;if(!m)return;
    const el=Date.now()-m.t0;
    // Progress is against the reference time, capped at 100%. Past the typical time the
    // bar holds full and the label keeps counting up - a run CAN take longer than usual,
    // and pretending otherwise (or racing to a fake 100%) would be the lie we are avoiding.
    const pct=Math.min(100,(el/m.ref)*100);
    // THIS bar's parts, not a page-wide id: a retry keeps attempt 1's finished bar in the
    // timeline, and an id lookup found that one - attempt 2's bar sat at 0:00, empty.
    const f=m.el.querySelector('.mb-fill'),num=m.el.querySelector('.mb-num');
    if(f)f.style.width=pct+'%';
    if(f&&el>=m.ref)f.classList.add('over');
    if(num)num.textContent=fmtDur(el)+(el>=m.ref&&m.hasHist?' · longer than usual':'');},
  endMesh(cells){const m=this.meshBar;if(!m)return;
    this.tickMesh();m.el.classList.add('done');
    const el=Date.now()-m.t0;
    // The run FINISHED. While it runs the bar is elapsed against the engine's
    // budget, but a delivered mesh is 100% of the work - a run that lands well
    // inside its budget must not be left as a 3% sliver that has turned green.
    const f=m.el.querySelector('.mb-fill');
    if(f){f.classList.remove('over');f.style.width='100%';}
    // and the sub-line stops predicting a run that already happened
    const sub=m.el.querySelector('.mb-sub');
    if(sub)sub.textContent=`finished in ${fmtDur(el)}`;
    const num=m.el.querySelector('.mb-num');
    if(num&&cells)num.textContent=`${fmtDur(el)} · ${cells}`;
    this.meshBar=null;},
  node(agent){this.ensureProc();
    if(this.nodes[agent])return this.nodes[agent];
    const n=document.createElement('div');n.className='tl-node open';
    n.innerHTML=`<div class="tl-dot active"></div>
      <div class="tl-name"><span class="nm">${esc(laneLabel(agent))}</span><span class="ct"></span>
        <span class="dur"></span><span class="tl-chev">∨</span></div>
      <div class="tl-body"><div class="tl-inner"></div></div>`;
    n.querySelector('.tl-name').onclick=()=>n.classList.toggle('open');
    this.tl.appendChild(n);
    this.nodes[agent]={el:n,body:n.querySelector('.tl-inner'),dot:n.querySelector('.tl-dot'),
                       ct:n.querySelector('.ct'),dur:n.querySelector('.dur'),
                       t0:this.now(),tEnd:null,count:0,
                       // what the lane's mark is read from: see markOf
                       agent,seq:++this._seq,worst:0};
    return this.nodes[agent];},
  done(a){const n=this.nodes[a];if(n&&!n.tEnd){n.dot.classList.remove('active');
    n.el.classList.remove('open');n.tEnd=Math.max(n.t0,this.now());
    if(n.dur)n.dur.textContent=fmtDur(n.tEnd-n.t0);this.paint(n);}},
  /* THE LANE'S MARK SAYS HOW IT ENDED. A tick is earned: it used to be every closed lane's, so a
     review that failed the mesh and sent it back for a rebuild closed with a green tick beside its
     FAIL, and "Mesh creation" was ticked on runs where nothing was built.
       bad    ✕  a check failed, an error was reported, or the review failed the mesh
       back   ↺  the attempt was sent back for a rebuild from here (set by `attempt`)
       warn   !  it only warned - or a delivered mesh's review left points open
       nomesh –  a mesh lane in which no mesher ran: nothing was built there
       ended  –  the run was cancelled while it was open
       done   ✓  it finished and nothing in it went wrong
     The mark is read from what the lane showed (failed checks, a note's tone, the verdict), never
     from the words of a note. */
  markOf(n){
    if(n.mark)return n.mark;
    if(n.verdict)return n.verdict==='PASS'?'done':'bad';
    if(n.worst>=2)return 'bad';
    if(n.worst===1)return 'warn';
    if(n.agent==='builder'&&!n.built)return 'nomesh';
    return 'done';},
  paint(n){if(!n||!n.tEnd)return;
    const m=this.markOf(n);
    Object.keys(MARKS).forEach(k=>n.dot.classList.remove(k));
    n.dot.classList.add(m);n.dot.title=MARKS[m];
    n.dot.setAttribute('role','img');n.dot.setAttribute('aria-label',MARKS[m]);},
  stage(agent){Object.keys(this.nodes).forEach(k=>{if(k!==agent)this.done(k);});this.node(agent);},
  bump(n){n.count++;n.ct.textContent=n.count+(n.count===1?' step':' steps');},
  scroll(n){n.body.scrollTop=n.body.scrollHeight;this.scrollBottom();},
  // A REBUILD starts the stages over. Appending attempt 2's checks under attempt 1's
  // produced "Mesh validation · 16 steps" - three attempts smeared into one lane.
  // Each retry re-opens the stages and marks which attempt they belong to.
  attempt(n){
    if(this._att===n)return;
    this.ensureProc();
    // THE LANE THAT SENT THE RUN BACK - the last one the attempt opened: the review that failed
    // the mesh, or the validation it did not pass. It reads "sent back for a rebuild", never a
    // tick, and a failed verdict is reworded as the request it turned out to be.
    const lanes=['builder','executor','classifier','reviewer'].map(a=>this.nodes[a]).filter(Boolean);
    const last=lanes.reduce((m,x)=>(!m||x.seq>m.seq?x:m),null);
    if(last){last.mark='back';
      if(last.verdict==='FAIL'&&last.verdictRow){const t=last.verdictRow.querySelector('.e-info');if(t)t.textContent=SENT_BACK;}}
    this._closeAttempt();
    this._att=n;
    // RETIRE the previous attempt's stages - close them and unregister the lane so the
    // next attempt opens fresh ones INSIDE the new group. The rows STAY in the DOM:
    // "why did attempt 1 fail" is the whole reason a user reads this timeline.
    lanes.forEach(x=>{this.done(x.agent);this.paint(x);delete this.nodes[x.agent];});
    const sep=document.createElement('div');sep.className='tl-att';
    sep.textContent=`Attempt ${n}`;
    // A run that passes first time has no "attempts" - it just has stages. Grouping is
    // only meaningful once there IS a second attempt, so attempt 1's header stays hidden
    // until one arrives, and then both appear together.
    if(n===1)sep.hidden=true;
    else this.tl.querySelectorAll('.tl-att[hidden]').forEach(e=>e.hidden=false);
    this.tl.appendChild(sep);this._attSep=sep;},
  // AN ATTEMPT THAT BUILT NOTHING SAYS SO on its header: "Attempt 2" alone read as a second mesh
  // on runs where every attempt was refused before a mesher ever started.
  _closeAttempt(){const s=this._attSep;if(!s||s.dataset.closed)return;s.dataset.closed='1';
    if(!this._built.has(this._att))s.textContent+=' · no mesh built';},
  // THE REVIEW'S VERDICT, kept on its lane: it decides the lane's mark (a FAIL is never a tick),
  // and a rebuild that follows rewords it as the request it was.
  verdict(agent,ok,text){this.info(agent,text,!ok);const n=this.node(agent);
    n.verdict=ok?'PASS':'FAIL';n.verdictRow=n.body.lastElementChild;this.paint(n);},

  // a passed/failed CHECK - the evidence the user is owed. The STATEMENT comes from
  // the engine (GateSpec.proves); the UI never translates an internal key.
  check(agent,text,ok){const n=this.node(agent);this.bump(n);
    if(!ok)n.worst=2;     // a failed check is never under a ticked lane
    const r=document.createElement('div');r.className='row';
    r.innerHTML=`<div class="e-check${ok?'':' bad'}"><span class="ic">${ok?'✓':'✕'}</span>`
      +`<span class="ct">${esc(text)}</span></div>`;
    n.body.appendChild(r);this.scroll(n);this.paint(n);},
  // `tone` is the note's own (info | warn | error): an error marks its lane failed, a warning
  // marks it warned. A caller that names no tone is read by `warn` alone, as before.
  info(agent,text,warn,tone){const n=this.node(agent);this.bump(n);const r=document.createElement('div');r.className='row';
    n.worst=Math.max(n.worst||0,tone==='error'?2:warn?1:0);
    r.innerHTML=`<div class="e-info${warn?' warn':''}">${esc(text)}</div>`;n.body.appendChild(r);this.scroll(n);this.paint(n);},
  // `action` arrives already in plain words - the tool roster is named on the backend,
  // beside the tools themselves. The UI does not know what a tool is called.
  tool(agent,action,detail){const n=this.node(agent);this.bump(n);const r=document.createElement('div');r.className='row';
    r.innerHTML=`<div class="e-tool"><span class="tag">step</span><span class="call">${esc(action)}</span></div>`;
    n.body.appendChild(r);
    if(detail){const d=document.createElement('div');d.className='e-detail';d.textContent=detail;n.body.appendChild(d);}
    this.scroll(n);},
  mcp(agent,query){const n=this.node(agent);this.bump(n);const r=document.createElement('div');r.className='row';
    r.innerHTML=`<div class="e-mcp"><span class="ico" aria-hidden="true"><svg viewBox="0 0 16 16" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="7" cy="7" r="4.5"/><line x1="10.6" y1="10.6" x2="14" y2="14" stroke-linecap="round"/></svg></span><span class="lbl">web search · MCP</span>
      <span class="q">${esc(query||'')}</span><span class="hid">results hidden</span></div>`;
    n.body.appendChild(r);this.scroll(n);},
  // A file the run PRODUCED. The event is emitted after the write landed, so the verb
  // is past tense and comes from the backend - the UI does not decide what happened.
  // Delivery is at-least-once and a reconnect replays the backlog, so the same file at
  // the same size renders once however many times it arrives.
  file(agent,path,bytes,op){const n=this.node(agent);
    const key=`${path}|${bytes}|${op||''}`;
    n.files=n.files||new Set();
    if(n.files.has(key))return;
    n.files.add(key);
    this.bump(n);const w=document.createElement('div');w.className='e-edit';
    w.innerHTML=`<div class="e-edit-h"><span class="ic"></span><span class="fname">${esc(path)}</span>
      <span class="act">${esc(op||'written')}</span><span class="badge">${esc(bytes||'')}</span></div>`;
    n.body.appendChild(w);this.scroll(n);},
  // REASONING - one card per reasoning id, updated across its lifecycle. The
  // backend decides whether there is any content to show; this only renders what
  // arrived. Nothing is estimated here: a duration or a token count that is not in
  // the event does not appear, because an invented "8.4s" is worse than silence.
  reasoning(agent,ev){
    const n=this.node(agent);
    n.think=n.think||{};
    const id=ev.id||('anon-'+n.count);
    let card=n.think[id];
    if(!card){
      card=document.createElement('div');card.className='row';
      card.innerHTML='<div class="e-think"><div class="lbl"></div><div class="body"></div></div>';
      n.body.appendChild(card);n.think[id]=card;this.bump(n);
    }
    // Three distinct states, and a fourth for a caller that declared none:
    //   started   -> "Thinking…"        (live, still running, marker pulses)
    //   completed -> "Thinking · 8.4s"  (measured values only, never invented)
    //   failed    -> "Thinking interrupted"
    //   no phase  -> "Thinking"         (prose with no lifecycle; claims nothing)
    const head=reasoningHeader(ev);
    card.querySelector('.lbl').textContent=head.text;
    // a terminal line is a statement about what happened, not a section label
    card.querySelector('.lbl').classList.toggle('measured',head.measured);
    // only an explicitly RUNNING card pulses; a phaseless one is static
    card.querySelector('.e-think').classList.toggle('active',head.running);
    const body=card.querySelector('.body');
    if(ev.content){body.textContent=ev.content;body.style.display='';}
    else{body.textContent='';body.style.display='none';}
    this.scroll(n);},

  // TOOLS - the backend supplies the words. In safe mode that is an approved public
  // label and nothing else; in raw mode the real name, arguments and result come
  // too. The browser never maps a tool name to a label: a browser that could has
  // already been told the name.
  toolCall(agent,ev){
    const n=this.node(agent);this.bump(n);
    n.tools=n.tools||{};
    if(ev.id&&n.tools[ev.id])return;                 // replay: one card per call
    const r=document.createElement('div');r.className='row';
    const blocked=ev.status==='blocked';
    r.innerHTML=`<div class="e-tool${blocked?' bad':''}"><span class="tag">${blocked?'blocked':'tool call'}</span>`
      +`<span class="call">${esc(ev.tool_name||ev.public_label||'')}</span></div>`;
    n.body.appendChild(r);
    if(ev.arguments!=null){const d=document.createElement('div');d.className='e-detail';
      d.textContent=JSON.stringify(ev.arguments,null,1);n.body.appendChild(d);}
    if(ev.id)n.tools[ev.id]={el:r};
    this.scroll(n);},
  toolResult(agent,ev){
    const n=this.node(agent);
    n.tools=n.tools||{};
    const t=ev.tool_call_id&&n.tools[ev.tool_call_id];
    const bits=[];
    if(ev.status&&ev.status!=='success')bits.push(ev.status);
    if(typeof ev.duration_ms==='number')bits.push((ev.duration_ms/1000).toFixed(1)+'s');
    if(t&&t.el&&!t.done){
      t.done=true;
      if(bits.length){const m=document.createElement('span');m.className='tres';
        m.textContent=' · '+bits.join(' · ');t.el.querySelector('.e-tool').appendChild(m);}
    }else if(!t){
      this.bump(n);const r=document.createElement('div');r.className='row';
      r.innerHTML=`<div class="e-tool${ev.status==='success'?'':' bad'}"><span class="tag">result</span>`
        +`<span class="call">${esc(ev.tool_name||ev.public_label||'')}</span>`
        +(bits.length?`<span class="tres"> · ${esc(bits.join(' · '))}</span>`:'')+`</div>`;
      n.body.appendChild(r);
    }
    if(ev.result!=null){const d=document.createElement('div');d.className='e-detail';
      d.textContent=JSON.stringify(ev.result,null,1);n.body.appendChild(d);}
    this.scroll(n);},

  // RATIONALE - the application's own conclusion. Always shown: it was written for
  // the reader, in both modes, and it is never a paraphrase of hidden reasoning.
  rationale(agent,ev){
    const n=this.node(agent);
    // at-least-once delivery: the same conclusion replayed is the same conclusion
    const key=(ev.conclusion||'')+'|'+(ev.because||'');
    n.rats=n.rats||new Set();
    if(n.rats.has(key))return;
    n.rats.add(key);
    this.bump(n);
    const r=document.createElement('div');r.className='row';
    r.innerHTML=`<div class="e-rat"><div class="rat-c">${esc(ev.conclusion||'')}</div>`
      +(ev.because?`<div class="rat-w">${esc(ev.because)}</div>`:'')+`</div>`;
    n.body.appendChild(r);this.scroll(n);},
  // THE REVIEWER'S PICTURES: one small grid under the reviewer's steps, three to a row, filled as
  // the pictures arrive. It sits outside the scrolling step list, so a picture is never pushed out
  // of sight by the steps that follow it; past a few rows the grid scrolls on its own.
  screenshot(b64){const n=this.node('reviewer');
    // a live event can arrive twice; the same render must not stack up. A PNG's last bytes hold
    // its final data checksum, so two different renders of the same size are still told apart.
    const s=String(b64),k=s.length+':'+s.slice(-64);
    n.shots=n.shots||new Set();
    if(n.shots.has(k))return;
    n.shots.add(k);
    if(!n.gallery){const g=document.createElement('div');g.className='e-shots';
      g.innerHTML='<div class="e-shots-h">Pictures<span class="e-shots-n"></span></div><div class="e-shots-grid"></div>';
      n.el.querySelector('.tl-body').appendChild(g);
      n.gallery={count:g.querySelector('.e-shots-n'),grid:g.querySelector('.e-shots-grid')};}
    const {grid,count}=n.gallery;
    // follow the newest picture only when the reader is not looking further up the grid
    const follow=grid.scrollHeight-grid.scrollTop-grid.clientHeight<8;
    const b=document.createElement('button');b.type='button';b.className='e-shot';
    b.setAttribute('aria-label',`Enlarge picture ${n.shots.size}`);
    const im=document.createElement('img');im.alt='';im.src='data:image/png;base64,'+s;
    b.onclick=()=>openLightbox(im.src);
    b.appendChild(im);grid.appendChild(b);
    count.textContent=String(n.shots.size);
    if(follow)grid.scrollTop=grid.scrollHeight;
    this.scrollBottom();},
  final(data){
    // ONE ENDING PER RUN: two status polls in flight can both see the job end, and a second
    // ending drew a second verdict row and a second closing message under the first.
    if(this._ended)return;this._ended=true;
    const cancelled=!!data.cancelled;
    // a delivered mesh whose review left points open is never stamped PASS
    const concerns=data.pass&&data.reviewOutcome==='delivered_with_concerns';
    const failed=!data.pass&&!cancelled;
    const outcome=concerns?'concerns':data.pass?'pass':cancelled?'cancelled':'fail';
    // THE MARKS FOLLOW HOW THE RUN ENDED (see markOf). A cancelled run's open lane stopped; a
    // failed run shows its failure where it stopped, unless a lane already shows it; a delivered
    // mesh whose review left points open is a warning on that review, not a failure.
    const lanes=Object.values(this.nodes);
    if(cancelled)lanes.filter(n=>!n.tEnd).forEach(n=>{n.mark='ended';});
    else if(failed&&!lanes.some(n=>this.markOf(n)==='bad'))lanes.filter(n=>!n.tEnd).forEach(n=>{n.mark='bad';});
    if(concerns&&this.nodes.reviewer&&this.nodes.reviewer.verdict==='FAIL')this.nodes.reviewer.mark='warn';
    this._closeAttempt();
    // A MESH BAR STILL RUNNING when the run ended stops where it is: it counted on for ever after a
    // run cancelled mid-mesh. It does not turn green either - nothing finished.
    if(this.meshBar){const s=this.meshBar.el.querySelector('.mb-sub');
      if(s)s.textContent='stopped when the run ended';this.meshBar.el.classList.add('stopped');this.meshBar=null;}
    // the open lanes close when the job ended, and the run's clock stops there
    this.at(data.endedAt);
    Object.keys(this.nodes).forEach(k=>this.done(k));
    lanes.forEach(n=>this.paint(n));
    this.clock(data.createdAt,data.endedAt);
    if(!this.finished){this.finished=true;
      const c=this.proc&&this.proc.querySelector('.proc-clock');if(c&&this.t0)c.textContent=fmtDur(Date.now()-this.t0);}
    // HOW MANY ATTEMPTS, as the timeline counted them: only one that ran a mesher counts
    const opened=this._att!=null||this._built.size>0;
    const att=attemptsLabel(opened,this._built.size,data.attempts);
    if(this.proc){this.proc.classList.add('done','r-'+outcome);   // the header's mark takes the verdict's colour
      this.proc.querySelector('.proc-title').textContent=data.pass?'Mesh ready':cancelled?'Run cancelled':'Run ended';
      this.proc.querySelector('#proc-sub').textContent=concerns?'delivered with concerns':data.pass?'completed':cancelled?'cancelled by you':'failed';
      const cb=this.proc.querySelector('#proc-cancel');if(cb)cb.hidden=true;}
    // verdict row in the timeline. A cancelled run has no verdict: nobody judged a mesh.
    if(this.proc){const n=this.node('result');const v=document.createElement('div');v.className='tl-verd';
      const pill=concerns?'DELIVERED WITH CONCERNS':data.pass?'PASS':cancelled?'CANCELLED':'FAIL';
      v.innerHTML=`<span class="pill${concerns?' concerns':data.pass?'':cancelled?' cancelled':' fail'}">${pill}</span><span class="mt">${esc(att)}</span>`;
      if(cancelled&&data.cancelReason){const r=document.createElement('span');r.className='mt';r.textContent=`reason: ${data.cancelReason}`;v.appendChild(r);}
      // the result lane's mark is the verdict's: never a tick beside a FAIL
      n.mark={pass:'done',concerns:'warn',cancelled:'ended',fail:'bad'}[outcome];
      n.body.appendChild(v);this.done('result');}
    if(opened)data={...data,attempts:this._built.size};    // the viewer says the same number

    // FINAL RESULT - one turn in the SAME conversation that took the request, so the
    // verified result reaches the user through the identity they have been talking to.
    // The text is rendered by application code from durable facts; nothing here reworks
    // it, and no second persona announces it.
    if(data.text)this.chat('assistant',data.text);

    const anchor=document.createElement('div');this.col().appendChild(anchor);
    // The stage does not know what a viewer is. It states what happened and where the result
    // may be attached; the application decides whether that means opening one.
    onResult(data, anchor);
    this.scrollBottom();},
};
