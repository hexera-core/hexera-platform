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
// a thermal model (an ECXML upload) has its own panel, card or stage alike
import { bindThermalPanel, thermalPanelHtml } from "./thermal_form.js";
// an FEA part has its own panel too: its size, unit, bodies and clean check, and no flow form
import { bindFeaPanel, feaPanelHtml, followAssembly, followClean, followMeasure } from "./fea_form.js";
// the run card's live line: the step running now, in plain words
import { createRunLine } from "./run_progress.js";
// the closing message of a finished run, as a summary rather than a wall of lines
import { outcomeHtml } from "./outcome.js";
const RunLine = createRunLine();

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

/* Installed by the entrypoint. A frame of the mesh-so-far exists: the stage says so and hands the
   pointer on. Reading the bytes and drawing them is the application's (viewer/mesh_partial.js) -
   this module renders and does not fetch. */
let onMeshPartial = () => {};
export function setMeshPartialHandler(fn) { onMeshPartial = fn || (() => {}); }

/* What each lane mark means, in words: the dot's title and its name for a screen reader. The
   marks themselves are drawn by css/theme.css; `Stage.markOf` decides which one a lane earns. */
const MARKS = {
  done: "finished", bad: "did not pass", back: "sent back for a rebuild",
  warn: "finished with a warning", nomesh: "no mesh was built here", ended: "stopped",
};

/** A tool call in words: the backend's public label, else its name with the underscores gone
 *  ("zoom_to_region" -> "Zoom to region"). */
export function toolWords(ev){
  const s=String((ev&&(ev.public_label||ev.tool_name))||'').replace(/[_-]+/g,' ').trim();
  return s?s.charAt(0).toUpperCase()+s.slice(1):'';}
