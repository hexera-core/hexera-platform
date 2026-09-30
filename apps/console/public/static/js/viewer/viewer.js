// Responsibility: Present the delivered mesh - its surface, its deliverables, and the controls on it.
// Owns: the vtk.js boundary - the surface fetch, the render loop, the camera, and the cleanup after them.
// Boundaries: it starts no run and reassigns no shared state; the dispute modals belong to dispute.js.

/* THE VIEWER: the delivered mesh, its deliverables, and the controls attached to it.
 *
 * One boundary for everything vtk.js-shaped. It fetches the surface, builds the panel, owns the
 * render loop and the camera, and cleans up after itself.
 *
 * What it no longer does: reach for `startPipeline`, mutate `UI.proc/nodes/tl`, or reassign
 * `jobId`. The dispute modals moved to viewer/dispute.js - they were a different concern that
 * happened to live here.
 */
import { esc, fmtBytes } from "../core/format.js";
import { getSurface } from "../api/endpoints.js";
import { headers } from "../api/client.js";
import { acceptMesh, flagDispute } from "./dispute.js";
import { VIEWER_FALLBACK } from "./config.js";

let _vtkP=null;
function loadVtk(){if(window.vtk)return Promise.resolve();
  if(!_vtkP)_vtkP=new Promise((res,rej)=>{
    const s=document.createElement('script');s.src='/static/vendor/vtk.js';
    s.onload=res;s.onerror=()=>rej(new Error('vtk.js failed to load'));
    document.head.appendChild(s);});
  return _vtkP;}

function b64f32(b){const bin=atob(b),n=bin.length,u=new Uint8Array(n);
  for(let i=0;i<n;i++)u[i]=bin.charCodeAt(i);return new Float32Array(u.buffer);}
function b64u8(b){const bin=atob(b),n=bin.length,u=new Uint8Array(n);
  for(let i=0;i<n;i++)u[i]=bin.charCodeAt(i);return u;}
function b64u32(b){return new Uint32Array(b64u8(b).buffer);}

/* geometry fills: a warm drawing-office grey, each patch a step of tint so the parts read
   apart without shouting; the ORANGE selection is never ambiguous against any of them */
const _PATCH_COLS=[[0.80,0.78,0.72],[0.64,0.77,0.75],[0.83,0.76,0.60],
                   [0.72,0.72,0.70],[0.66,0.71,0.73],[0.76,0.70,0.66]];
const _EDGE=[0.27,0.25,0.22];          /* hairline edges at rest */
const _EDGE_HEAT=[0.47,0.51,0.57];     /* soft grey while the faces carry colour: the data reads through */
const _SEL=[1.0,0.31,0.0],_SEL_EDGE=[0.62,0.22,0.02];
const _SEL_COLS=['#e8613c','#f2c744','#3fa650','#3f7fd9','#b455c8','#38c2c2'];

export async function openViewer(job,anchorEl,opts){
  opts=opts||{};
  if(document.getElementById('viewer-'+job)){document.getElementById('viewer-'+job).scrollIntoView({behavior:'smooth'});return;}
  const box=document.createElement('div');box.className='viewer';box.id='viewer-'+job;
  const _m=opts.metrics||{};
  const _dl=(opts.files||[]).filter(f=>f.type!=='video');
  const _dlHtml=_dl.length?_dl.map(f=>{
      const primary=f.type==='mesh_bundle'?' primary':'';
      return `<a class="v-dlbtn${primary}" href="${esc(f.url||'#')}" download target="_blank" rel="noopener">
                <b>${esc(f.label||f.type)}</b><span>${fmtBytes(f.size)}</span></a>`;}).join(''):'';
  box.innerHTML=`<div class="v-bar">
      <div class="v-row">
        <b>${opts.unreviewed?'Mesh - not signed off':'Your mesh'}</b>
        <span class="v-meta">
          ${opts.unreviewed
            ? `<span class="v-pill fail">DID NOT PASS REVIEW</span>`
            : (_m.pass!==undefined?`<span class="v-pill${_m.pass?'':' fail'}">${_m.pass?'PASS':'FAIL'}</span>`:'')}
          ${_m.attempts?`<span class="sep">·</span><span>${_m.attempts} attempt${_m.attempts!==1?'s':''}</span>`:''}
        </span>
        <span style="flex:1"></span>
        <div class="v-dl">${_dlHtml}</div>
      </div>
      ${opts.unreviewed?`<div class="v-why">
        <b>Every validity gate passed</b> - the mesh is structurally sound and solvable.
        The reviewer would not sign it off on quality${(opts.findings||[]).length
          ? ': <span class="v-axes">'+(opts.findings||[]).map(f=>esc(f)).join(' · ')+'</span>':'.'}
        ${opts.reasoning?`<div class="v-reason">${esc(String(opts.reasoning).slice(0,600))}</div>`:''}
        <div class="v-why-act">
          Look at it yourself. If it is good enough for your study, say so - it will be
          re-reviewed against <i>your</i> bar and delivered.
          <button class="v-btn primary" id="v-accept-${job}">This is acceptable →</button>
        </div></div>`:''}
      <div class="v-row"><span class="v-status" id="v-status-${job}">loading…</span></div>
      <div class="v-row">
        <button class="v-btn" id="v-selbtn-${job}">Mark region</button>
        <span class="v-brush" title="region size - scales with your zoom">region
          <input type="range" id="v-brush-${job}" min="2" max="30" value="8">
          <span id="v-brushlbl-${job}"></span></span>
        <button class="v-btn" id="v-clearbtn-${job}" disabled>Clear marks</button>
        <span style="flex:1"></span>
        <button class="v-btn" id="v-fitbtn-${job}">Fit</button>
        </div>
      <div class="v-row v-act">
        <button class="v-btn primary" id="v-submit-${job}">Request a change…</button>
        <span class="v-cost">a change request re-runs the pipeline - minutes, not seconds</span>
      </div>
      <!-- MESH PARTS: every selectable entity the loaded case actually declares -
           boundary patches, regions, cell zones, face zones, layer groups. The list
           is read from the mesh payload, so a part appears because the case has it. -->
      <div class="v-facts" id="v-facts-${job}"></div>
      <div class="v-bnd" id="v-bnd-${job}">
        <div class="v-bnd-head">
          <span class="v-bnd-lbl">Mesh parts</span>
          <span class="v-bnd-count" id="v-bnd-count-${job}"></span>
          <span style="flex:1"></span>
          <button class="v-bnd-act" id="v-bnd-restore-${job}">Restore full mesh</button>
        </div>
        <div class="v-bnd-cols">
          <div class="v-bnd-list" id="v-bnd-chips-${job}"></div>
          <div class="v-bnd-insp" id="v-bnd-note-${job}"></div>
        </div>
      </div></div>
    <div class="v-canvas" id="v-canvas-${job}">
      <div class="v-loading" id="v-load-${job}"><svg class="v-spin" viewBox="0 0 24 24" aria-hidden="true"><circle class="sp-arm" cx="12" cy="12" r="7.2"/><path class="sp-hd" d="M19.20 8.16L22.40 14.56L16.00 14.56Z"/><path class="sp-hd" d="M4.80 15.84L1.60 9.44L8.00 9.44Z"/></svg><div>Loading mesh…</div></div>
      <div class="v-hint" id="v-hint-${job}">rotate: drag · zoom: wheel or right-drag · pan: shift+drag</div>
      <div class="v-count" id="v-count-${job}"></div></div>
    <div class="v-flags" id="v-flags-${job}" style="display:none"></div>`;
  // THE MESH TAKES THE STAGE. With a workbench on the page the delivered mesh fills it and the
  // conversation becomes the drawer beside it; an earlier result stays in the DOM, hidden, so
  // its context is not torn down under a still-running render. Without one (an embedding
  // page) the viewer opens inline after its anchor, as it always did.
  const _wb=document.getElementById('workbench'),_app=document.getElementById('app');
  if(_wb&&_app&&opts.takeover!==false){
    _wb.querySelectorAll('.viewer').forEach(v=>{v.hidden=true;});
    _wb.appendChild(box);
    _app.classList.add('wb');_app.classList.remove('wb-collapsed');
    const _tg=document.getElementById('wb-toggle');
    if(_tg){_tg.hidden=false;
      _tg.onclick=()=>{const on=_app.classList.toggle('wb-collapsed');
        _tg.textContent=on?'Show conversation':'Hide conversation';
        _tg.setAttribute('aria-pressed',on?'true':'false');};}
  } else anchorEl.after(box);
  // CASE B - "this is acceptable": the user amends the ACCEPTANCE CRITERIA, not the
  // mesh. Their statement joins the review brief, the SAME mesh is re-reviewed against
  // it, and the verdict is honoured (it can still fail). Validity is never overridden.
  const _acc=document.getElementById('v-accept-'+job);
  if(_acc)_acc.onclick=()=>acceptMesh(job,opts.findings||[]);

  // lazy: fetch + init only once the panel is actually on screen - until
  // then (and while fetching) the viewport shows the loading screen
  const start=async()=>{
    try{
      await loadVtk();
      // policy values come from the backend (single source of truth in settings, served by
      // api/v1/client_config.py); VIEWER_FALLBACK is only the degraded-mode default
      const uiCfg={...VIEWER_FALLBACK};
      const cr=await fetch('/api/v1/client-config',{headers:headers()}).catch(()=>null);
      if(cr&&cr.ok)Object.assign(uiCfg,(await cr.json()).viewer||{});
      const surf=await getSurface(job);
      initViewer(job,surf,uiCfg);
      document.getElementById('v-load-'+job)?.remove();
    }catch(e){
      // SAY WHY. A copy of the delivered-mesh figures block used to sit here, pasted into the
      // failure path where its `surf` is not in scope; it threw a ReferenceError before this
      // line could run, so a viewer that failed showed a stuck panel and no reason at all -
      // and the owner-scoped 404 hint below had never once reached a user. The figures belong
      // to the SUCCESS path and are rendered by initViewer, which is where the copy that works
      // has always been.
      const l=document.getElementById('v-load-'+job);
      if(l)l.innerHTML='<div>viewer unavailable</div>';
      document.getElementById('v-status-'+job).textContent='viewer unavailable: '+e.message
        +(String(e.message).includes('404')
          ? ' - the job is owner-scoped: add &user=<owner> to the URL and reload' : '');}};
  const io=new IntersectionObserver(es=>{
    if(es.some(x=>x.isIntersecting)){io.disconnect();start();}},{threshold:0.05});
  io.observe(box);
}

