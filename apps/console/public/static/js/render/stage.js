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
import { laneLabel, reasoningHeader } from "../core/events.js";

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

export const Stage = {
  // The empty state names no formats. It used to say "(STL or STEP)" while the server accepted
  // four, so a user holding a .vtp or .iges read that their file was unsupported and never tried
  // it - the picker offered it, the server accepted it, and only this sentence disagreed. The
  // list now comes from the capability response through `setSupportedCopy`, and until that
  // arrives the copy stays format-neutral rather than guessing.
  mount(){document.getElementById('stage').innerHTML='<div class="scroll"><div class="chat-col" id="cc"><div id="empty"><span id="empty-lead">Upload geometry to begin.</span><br><span id="empty-formats"></span>Hexera will guide you through the simulation setup.</div></div></div>';
    this.nodes={};this.proc=null;this.tl=null;this._briefEl=null;this._briefSig='';},
  // Fill the empty state's format line from the SERVER's capability description. Advisory and
  // best-effort: when capabilities are unavailable the line stays empty and the neutral lead
  // sentence still invites an upload, because the server is the authority on what it accepts.
  setSupportedCopy(text){const el=document.getElementById('empty-formats');
    if(el)el.innerHTML=text?String(text).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))+'<br>':'';},
  col(){return document.getElementById('cc');},
  scrollBottom(){const s=document.querySelector('#stage .scroll');if(s)s.scrollTop=s.scrollHeight;},
  clearEmpty(){document.getElementById('empty')?.remove();},
  // There is ONE assistant identity in this product and it is Hexera - the same
  // one that took the request also delivers the result, so the default names it
  // rather than leaving a generic "Assistant" to sign off the run. `who` only
  // overrides that when some other party is speaking.
  chat(role,text,who){this.clearEmpty();const g=document.createElement('div');g.className='im '+(role==='user'?'user':'assistant');
    if(role==='user')g.innerHTML=`<div class="who">${esc(who||'You')}</div><div class="bub">${mdBlock(text)}</div>`;
    else g.innerHTML=`<div class="who">${esc(who||'Hexera')}</div><div class="txt">${mdBlock(text)}</div>`;
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

  ensureProc(){if(this.proc)return;this.clearEmpty();
    const p=document.createElement('div');p.className='proc';
    p.innerHTML=`<div class="proc-head"><svg class="proc-orb" viewBox="0 0 24 24" aria-hidden="true"><circle class="sp-arm" cx="12" cy="12" r="7.2"/><path class="sp-hd" d="M19.20 8.16L22.40 14.56L16.00 14.56Z"/><path class="sp-hd" d="M4.80 15.84L1.60 9.44L8.00 9.44Z"/></svg>
      <span class="proc-title">Generating mesh</span>
      <span class="proc-sub" id="proc-sub">working…</span>
      <span class="proc-clock" id="proc-clock">0:00</span></div>
      <div class="tl" id="tl"></div>`;
    this.col().appendChild(p);this.proc=p;this.tl=p.querySelector('#tl');
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

  // MESH RUN - bounded by the engine's DECLARED budget (published by the backend).
  // We bar elapsed against that real cap; we do not invent a percentage.
  startMesh(engine,budget,history){
    const n=this.node('builder');   // meshing IS the builder's run_mesh call
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
        <span class="mb-num" id="mb-num">0:00</span></div>
      <div class="mb-track"><div class="mb-fill" id="mb-fill"></div></div>
      <div class="mb-sub" id="mb-sub">${esc(sub)}</div>`;
    n.body.appendChild(w);this.scroll(n);
    this.meshBar={el:w,t0:Date.now(),ref:ref,budget:budget*1000,hasHist:hasHist};},
  tickMesh(){const m=this.meshBar;if(!m)return;
    const el=Date.now()-m.t0;
    // Progress is against the reference time, capped at 100%. Past the typical time the
    // bar holds full and the label keeps counting up - a run CAN take longer than usual,
    // and pretending otherwise (or racing to a fake 100%) would be the lie we are avoiding.
    const pct=Math.min(100,(el/m.ref)*100);
    const f=document.getElementById('mb-fill'),num=document.getElementById('mb-num');
    if(f)f.style.width=pct+'%';
    if(f&&el>=m.ref)f.classList.add('over');
    if(num)num.textContent=fmtDur(el)+(el>=m.ref&&m.hasHist?' · longer than usual':'');},
  endMesh(cells){const m=this.meshBar;if(!m)return;
    this.tickMesh();m.el.classList.add('done');
    const el=Date.now()-m.t0;
    // The run FINISHED. While it runs the bar is elapsed against the engine's
    // budget, but a delivered mesh is 100% of the work - a run that lands well
    // inside its budget must not be left as a 3% sliver that has turned green.
    const f=document.getElementById('mb-fill');
    if(f){f.classList.remove('over');f.style.width='100%';}
    // and the sub-line stops predicting a run that already happened
    const sub=document.getElementById('mb-sub');
    if(sub)sub.textContent=`finished in ${fmtDur(el)}`;
    const num=document.getElementById('mb-num');
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
                       t0:Date.now(),tEnd:null,count:0};
    return this.nodes[agent];},
  done(a){const n=this.nodes[a];if(n&&!n.tEnd){n.dot.classList.remove('active');n.dot.classList.add('done');
    n.el.classList.remove('open');n.tEnd=Date.now();
    if(n.dur)n.dur.textContent=fmtDur(n.tEnd-n.t0);}},
  stage(agent){Object.keys(this.nodes).forEach(k=>{if(k!==agent)this.done(k);});this.node(agent);},
  bump(n){n.count++;n.ct.textContent=n.count+(n.count===1?' step':' steps');},
  scroll(n){n.body.scrollTop=n.body.scrollHeight;this.scrollBottom();},
  // A REBUILD starts the stages over. Appending attempt 2's checks under attempt 1's
  // produced "Mesh validation · 16 steps" - three attempts smeared into one lane.
  // Each retry re-opens the stages and marks which attempt they belong to.
  attempt(n){
    if(this._att===n)return;
    this._att=n;
    // RETIRE the previous attempt's stages - close them and unregister the lane so the
    // next attempt opens fresh ones INSIDE the new group. The rows STAY in the DOM:
    // "why did attempt 1 fail" is the whole reason a user reads this timeline.
    ['builder','executor','classifier','reviewer'].forEach(a=>{
      if(this.nodes[a]){this.done(a);delete this.nodes[a];}});
    const sep=document.createElement('div');sep.className='tl-att';
    sep.textContent=`Attempt ${n}`;
    // A run that passes first time has no "attempts" - it just has stages. Grouping is
    // only meaningful once there IS a second attempt, so attempt 1's header stays hidden
    // until one arrives, and then both appear together.
    if(n===1)sep.hidden=true;
    else this.tl.querySelectorAll('.tl-att[hidden]').forEach(e=>e.hidden=false);
    this.tl.appendChild(sep);},

  // a passed/failed CHECK - the evidence the user is owed. The STATEMENT comes from
  // the engine (GateSpec.proves); the UI never translates an internal key.
  check(agent,text,ok){const n=this.node(agent);this.bump(n);
    const r=document.createElement('div');r.className='row';
    r.innerHTML=`<div class="e-check${ok?'':' bad'}"><span class="ic">${ok?'✓':'✕'}</span>`
      +`<span class="ct">${esc(text)}</span></div>`;
    n.body.appendChild(r);this.scroll(n);},
  info(agent,text,warn){const n=this.node(agent);this.bump(n);const r=document.createElement('div');r.className='row';
    r.innerHTML=`<div class="e-info${warn?' warn':''}">${esc(text)}</div>`;n.body.appendChild(r);this.scroll(n);},
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
  screenshot(b64){const n=this.node('reviewer');
    // a reconnect replays the backlog; the same render must not stack up
    n.shots=n.shots||new Set();
    const k=String(b64).slice(0,64)+':'+String(b64).length;
    if(n.shots.has(k))return;
    n.shots.add(k);
    const w=document.createElement('div');w.className='e-shot';
    const im=document.createElement('img');im.src='data:image/png;base64,'+b64;im.onclick=()=>openLightbox(im.src);
    w.appendChild(im);n.body.appendChild(w);this.scroll(n);},
  final(data){Object.keys(this.nodes).forEach(k=>this.done(k));
    if(this.proc){this.proc.classList.add('done');this.proc.querySelector('.proc-title').textContent=data.pass?'Mesh ready':'Run ended';
      this.proc.querySelector('#proc-sub').textContent=data.pass?'completed':'failed';}
    // verdict row in the timeline
    if(this.proc){const n=this.node('result');const v=document.createElement('div');v.className='tl-verd';
      const att=data.attempts>0?`${data.attempts} attempt${data.attempts!==1?'s':''}`:'';
      v.innerHTML=`<span class="pill${data.pass?'':' fail'}">${data.pass?'PASS':'FAIL'}</span><span class="mt">${esc(att)}</span>`;
      n.body.appendChild(v);this.done('result');}

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