/** True for text that is a JSON object or list, whole or in part - never shown as reasoning. */
export function looksLikeJson(text){
  const t=String(text||'').trim();
  return /^[\[{]/.test(t)||(t.match(/"[^"\n]{1,60}"\s*:/g)||[]).length>=2;}
/** Reasoning as a person reads it: no JSON drafts, no markdown stars. '' when nothing is left. */
export function plainReasoning(text){
  if(!text||looksLikeJson(text))return '';
  return String(text).replace(/\*\*([^*]+)\*\*/g,'$1').trim();}

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
    this.finished=false;this._evt=0;RunLine.reset();},
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
  // THE CLOSING ACCOUNT of a finished run, in the same voice as every other turn (render/outcome.js)
  outcome(text,opts){this.clearEmpty();const g=document.createElement('div');g.className='im assistant';
    g.innerHTML=`<div class="who">Hexera</div><div class="txt">${outcomeHtml(displayText(text),opts)}</div>`;
    this.col().appendChild(g);this.scrollBottom();},
  // THE FINALIZED BRIEF, as the application settled it - not the model's prose
  // re-read here. Rendered once and then updated in place, because a later turn
  // re-sends the same brief and a conversation should not accumulate copies of it.
  brief(b,addNote,parts){
    if(!b)return;
    const sig=JSON.stringify(b);
    if(this._briefSig===sig)return;
    this._briefSig=sig;
    // A WHOLE THERMAL MODEL reads as an engineer's job sheet: the job in one paragraph from the
    // file's own numbers, then only what the sheet does not already say
    const thermal=b.check_mode==='thermal'&&b.job_sheet;
    const ROWS=thermal?[['engine','Engine'],['mesh_fidelity','Detail level'],['label','Case']]
      :[['purpose','Purpose'],['engine','Engine'],['input_kind','Input'],
        ['dimensionality','Dimensions'],['mesh_fidelity','Detail level'],
        ['label','Case']];
    // Prefer the server's display label. A field may be label-ONLY: the fidelity tier always
    // applies, even when the user stated no preference and there is no raw value.
    // AN FEA PART has no detail level to show: its mesh is the rules-based recommended one
    const fea=b.purpose==='structural';
    let rows=ROWS.filter(([k])=>(b[k]||b[k+'_label'])&&!(fea&&k==='mesh_fidelity')).map(([k,l])=>
      `<div class="rc-row"><div class="rc-k">${esc(l)}</div><div class="rc-v">${esc(b[k+'_label']||b[k])}</div></div>`).join('');
    if(b.engine_params&&Object.keys(b.engine_params).length)
      rows+=`<div class="rc-row"><div class="rc-k">Engine settings</div>`
           +`<div class="rc-v">${esc(Object.entries(b.engine_params).map(([k,v])=>
             `${String(k).replace(/_/g,' ')}: ${typeof v==='object'?JSON.stringify(v):v}`).join(', '))}</div></div>`;
    (b.boundary_assignments||[]).forEach(p=>{
      rows+=`<div class="rc-row"><div class="rc-k">${esc(p.name)}</div>`
           +`<div class="rc-v">${esc(p.role||'')}</div></div>`;});
    // why the boundaries have these roles, when the uploaded file declares them
    if(b.boundaries_reason)
      rows+=`<div class="rc-row"><div class="rc-k">Why</div>`
           +`<div class="rc-v">${esc(b.boundaries_reason.charAt(0).toUpperCase()+b.boundaries_reason.slice(1))}</div></div>`;
    const ck=(b.checks||[]).map(c=>{
      const good=c.status==='pass',unknown=c.status==='unknown';
      return `<div class="rc-ck${good?'':(unknown?' unk':' bad')}">`
        +`<span class="ic">${good?'✓':(unknown?'?':'✕')}</span>`
        +`<span>${esc(c.label)}${c.detail?` <span class="rc-d">${esc(c.detail)}</span>`:''}</span></div>`;}).join('');
    // THE USER'S OWN NOTES, exactly as typed (a pasted spec keeps its lines), and the box that adds
    // one. Guidance for the mesh, never a check: the plan above is what is approved.
    // A short list, then one quiet line that grows as it is used: Enter adds, Shift+Enter starts a line.
    const items=(b.user_notes||[]).map(n=>`<li class="rc-note">${esc(n.text)}</li>`).join('');
    const notes=items?`<ul class="rc-notes-list">${items}</ul>`:'';
    const box=addNote?`<div class="rc-notes-box"><div class="rc-notes-row"><textarea class="rc-notes-in" rows="1" maxlength="20000"
        aria-label="Add a note" placeholder="Add a note, or paste a spec…"></textarea>
        <button type="button" class="rc-notes-add" disabled>Add</button></div>
        <span class="rc-notes-msg" role="status"></span></div>`:'';
    const html=`<div class="who">Hexera · your request, as understood</div>
      <div class="req-card${(b.checks||[]).every(c=>c.status==='pass')?' ok':''}">
        ${thermal?`<div class="rc-h">Job sheet</div><p class="rc-sheet">${esc(b.job_sheet)}</p>`
          :'<div class="rc-h">Mesh plan</div>'}${rows}
        ${ck?`<div class="rc-h rc-h2">Checked</div>${ck}`:''}
        ${notes||box?`<div class="rc-h rc-h2">Your notes</div>${notes}<div class="rc-pm"></div>${box}`:''}
      </div>`;
    if(this._briefEl){this._briefEl.innerHTML=html;}
    else{this.clearEmpty();
      const g=document.createElement('div');g.className='im assistant';g.innerHTML=html;
      this.col().appendChild(g);this._briefEl=g;}
    this._partMatches(b.part_matches,parts);
    const add=this._briefEl.querySelector('.rc-notes-add');
    const inp=this._briefEl.querySelector('.rc-notes-in');
    if(add&&inp){
      const msg=this._briefEl.querySelector('.rc-notes-msg');
      // the box is one line until it holds more: it grows with what is typed or pasted
      const fit=()=>{inp.style.height='';if(inp.value)inp.style.height=Math.min(inp.scrollHeight+2,240)+'px';
        add.disabled=!inp.value.trim();};
      inp.addEventListener('input',fit);
      const send=async()=>{
        const text=inp.value;if(!text.trim()||add.dataset.busy)return;
        add.dataset.busy='1';add.disabled=true;msg.textContent='';
        try{this._briefSig='';await addNote(text);}
        catch(e){delete add.dataset.busy;add.disabled=false;msg.textContent=(e&&e.message)||'The note was not saved.';}};
      add.onclick=send;
      inp.addEventListener('keydown',(e)=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send();}});}
    this.scrollBottom();},

  /* THE PARTS YOUR NOTES MEAN, under the notes, before Approve: one picture of the model, then a row per phrase
     ("frame rail · part 9 · 3–4 mm") with its colour, and a line per phrase that could not be placed. The picture
     tints each row's parts in the row's colour and leaves the rest grey; hovering or opening a row shows only ITS
     parts. A row opens the part list; a click on a part - in the list or on the picture - keeps it at once (no save
     button: the normal Approve takes it). A second side of the model is offered only when the first hides a part.
     Nothing at all when the notes name no part. `parts`: {get(), pick(phrase, ids), picture(name)}. */
  _partMatches(m,parts){
    clearTimeout(this._pmTimer);
    const el=this._briefEl&&this._briefEl.querySelector('.rc-pm');
    if(!el)return;
    this._pm=m||null;this._pmParts=parts||null;
    const draw=()=>{el.innerHTML=this._pmHtml(this._pm);this._pmPaint(el);};
    this._pmDraw=draw;
    draw();
    if(m&&(m.status==='reading'||m.status==='waiting')&&parts){
      const t0=Date.now();
      const poll=async()=>{
        let got=null;try{got=await parts.get();}catch{/* try again */}
        if(got)this._pm=got.status==='none'?null:got;
        draw();
        const busy=this._pm&&(this._pm.status==='reading'||this._pm.status==='waiting');
        if((busy||!got)&&Date.now()-t0<8*60e3)this._pmTimer=setTimeout(poll,3000);};
      this._pmTimer=setTimeout(poll,2500);}
    const rowOf=(i)=>{const m=this._pm;return m&&[...(m.rows||[]),...(m.unsure||[])].find(x=>x.phrase===i);};
    // a part toggled for the open row: kept at once, the card redrawn from the server's checked answer
    this._pmToggle=(id)=>{
      const i=this._pmOpen,r=rowOf(i);if(!r||!parts)return;
      const on=new Set(r.parts||[]);
      if(on.has(id))on.delete(id);else on.add(id);
      r.parts=[...on];delete r.lit;
      el.querySelectorAll(`.rc-pm-part[data-part="${CSS.escape(id)}"]`).forEach(c=>{
        c.classList.toggle('on',on.has(id));c.setAttribute('aria-pressed',String(on.has(id)));});
      this._pmPaint(el);
      clearTimeout(this._pmSave);
      this._pmSave=setTimeout(async()=>{
        try{const got=await parts.pick(i,[...on]);if(got&&this._pmOpen===i){this._pm=got;draw();}}
        catch(err){const msg=el.querySelector('.rc-pm-msg');if(msg)msg.textContent=(err&&err.message)||'Not saved.';}},450);};
    el.onclick=(e)=>{
      const m=this._pm;if(!m||m.status!=='done')return;
      if(e.target.closest('.rc-pm-flip')){this._pmView=this._pmView?0:1;this._pmPaint(el);return;}
      const chip=e.target.closest('.rc-pm-part');
      if(chip){this._pmToggle(chip.dataset.part);return;}
      const row=e.target.closest('.rc-pm-row');if(!row)return;
      const i=Number(row.dataset.phrase);
      this._pmOpen=this._pmOpen===i?null:i;draw();};
    el.onmouseover=(e)=>{const row=e.target.closest('.rc-pm-row');const i=row?Number(row.dataset.phrase):null;
      if(i!==this._pmHover){this._pmHover=i;this._pmPaint(el);}};
    el.onmouseleave=()=>{if(this._pmHover!=null){this._pmHover=null;this._pmPaint(el);}};},

  // the rows' colours: muted, distinct, the same order every time
  _PM_COL:['#5B9BD5','#E3A23B','#62B66C','#D9675F','#A37FCB','#4FB0AC','#CDB447','#E08DB0'],

  _pmHtml(m){
    if(!m)return '';
    if(m.status==='reading')return '<div class="rc-pm-wait" role="status">Reading your notes…</div>';
    if(m.status!=='done')return '';
    const open=this._pmOpen;
    const row=(r,k,unsure)=>{
      const i=r.phrase,isOpen=open===i;
      const what=unsure?esc(r.text):`<span class="rc-pm-w" title="${esc(r.quote)}">${esc(r.words)}</span>`;
      const where=unsure?'Pick parts'
        :`${esc(r.parts_text)}${r.size?`<span class="rc-pm-s">${esc(r.size)}</span>`:''}`;
      const dot=unsure?'<span class="rc-pm-dot none"></span>'
        :`<span class="rc-pm-dot" style="background:${this._PM_COL[k%this._PM_COL.length]}"></span>`;
      return `<button type="button" class="rc-pm-row${unsure?' unk':''}${isOpen?' open':''}" data-phrase="${i}"`
        +` aria-expanded="${isOpen}">${dot}<span class="rc-pm-q">${what}</span><span class="rc-pm-v">${where}</span>`
        +`<span class="rc-pm-c" aria-hidden="true"></span></button>`+(isOpen?this._pmPicker(m,r):'');};
    const views=m.views||[];
    const fig=views.length?`<div class="rc-pm-fig"><canvas class="rc-pm-pic" role="img"
        aria-label="Your model; the parts each note means are in its colour"></canvas>${views.length>1
        ?'<button type="button" class="rc-pm-flip" title="Other side" aria-label="Show the other side"></button>':''}</div>`:'';
    return `<div class="rc-pm-h">Parts in your notes</div>${fig}`
      +(m.rows||[]).map((r,k)=>row(r,k,false)).join('')+(m.unsure||[]).map(r=>row(r,0,true)).join('');},

  _pmColour(m,r){const k=(m.rows||[]).indexOf(r);return k>=0?this._PM_COL[k%this._PM_COL.length]:'#E3A23B';},

  _pmPicker(m,r){
    const on=new Set(r.parts||[]),c=this._pmColour(m,r);
    return `<div class="rc-pm-pick" role="group" aria-label="Which parts" style="--pm-c:${c}">`
      +(m.parts||[]).map(p=>`<button type="button" class="rc-pm-part${on.has(p.id)?' on':''}" data-part="${esc(p.id)}"`
        +` aria-pressed="${on.has(p.id)}"><span class="dot"></span>${esc(p.n)}`
        +`${p.name?`<span class="nm">${esc(p.name)}</span>`:''}</button>`).join('')
      +`${(m.views||[]).length?'<span class="rc-pm-hint">or click the parts on the picture</span>':''}`
      +'<span class="rc-pm-msg" role="status"></span></div>';},

  /* the picture: the neutral shaded side, each pixel's part read off its id picture, tinted by what the user is
     looking at; the numbers only where a part is seen (placed on the server, clear of each other) */
  async _pmPaint(el){
    const m=this._pm,cv=el.querySelector('.rc-pm-pic');
    if(!m||!cv||!(m.views||[]).length||!this._pmParts||!this._pmParts.picture)return;
    const view=m.views[Math.min(this._pmView||0,m.views.length-1)];
    this._pmCache=this._pmCache||{};
    const load=async(name)=>{
      if(!this._pmCache[name])this._pmCache[name]=this._pmParts.picture(name).then(b=>createImageBitmap(b));
      return this._pmCache[name];};
    let shaded,idsImg;
    try{[shaded,idsImg]=await Promise.all([load(view.name),load(view.name+'_ids')]);}
    catch{delete this._pmCache[view.name];delete this._pmCache[view.name+'_ids'];return;}
    if(!cv.isConnected)return;
    const w=shaded.width,h=shaded.height;
    cv.width=w;cv.height=h;
    const g=cv.getContext('2d');
    const key=view.name+'#ids';
    if(!this._pmCache[key]){
      const c2=new OffscreenCanvas(w,h).getContext('2d');c2.drawImage(idsImg,0,0);
      const d=c2.getImageData(0,0,w,h).data,ids=new Int32Array(w*h);
      for(let p=0;p<w*h;p++){const o=p*4;ids[p]=(d[o+3]>0&&Math.abs(d[o+2]-200)<=1)?d[o]+256*d[o+1]-1:-1;}
      this._pmCache[key]=ids;}
    const ids=this._pmCache[key];
    g.clearRect(0,0,w,h);g.drawImage(shaded,0,0);
    const img=g.getImageData(0,0,w,h),px=img.data;
    const order=m.order||(m.parts||[]).map(p=>p.id);
    const rows=m.rows||[],col=this._PM_COL;
    const rowOfPart={};rows.forEach((r,k)=>(r.lit||r.parts||[]).forEach(id=>{if(!(id in rowOfPart))rowOfPart[id]=k;}));
    const active=this._pmHover!=null?this._pmHover:this._pmOpen;
    const act=active!=null?[...rows,...(m.unsure||[])].find(r=>r.phrase===active):null;
    const actSet=act?new Set(act.lit||act.parts||[]):null;
    const actCol=act?this._pmColour(m,act):'#E3A23B';
    const rgb=(hex)=>[parseInt(hex.slice(1,3),16),parseInt(hex.slice(3,5),16),parseInt(hex.slice(5,7),16)];
    const tint=order.map(id=>{
      if(actSet)return actSet.has(id)?rgb(actCol):null;
      return id in rowOfPart?rgb(col[rowOfPart[id]%col.length]):null;});
    for(let p=0;p<w*h;p++){
      const k=ids[p];if(k<0)continue;
      const o=p*4,L=px[o]/255,t=tint[k];
      if(t){const f=0.28+0.9*L;px[o]=Math.min(255,t[0]*f);px[o+1]=Math.min(255,t[1]*f);px[o+2]=Math.min(255,t[2]*f);}
      else{const v=actSet?60+70*L:85+95*L;px[o]=px[o+1]=px[o+2]=v;}}
    g.putImageData(img,0,0);
    // the numbers: where the part is seen; faded when another row is looked at
    g.font='600 14px Geist, system-ui, sans-serif';g.textAlign='center';g.textBaseline='middle';
    for(const t of view.tags||[]){
      const lit=!actSet||actSet.has(t.part);
      const tw=12+9*String(t.n).length;
      g.globalAlpha=lit?1:0.35;
      g.fillStyle='rgba(255,255,255,0.92)';g.strokeStyle='rgba(0,0,0,0.55)';g.lineWidth=1;
      g.beginPath();g.roundRect(t.x-tw/2,t.y-11,tw,22,6);g.fill();g.stroke();
      g.fillStyle='#111';g.fillText(String(t.n),t.x,t.y+0.5);}
    g.globalAlpha=1;
    // the picture answers the cursor: which part is here, and a click picks it for the open row
    const at=(e)=>{const r=cv.getBoundingClientRect();
      const x=Math.floor((e.clientX-r.left)*w/r.width),y=Math.floor((e.clientY-r.top)*h/r.height);
      const k=(x>=0&&y>=0&&x<w&&y<h)?ids[y*w+x]:-1;return k>=0?order[k]:null;};
    const name=(id)=>{const p=(m.parts||[]).find(q=>q.id===id);return p?`Part ${p.n}${p.name?` · ${p.name}`:''}`:'';};
    cv.onmousemove=(e)=>{const id=at(e);cv.title=id?name(id):'';cv.style.cursor=id&&this._pmOpen!=null?'pointer':'';};
    cv.onclick=(e)=>{e.stopPropagation();const id=at(e);if(!id)return;
      if(this._pmOpen!=null){this._pmToggle(id);return;}
      // no row open: open the row this part belongs to, so its match can be checked and fixed
      const r=rows.find(x=>(x.parts||[]).includes(id));
      if(r){this._pmOpen=r.phrase;this._pmDraw();}};},

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
    // the card follows the analysis the user chose: geometryCheckUpdate re-draws it as another kind
    this._gcConfirm=confirm;this._gcKind=d&&d.thermal?'thermal':d&&d.fea?'fea':'flow';
    if(d&&d.thermal)return this.thermalCheck(d,confirm);
    if(d&&d.fea)return this.feaCheck(d,confirm);
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
  /* A THERMAL MODEL AS A CARD: the same panel the stage shows, without the picture - the parts,
     the file's numbers, the detail level and "Mesh this model". */
  thermalCheck(d,confirm){
    this.clearEmpty();
    if(this._gcEl)this._gcEl.remove();
    const g=document.createElement('div');g.className='im assistant';
    g.innerHTML=`<div class="who">Thermal model · what Hexera read from the file</div>
      <div class="req-card tm-card">${thermalPanelHtml(d.thermal)}</div>`;
    this.col().appendChild(g);this._gcEl=g;this._gcP=null;
    bindThermalPanel(g.querySelector('.tm-card'),d.thermal,{confirm:async(body)=>{
      await confirm(body,'');g.querySelector('.tm-card').classList.add('ok');this.scrollBottom();}});
    this.scrollBottom();},

  /* AN FEA PART AS A CARD: the same panel the FEA stage shows, without the picture - the size, the
     unit box, the bodies, the clean check and the one question. No flow form, no stickers. */
  feaCheck(d,confirm){
    this.clearEmpty();
    if(this._gcEl)this._gcEl.remove();
    const p={...(d.proposal||{}),openings:[]};
    const g=document.createElement('div');g.className='im assistant';
    g.innerHTML=`<div class="who">Your part · for structural analysis</div>
      <div class="req-card fea-card">${feaPanelHtml(d.fea||{},p)}</div>`;
    this.col().appendChild(g);this._gcEl=g;this._gcP=p;
    bindFeaPanel(g.querySelector('.fea-card'),d.fea||{},p,{confirm:(body,edits)=>confirm(body,edits),
      done:()=>{g.querySelector('.fea-card').classList.add('ok');this.scrollBottom();}});
    this.scrollBottom();},

  /* THE BUTTONS UNDER AN APPLICATION QUESTION (application/purpose_map): what the mesh is for when
     the words did not say, and an FEA part's analysis type. A click sends the button's words as the
     user's own answer. A "coming soon" choice is greyed but still clickable: the part is then
     meshed as for static stress, and the chat says so. One row per open question; the next reply
     retires it. */
  choices(list,pick){
    const old=this._qcEl;
    if(old){old.querySelectorAll('button').forEach(b=>{b.disabled=true;});old.classList.add('done');this._qcEl=null;}
    if(!list||!list.length)return;
    this.clearEmpty();
    const g=document.createElement('div');g.className='im assistant qc';
    g.innerHTML=`<div class="qc-row" role="group" aria-label="Pick an answer">${list.map((c,i)=>
      `<button type="button" class="qc-btn${c.soon?' soon':''}" data-i="${i}"${c.soon?' title="Coming soon: meshed as for static stress for now"':''}>${esc(c.label)}</button>`).join('')}</div>`;
    g.querySelectorAll('.qc-btn').forEach(b=>b.addEventListener('click',()=>{
      const c=list[Number(b.dataset.i)]||{};
      g.querySelectorAll('.qc-btn').forEach(x=>{x.disabled=true;});
      b.classList.add('picked');g.classList.add('done');this._qcEl=null;
      if(pick)pick(c.send||c.label);}));
    this.col().appendChild(g);this._qcEl=g;this.scrollBottom();},

  geometryCheckUpdate(d){
    const g=this._gcEl,p=this._gcP;if(!g||!p||g.querySelector('.gc-done'))return;
    // AN FEA PART NEVER KEEPS THE FLOW CARD: a check served for another analysis re-draws the card
    if(d&&(d.route||d.thermal)&&this._gcConfirm&&(d.thermal?'thermal':d.fea?'fea':'flow')!==this._gcKind)
      return this.geometryCheck(d,this._gcConfirm);
    const q=(d&&d.proposal)||{};
    if(d&&d.proposal)this._gcServed=servedProposal(this._gcServed,d.proposal);
    followUnit(g,p,q.unit,q.unit_basis);followSuggestion(g,p,q.unit_suggestion);
    // an FEA part's measurement, when it lands after the card was drawn
    // the bodies, the clean check and the parts' joints follow too, each on its own
    const fol=(fn)=>{try{fn();}catch(e){console.warn('FEA card: not redrawn',e);}};
    if(d&&d.fea){fol(()=>followClean(g,d.fea));if(d.fea.assembly)fol(()=>followAssembly(g,d.fea.assembly));}
    if(d&&d.fea&&d.fea.measure)fol(()=>followMeasure(g,d.fea.measure,p,null));},

  ensureProc(){if(this.proc)return;this.clearEmpty();
    const p=document.createElement('div');p.className='proc';
    p.innerHTML=`<div class="proc-head"><svg class="proc-orb" viewBox="0 0 24 24" aria-hidden="true"><circle class="sp-arm" cx="12" cy="12" r="7.2"/><path class="sp-hd" d="M19.20 8.16L22.40 14.56L16.00 14.56Z"/><path class="sp-hd" d="M4.80 15.84L1.60 9.44L8.00 9.44Z"/></svg>
      <span class="proc-title">Generating mesh</span>
      <span class="proc-sub" id="proc-sub"></span>
      <span class="proc-clock" id="proc-clock">0:00</span>
      <button class="proc-cancel" id="proc-cancel" type="button" hidden>Cancel run</button></div>
      <div class="proc-live" id="proc-live"></div>
      <div class="tl" id="tl"></div>`;
    this.col().appendChild(p);this.proc=p;this.tl=p.querySelector('#tl');
    RunLine.attach(p.querySelector('#proc-live'));RunLine.draw();
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
      // the live line's notes follow the clock ("taking longer than similar runs")
      if(!this.finished&&(this._lineTick=(this._lineTick||0)+1)%10===0)RunLine.draw();
    },500);},

  // WAITING FOR A WORKER. The sub-line under "Generating mesh" said "working…" from the
  // moment a job was created, including the five to ten minutes a job can sit `pending`
  // while the fleet wakes from zero - a silent spinner that read as a hang. The poller hands
  // every status here; a non-empty text is the wait (with the backend's estimate), an empty one
  // means a worker has it and the sub-line empties: the live line under the header says what is
  // running, so a filler word there only repeated it. A finished run is left alone.
  waiting(text){if(!this.proc||this.proc.classList.contains('done'))return;
    const s=this.proc.querySelector('#proc-sub');if(!s)return;
    s.textContent=text||'';
    RunLine.model.waiting(text);RunLine.draw();},
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
      ?`similar runs take about ${fmtDur(history.typical_s*1000)} (usually ${fmtDur(history.p10_s*1000)} to ${fmtDur(history.p90_s*1000)})`
      :`no similar runs yet · stops by itself at ${fmtDur(budget*1000)}`;
    const w=document.createElement('div');w.className='meshbar';
    w.innerHTML=`<div class="mb-top"><span class="mb-lbl">meshing · ${esc(engine||'')}</span>
        <span class="mb-num">0:00</span></div>
      <div class="mb-track"><div class="mb-fill"></div></div>
      <div class="mb-sub">${esc(sub)}</div>`;
    n.body.appendChild(w);this.scroll(n);
    this.meshBar={el:w,t0:Date.now(),ref:ref,budget:budget*1000,hasHist:hasHist};
    RunLine.model.meshing(engine,budget,history,this.now());RunLine.draw();},
  // WHAT THE RUNNING MESHER SAYS IT IS DOING ("adding wall layers"), under its bar and on the line
  meshStage(label){
    // a narrated run says the stage in the user's words (narrate below); the mesher's label can name a tool
    const m=this.meshBar;if(m&&label&&!m.narrated){const l=m.el.querySelector('.mb-lbl');if(l)l.textContent=`meshing · ${label}`;}
    RunLine.model.meshStage(label,this.now());RunLine.draw();},
  // THE RUN'S NARRATION: what it is doing now and what it decided, in plain words, on the line
  narrate(agent,ev){if(!ev||!ev.text)return;
    const m=this.meshBar;
    if(m&&ev.kind!=='say'&&!/^Still /.test(ev.text)){m.narrated=true;
      const l=m.el.querySelector('.mb-lbl');if(l)l.textContent=String(ev.text);}
    RunLine.model.narrate(ev,this.now());RunLine.draw();},
  // A FRAME OF THE MESH-SO-FAR was published: a pointer, never geometry. The stage holds no
  // viewer and reads no bytes - it passes the pointer to whoever was installed to draw it, and a
  // build with nobody installed renders exactly what it did before.
  meshPartial(phase,key){onMeshPartial(phase,key);},
  // THE RECEIPT a phase published: deterministic rows the user opens on that step of the live line.
  // It is detail, not narration - the step's own one-line summary is untouched, and a receipt for a
  // stage this build has no step for is dropped by the model.
  receipt(agent,ev){if(!ev)return;
    RunLine.model.receipt({...ev,stage:ev.stage||agent},this.now());RunLine.draw();},
  // THE PLAN the run will build (cells, wall layers, where it refines), in its lane and on the line
  plan(agent,summary){if(summary)this.info(agent,'Plan: '+summary);
    RunLine.model.plan(summary,this.now());RunLine.draw();},
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
    this.meshBar=null;
    RunLine.model.meshed(cells,this.now());RunLine.draw();},
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
    RunLine.model.lane(agent,this.now());RunLine.draw();
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
    this.tl.appendChild(sep);this._attSep=sep;
    RunLine.model.attempt(n,this.now());RunLine.draw();},
  // AN ATTEMPT THAT BUILT NOTHING SAYS SO on its header: "Attempt 2" alone read as a second mesh
  // on runs where every attempt was refused before a mesher ever started.
  _closeAttempt(){const s=this._attSep;if(!s||s.dataset.closed)return;s.dataset.closed='1';
    if(!this._built.has(this._att))s.textContent+=' · no mesh built';},
  // THE REVIEW'S VERDICT, kept on its lane: it decides the lane's mark (a FAIL is never a tick),
  // and a rebuild that follows rewords it as the request it was.
  verdict(agent,ok,text){this.info(agent,text,!ok);const n=this.node(agent);
    n.verdict=ok?'PASS':'FAIL';n.verdictRow=n.body.lastElementChild;this.paint(n);
    RunLine.model.verdict(ok,this.now());RunLine.draw();},

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
  info(agent,text,warn,tone){const n=this.node(agent);
    n.worst=Math.max(n.worst||0,tone==='error'?2:warn?1:0);
    // the repair check's one line ("Your part needed no repair", "Repaired 3 defects: ...") sits on
    // the lane's own row beside its name, in sight when the lane is folded; a warning marks it "!"
    if(agent==='repair'){const t=String(text||'').trim();n.ct.textContent=t;n.ct.title=t;
      n.ct.classList.add('said');this.paint(n);this.scrollBottom();return;}
    this.bump(n);const r=document.createElement('div');r.className='row';
    // the engine lane's own sentence ("Mesh engine: cfMesh - ...") is the line's engine step, as said
    if(agent==='engine_select'){RunLine.model.engine(text,this.now());RunLine.draw();}
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
    const said=plainReasoning(ev.content);
    if(said){body.textContent=said;body.style.display='';}
    else{body.textContent='';body.style.display='none';}
    this.scroll(n);},

  // TOOLS - the backend supplies the words. In safe mode that is an approved public
  // label and nothing else; in raw mode the real name, arguments and result come
  // too. The browser never maps a tool name to a label: a browser that could has
  // already been told the name.
  // NEVER RAW JSON ON THE PAGE (dev-areen 2026-10-30: the open Review lane showed the reviewer's
  // arguments, results and JSON drafts). A call shows its name in words; its arguments and its
  // result stay out of the page; a reasoning text that is a JSON draft shows only its header.
  toolCall(agent,ev){
    const n=this.node(agent);this.bump(n);
    n.tools=n.tools||{};
    if(ev.id&&n.tools[ev.id])return;                 // replay: one card per call
    const r=document.createElement('div');r.className='row';
    const blocked=ev.status==='blocked';
    r.innerHTML=`<div class="e-tool${blocked?' bad':''}"><span class="tag">${blocked?'blocked':'tool call'}</span>`
      +`<span class="call">${esc(toolWords(ev))}</span></div>`;
    n.body.appendChild(r);
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
        +`<span class="call">${esc(toolWords(ev))}</span>`
        +(bits.length?`<span class="tres"> · ${esc(bits.join(' · '))}</span>`:'')+`</div>`;
      n.body.appendChild(r);
    }
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
    // a valid mesh the measured limits passed with some figures past a limit: "Passed, with notes",
    // the same words its result screen uses
    const notes=data.pass&&data.reviewOutcome==='passed_with_notes';
    const failed=!data.pass&&!cancelled;
    const outcome=concerns?'concerns':notes?'notes':data.pass?'pass':cancelled?'cancelled':'fail';
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
    {const t0=Date.parse(data.createdAt||''),t1=Date.parse(data.endedAt||'');
     RunLine.model.final({...data,concerns,notes,totalMs:t0>0&&t1>0?t1-t0:(this.t0?this.now()-this.t0:null)},this.now());
     RunLine.draw();}
    Object.keys(this.nodes).forEach(k=>this.done(k));
    lanes.forEach(n=>this.paint(n));
    this.clock(data.createdAt,data.endedAt);
    if(!this.finished){this.finished=true;
      const c=this.proc&&this.proc.querySelector('.proc-clock');if(c&&this.t0)c.textContent=fmtDur(Date.now()-this.t0);}
    // HOW MANY ATTEMPTS, as the timeline counted them: only one that ran a mesher counts
    const opened=this._att!=null||this._built.size>0;
    const att=attemptsLabel(opened,this._built.size,data.attempts);
    if(this.proc){this.proc.classList.add('done','r-'+outcome);   // the header's mark takes the verdict's colour
      // ONE STATEMENT OF THE ENDING: the title says it; the live line under it gives the time
      this.proc.querySelector('.proc-title').textContent=concerns?'Mesh ready, with concerns':notes?'Mesh ready, with notes':data.pass?'Mesh ready':cancelled?'Run cancelled':'Run failed';
      this.proc.querySelector('#proc-sub').textContent='';
      const cb=this.proc.querySelector('#proc-cancel');if(cb)cb.hidden=true;}
    // verdict row in the timeline. A cancelled run has no verdict: nobody judged a mesh.
    if(this.proc){const n=this.node('result');const v=document.createElement('div');v.className='tl-verd';
      const pill=concerns?'DELIVERED WITH CONCERNS':notes?'PASSED, WITH NOTES':data.pass?'PASS':cancelled?'CANCELLED':'FAIL';
      v.innerHTML=`<span class="pill${concerns?' concerns':notes?' notes':data.pass?'':cancelled?' cancelled':' fail'}">${pill}</span><span class="mt">${esc(att)}</span>`;
      if(cancelled&&data.cancelReason){const r=document.createElement('span');r.className='mt';r.textContent=`reason: ${data.cancelReason}`;v.appendChild(r);}
      // the result lane's mark is the verdict's: never a tick beside a FAIL
      n.mark={pass:'done',notes:'done',concerns:'warn',cancelled:'ended',fail:'bad'}[outcome];
      n.body.appendChild(v);this.done('result');}
    if(opened)data={...data,attempts:this._built.size};    // the viewer says the same number

    // FINAL RESULT - one turn in the SAME conversation that took the request, so the
    // verified result reaches the user through the identity they have been talking to.
    // The text is rendered by application code from durable facts; nothing here reworks
    // it, and no second persona announces it.
    // A message of several lines is the backend's structured account (final_result.render_message):
    // drawn as a summary with its detail folded away. A one-line ending stays a plain chat line.
    if(data.text&&String(data.text).includes('\n'))this.outcome(data.text,{concerns,notes});
    else if(data.text)this.chat('assistant',data.text);

    const anchor=document.createElement('div');this.col().appendChild(anchor);
    // The stage does not know what a viewer is. It states what happened and where the result
    // may be attached; the application decides whether that means opening one.
    onResult(data, anchor);
    this.scrollBottom();},
};