function initViewer(job,surf,uiCfg){
  const host=document.getElementById('v-canvas-'+job);
  const grw=vtk.Rendering.Misc.vtkGenericRenderWindow.newInstance({background:[0.035,0.055,0.075]});
  grw.setContainer(host);
  const ren=grw.getRenderer(),rw=grw.getRenderWindow();
  const apiRW=grw.getApiSpecificRenderWindow?grw.getApiSpecificRenderWindow():grw.getOpenGLRenderWindow();
  grw.resize();
  const ro=new ResizeObserver(()=>{setRenderScale();rw.render();});ro.observe(host);
  host.addEventListener('contextmenu',e=>e.preventDefault());

  /* one IMMUTABLE actor per patch - real polygon cells */
  const entries=[];let totalFaces=0;
  (surf.patches||[]).forEach((p,i)=>{
    let pts,polys,nCells;
    if(p.polys_b64){pts=b64f32(p.points_b64);polys=b64u32(p.polys_b64);
      // Count the polygons ACTUALLY in this connectivity array. face_count is the
      // patch's face count in the MESH, which a decimated preview surface does not
      // ship one-for-one; trusting it walks the offsets table off the end of polys.
      nCells=0;for(let q=0;q<polys.length;q+=polys[q]+1)nCells++;}
    else{pts=b64f32(p.positions_b64);nCells=pts.length/9;    // STL fallback: tri soup
      polys=new Uint32Array(nCells*4);
      for(let t=0;t<nCells;t++){polys[t*4]=3;polys[t*4+1]=t*3;polys[t*4+2]=t*3+1;polys[t*4+3]=t*3+2;}}
    const pd=vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(pts,3);
    pd.getPolys().setData(polys);
    const mapper=vtk.Rendering.Core.vtkMapper.newInstance();
    mapper.setInputData(pd);
    const actor=vtk.Rendering.Core.vtkActor.newInstance();
    actor.setMapper(mapper);
    const base=_PATCH_COLS[i%_PATCH_COLS.length];
    const pr=actor.getProperty();
    pr.setColor(base[0],base[1],base[2]);
    pr.setEdgeVisibility(true);pr.setEdgeColor(_EDGE[0],_EDGE[1],_EDGE[2]);pr.setLineWidth(1);
    pr.setAmbient(0.30);pr.setDiffuse(0.78);
    pr.setSpecular(0.10);pr.setSpecularPower(18);pr.setSpecularColor(1,1,1);
    // DOUBLE-SIDED. An OpenFOAM boundary face points out of the FLUID, so a car's wall faces point
    // into the car; with back faces culled the near side vanished and the viewer drew the inside of
    // the body (a car read as an open tray). The depth test does the hiding instead.
    if(pr.setBackfaceCulling)pr.setBackfaceCulling(false);
    ren.addActor(actor);
    const b=pd.getBounds();
    entries.push({actor,pd,patch:p.name,polys,pts,nCells,offsets:null,
      diag:Math.hypot(b[1]-b[0],b[3]-b[2],b[5]-b[4])});
    totalFaces+=nCells;});

  let edgeCol=_EDGE;             // what paint() draws edges with; the heatmap softens it
  // the parts panel's own repaint, and a listener for it: the heatmap hands edges back through the
  // panel (so a selected part keeps its selection edge) and re-reads what is on screen after it
  let repaintParts=null,onPartsPainted=null;
  const cam=ren.getActiveCamera();
  // CAD is z-up, and the geometry check already draws parts that way: start from a front-quarter
  // view looking slightly down with +z up. vtk's default camera is +y-up, which laid cars and
  // aircraft on their side in the mesh viewer.
  cam.setFocalPoint(0,0,0);cam.setPosition(1,-1,0.7);cam.setViewUp(0,0,1);
  ren.resetCamera();rw.render();
  const _bs=ren.computeVisiblePropBounds();
  const diag=Math.max(Math.hypot(_bs[1]-_bs[0],_bs[3]-_bs[2],_bs[5]-_bs[4]),1e-9);
  // The viewer renders the BOUNDARY surface, so `totalFaces` is boundary faces - not
  // cells. Say the real cell count when the backend knows it, and name what we counted
  // when it does not. (This badge used to call 8,260 boundary faces "8,260 cells".)
  document.getElementById('v-count-'+job).textContent=
    surf.cell_count?surf.cell_count.toLocaleString()+' cells'
                   :totalFaces.toLocaleString()+' boundary faces';

  document.getElementById('v-fitbtn-'+job).onclick=()=>{ren.resetCamera();clampCam();rw.render();};

 /* camera envelope: adaptive zoom caps computed from the real cells
     Full fidelity always - edges always on, full resolution. Instead of
     degrading quality in useless regimes, the camera cannot enter them:
     zoom-out stops where a TYPICAL cell (area-weighted median, shipped in
     cell_stats) hits ~2.5px so the grid never dissolves; zoom-in stops when
     ~8 of the FINEST cells fill the view (nothing more to resolve); the
     orbit target cannot leave the model box. STL fallback (no real cells)
     uses bbox-derived caps instead. */
  const _DPR=Math.min(window.devicePixelRatio||1,2);
  const stats=surf.cell_stats||null;
  const GRID_PX=uiCfg.grid_px, FINE_FILL=uiCfg.fine_fill,
        FRAME_FRAC=uiCfg.frame_frac, SPAN_F=uiCfg.flag_span_factor,
        MAX_FLAGS=uiCfg.max_flags;
  function setRenderScale(){
    const r=host.getBoundingClientRect();
    apiRW.setSize(Math.max(2,Math.round(r.width*_DPR)),
                  Math.max(2,Math.round(r.height*_DPR)));}
  function capRange(){
    const t=Math.tan(cam.getViewAngle()*Math.PI/360);
    if(stats&&stats.typ>0&&stats.p10>0){
      // zoom-out: the farther of (a) grid clearly visible - typical cell at
      // GRID_PX - and (b) framing ~the whole model (85% of the fit distance),
      // so huge refined meshes can still be seen "as a whole"
      const dGrid=stats.typ*(apiRW.getSize()[1]/(2*t))/GRID_PX;
      const dMax=Math.max(dGrid,_fitDist*FRAME_FRAC,_refitDist);
      const dMin=Math.min(FINE_FILL*stats.p10/(2*t),dMax*0.05);
      return [dMin,dMax];}
    return [diag*0.02,Math.max(_fitDist*1.1,_refitDist)];}
  // set by refit(): the distance the CURRENT visible set needs. The zoom-out cap
  // must not hold the camera closer than this, or re-showing a large enclosing
  // boundary leaves the viewer stuck inside it.
  let _refitDist=0;
  let _clamping=false;
  function clampCam(){
    if(_clamping)return;_clamping=true;
    try{
      // pan clamp: the orbit target stays inside the model box (+10%)
      const fp=cam.getFocalPoint(),cp=cam.getPosition(),m=0.1*diag;
      const cl=[Math.min(Math.max(fp[0],_bs[0]-m),_bs[1]+m),
                Math.min(Math.max(fp[1],_bs[2]-m),_bs[3]+m),
                Math.min(Math.max(fp[2],_bs[4]-m),_bs[5]+m)];
      const dx=cl[0]-fp[0],dy=cl[1]-fp[1],dz=cl[2]-fp[2];
      if(dx||dy||dz){cam.setFocalPoint(cl[0],cl[1],cl[2]);
        cam.setPosition(cp[0]+dx,cp[1]+dy,cp[2]+dz);}
      // zoom clamp
      const f=cam.getFocalPoint(),q=cam.getPosition();
      const vx=q[0]-f[0],vy=q[1]-f[1],vz=q[2]-f[2];
      const d=Math.hypot(vx,vy,vz)||1e-12;
      const r=capRange();
      const k=d<r[0]?r[0]/d:(d>r[1]?r[1]/d:1);
      if(k!==1)cam.setPosition(f[0]+vx*k,f[1]+vy*k,f[2]+vz*k);
    }finally{_clamping=false;}}

 /* region markers: one click = one concern
     Cell painting was scrapped: on a 235k-cell mesh a drag selects hundreds
     of cells only to be collapsed into {centroid, span} for the reviewer
     anyway. A marker IS that payload - it scales to any cell count. */
  const selBtn=document.getElementById('v-selbtn-'+job);
  const clearBtn=document.getElementById('v-clearbtn-'+job);
  const submitBtn=document.getElementById('v-submit-'+job);
  const flagsEl=document.getElementById('v-flags-'+job);
  const hintEl=document.getElementById('v-hint-'+job);
  const sizeEl=document.getElementById('v-brush-'+job);
  const sizeLbl=document.getElementById('v-brushlbl-'+job);
  const units=surf.mesh_units||'m';
  const flags=[];              // {x,y,z,span,patch,note,col,actors:[..],el}
  let markMode=false,downXY=null,_colorSeq=0,_lastR=0;

/* /
     BOUNDARY SELECTION - operate on the patch actors that already exist.
     Each named patch is one immutable actor (built above), so selecting,
     hiding and isolating are property changes on those actors: no geometry is
     rebuilt, and the mesh the user is judging is never swapped for a picture.
     Orange is reserved for the SELECTED boundary, per the token contract.
/ */
/* /
     MESH PARTS - every selectable entity the LOADED CASE declares.

     The list is discovered from the mesh payload: boundary patches, mesh
     regions, cellZones, faceZones and layer groups all arrive as `parts`, each
     with its own OpenFOAM name, type and statistics. Nothing here knows that
     this sample happens to contain "aircraft" and "farfield" - those appear
     because the case has them, and a case with different parts lists those.

     Parts that carry surface geometry are bound to the immutable patch actors
     built above, so highlight / hide / isolate / opacity are property changes
     on real actors. Parts the payload describes but does not ship geometry for
     (regions, cell shape groups, slice planes) are listed and inspectable, and
     say so rather than pretending to be renderable.
/ */
  (function meshParts(){
    const chips=document.getElementById('v-bnd-chips-'+job);
    if(!chips)return;
    const bRest =document.getElementById('v-bnd-restore-'+job),
          cntEl =document.getElementById('v-bnd-count-'+job),
          note  =document.getElementById('v-bnd-note-'+job);

    const actorOf={};entries.forEach(en=>{actorOf[en.patch]=en;});

    // DISCOVERY. The payload DECLARES its parts - identity, label, kind, whether
    // geometry was shipped for it, and how it should open. The viewer renders what
    // is declared and decides nothing about which entities exist. A payload without
    // a parts list still lists the geometry it shipped, so an older mesh degrades
    // to its patches rather than to nothing.
    const declared=Array.isArray(surf.parts)&&surf.parts.length;
    let parts=(declared?surf.parts:(surf.patches||[]).map(p=>({
        id:'boundary:'+p.name,label:p.name,kind:'boundary',selectable:true,
        renderable:true,default_visibility:'visible',
        metadata:{name:p.name,role:p.type||'',faces:p.face_count}})))
      .map(p=>{
        const md=p.metadata||{};
        // the mesh ENTITY this part maps back to - carried by the backend, because
        // a label is for reading and an entity name is for looking up an actor
        const entity=md.name||p.label;
        return {id:p.id,label:p.label,kind:p.kind,meta:md,entity:entity,
                role:md.role||'',
                // declared renderable AND actually present in this payload
                renderable:p.renderable!==false&&!!actorOf[entity],
                hiddenByDefault:p.default_visibility==='hidden'};});

    const KIND={boundary:'Boundary patches',region:'Regions',cellGroup:'Cell types',
                inspection:'Inspection structures'};
    const kindLabel=k=>KIND[k]||String(k).replace(/([A-Z])/g,' $1')
                                         .replace(/^./,c=>c.toUpperCase());

    // A part the backend opens HIDDEN is one that encloses everything else: shown
    // solid it is a wall of colour with the body trapped inside. It opens hidden AND
    // translucent, so one click brings it back as a shell you can see through. The
    // BACKEND decides which parts those are - from the case's own declared roles -
    // and "Restore full mesh" returns to exactly that.
    const dflt=p=>({hidden:!!(p.renderable&&p.hiddenByDefault),
                    opacity:p.hiddenByDefault?0.18:1});
    const st={};parts.forEach(p=>{
      const en=actorOf[p.entity],d=dflt(p);
      st[p.id]={hidden:d.hidden,opacity:d.opacity,entity:p.entity,
                base:en?en.actor.getProperty().getColor().slice():null};});
    let sel=null,isolated=false;

    // Re-frame on what is VISIBLE. Without this, hiding the enclosing outer
    // boundary leaves the body a speck inside a fit computed for the whole box.
    // The zoom clamp runs on every camera change, so it would drag resetCamera()
    // back to the PREVIOUS framing before the new distance is even known. Re-frame
    // with the clamp suspended, publish the distance the visible set needs, then
    // clamp once against the widened envelope.
    function refit(){try{
        _clamping=true;
        ren.resetCamera();
        const f=cam.getFocalPoint(),q=cam.getPosition();
        _refitDist=Math.hypot(q[0]-f[0],q[1]-f[1],q[2]-f[2])||0;
        _clamping=false;
        clampCam();
        rw.render();
      }catch(e){_clamping=false;}}

    function paint(){
      const selPart=sel?parts.find(x=>x.id===sel):null;
      entries.forEach(en=>{
        const p=parts.find(x=>x.renderable&&x.entity===en.patch);
        const s=p?st[p.id]:null;if(!s)return;
        const pr=en.actor.getProperty(),isSel=!!(selPart&&selPart.entity===en.patch);
        en.actor.setVisibility(!s.hidden&&(!isolated||isSel));
        pr.setOpacity(s.opacity);
        if(isSel){pr.setColor(_SEL[0],_SEL[1],_SEL[2]);
                  pr.setEdgeColor(_SEL_EDGE[0],_SEL_EDGE[1],_SEL_EDGE[2]);pr.setAmbient(0.40);}
        else{pr.setColor(s.base[0],s.base[1],s.base[2]);
             pr.setEdgeColor(edgeCol[0],edgeCol[1],edgeCol[2]);pr.setAmbient(0.30);}
      });
      rw.render();
      chips.querySelectorAll('.v-bnd-chip').forEach(c=>{
        const s=st[c.dataset.p];
        c.classList.toggle('sel',c.dataset.p===sel);
        c.classList.toggle('hidden-b',!!(s&&s.hidden));
        const v=c.querySelector('.v-bnd-vis');
        if(v)v.textContent=s&&s.hidden?'hidden':'';
      });
      note.innerHTML=inspect(selPart);
      wire();
      if(onPartsPainted)onPartsPainted();
    }
    repaintParts=paint;

    // INSPECT - one part at a time: its OpenFOAM identity, its own statistics,
    // and the controls that apply to IT. Nothing is on screen for a part the
    // user has not selected, which is why the list stays a list.
    function inspect(p){
      if(!p){
        const off=parts.filter(x=>x.renderable&&st[x.id].hidden).map(x=>x.label);
        return `<div class="ins-empty">Select a part to inspect it.<br>`
          +`<span>${parts.length} selectable entities were read from this case.</span>`
          +(off.length?`<span class="ins-off">hidden · ${off.map(esc).join(' · ')}</span>`:'')
          +`</div>`;
      }
      const rend=p.renderable,s=st[p.id];
      const rows=Object.entries(p.meta||{}).filter(([k])=>k!=='name'&&k!=='role')
        .filter(([,v])=>v!==null&&v!==undefined)
        .map(([k,v])=>`<div class="ins-cell"><div class="ins-k">${esc(k.replace(/_/g,' '))}</div>`
          +`<div class="ins-v">${esc(Array.isArray(v)?v.join(', ')
             :(typeof v==='number'?v.toLocaleString():String(v)))}</div></div>`).join('');
      return `<div class="ins-eyebrow">Selected part</div>
        <div class="ins-top"><span class="ins-nm">${esc(p.label)}</span>
          <span class="ins-badge">${esc(p.role||p.kind)}</span></div>
        <div class="ins-grid">
          <div class="ins-cell"><div class="ins-k">kind</div>
            <div class="ins-v">${esc(kindLabel(p.kind))}</div></div>
          <div class="ins-cell"><div class="ins-k">declared type</div>
            <div class="ins-v">${esc(p.role||'-')}</div></div>
          ${rows}
        </div>
        ${rend?`<div class="ins-acts">
          <button class="v-bnd-act on" data-a="hl">Highlighted</button>
          <button class="v-bnd-act${s.hidden?' on':''}" data-a="hide">${s.hidden?'Show':'Hide'}</button>
          <button class="v-bnd-act${isolated?' on':''}" data-a="iso">Isolate</button>
          <label class="v-bnd-op">opacity
            <input type="range" data-a="op" min="10" max="100" value="${Math.round(s.opacity*100)}">
            <span>${Math.round(s.opacity*100)}%</span></label>
        </div>`
        :`<div class="ins-no">This entity is declared by the case but ships no surface
            geometry in the viewer payload, so it is inspect-only here.</div>`}`;
    }

    // the inspector is re-rendered on every paint, so its controls are re-bound
    function wire(){
      note.querySelectorAll('[data-a]').forEach(el=>{
        const a=el.dataset.a;
        if(a==='op'){el.oninput=()=>{st[sel].opacity=(+el.value)/100;
                       el.nextElementSibling.textContent=el.value+'%';
                       entries.forEach(en=>{if(en.patch!==st[sel].entity)return;
                         const pr=en.actor.getProperty();
                         pr.setOpacity(st[sel].opacity);});
                       rw.render();};return;}
        el.onclick=()=>{
          if(a==='hide'){const s=st[sel];s.hidden=!s.hidden;
                         if(s.hidden&&isolated)isolated=false;paint();refit();}
          if(a==='iso'){isolated=!isolated;
                        if(isolated)st[sel].hidden=false;paint();refit();}
          if(a==='hl'){sel=null;isolated=false;paint();}
        };});
    }

    // grouped by kind, in discovery order
    const seen=[];parts.forEach(p=>{if(!seen.includes(p.kind))seen.push(p.kind);});
    cntEl.textContent=parts.length+' in this case';
    seen.forEach(k=>{
      const g=document.createElement('div');g.className='v-bnd-group';
      g.innerHTML=`<div class="v-bnd-gl">${esc(kindLabel(k))}</div>`;
      parts.filter(p=>p.kind===k).forEach(p=>{
        const c=document.createElement('button');
        c.className='v-bnd-chip'+(p.renderable?'':' meta');c.dataset.p=p.id;
        const stat=p.meta&&(p.meta.faces||p.meta.cells);
        c.innerHTML=`<span class="sw"></span>
          <span class="nm">${esc(p.label)}</span>
          <span class="v-bnd-vis"></span>
          <span class="fc">${stat?Number(stat).toLocaleString():''}</span>`;
        c.onclick=()=>{sel=(sel===p.id)?null:p.id;
                       if(!sel||!p.renderable)isolated=false;paint();};
        g.appendChild(c);});
      chips.appendChild(g);});

    bRest.onclick=()=>{sel=null;isolated=false;
                       parts.forEach(p=>{const d=dflt(p);
                         st[p.id].hidden=d.hidden;st[p.id].opacity=d.opacity;});
                       paint();refit();};
    paint();refit();          // open framed on what the defaults actually show
  })();

  const selector=apiRW.getSelector();
  selector.setCaptureZValues(false);
  selector.setFieldAssociation(1);   // FieldAssociations.FIELD_ASSOCIATION_CELLS

  function cellOffsets(en){          // cell id → offset into the [n, ids...] array
    if(!en.offsets){const off=new Uint32Array(en.nCells);let q=0;
      for(let c=0;c<en.nCells;c++){off[c]=q;q+=en.polys[q]+1;}en.offsets=off;}
    return en.offsets;}
  function cellCenter(en,cid){const o=cellOffsets(en)[cid],n=en.polys[o];
    let x=0,y=0,z=0;for(let j=1;j<=n;j++){const q=en.polys[o+j]*3;
      x+=en.pts[q];y+=en.pts[q+1];z+=en.pts[q+2];}
    return [x/n,y/n,z/n];}
  function spherePd(cx,cy,cz,r){     // UV sphere as plain polydata (no source dep)
    const la=12,lo=16,pts=[],polys=[];
    for(let i=0;i<=la;i++){const th=Math.PI*i/la;
      for(let j=0;j<=lo;j++){const ph=2*Math.PI*j/lo;
        pts.push(cx+r*Math.sin(th)*Math.cos(ph),cy+r*Math.cos(th),
                 cz+r*Math.sin(th)*Math.sin(ph));}}
    const W=lo+1;
    for(let i=0;i<la;i++)for(let j=0;j<lo;j++){
      const a=i*W+j;polys.push(4,a,a+1,a+W+1,a+W);}
    const pd=vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(Float32Array.from(pts),3);
    pd.getPolys().setData(Uint32Array.from(polys));
    return pd;}

  function hex2rgb(h){return [parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)];}

  function fmtR(r){const v=r.toPrecision(2);return `${v} ${units}`;}
  // region size scales with the CURRENT view - a fraction of the visible
  // height - so a mark placed zoomed-in is small, one placed zoomed-out is big
  function worldR(){
    const f=cam.getFocalPoint(),q=cam.getPosition();
    const dist=Math.hypot(q[0]-f[0],q[1]-f[1],q[2]-f[2])||diag;
    const viewH=2*dist*Math.tan(cam.getViewAngle()*Math.PI/360);
    return viewH*(+sizeEl.value)/200;}
  sizeEl.oninput=()=>{sizeLbl.textContent=fmtR(worldR());};

  function setMarkMode(on){markMode=on;selBtn.classList.toggle('armed',on);
    selBtn.textContent=on?'Marking… (click the mesh)':'Mark region';
    host.style.cursor=on?'crosshair':'';
    hintEl.textContent=on?'click a spot on the mesh to mark it · drag still rotates'
                         :'rotate: drag · zoom: wheel or right-drag · pan: shift+drag';}
  selBtn.onclick=()=>setMarkMode(!markMode);

  function pickCell(e,cb){
    const rect=host.getBoundingClientRect(),size=apiRW.getSize();
    const x=Math.round((e.clientX-rect.left)*(size[0]/rect.width));
    const y=Math.round((rect.height-(e.clientY-rect.top))*(size[1]/rect.height));
    selector.getSourceDataAsync(ren,x,y,x,y).then(src=>{
      if(!src)return;
      const sels=src.generateSelection(x,y,x,y);
      if(!sels||!sels.length)return;
      const pr=sels[0].getProperties();
      const en=entries.find(t=>t.actor===pr.prop);if(!en)return;
      const ids=(pr.selectionList&&pr.selectionList.length)?pr.selectionList
               :(pr.attributeID!=null&&pr.attributeID>=0?[pr.attributeID]:[]);
      const cid=Number(ids[0]);
      if(cid>=0&&cid<en.nCells)cb(en,cid);}).catch(()=>{});}

  function newell(en,cid){              // twice the polygon's area vector, in its own vertex order
    const o=cellOffsets(en)[cid],n=en.polys[o],P=en.pts;let nx=0,ny=0,nz=0;
    for(let j=1;j<=n;j++){const a=en.polys[o+j]*3,b=en.polys[o+(j%n)+1]*3;
      nx+=(P[a+1]-P[b+1])*(P[a+2]+P[b+2]);ny+=(P[a+2]-P[b+2])*(P[a]+P[b]);
      nz+=(P[a]-P[b])*(P[a+1]+P[b+1]);}
    return [nx,ny,nz];}
  // WHICH WAY IS OUT of a part. OpenFOAM turns a boundary face out of the fluid - into a car body -
  // and an STL points wherever it was drawn, so each part's sense is read off the part itself: for a
  // closed body the faces' reach from its centre is all one sign. A flat or open part (an inlet
  // disc) reads near zero and keeps its own order.
  function outSign(en){
    if(en.outSign===undefined){const b=en.pd.getBounds(),c=[(b[0]+b[1])/2,(b[2]+b[3])/2,(b[4]+b[5])/2];
      let s=0,a=0;const step=Math.max(1,Math.floor(en.nCells/4000));
      for(let k=0;k<en.nCells;k+=step){const p=cellCenter(en,k),v=newell(en,k);
        const d=(p[0]-c[0])*v[0]+(p[1]-c[1])*v[1]+(p[2]-c[2])*v[2];s+=d;a+=Math.abs(d);}
      en.outSign=a>0&&s/a<-0.3?-1:1;en.outFirm=a>0&&Math.abs(s/a)>=0.3;}
    return en.outSign;}
  function cellNormal(en,cid){          // unit normal of the polygon, pointing out of its part
    const v=newell(en,cid),s=outSign(en)/(Math.hypot(v[0],v[1],v[2])||1);
    return [v[0]*s,v[1]*s,v[2]*s];}
  function ringPd(c,nrm,r){             // thin circle in the surface tangent plane
    let u=Math.abs(nrm[0])>0.9?[0,1,0]:[1,0,0];
    let ux=u[1]*nrm[2]-u[2]*nrm[1],uy=u[2]*nrm[0]-u[0]*nrm[2],uz=u[0]*nrm[1]-u[1]*nrm[0];
    const ul=Math.hypot(ux,uy,uz)||1;ux/=ul;uy/=ul;uz/=ul;
    const vx=nrm[1]*uz-nrm[2]*uy,vy=nrm[2]*ux-nrm[0]*uz,vz=nrm[0]*uy-nrm[1]*ux;
    const lift=r*0.08;
    const cx=c[0]+nrm[0]*lift,cy=c[1]+nrm[1]*lift,cz=c[2]+nrm[2]*lift;
    const N=48,pts=new Float32Array(N*3),lines=new Uint32Array(N+2);
    lines[0]=N+1;
    for(let i=0;i<N;i++){const t=2*Math.PI*i/N,co=Math.cos(t)*r,si=Math.sin(t)*r;
      pts[i*3]=cx+ux*co+vx*si;pts[i*3+1]=cy+uy*co+vy*si;pts[i*3+2]=cz+uz*co+vz*si;
      lines[i+1]=i;}
    lines[N+1]=0;
    const pd=vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(pts,3);
    pd.getLines().setData(lines);
    return pd;}
  function editNote(f){f._editing=true;renderFlags();}

  function placeMarker(en,cid){
    if(flags.length>=MAX_FLAGS){
      hintEl.textContent=`marker limit reached (${MAX_FLAGS}) - remove one to add another`;
      return;}
    const rr=worldR();
    const col=_SEL_COLS[_colorSeq++%_SEL_COLS.length];   // fresh colour per mark
    const c=cellCenter(en,cid),rgb=hex2rgb(col),nrm=cellNormal(en,cid);
    const _nrm=nrm;
    const actors=[];
    [[ringPd(c,nrm,rr),1.0],[spherePd(c[0],c[1],c[2],rr*0.09),1.0]].forEach(([pd,op])=>{
      const mp=vtk.Rendering.Core.vtkMapper.newInstance();mp.setInputData(pd);
      const ac=vtk.Rendering.Core.vtkActor.newInstance();
      ac.setMapper(mp);ac.setPickable(false);
      const pp=ac.getProperty();
      pp.setColor(rgb[0]/255,rgb[1]/255,rgb[2]/255);
      pp.setOpacity(op);pp.setAmbient(1.0);pp.setDiffuse(0.0);pp.setLighting(false);
      ren.addActor(ac);actors.push(ac);});
    const el=document.createElement('div');el.className='v-pin';
    el.style.background=col;
    host.appendChild(el);
    const f={x:c[0],y:c[1],z:c[2],nrm:_nrm,span:rr*SPAN_F,patch:en.patch,
             note:'',col:col,actors:actors,el:el};
    el.onclick=ev=>{ev.stopPropagation();editNote(f);};
    flags.push(f);
    rw.render();renderFlags();}

  /* pin badges track their 3-D anchors every frame (stops when the viewer
     panel is removed from the document) */
  (function badgeLoop(){
    if(!document.body.contains(host))return;
    const rr=worldR();
    if(!_lastR||Math.abs(rr-_lastR)>_lastR*0.02){_lastR=rr;sizeLbl.textContent=fmtR(rr);}
    const size=apiRW.getSize(),aspect=size[0]/size[1];
    const rect=host.getBoundingClientRect();
    flags.forEach(f=>{if(!f.el)return;
      const nd=ren.worldToNormalizedDisplay(f.x,f.y,f.z,aspect);
      const ok=nd[2]>0&&nd[2]<1&&nd[0]>=0&&nd[0]<=1&&nd[1]>=0&&nd[1]<=1;
      f.el.style.display=ok?'':'none';
      if(ok){f.el.style.left=(nd[0]*rect.width)+'px';
             f.el.style.top=((1-nd[1])*rect.height)+'px';
             // DEPTH CUE: the pin is an HTML overlay, so it cannot be occluded by the
             // mesh. A mark whose surface normal points away from the camera is on the
             // FAR side - dim it, so a back-face pin never reads as a near one.
             const cam=ren.getActiveCamera(),cp=cam.getPosition();
             const vx=f.x-cp[0],vy=f.y-cp[1],vz=f.z-cp[2];
             const facing=(f.nrm?(f.nrm[0]*vx+f.nrm[1]*vy+f.nrm[2]*vz):-1)<0;
             f.el.classList.toggle('back',!facing);}});
    requestAnimationFrame(badgeLoop);})();

  // a click (not a drag) while armed places a marker - the camera still
  // orbits. Capture phase: the vtk interactor stops propagation of pointer
  // events, so bubble listeners on the container never fire.
  host.addEventListener('pointerdown',e=>{downXY=[e.clientX,e.clientY];},true);
  host.addEventListener('pointerup',e=>{
    if(!markMode||!downXY)return;
    if(e.target&&e.target.classList&&e.target.classList.contains('v-pin'))return;
    if(Math.hypot(e.clientX-downXY[0],e.clientY-downXY[1])>6)return;
    pickCell(e,placeMarker);},true);

  function renderFlags(){flagsEl.style.display=flags.length?'':'none';
    clearBtn.disabled=!flags.length;
    // ONE primary action whose meaning follows the state: with marks it sends them,
    // without marks it is a plain free-text change request. (Two competing buttons
    // made the with-marks / without-marks paths impossible to tell apart.)
    submitBtn.textContent=flags.length
      ?`Send ${flags.length} mark${flags.length>1?'s':''} for re-review`
      :'Request a change…';
    flagsEl.innerHTML=flags.length
      ?'<b>Marked regions</b><div class="v-fhint">The reviewer will inspect each marked spot on the delivered mesh. Add a note to say what looks wrong there.</div>':'';
    flags.forEach((f,i)=>{
      if(f.el){f.el.textContent=String(i+1);
        f.el.title=f.note||'click to add a note';}
      const d=document.createElement('div');d.className='v-flag';
      d.innerHTML=`<span class="v-chip" style="background:${f.col}">${i+1}</span>`
        +`<div class="f-body"><div class="f-title">Region ${i+1} · ${esc(f.patch)}</div>`
        +`<div class="f-meta">radius ≈ ${esc(fmtR(f.span/SPAN_F))}</div>`
        +(f._editing
            ?`<textarea class="f-edit" rows="2" maxlength="480"
                 placeholder="What looks wrong here?">${esc(f.note||'')}</textarea>`
            :(f.note?`<div class="f-note">${esc(f.note)}</div>`
                    :'<div class="f-note empty">Add a note describing the issue…</div>'))
        +'</div>'
        +'<span class="fx" title="edit note" style="color:#8fa3bd">✎</span>'
        +'<span class="fx" title="remove">✕</span>';
      const emptyNote=d.querySelector('.f-note.empty');
      if(emptyNote)emptyNote.onclick=()=>editNote(f);
      const [editEl,delEl]=d.querySelectorAll('.fx');
      editEl.onclick=()=>editNote(f);
      d.querySelector('.f-note')?.addEventListener('click',()=>editNote(f));
      const ta=d.querySelector('.f-edit');
      if(ta){
        const save=()=>{f.note=ta.value.trim().slice(0,480);f._editing=false;renderFlags();};
        ta.addEventListener('blur',save);
        ta.addEventListener('keydown',ev=>{
          if(ev.key==='Enter'&&!ev.shiftKey){ev.preventDefault();save();}
          if(ev.key==='Escape'){f._editing=false;renderFlags();}});
        setTimeout(()=>{ta.focus();ta.selectionStart=ta.value.length;},0);}
      delEl.onclick=()=>{f.actors.forEach(a=>ren.removeActor(a));
        if(f.el)f.el.remove();
        flags.splice(i,1);rw.render();renderFlags();};
      flagsEl.appendChild(d);});}

  clearBtn.onclick=()=>{flags.forEach(f=>{f.actors.forEach(a=>ren.removeActor(a));
      if(f.el)f.el.remove();});
    flags.length=0;rw.render();renderFlags();};

  // ONE handler for the one action: with marks it sends them, with none it is the
  // plain conversation path (flags.map() is simply [] then).
  submitBtn.onclick=()=>flagDispute(job,
    flags.map(f=>({x:f.x,y:f.y,z:f.z,span:f.span,patch:f.patch,note:f.note})));

  // engage the camera envelope (fit distance must be captured before caps apply)
  const _fp0=cam.getFocalPoint(),_cp0=cam.getPosition();
  const _fitDist=Math.hypot(_cp0[0]-_fp0[0],_cp0[1]-_fp0[1],_cp0[2]-_fp0[2])||1;
  setRenderScale();
  cam.onModified(clampCam);
  clampCam();                    // opening view = closest allowed to Fit
  rw.render();

  /* THE DELIVERED MESH'S OWN FIGURES, straight from the payload. The UI holds an
     ORDER and a label for the ones it knows how to name, and shows anything else
     the backend declared under its own key - a metric this build has never heard
     of still reaches the user rather than being silently dropped. Nothing is
     defaulted: a figure the mesh does not carry simply has no cell. */
  (function meshFacts(){
    const host=document.getElementById('v-facts-'+job),q=Object.assign({},surf.quality||{});
    // aspect ratio is measured by the heatmap pass, not by checkMesh's summary block; it gets a
    // figure here so it is a control like the other two
    const _ar=surf.quality_fields&&surf.quality_fields.metrics&&surf.quality_fields.metrics.aspect_ratio;
    if(_ar&&!('max_aspect_ratio' in q)&&typeof _ar.max==='number')q.max_aspect_ratio=Number(_ar.max.toFixed(2));
    if(!host||!Object.keys(q).length)return;
    const LBL={cells:'cells',faces:'faces',hexahedra:'hexahedra',polyhedra:'polyhedra',
               prisms:'prisms',pyramids:'pyramids',tetrahedra:'tetrahedra',
               regions:'mesh regions',max_non_ortho:'max non-orthogonality',
               max_skewness:'max skewness',max_aspect_ratio:'max aspect ratio',skew_faces:'skewed faces',
               avg_non_ortho:'mean non-orthogonality'};
    const ORDER=['cells','faces','hexahedra','polyhedra','prisms','pyramids','tetrahedra',
                 'regions','max_non_ortho','max_skewness','max_aspect_ratio','skew_faces','avg_non_ortho'];
    const keys=ORDER.filter(k=>k in q)
      .concat(Object.keys(q).filter(k=>!ORDER.includes(k)&&k!=='units'&&k!=='engine'));
    if(!keys.length)return;
    const cell=k=>{
      const v=q[k];
      // integers read as counts; a measured value reads to a few figures, and a
      // very small one reads in exponent form rather than a run of zeroes
      const shown=typeof v!=='number' ? String(v)
        : Number.isInteger(v) ? v.toLocaleString()
        : (Math.abs(v)>0&&Math.abs(v)<0.001) ? v.toExponential(2)
        : Number(v.toPrecision(6)).toLocaleString(undefined,{maximumFractionDigits:6});
      const suffix=k==='max_non_ortho'||k==='avg_non_ortho'?'<small>°</small>':'';
      const of=k==='skew_faces'&&typeof q.faces==='number'
        ? `<small> of ${q.faces.toLocaleString()}</small>`:'';
      return `<div class="mx-c" data-key="${esc(k)}"><div class="mx-k">${esc(LBL[k]||k.replace(/_/g,' '))}</div>`
            +`<div class="mx-v">${shown}${suffix}${of}</div></div>`;};
    host.innerHTML=`<div class="mx"><div class="mx-h"><span>Delivered mesh</span>`
      +(q.engine?`<span class="eng">${esc(q.engine)}</span>`:'')
      +(q.units?`<span class="eng">${esc(q.units)}</span>`:'')
      +`</div><div class="mx-grid">${keys.map(cell).join('')}</div></div>`;
  })();

  document.getElementById('v-status-'+job).textContent=
    (surf.is_mesh?'your delivered mesh · '
                :'input surface - the mesh itself could not be rendered · ')
    +(surf.patches||[]).map(p=>`${p.name}: ${(p.face_count||p.tri_count||0).toLocaleString()} faces`).join(' · ');

  /* support/debug hook (read-only-ish; used by headless verification) */
  window._vdbg=window._vdbg||{};
  window._vdbg[job]={
    caps:()=>capRange(),
    dist:()=>{const f=cam.getFocalPoint(),q=cam.getPosition();
      return Math.hypot(q[0]-f[0],q[1]-f[1],q[2]-f[2]);},
    gridPx:()=>{const f=cam.getFocalPoint(),q=cam.getPosition();
      const d=Math.hypot(q[0]-f[0],q[1]-f[1],q[2]-f[2])||1e-12;
      return stats?stats.typ*(apiRW.getSize()[1]/(2*Math.tan(cam.getViewAngle()*Math.PI/360)))/d:null;},
    fitDist:()=>_fitDist,stats:stats,diag:diag,size:()=>apiRW.getSize(),
    markers:()=>flags.length,up:()=>cam.getViewUp().slice(),
    render:()=>rw.render()};

  /* PARAVIEW EXPORT - the same boundary and the same per-face numbers as a legacy VTK file,
     built by the API from the stored payload. Fetched with the API headers (a plain link would
     carry none) and handed to the browser as a download; the button says what it is doing. */
  (function vtkExport(){
    if(surf.kind!=='polymesh')return;
    const bar=document.querySelector('#viewer-'+job+' .v-dl');if(!bar)return;
    const a=document.createElement('a');a.className='v-dlbtn';a.href='#';a.id='v-vtk-'+job;
    a.title='the boundary with its quality fields, for ParaView';
    a.innerHTML='<b>ParaView</b><span>.vtk with quality fields</span>';
    a.onclick=async e=>{e.preventDefault();if(a.dataset.busy)return;a.dataset.busy='1';
      const lbl=a.querySelector('span'),was=lbl.textContent;lbl.textContent='building…';
      try{const r=await fetch(`/api/v1/simulation/${job}/surface.vtk`,{headers:headers()});
        if(!r.ok)throw new Error('export '+r.status);
        const blob=await r.blob(),url=URL.createObjectURL(blob);
        const dl=document.createElement('a');dl.href=url;dl.download=`mesh_quality_${String(job).slice(0,8)}.vtk`;
        document.body.appendChild(dl);dl.click();dl.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);
        lbl.textContent=fmtBytes(blob.size);}
      catch(err){lbl.textContent='export failed';console.warn('vtk export',err);}
      finally{delete a.dataset.busy;setTimeout(()=>{lbl.textContent=was;},4000);}};
    bar.appendChild(a);
    window._vdbg[job].vtk=()=>a;
  })();

  /* QUALITY HEATMAP - the boundary coloured by the quality of the cell behind each face.
     The payload carries one float per drawn polygon (render/face_quality.py, aligned with the
     surface reader's own filter), the bar the engine's gate judges against, and the worst
     spots. The delivered-mesh FIGURES are the controls: the non-orthogonality, skewness and
     aspect-ratio numbers become clickable when fields exist for them - clicking a number shows where it
     lives. Nothing here touches the geometry: colouring is cell scalars on the same immutable
     actors, and turning it off restores exactly the property colours the parts panel set. */
  let activeMetric=null,lastProbe=null;
  (function heatmap(){
    const qf=surf.quality_fields;
    if(!qf||!qf.patches||!qf.metrics||!window.vtk)return;
    const META={non_ortho:{fact:'max_non_ortho',b64:'non_ortho_b64'},
                skewness:{fact:'max_skewness',b64:'skewness_b64'},
                aspect_ratio:{fact:'max_aspect_ratio',b64:'aspect_ratio_b64'}};
    const avail=Object.keys(META).filter(m=>qf.metrics[m]);
    // decode once. An array that does not match the polygon count is dropped, not trusted:
    // a misaligned field would paint every face with its neighbour's number.
    entries.forEach(en=>{const p=qf.patches[en.patch];en.q={};if(!p)return;
      avail.forEach(m=>{if(!p[META[m].b64])return;const a=b64f32(p[META[m].b64]);
        if(a.length===en.nCells)en.q[m]=a;});
      if(p.cell_faces_b64){const cf=b64u8(p.cell_faces_b64);if(cf.length===en.nCells)en.cellFaces=cf;}});
    if(!entries.some(en=>Object.keys(en.q).length))return;

    /* one ramp per metric: calm below the bar, warm approaching it, red past it. The colours
       are computed here and handed to the mapper as direct per-face RGB, and the legend is
       built from the SAME stops - so the bar on screen is the bar in the colours, and nothing
       depends on which lookup-table classes the vendored bundle happens to export. */
    /* READABLE ON A GOOD MESH. Almost every face of a mesh that passes sits near 0, so the calm end
       is a light, lit grey-blue - the part reads as a clean surface, brighter than the plain view -
       and the colour warms through amber (80% of the bar) and international orange (the bar) to
       crimson past it. The old deep-steel calm end turned a good mesh into a dark silhouette on the
       dark canvas, and a click on its figure looked like it did nothing. */
    const STOPS=[[0,[206,217,230]],[0.5,[158,184,214]],[0.8,[255,176,64]],[1.0,[255,79,0]]];
    const PAST=[178,18,42];
    const NEAR=0.8;                  // "near the limit": the amber stop, and where the grey-out cuts
    const NEUTRAL=[66,70,76];        // flat dark grey for the faces "near the limit only" rests
    function ramp(m){const md=qf.metrics[m],lim=md.limit||1,lo=md.floor||0;
      // a metric whose bar sits far above everything the mesh has (aspect ratio: checkMesh's
      // 1000 against layers at 10 to 50) spans its colours over the mesh's own range instead,
      // and the bar is drawn as a tick only when it falls inside that range
      const open=md.scale_to>0&&lim>md.scale_to;
      const hi=open?md.scale_to:Math.max(lim*1.3,md.max||0);
      const span=open?(hi-lo):(lim-lo);
      const pts=STOPS.map(([f,c])=>[lo+f*span,c]).concat(open?[]:[[hi,PAST]]);
      const color=(v,out,o)=>{
        if(!(v>lo)){out[o]=pts[0][1][0];out[o+1]=pts[0][1][1];out[o+2]=pts[0][1][2];return;}
        let i=1;while(i<pts.length-1&&v>pts[i][0])i++;
        const [a,ca]=pts[i-1],[b,cb]=pts[i],t=Math.min(1,Math.max(0,(v-a)/((b-a)||1)));
        out[o]=ca[0]+(cb[0]-ca[0])*t;out[o+1]=ca[1]+(cb[1]-ca[1])*t;out[o+2]=ca[2]+(cb[2]-ca[2])*t;};
      const pc=v=>(100*(v-lo)/((hi-lo)||1)).toFixed(1)+'%';
      const css='linear-gradient(0deg,'+pts.map(([v,c])=>`rgb(${c}) ${pc(v)}`).join(',')+')';
      return {color,hi,lo,open,css,limitPct:pc(lim)};}
    function fmt(m,v){const u=qf.metrics[m].unit||'';return (u==='°'?v.toFixed(1):v.toFixed(2))+u;}

    /* WHAT IS ON SCREEN. Sizes come from the drawn faces of the parts that are showing, not from
       the whole case: a hidden farfield box is ten times the part and carries the mesh's coarsest
       cells, so the case-wide typical cell made a marker on a car the size of its bonnet. */
    const shown=()=>entries.filter(en=>en.actor.getVisibility());
    const shownSig=()=>shown().map(en=>en.patch).join('|');
    const faceSize=(en,cid)=>{const v=newell(en,cid);return Math.sqrt(0.5*Math.hypot(v[0],v[1],v[2]));};
    let _typ=null;
    function faceTyp(){              // the median drawn face of the parts on screen, sampled
      const sig=shownSig();if(_typ&&_typ.sig===sig)return _typ.v;
      const vis=shown(),tot=vis.reduce((a,en)=>a+en.nCells,0),step=Math.max(1,Math.floor(tot/3000)),s=[];
      vis.forEach(en=>{for(let c=0;c<en.nCells;c+=step)s.push(faceSize(en,c));});
      s.sort((a,b)=>a-b);
      const v=(s.length&&s[s.length>>1])||(stats&&stats.typ)||diag*0.005;
      _typ={sig,v};return v;}
    function shownDiag(){const vis=shown();if(!vis.length)return diag;
      const b=[Infinity,-Infinity,Infinity,-Infinity,Infinity,-Infinity];
      vis.forEach(en=>{const e=en.pd.getBounds();
        for(let k=0;k<6;k+=2){b[k]=Math.min(b[k],e[k]);b[k+1]=Math.max(b[k+1],e[k+1]);}});
      return Math.max(Math.hypot(b[1]-b[0],b[3]-b[2],b[5]-b[4]),1e-9);}
    const tanHalf=()=>Math.tan(cam.getViewAngle()*Math.PI/360);
    function camDist(){const f=cam.getFocalPoint(),q=cam.getPosition();
      return Math.hypot(q[0]-f[0],q[1]-f[1],q[2]-f[2])||1e-12;}

    /* THE EDGES STEP BACK. At full view a dense mesh has more hairline than face on screen, and the
       lines paint the part their own colour; with the data on they are drawn only once a typical
       face spans EDGE_PX on screen, and then in a soft grey the colours read through. Off, they
       come back exactly as the parts panel draws them. */
    const EDGE_PX=9;
    let edgesOn=true;
    function setEdges(on){if(on===edgesOn)return;edgesOn=on;
      entries.forEach(en=>en.actor.getProperty().setEdgeVisibility(on));}
    function heatEdges(){if(activeMetric)
      setEdges(faceTyp()*(host.clientHeight||1)/(2*camDist()*tanHalf())>=EDGE_PX);}

    /* THE WORST SPOTS, on every mesh. The payload's hotspots are the faces OVER the bar, worst first
       - a mesh that passes has none. So the list starts with them and goes on with the drawn faces
       themselves: the highest-reading faces of the parts on screen, one per neighbourhood, worst
       first, up to 60. A good mesh therefore still shows where its worst cells are. */
    const MAX_SPOTS=60;
    let spots=[],spotSig='';
    function buildSpots(m){
      const vis=shown(),out=[],lim=qf.metrics[m].limit;
      const sep=Math.max(4*faceTyp(),0.02*shownDiag()),sep2=sep*sep;
      const taken=(x,y,z)=>out.some(s=>(s.x-x)**2+(s.y-y)**2+(s.z-z)**2<sep2);
      (qf.hotspots||[]).forEach(h=>{
        if(h.metric!==m||out.length>=MAX_SPOTS)return;
        if(h.patch&&!vis.some(en=>en.patch===h.patch))return;   // a hidden part's would mark empty space
        if(taken(h.x,h.y,h.z))return;
        out.push({x:h.x,y:h.y,z:h.z,value:h.value,patch:h.patch||null,cellFaces:h.cell_faces,
                  over:h.value>lim,en:null,cid:-1});});
      // the drawn faces: a histogram finds the reading the top ~2000 sit above, and only those sort
      let lo=Infinity,hi=-Infinity;
      vis.forEach(en=>{const v=en.q[m];if(v)for(let i=0;i<v.length;i++){const x=v[i];
        if(x<lo)lo=x;if(x>hi)hi=x;}});
      if(out.length<MAX_SPOTS&&hi>=lo){
        const B=1024,hist=new Uint32Array(B),w=(hi-lo)||1;
        const bin=x=>Math.min(B-1,Math.max(0,Math.floor((x-lo)/w*B)));
        vis.forEach(en=>{const v=en.q[m];if(v)for(let i=0;i<v.length;i++)if(v[i]===v[i])hist[bin(v[i])]++;});
        let k=B-1,above=0;while(k>0&&above+hist[k]<2000){above+=hist[k];k--;}
        let room=Math.max(0,4000-above);const cand=[];
        vis.forEach(en=>{const v=en.q[m];if(v)for(let i=0;i<v.length;i++){const x=v[i];if(x!==x)continue;
          const b=bin(x);if(b>k||(b===k&&room-->0))cand.push([x,en,i]);}});
        cand.sort((a,b)=>b[0]-a[0]);
        // a drawn face past the bar reads its cell's worst face, which the payload already lists
        // as a hotspot: it would only be the same bad cell again
        const listed=(qf.hotspots||[]).some(h=>h.metric===m);
        for(const [v,en,cid] of cand){if(out.length>=MAX_SPOTS)break;
          if(listed&&v>lim)continue;
          const p=cellCenter(en,cid);if(taken(p[0],p[1],p[2]))continue;
          out.push({x:p[0],y:p[1],z:p[2],value:v,patch:en.patch,
                    cellFaces:en.cellFaces?en.cellFaces[cid]:null,over:v>lim,en,cid});}}
      return out;}
    // the drawn face nearest a point, among the parts on screen (a payload hotspot carries a place,
    // not a polygon index)
    function centres(en){if(!en.ctr){const C=new Float32Array(en.nCells*3);
        for(let c=0;c<en.nCells;c++){const p=cellCenter(en,c);C[c*3]=p[0];C[c*3+1]=p[1];C[c*3+2]=p[2];}
        en.ctr=C;}
      return en.ctr;}
    function nearestFace(s){let best=null,bd=Infinity;
      shown().forEach(en=>{const C=centres(en);
        for(let c=0;c<en.nCells;c++){const dx=C[c*3]-s.x,dy=C[c*3+1]-s.y,dz=C[c*3+2]-s.z,d=dx*dx+dy*dy+dz*dz;
          if(d<bd){bd=d;best={en,cid:c};}}});
      return best?Object.assign(best,{dist:Math.sqrt(bd)}):null;}

    /* THE MARKERS read at full view: the larger of 1.6 typical faces and 0.6% of what is on screen,
       and never more than BEAD_PX across on screen, so zooming in does not grow them over the very
       cells they point at. A spot past the bar is crimson; one near it (above 80%) wears the
       scale's own colour for its reading, so the beads say where the worst cells are on a mesh that
       passes as well. The spot the walker is on gives its bead up to the ring. */
    const BEAD_PX=7;
    const hotActors=[],hotOf=[];let hotR=0,unitBead=null;   // hotOf[k]: the spot bead k marks
    function clearHot(){hotActors.forEach(a=>ren.removeActor(a));hotActors.length=0;hotOf.length=0;}
    function scaleHot(){if(!hotActors.length)return;
      const perPx=2*camDist()*tanHalf()/(host.clientHeight||600),r=Math.min(hotR,BEAD_PX*perPx);
      hotActors.forEach(a=>{const s=a.getScale();if(Math.abs(s[0]-r)>r*0.01)a.setScale(r,r,r);});
      // the spot the walker is on gives its bead up to the ring - while the ring is big enough to see
      const cur=focusActors.length&&focusR/perPx>=2*BEAD_PX?walkIdx:-1;
      hotActors.forEach((a,k)=>{const on=hotOf[k]!==cur;if(a.getVisibility()!==on)a.setVisibility(on);});}
    function showHot(m){clearHot();
      const md=qf.metrics[m],rp=ramp(m),rgb=[0,0,0];
      hotR=Math.max(1.6*faceTyp(),0.006*shownDiag());
      if(!unitBead){unitBead=vtk.Rendering.Core.vtkMapper.newInstance();unitBead.setInputData(spherePd(0,0,0,1));}
      spots.forEach((s,i)=>{
        if(!(s.over||(!rp.open&&s.value>=NEAR*md.limit)))return;
        if(s.over){rgb[0]=224;rgb[1]=36;rgb[2]=40;}else rp.color(s.value,rgb,0);
        const ac=vtk.Rendering.Core.vtkActor.newInstance();ac.setMapper(unitBead);ac.setPickable(false);
        ac.setPosition(s.x,s.y,s.z);
        const pp=ac.getProperty();pp.setColor(rgb[0]/255,rgb[1]/255,rgb[2]/255);
        pp.setAmbient(0.55);pp.setDiffuse(0.6);pp.setSpecular(0.3);pp.setSpecularPower(24);
        ren.addActor(ac);hotActors.push(ac);hotOf.push(i);});
      scaleHot();}

    /* THE CURRENT SPOT: a quiet target ring lying on the surface - white between two dark bands, so
       it reads on the pale calm colour and on the warm one alike. */
    let focusActors=[],focusR=0;
    function clearFocus(){focusActors.forEach(a=>ren.removeActor(a));focusActors=[];scaleHot();}
    function annulusPd(c,nrm,r0,r1){
      let ux=Math.abs(nrm[0])>0.9?0:1,uy=Math.abs(nrm[0])>0.9?1:0,uz=0;
      let ax=uy*nrm[2]-uz*nrm[1],ay=uz*nrm[0]-ux*nrm[2],az=ux*nrm[1]-uy*nrm[0];
      const al=Math.hypot(ax,ay,az)||1;ax/=al;ay/=al;az/=al;
      const bx=nrm[1]*az-nrm[2]*ay,by=nrm[2]*ax-nrm[0]*az,bz=nrm[0]*ay-nrm[1]*ax;
      const lift=r1*0.06,cx=c[0]+nrm[0]*lift,cy=c[1]+nrm[1]*lift,cz=c[2]+nrm[2]*lift;
      const N=72,pts=new Float32Array(N*6),polys=new Uint32Array(N*5);
      for(let i=0;i<N;i++){const t=2*Math.PI*i/N,co=Math.cos(t),si=Math.sin(t);
        [r0,r1].forEach((r,k)=>{const o=(i*2+k)*3;
          pts[o]=cx+(ax*co+bx*si)*r;pts[o+1]=cy+(ay*co+by*si)*r;pts[o+2]=cz+(az*co+bz*si)*r;});
        const j=(i+1)%N;polys.set([4,i*2,i*2+1,j*2+1,j*2],i*5);}
      const pd=vtk.Common.DataModel.vtkPolyData.newInstance();
      pd.getPoints().setData(pts,3);pd.getPolys().setData(polys);
      return pd;}
    function showFocus(c,nrm,r){clearFocus();focusR=r;
      [[0.70,0.82,[0.03,0.04,0.06]],[0.82,1.0,[1,1,1]],[1.0,1.13,[0.03,0.04,0.06]]].forEach(([a,b,col])=>{
        const mp=vtk.Rendering.Core.vtkMapper.newInstance();mp.setInputData(annulusPd(c,nrm,a*r,b*r));
        const ac=vtk.Rendering.Core.vtkActor.newInstance();ac.setMapper(mp);ac.setPickable(false);
        const pp=ac.getProperty();pp.setColor(col[0],col[1],col[2]);
        pp.setAmbient(1.0);pp.setDiffuse(0.0);pp.setLighting(false);
        if(pp.setBackfaceCulling)pp.setBackfaceCulling(false);
        ren.addActor(ac);focusActors.push(ac);});
      scaleHot();}

    /* THE FLIGHT. The camera glides to a spot over FLY_MS - focal point straight, distance
       geometric so a long zoom feels even, direction turned about one axis - rather than jumping,
       so the viewer keeps their bearings. Any drag, wheel or Fit takes the camera back at once. */
    const FLY_MS=800;
    let fly=null;
    function cancelFly(){if(fly){const f=fly;fly=null;f.resolve(false);}}
    const ease=t=>t<0.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2;
    function flyTo(tf,dir,dist){
      cancelFly();
      const f0=cam.getFocalPoint().slice(),p0=cam.getPosition().slice();
      const v0=[p0[0]-f0[0],p0[1]-f0[1],p0[2]-f0[2]],d0=Math.hypot(v0[0],v0[1],v0[2])||1e-9;
      for(let k=0;k<3;k++)v0[k]/=d0;
      // the turn: about the axis square to both directions (about the view-up for a half turn)
      const ang=Math.acos(Math.max(-1,Math.min(1,v0[0]*dir[0]+v0[1]*dir[1]+v0[2]*dir[2])));
      let ax=[v0[1]*dir[2]-v0[2]*dir[1],v0[2]*dir[0]-v0[0]*dir[2],v0[0]*dir[1]-v0[1]*dir[0]];
      if(Math.hypot(ax[0],ax[1],ax[2])<1e-6){const u=cam.getViewUp(),d=u[0]*v0[0]+u[1]*v0[1]+u[2]*v0[2];
        ax=[u[0]-d*v0[0],u[1]-d*v0[1],u[2]-d*v0[2]];}
      const al=Math.hypot(ax[0],ax[1],ax[2])||1;ax=ax.map(x=>x/al);
      const kx=[ax[1]*v0[2]-ax[2]*v0[1],ax[2]*v0[0]-ax[0]*v0[2],ax[0]*v0[1]-ax[1]*v0[0]];
      const calm=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const dur=calm?0:FLY_MS,t0=performance.now();
      const me={};me.done=new Promise(r=>{me.resolve=r;});fly=me;
      const place=e=>{
        const co=Math.cos(ang*e),si=Math.sin(ang*e),d=d0*Math.pow(dist/d0,e);
        const v=[0,1,2].map(k=>v0[k]*co+kx[k]*si),f=[0,1,2].map(k=>f0[k]+(tf[k]-f0[k])*e);
        _clamping=true;
        try{cam.setFocalPoint(f[0],f[1],f[2]);cam.setPosition(f[0]+v[0]*d,f[1]+v[1]*d,f[2]+v[2]*d);
            cam.orthogonalizeViewUp();}
        finally{_clamping=false;}
        clampCam();ren.resetCameraClippingRange();rw.render();};
      const land=()=>{if(fly!==me)return;place(1);fly=null;me.resolve(true);};
      const step=()=>{if(fly!==me)return;const t=dur?(performance.now()-t0)/dur:1;
        if(t>=1){land();return;}place(ease(t));requestAnimationFrame(step);};
      setTimeout(land,dur+300);      // a backgrounded tab gets no animation frames: land anyway
      step();
      return me.done;}
    host.addEventListener('pointerdown',e=>{
      if(!(e.target&&e.target.closest&&e.target.closest('.v-legend,.v-probe')))cancelFly();},true);
    host.addEventListener('wheel',()=>cancelFly(),{capture:true,passive:true});
    document.getElementById('v-fitbtn-'+job).addEventListener('click',()=>cancelFly());

    /* THE WALKER - "Worst spot" steps through the list, worst first, on every mesh: flies to the
       spot keeping the view direction (a spot on the far side is seen from its own side, a grazing
       one a little more square-on), stops where the cells around it read as cells, lights it, and
       opens the same readout a click on that face opens. */
    const WORK_PX=26;                // CSS px the spot's face spans on arrival
    let walkIdx=-1,lastFocus=null;
    function workDist(size){const r=capRange();
      const d=size*(host.clientHeight||600)/(2*tanHalf()*WORK_PX);
      return Math.max(r[0],Math.min(r[1],d,_fitDist*0.5));}
    function refreshSpots(){const m=activeMetric;if(!m)return;
      const sig=m+'#'+shownSig();if(sig===spotSig)return;
      spots=buildSpots(m);spotSig=sig;walkIdx=-1;showHot(m);walkLabel();}
    function walkLabel(){const n=legend&&legend.querySelector('.v-walk-n');
      if(n)n.textContent=`${walkIdx+1} / ${spots.length}`;
      const b=legend&&legend.querySelector('.v-walk');if(b)b.disabled=!spots.length;}
    function focusSpot(i){
      if(!activeMetric)return null;
      refreshSpots();if(!spots.length)return null;
      walkIdx=((i%spots.length)+spots.length)%spots.length;
      const s=spots[walkIdx],typ=faceTyp();
      // the spot's own face: known for a drawn-face spot; for a payload hotspot, the drawn face at
      // its centre on its own part. An inner face has none - the nearest one only orients the view.
      let face=s.en?{en:s.en,cid:s.cid}:null,near=null;
      if(!face){near=nearestFace(s);
        if(near&&s.patch&&near.en.patch===s.patch&&near.dist<=0.5*faceSize(near.en,near.cid))face=near;}
      const at=face||(near&&near.dist<=4*typ?near:null);
      const nrm=at?cellNormal(at.en,at.cid):null;
      const size=Math.min(2*typ,Math.max(0.5*typ,at?faceSize(at.en,at.cid):typ));
      const f=cam.getFocalPoint(),p=cam.getPosition();
      let v=[p[0]-f[0],p[1]-f[1],p[2]-f[2]];const L=Math.hypot(v[0],v[1],v[2])||1;v=v.map(x=>x/L);
      if(nrm){let dp=v[0]*nrm[0]+v[1]*nrm[1]+v[2]*nrm[2];
        // a part with no firm outside (a flat inlet disc) is seen from whichever side faces us
        if(dp<0&&!at.en.outFirm){nrm[0]=-nrm[0];nrm[1]=-nrm[1];nrm[2]=-nrm[2];dp=-dp;}
        if(dp<0){v=v.map((x,k)=>x-2*dp*nrm[k]);dp=-dp;}
        if(dp<0.55){v=v.map((x,k)=>x+(0.55-dp)*nrm[k]);const l=Math.hypot(v[0],v[1],v[2])||1;v=v.map(x=>x/l);}}
      const d=workDist(size);
      flyTo([s.x,s.y,s.z],v,d);
      showFocus([s.x,s.y,s.z],nrm||v,0.045*2*d*tanHalf());
      const head=`worst spot ${walkIdx+1} / ${spots.length}`;
      if(face)showProbe(face.en,face.cid,head);else showSpotCard(s,at,head);
      walkLabel();
      lastFocus={index:walkIdx,n:spots.length,x:s.x,y:s.y,z:s.z,patch:s.patch,value:s.value,over:s.over,
                 face:face?{patch:face.en.patch,cell:face.cid}:null};
      return lastFocus;}

    /* NEAR THE LIMIT ONLY. On a big mesh the faces that matter are a few hundred of millions; with
       the switch on, every face below 80% of the bar goes one flat dark grey and the warm ones are
       the only colour left. A per-viewer preference; nothing in the payload changes. */
    let nearOnly=false,curRp=null;
    function paintFaces(m,rp){const cut=m?NEAR*qf.metrics[m].limit:0;
      entries.forEach(en=>{const mp=en.actor.getMapper();
        if(rp&&en.q[m]){
          const v=en.q[m],rgb=new Uint8Array(v.length*3);
          for(let i=0;i<v.length;i++){
            if(nearOnly&&!(v[i]>=cut)){rgb[i*3]=NEUTRAL[0];rgb[i*3+1]=NEUTRAL[1];rgb[i*3+2]=NEUTRAL[2];}
            else rp.color(v[i],rgb,i*3);}
          en.pd.getCellData().setScalars(vtk.Common.Core.vtkDataArray.newInstance(
            {name:'quality',values:rgb,numberOfComponents:3}));
          mp.setScalarModeToUseCellData();mp.setColorModeToDirectScalars();
          if(mp.setInterpolateScalarsBeforeMapping)mp.setInterpolateScalarsBeforeMapping(false);
          mp.setScalarVisibility(true);}
        else mp.setScalarVisibility(false);});}
    function hasNear(m){const md=qf.metrics[m];if(!(md.limit>0))return false;const cut=NEAR*md.limit;
      return entries.some(en=>{const v=en.q[m];if(!v)return false;
        for(let i=0;i<v.length;i++)if(v[i]>=cut)return true;return false;});}
    function setNearOnly(b){nearOnly=!!b;
      if(activeMetric&&curRp){paintFaces(activeMetric,curRp);
        const bt=legend&&legend.querySelector('.v-only');
        if(bt){bt.classList.toggle('on',nearOnly);bt.setAttribute('aria-pressed',String(nearOnly));}
        rw.render();}
      return nearOnly;}

    let legend=null,probeEl=null;
    function hideProbe(){if(probeEl){probeEl.remove();probeEl=null;}}
    function setMetric(m){
      if(m===activeMetric)m=null;
      cancelFly();clearFocus();
      activeMetric=m;nearOnly=false;walkIdx=-1;lastFocus=null;
      const rp=m?ramp(m):null;curRp=rp;
      paintFaces(m,rp);
      if(legend){legend.remove();legend=null;}
      clearHot();hideProbe();
      // the edges step back while the faces carry the data, and are handed back through the parts
      // panel when it is switched off, so a selected part keeps its selection edge
      edgeCol=rp?_EDGE_HEAT:_EDGE;
      if(rp)heatEdges();else setEdges(true);
      spots=[];spotSig='';
      if(rp){spots=buildSpots(m);spotSig=m+'#'+shownSig();}
      if(repaintParts)repaintParts();
      else entries.forEach(en=>{en.actor.getProperty().setEdgeColor(edgeCol[0],edgeCol[1],edgeCol[2]);});
      if(rp){const md=qf.metrics[m];showHot(m);
        legend=document.createElement('div');legend.className='v-legend';
        // an instrument scale: the bar stands upright beside the viewport, the limit is a
        // tick with its value, the top of the bar is a third past the limit
        legend.innerHTML=`<b>${esc(md.label)}</b>`
          +`<span class="scale"><span class="ticks">`
            +`<span style="bottom:100%">${esc(fmt(m,rp.hi))}</span>`
            +(rp.open?'':`<span class="lim" style="bottom:${rp.limitPct}">${esc(fmt(m,md.limit))} limit</span>`)
            +`<span style="bottom:0">${rp.lo?esc(fmt(m,rp.lo)):'0'}</span></span>`
          +`<span class="bar" style="background:${rp.css}">${rp.open?'':`<i style="bottom:${rp.limitPct}"></i>`}</span></span>`
          +`<span class="mxv">max ${esc(fmt(m,md.max||0))}</span>`
          +(md.n_over
             ?`<span class="over">${md.n_over.toLocaleString()} face${md.n_over!==1?'s':''} over</span>`
             :rp.open?`<span class="ok">none near the ${esc(fmt(m,md.limit))} bar</span>`
                     :`<span class="ok">none over</span>`)
          +`<span class="acts">`
            +`<button class="v-btn v-walk" title="fly to the next worst spot, worst first">`
              +`Worst spot <span class="v-walk-n">0 / ${spots.length}</span></button>`
            +(hasNear(m)
               ?`<button class="v-btn v-only" aria-pressed="false" title="grey out every face below `
                 +`${esc(fmt(m,NEAR*md.limit))} (80% of the limit)">Near the limit only</button>`:'')
          +`</span>`
          +`<span class="x" title="turn colouring off">✕</span>`;
        legend.querySelector('.x').onclick=()=>setMetric(null);
        legend.querySelector('.v-walk').onclick=()=>focusSpot(walkIdx+1);
        const ob=legend.querySelector('.v-only');if(ob)ob.onclick=()=>setNearOnly(!nearOnly);
        // a press on the scale is not the start of an orbit
        legend.addEventListener('pointerdown',e=>e.stopPropagation());
        host.appendChild(legend);walkLabel();
        hintEl.textContent='click a face to see its numbers · drag still rotates';}
      else hintEl.textContent='rotate: drag · zoom: wheel or right-drag · pan: shift+drag';
      document.querySelectorAll(`#v-facts-${job} .mx-c.live, #v-heatctl-${job} a`)
        .forEach(c=>c.classList.toggle('on',c.dataset.metric===m));
      rw.render();}

    // the figures are the controls; a payload with no figures block gets a plain text one
    const facts=document.getElementById('v-facts-'+job);
    let wired=0;
    if(facts)avail.forEach(m=>{
      const cell=[...facts.querySelectorAll('.mx-c')].find(c=>c.dataset.key===META[m].fact);
      if(!cell)return;
      cell.classList.add('live');cell.dataset.metric=m;cell.title='colour the mesh by this';
      cell.onclick=()=>setMetric(m);wired++;});
    if(wired<avail.length){
      const ctl=document.createElement('div');ctl.className='v-heatctl';ctl.id='v-heatctl-'+job;
      ctl.innerHTML='colour by '+avail.map(m=>`<a data-metric="${m}">${esc(qf.metrics[m].label)}</a>`).join(' · ');
      ctl.querySelectorAll('a').forEach(a=>{a.onclick=()=>setMetric(a.dataset.metric);});
      host.appendChild(ctl);}

    /* CLICK TO EXPLAIN. A click on a coloured face says what its number is, whether it clears
       the gate's bar, what the cell behind it is, and the likely reason - then offers to mark
       the spot, which is the existing dispute path: the reviewer re-inspects there and the mesh
       is rebuilt. The reason is a reading of the cell's shape and place, and says "likely". */
    function why(cf,role){
      const at=/inlet|outlet|port/i.test(role||'')?'at a port mouth, ':'';
      if(cf>6)return at+'a polyhedral cell where refinement levels meet - the mesher splits hexes at a level change and the split faces sit askew';
      if(cf===5)return at+'a boundary-layer prism squeezed where the surface curves or folds';
      if(cf===6)return at+'a hex distorted while snapping to the curved surface here';
      return at+'an irregular cell';}
    const shapeOf=cf=>cf===6?'hex':cf===5?'prism':cf===4?'tet':'polyhedron';
    function probeActs(onMark){
      probeEl.innerHTML+=`<div class="acts">`
        +(onMark?`<button class="v-btn" data-a="mark">Mark this spot</button>`:'')
        +`<button class="v-btn" data-a="close">Close</button></div>`;
      if(onMark)probeEl.querySelector('[data-a=mark]').onclick=()=>{onMark();hideProbe();};
      probeEl.querySelector('[data-a=close]').onclick=hideProbe;
      host.appendChild(probeEl);}
    function showProbe(en,cid,head){
      hideProbe();
      const role=((surf.patches||[]).find(p=>p.name===en.patch)||{}).type||'';
      const cf=en.cellFaces?en.cellFaces[cid]:null;
      const have=avail.filter(m=>en.q[m]);
      const over=have.filter(m=>en.q[m][cid]>qf.metrics[m].limit);
      lastProbe={patch:en.patch,cell:cid,cellFaces:cf,over,
                 values:Object.fromEntries(have.map(m=>[m,en.q[m][cid]]))};
      const rows=have.map(m=>{const v=en.q[m][cid],md=qf.metrics[m],bad=v>md.limit;
        return `<div><b>${esc(md.label)}</b> ${esc(fmt(m,v))} `
          +`<span class="${bad?'over':'ok'}">${bad?'over':'under'} the ${esc(fmt(m,md.limit))} limit</span></div>`;}).join('');
      probeEl=document.createElement('div');probeEl.className='v-probe';
      probeEl.innerHTML=`<div class="pk">${head?esc(head)+' · ':''}${esc(en.patch)} · face ${cid.toLocaleString()}</div>${rows}`
        +(cf!=null?`<div>cell behind it: ${cf}-face ${shapeOf(cf)}</div>`:'')
        +(over.length?`<div class="why">likely: ${esc(why(cf,role))}</div>`:'');
      probeActs(()=>placeMarker(en,cid));}
    // a payload hotspot on an inner face has no polygon on screen: its own reading, in the same card
    function showSpotCard(s,at,head){
      hideProbe();
      const m=activeMetric,md=qf.metrics[m],bad=s.value>md.limit,cf=s.cellFaces;
      lastProbe={patch:s.patch,cell:null,cellFaces:cf,over:bad?[m]:[],values:{[m]:s.value},hotspot:true};
      probeEl=document.createElement('div');probeEl.className='v-probe';
      probeEl.innerHTML=`<div class="pk">${esc(head)} · ${s.patch?esc(s.patch):'inside the mesh'}</div>`
        +`<div><b>${esc(md.label)}</b> ${esc(fmt(m,s.value))} `
        +`<span class="${bad?'over':'ok'}">${bad?'over':'under'} the ${esc(fmt(m,md.limit))} limit</span></div>`
        +(cf!=null?`<div>cell: ${cf}-face ${shapeOf(cf)}</div>`:'')
        +(bad?`<div class="why">likely: ${esc(why(cf,''))}</div>`:'');
      probeActs(at?()=>placeMarker(at.en,at.cid):null);}
    host.addEventListener('pointerup',e=>{
      if(markMode||!activeMetric||!downXY)return;
      if(e.target&&e.target.closest&&e.target.closest('.v-probe,.v-legend,.v-pin,.v-heatctl'))return;
      if(Math.hypot(e.clientX-downXY[0],e.clientY-downXY[1])>6)return;
      pickCell(e,(en,cid)=>showProbe(en,cid));},true);
    // hiding, showing or isolating a part changes what the spots and the edge density are read from
    onPartsPainted=()=>{if(!activeMetric)return;refreshSpots();heatEdges();};
    // every camera move: edges in or out with the face density, beads held to their screen size
    cam.onModified(()=>{if(activeMetric){heatEdges();scaleHot();}});

    /* support/debug hook - lets the browser tier drive the heatmap without pixel picking; the demo
       recorder clicks the real buttons */
    window._vdbg[job+':heat']={metrics:avail,set:setMetric,active:()=>activeMetric,
      probe:(patch,cid)=>{const en=entries.find(t=>t.patch===patch);if(en)showProbe(en,cid);return lastProbe;},
      last:()=>lastProbe,legend:()=>!!legend,hotspots:()=>hotActors.length,
      wired:()=>wired,
      walk:()=>focusSpot(walkIdx+1),
      only:b=>setNearOnly(b===undefined?!nearOnly:b),
      nearOnly:()=>nearOnly,
      spots:()=>spots.map(s=>({x:s.x,y:s.y,z:s.z,value:s.value,patch:s.patch,over:s.over})),
      focus:()=>lastFocus,focal:()=>cam.getFocalPoint(),dist:()=>camDist(),
      flying:()=>!!fly,settled:()=>fly?fly.done:Promise.resolve(true),
      ring:()=>focusActors.length,
      edges:()=>{const pp=entries[0].actor.getProperty();return {on:!!pp.getEdgeVisibility(),color:pp.getEdgeColor()};},
      colorOf:(patch,cid)=>{const en=entries.find(t=>t.patch===patch);if(!en)return null;
        const sc=en.pd.getCellData().getScalars();if(!sc||!en.actor.getMapper().getScalarVisibility())return null;
        const a=sc.getData();return [a[cid*3],a[cid*3+1],a[cid*3+2]];}};
  })();
}
