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
import { roundNumbers } from "../render/outcome.js";
import { bindFreshDownloads, downloadAttrs } from "../render/download.js";
import { MOUSE_HINT, bindMouse, isUpAxis, orient, saveUp, savedUp, shield, upSelectHtml, upVector } from "./view_controls.js";
// a delivered thermal model (an ECXML upload) is read part by part, with slices and its own files
import { bindThermalResults, sliceColour, thermalDownloads, thermalResultsHtml } from "./thermal_results.js";
// a delivered FEA mesh (a structural solid) is read face by face, with its quality against each
// solver limit, its plan and its hot spots (FEA build step 5)
import { bindFeaResults, feaDownloads, feaResultsHtml } from "./fea_results.js";
// the reading instruments on the mesh: the inspect window's controls, the size legend, and the
// plain words for what is on screen (viewer/inspect.js)
import { cutText, dialRadius, drawingOf, EXACT_HELP_ROW, fmtLen, inspectorHtml,
         odometerText, radiusDial, scaleBar } from "./inspect.js";

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
/* THE SIXTH READING. A poor-cell set is the boundary of the cells that failed a
   check, and it used to be drawn in whatever palette colour its index landed on -
   indistinguishable from a wall, listed between ymin and zmax. It is a highlight or
   it is nothing. */
const _POOR_TYPE='poorQuality';
const _POOR_COL=[0.93,0.27,0.22];
const _PATCH_COLS=[[0.80,0.78,0.72],[0.64,0.77,0.75],[0.83,0.76,0.60],
                   [0.72,0.72,0.70],[0.66,0.71,0.73],[0.76,0.70,0.66]];
const _EDGE=[0.27,0.25,0.22];          /* hairline edges at rest */
/* EDGES ARE A SECOND DRAW CALL, AND PAST A CERTAIN SIZE THEY DO NOT SLOW THE FRAME, THEY TAKE THE
   CONTEXT AWAY. Measured 2026-11-08 on solid_to_air drawn at 1:1, 1,904,636 faces / 3,810,902
   triangles in one actor, Intel UHD via ANGLE/D3D11, WebGL2:

     edges off   1 draw call    174.4 MB VBO    18.1 ms/frame   context fine
     edges on    2 draw calls   348.9 MB VBO   302.8 ms/frame   gl.getError() 37442
                 the second being 7,620,174 line segments       CONTEXT_LOST_WEBGL

   and the loss outlived the page: repeated losses left the browser refusing a new context until
   the tab was replaced. WebGL exposes no way to ask how much video memory is left, so this cannot
   be detected and has to be bounded instead.

   WHERE THE CAP IS SET, AND WHAT IS AND IS NOT KNOWN. The largest actor the viewer draws today is
   bl3's fluid_to_solid at 590,990 polygons, and every page in the roster works, so everything at
   or below that is empirically fine. The only measured failure is at 1,904,636. The true boundary
   is somewhere in between and has not been measured, so the cap sits just above what ships rather
   than pretending to know: it changes nothing today and it bounds the case that actually broke,
   a patch streamed at 1:1.

   Measured per-patch polygon counts behind that choice, counted the way _countPolys does:
     bl3 / cbox2b / cbox2 / plate  fluid_to_solid   590,990 / 518,038 / 478,388 / 459,608
     chip                          zMax, zMin       287,448 / 252,222
     slabw                         oldInternalFaces 174,086
     slab8                         oldInternalFaces  25,149

   Note it is not purely a memory limit: 15.2M triangles at 1,396 MB rendered fine with edges off,
   while 3.8M triangles plus 7.6M LINE SEGMENTS at 349 MB did not. Line rendering is the expensive
   part, and a quad contributes four segments, so segments run about 4x the face count. */
const _EDGE_MAX_POLYS=750000;
/* Count the polygons actually in a VTK connectivity array, [n, i0..in-1] repeated - the same walk
   the surface builder does, because a patch's face_count is its count in the MESH and a decimated
   surface does not ship one polygon per delivered face. */
function _countPolys(polys){let n=0;for(let q=0;q<polys.length;q+=polys[q]+1)n++;return n;}
/* Edge visibility from the polygon count. Returns whether they went on. */
function setEdges(pr,nPolys,col){
  const on=nPolys>0&&nPolys<=_EDGE_MAX_POLYS;
  if(pr.setEdgeVisibility)pr.setEdgeVisibility(on);
  if(on){const c=col||_EDGE;pr.setEdgeColor(c[0],c[1],c[2]);pr.setLineWidth(1);}
  return on;}
/* How much of the merged surface is left showing behind an inspect window, so a cut has a part
   around it instead of black. Low enough that it cannot be mistaken for the cells. */
const GHOST_OPACITY=0.10;
/* darker while the faces carry colour, so every cell still reads against the data (the original
   heatmap's edge, lightened with the calm end of its scale so a whole part does not read dark) */
const _EDGE_HEAT=[0.24,0.23,0.22];
/* the isolate / selection colour is --steel-bright over --steel-muted, not the accent
   orange: red on a mesh reads as a fault flag, and the quality bands already own red */
const _SEL=[0.616,0.714,0.831],_SEL_EDGE=[0.341,0.439,0.561];
const HEAT_HINT='Click a face to see its numbers · Drag still rotates';
const _SEL_COLS=['#e8613c','#f2c744','#3fa650','#3f7fd9','#b455c8','#38c2c2'];

/* THE REVIEW'S POINTS, one per row. A point arrives as one string - "Important: Cells degrade
   where ..." or "Reviewer note: Wall layers: 4.71 of 5 ..." - so its lead word becomes a tag and
   its label leads the row; long decimals are printed as an engineer would (71.0953 -> 71.1). */
function reviewList(items){
  const rows=(items||[]).map(x=>String(x||'').trim()).filter(Boolean).map(t=>{
    const imp=/^important\s*[:-]\s*/i.test(t);
    const body=t.replace(/^important\s*[:-]\s*/i,'').replace(/^reviewer note:\s*/i,'');
    return `<li class="oc-row${imp?' important':''}">${imp?'<span class="oc-tag">Important</span>':''}`
      +`<span class="oc-txt">${esc(roundNumbers(body))}</span></li>`;}).join('');
  return rows?`<ul class="oc-list v-concern-list">${rows}</ul>`:'';
}
/* the reviewer's full reasoning, whole and folded away - it used to be cut at 600 characters */
function reasonDetails(text){
  const t=String(text||'').trim();
  return t?`<details class="oc-more"><summary>Why the reviewer said this</summary><p>${esc(t)}</p></details>`:'';
}

export async function openViewer(job,anchorEl,opts){
  opts=opts||{};
  if(document.getElementById('viewer-'+job)){document.getElementById('viewer-'+job).scrollIntoView({behavior:'smooth'});return;}
  const box=document.createElement('div');box.className='viewer';box.id='viewer-'+job;
  const _m=opts.metrics||{};
  const _dl=(opts.files||[]).filter(f=>f.type!=='video');
  // the case the user asked for leads; the previews follow it
  const _dlHtml=_dl.slice().sort((a,b)=>(b.type==='mesh_bundle')-(a.type==='mesh_bundle')).map(f=>{
      const primary=f.type==='mesh_bundle'?' primary':'';
      return `<a class="v-dlbtn${primary}" href="${esc(f.url||'#')}"${downloadAttrs(job,f)} download target="_blank" rel="noopener">
                <b>${esc(f.label||f.type)}</b><span>${fmtBytes(f.size)}</span></a>`;}).join('');
  const upAxis=savedUp('job',job)||'+z';
  // THE PANEL READS TOP TO BOTTOM the way the result is used: the verdict and the download, the
  // mesh's own figures, its parts, and last the way to ask for a change - beside the marks it sends.
  box.innerHTML=`<div class="v-bar">
      <div class="v-sec v-head">
        <div class="v-row v-titlerow">
          <b class="v-title">${opts.unreviewed?'Mesh - not signed off':'Your mesh'}</b>
          <span class="v-meta">
            ${opts.unreviewed
              ? `<span class="v-pill fail">DID NOT PASS REVIEW</span>`
              : opts.concerns
              ? `<span class="v-pill concerns">DELIVERED WITH CONCERNS</span>`
              : opts.withNotes
              ? `<span class="v-pill concerns">PASSED, WITH NOTES</span>`
              : (_m.pass!==undefined?`<span class="v-pill${_m.pass?'':' fail'}">${_m.pass?'PASS':'FAIL'}</span>`:'')}
            ${_m.attempts?`<span class="sep">·</span><span>${_m.attempts} attempt${_m.attempts!==1?'s':''}</span>`:''}
          </span>
        </div>
        <div class="v-row"><span class="v-status" id="v-status-${job}">loading…</span></div>
      </div>
      ${opts.unreviewed?`<div class="v-why">
        <p class="v-why-h">Sound, but not signed off</p>
        <p class="v-why-p">The mesh passed every validity check, so it will solve. The reviewer did
        not sign it off on quality${(opts.findings||[]).length?':':'.'}</p>
        ${reviewList(opts.findings)}
        ${reasonDetails(opts.reasoning)}
        <div class="v-why-act">
          Look at it yourself. If it is good enough for your study, say so: it is checked again
          against <i>your</i> standard and delivered.
          <button class="v-btn primary" id="v-accept-${job}">This is acceptable →</button>
        </div></div>`:''}
      ${opts.withNotes&&opts.withNotes.length?`<div class="v-why v-concerns">
        <p class="v-why-h">Passed, with notes</p>
        <p class="v-why-p">The mesh is valid and passed every check that decides a run. These figures are
        past a limit, listed so you can weigh them:</p>
        ${reviewList(opts.withNotes)}
        </div>`:''}
      ${opts.concerns?`<div class="v-why v-concerns">
        <p class="v-why-h">Points to weigh</p>
        <p class="v-why-p">The mesh passed every validity check, so it is sound, will solve and is
        yours. Look at these before you rely on the results:</p>
        ${reviewList(opts.concerns)}
        ${reasonDetails(opts.reasoning)}
        </div>`:''}
      ${(opts.notes||[]).length?`<div class="v-why v-notes">
        <p class="v-why-h">Reviewer notes</p>
        <p class="v-why-p">An AI reviewer looked at the mesh. Its notes are for you to weigh; they
        do not decide the result.</p>
        ${reviewList(opts.notes)}
        </div>`:''}
      <div class="v-sec v-dl">${_dlHtml}</div>
      <div class="v-thermal" id="v-thermal-${job}"></div>
      <div class="v-fea" id="v-fea-${job}"></div>
      <div class="v-facts" id="v-facts-${job}"></div>
      <!-- MESH PARTS: every selectable entity the loaded case actually declares -
           boundary patches, regions, cell zones, face zones, layer groups. The list
           is read from the mesh payload, so a part appears because the case has it. -->
      <div class="v-bnd" id="v-bnd-${job}">
        <div class="v-bnd-head">
          <span class="v-bnd-lbl">Mesh parts</span>
          <span class="v-bnd-count" id="v-bnd-count-${job}"></span>
          <span style="flex:1"></span>
          <button class="v-bnd-act v-quiet" id="v-bnd-restore-${job}">Restore full mesh</button>
        </div>
        <div class="v-bnd-cols">
          <div class="v-bnd-list" id="v-bnd-chips-${job}"></div>
          <div class="v-bnd-insp" id="v-bnd-note-${job}"></div>
        </div>
      </div>
      <div class="v-sec v-review">
        <div class="v-sec-h">Changes</div>
        <div class="v-row">
          <button class="v-btn" id="v-selbtn-${job}">Mark region</button>
          <span class="v-brush" title="region size - scales with your zoom">Size
            <input type="range" id="v-brush-${job}" min="2" max="30" value="8" aria-label="region size">
            <span id="v-brushlbl-${job}"></span></span>
        </div>
        <div class="v-row v-act">
          <button class="v-btn" id="v-submit-${job}">Request a change…</button>
          <button class="v-btn v-quiet" id="v-clearbtn-${job}" disabled>Clear marks</button>
        </div>
        <p class="v-cost">A change starts a new run. It takes a few minutes.</p>
      </div></div>
    <div class="v-canvas" id="v-canvas-${job}">
      <div class="v-loading" id="v-load-${job}"><svg class="v-spin" viewBox="0 0 24 24" aria-hidden="true"><circle class="sp-arm" cx="12" cy="12" r="7.2"/><path class="sp-hd" d="M19.20 8.16L22.40 14.56L16.00 14.56Z"/><path class="sp-hd" d="M4.80 15.84L1.60 9.44L8.00 9.44Z"/></svg><div>Loading mesh…</div></div>
      <div class="v-tools">${upSelectHtml('v-up-'+job,upAxis)}<button class="v-btn v-tool" id="v-fitbtn-${job}" type="button" title="Frame the whole mesh">Fit</button></div>
      <div class="v-hint" id="v-hint-${job}">${MOUSE_HINT}</div>
      ${inspectorHtml(job,{xray:true,clip:true,worst:true,fly:true})}
      <div class="v-count" id="v-count-${job}"></div></div>
    <div class="v-flags" id="v-flags-${job}" style="display:none"></div>`;
  // each Download click asks the API for a fresh link, so one read long after the page loaded -
  // or a large file's retry - never meets an expired one
  bindFreshDownloads(box);
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
      if(l)l.innerHTML='<div>The 3D view could not load</div>';
      document.getElementById('v-status-'+job).textContent='The 3D view could not load ('+e.message+').'
        +(String(e.message).includes('404')
          ? ' This run may belong to another account.' : ' The downloads still work.');}};
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
  /* A LOST CONTEXT USED TO BE A DEAD CANVAS. The browser takes the context away rather than
     degrading the frame when video memory runs out, and the DEFAULT behaviour on the event is to
     make that permanent - so preventDefault is most of the fix, and redrawing when the browser
     hands it back is the rest. Measured losing it on 8 November with edges on at 1.9M faces. The
     cap in setEdges stops that particular cause; this stops the next one leaving a black
     rectangle with no explanation. */
  const _canvas=(apiRW&&apiRW.getCanvas)?apiRW.getCanvas():null;
  if(_canvas&&_canvas.addEventListener){
    _canvas.addEventListener('webglcontextlost',e=>{
      e.preventDefault();                       // without this it is never restored
      const s=document.getElementById('v-status-'+job);
      if(s)s.textContent='The 3D view ran out of video memory. It will redraw shortly.';
      console.warn('viewer: webgl context lost',job);},false);
    _canvas.addEventListener('webglcontextrestored',()=>{
      console.warn('viewer: webgl context restored',job);
      const s=document.getElementById('v-status-'+job);
      try{grw.resize();rw.render();if(s)s.textContent='';}
      catch(err){if(s)s.textContent='The 3D view could not redraw after losing video memory.';
                 console.warn('viewer: redraw after restore failed',err);}},false);}
  const ro=new ResizeObserver(()=>{setRenderScale();rw.render();});ro.observe(host);
  host.addEventListener('contextmenu',e=>e.preventDefault());
  bindMouse(grw);                     // left-drag turns, right-drag pans, the wheel zooms

  /* one IMMUTABLE actor per patch - real polygon cells */
  const entries=[];let totalFaces=0;

  /* ONE RAMP, AT THIS SCOPE, BECAUSE TWO THINGS COLOUR WITH IT. It used to live inside the
     heatmap's closure, where the detail window could not see it - the same cross-closure mistake
     `radiusNow` and `tanHalf` made, and it threw the same way, into the button's label.

     It must also be reachable when the payload has NO metrics block, which is the case that
     matters most: `quality_fields` refuses a mesh past 40 M faces and this plate's coolant side
     alone is 235 M, so the heatmap's own closure returns before it defines anything. A cut of
     that mesh still carries its own descriptors, measured by checkMesh, and colours itself from
     them. The ramp cannot be behind that early return or the colours are unreachable on exactly
     the meshes they were built for.

     Calm below the bar, warm approaching it, red past it. The colours are computed here and
     handed to the mapper as direct per-face RGB, and the legend is built from the SAME stops, so
     the bar on screen is the bar in the colours. The console's original ramp, its calm blues
     about 30% lighter (Areen, 2026-10-01): steel below, the wireframe blue through the calm
     range, warming to amber and international orange at the bar, crimson past it. The colour is
     the surface's own - nothing is drawn on top of the mesh. */
  const _STOPS=[[0,[92,120,152]],[0.5,[142,178,218]],[0.8,[255,168,90]],[1.0,[255,88,0]]];
  const _PAST=[168,31,25];
  function rampOf(md){if(!md)return null;const lim=md.limit||1,lo=md.floor||0;
    // a metric whose bar sits far above everything the mesh has (aspect ratio: checkMesh's
    // 1000 against layers at 10 to 50) spans its colours over the mesh's own range instead,
    // and the bar is drawn as a tick only when it falls inside that range
    const open=md.scale_to>0&&lim>md.scale_to;
    const hi=open?md.scale_to:Math.max(lim*1.3,md.max||0);
    const span=open?(hi-lo):(lim-lo);
    const pts=_STOPS.map(([f,c])=>[lo+f*span,c]).concat(open?[]:[[hi,_PAST]]);
    const color=(v,out,o)=>{
      if(!(v>lo)){out[o]=pts[0][1][0];out[o+1]=pts[0][1][1];out[o+2]=pts[0][1][2];return;}
      let i=1;while(i<pts.length-1&&v>pts[i][0])i++;
      const [a,ca]=pts[i-1],[b,cb]=pts[i],t=Math.min(1,Math.max(0,(v-a)/((b-a)||1)));
      out[o]=ca[0]+(cb[0]-ca[0])*t;out[o+1]=ca[1]+(cb[1]-ca[1])*t;out[o+2]=ca[2]+(cb[2]-ca[2])*t;};
    const pc=v=>(100*(v-lo)/((hi-lo)||1)).toFixed(1)+'%';
    const css='linear-gradient(0deg,'+pts.map(([v,c])=>`rgb(${c}) ${pc(v)}`).join(',')+')';
    return {color,hi,lo,open,css,limitPct:pc(lim)};}
  (surf.patches||[]).forEach((p,i)=>{
    let pts,polys,nCells,nTris;
    if(p.polys_b64){pts=b64f32(p.points_b64);polys=b64u32(p.polys_b64);
      // Count the polygons ACTUALLY in this connectivity array. face_count is the
      // patch's face count in the MESH, which a decimated preview surface does not
      // ship one-for-one; trusting it walks the offsets table off the end of polys.
      // AND THE TRIANGLES THEY FAN TO, off the stream rather than as 2x the polygons: the
      // lossless merge emits 4-corner rectangles beside faces kept whole at 4 to 8 corners, and
      // an n-gon fans to n - 2. It is counted here because this is the one walk of the stream
      // that already happens, and `_vdbg.drawn()` has no other way to say what is on screen.
      nCells=0;nTris=0;
      for(let q=0;q<polys.length;q+=polys[q]+1){nCells++;nTris+=Math.max(polys[q]-2,0);}}
    else{pts=b64f32(p.positions_b64);nCells=pts.length/9;nTris=nCells;  // STL fallback: tri soup
      polys=new Uint32Array(nCells*4);
      for(let t=0;t<nCells;t++){polys[t*4]=3;polys[t*4+1]=t*3;polys[t*4+2]=t*3+1;polys[t*4+3]=t*3+2;}}
    const pd=vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(pts,3);
    pd.getPolys().setData(polys);
    const mapper=vtk.Rendering.Core.vtkMapper.newInstance();
    mapper.setInputData(pd);
    const actor=vtk.Rendering.Core.vtkActor.newInstance();
    actor.setMapper(mapper);
    const poor=(p.type===_POOR_TYPE);
    const base=poor?_POOR_COL:_PATCH_COLS[i%_PATCH_COLS.length];
    const pr=actor.getProperty();
    pr.setColor(base[0],base[1],base[2]);
    setEdges(pr,nCells);
    // lit flat and hot rather than shaded, so a set of a few hundred cells inside a mesh of
    // millions reads as a mark on it and not as another surface
    pr.setAmbient(poor?0.75:0.30);pr.setDiffuse(poor?0.35:0.78);
    pr.setSpecular(poor?0:0.10);pr.setSpecularPower(18);pr.setSpecularColor(1,1,1);
    // DOUBLE-SIDED. An OpenFOAM boundary face points out of the FLUID, so a car's wall faces point
    // into the car; with back faces culled the near side vanished and the viewer drew the inside of
    // the body (a car read as an open tray). The depth test does the hiding instead.
    if(pr.setBackfaceCulling)pr.setBackfaceCulling(false);
    ren.addActor(actor);
    const b=pd.getBounds();
    entries.push({actor,pd,patch:p.name,polys,pts,nCells,nTris,offsets:null,
      diag:Math.hypot(b[1]-b[0],b[3]-b[2],b[5]-b[4])});
    totalFaces+=nCells;});

  let edgeCol=_EDGE;             // what paint() draws edges with; the heatmap darkens it
  // A DELIVERED THERMAL MODEL: its parts table shows one part alone through the parts panel
  // (partsApi), and a ready-made slice replaces the surfaces while it is on (sliceOn)
  const thermal=surf.thermal||null;
  let partsApi=null,sliceOn=null;
  // the parts panel's own repaint: the heatmap hands edges back through the panel, so a selected
  // part keeps its selection edge - and a listener on it, so the problem areas follow what is shown
  let repaintParts=null,onPartsPainted=null,onVisibleChanged=null;
  //: The region a reader has ISOLATED, or null. Set by `paint`, read by the deep-zoom keep list,
  //: which otherwise can only name patches this payload happens to draw - and the metal's are
  //: nearly all of the ones it does not. See `detail_window._visible_rows`.
  let isolatedRegion=null;
  const cam=ren.getActiveCamera();
  /* HALF THE VIEW'S HEIGHT PER UNIT OF DISTANCE, in the scope BOTH closures below
     need it in. It used to be a const inside the heatmap's IIFE, and `radiusNow` in
     the detail window's IIFE called it by name - a different closure, so the name was
     never in its scope chain and every click of Detail threw
     `ReferenceError: tanHalf is not defined` before it asked the server for anything.
     Measured in a browser on the delivered coupon, 2026-10-29. */
  const tanHalf=()=>Math.tan(cam.getViewAngle()*Math.PI/360);
  /* The detail window's own way of putting the merged surface back, for the things that
     have to put it back and are not the detail window. Null until it exists. */
  let closeDetail=null;
  /* THE SAME, WITHOUT TURNING A MODE OFF: just put the merged surface back on screen. For the
     things that re-frame the camera, because `resetCamera` frames what is VISIBLE and a window
     that is up has hidden the mesh and put a few thousand real cells in front of it. Null until
     the detail window exists. */
  let showSurfaceAgain=null;
  // CAD is z-up, and the geometry check already draws parts that way: start from a front-quarter
  // view looking slightly down with +z up. vtk's default camera is +y-up, which laid cars and
  // aircraft on their side in the mesh viewer. A part modelled another way up opens the way the
  // user last set it for this job (the "Up" control on the canvas).
  const VIEW_DIR=[1,-1,0.7];
  const upSel=document.getElementById('v-up-'+job);
  cam.setFocalPoint(0,0,0);cam.setPosition(...VIEW_DIR);cam.setViewUp(0,0,1);
  // with no choice stored for this job, the way up the geometry check proposed (surf.up_axis)
  if(upSel&&!savedUp('job',job)&&isUpAxis(surf.up_axis))upSel.value=surf.up_axis;
  orient(cam,(upSel&&upSel.value)||'+z',VIEW_DIR);
  ren.resetCamera();rw.render();
  const _bs=ren.computeVisiblePropBounds();
  const diag=Math.max(Math.hypot(_bs[1]-_bs[0],_bs[3]-_bs[2],_bs[5]-_bs[4]),1e-9);
  // The viewer renders the BOUNDARY surface, so `totalFaces` is boundary faces - not
  // cells. Say the real cell count when the backend knows it, and name what we counted
  // when it does not. (This badge used to call 8,260 boundary faces "8,260 cells".)
  /* ...and when the mesh was merged to fit the budget, `totalFaces` is not the boundary either:
     it is the DRAWN polygons. On the customer plate that made the badge read "486,372 boundary
     faces" for a mesh whose boundary is 30,011,162 - the drawn figure wearing the mesh's label,
     one step along from the bug above. `reduced` carries both numbers precisely because they are
     different, so the badge says both. */
  /* EITHER DRAWING ROUTE CARRIES BOTH NUMBERS, and the badge needs both whichever one was taken.
     `exact` means nothing was approximated, so the badge says so - "drawn of" would read as a
     shortfall on a surface that has none. */
  const _draw=drawingOf(surf),_red=_draw.block;
  /* THE HELP'S FIRST ROW IS THE ONE CLAIM THAT DEPENDS ON THE PAYLOAD, and the inspector's HTML is
     written into the shell before the surface has been fetched - so it cannot know then. It is
     swapped here instead of being left to say "a stand-in" about a surface that is the mesh, which
     would send a reader to Inspect to check a shape that is already exact. */
  if(_draw.exact){
    const _hrow=document.querySelector('#v-insp-helpbox-'+job+' .v-insp-hrow');
    if(_hrow)_hrow.innerHTML=`<b>${EXACT_HELP_ROW[0]}</b><p>${EXACT_HELP_ROW[1]}</p>`;}
  document.getElementById('v-count-'+job).textContent=
    surf.cell_count?surf.cell_count.toLocaleString()+' cells'
    :_red&&_red.delivered_faces
      ?(_draw.exact
        ?`${totalFaces.toLocaleString()} polygons, exactly ${(+_red.delivered_faces).toLocaleString()} faces`
        :`${totalFaces.toLocaleString()} drawn of ${(+_red.delivered_faces).toLocaleString()} faces`)
      :totalFaces.toLocaleString()+' boundary faces';

  /* FIT FRAMES THE MESH, and it could not while a window was open. `resetCamera` frames the
     VISIBLE props, and a detail window hides the merged surface and puts its own few thousand
     real cells in front of it - so Fit framed the cut. Measured on this plate with the deep mode
     on: Fit from a 7.8 mm view of the coolant left the camera 0.054 m out on a 0.386 m part, and
     every zoom after it started from there, bottoming out on the near clamp with 4 real cells in
     a 0.42 mm box. The surface goes back first; the mode is left alone and puts its cut back on
     the camera move this makes, which is what it does after any other camera move. */
  document.getElementById('v-fitbtn-'+job).onclick=()=>{
    if(showSurfaceAgain)showSurfaceAgain();
    ren.resetCamera();clampCam();rw.render();};
  // WHICH WAY IS UP: turn the camera to the chosen axis, re-frame, and remember it for this job
  function setUp(axis){
    if(showSurfaceAgain)showSurfaceAgain();   // re-frames, so the same as Fit above
    orient(cam,axis,VIEW_DIR);ren.resetCamera();clampCam();rw.render();
    if(upSel&&upSel.value!==axis)upSel.value=axis;
    saveUp('job',job,axis);}
  if(upSel)upSel.onchange=()=>setUp(upSel.value);
  shield(host.querySelector('.v-tools'));

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
        // the mesh ENTITIES this part maps back to - carried by the backend, because
        // a label is for reading and an entity name is for looking up an actor.
        // USUALLY ONE, SOMETIMES SEVERAL. A multi-region engine draws "<region>/<patch>"
        // because its regions own identically named block faces, while the manifest keeps the
        // bare name the user asked for at intake. `drawn_as` is the backend saying which drawn
        // surfaces carry this one declared boundary; both regions can reach ymin, so it is a
        // list. Without it the declared patch found no actor, read as un-renderable, and the
        // drawn surfaces were listed again beside it as parts of their own.
        const entity=md.name||p.label;
        const entities=(Array.isArray(md.drawn_as)&&md.drawn_as.length?md.drawn_as:[entity])
                        .filter(e=>!!actorOf[e]);
        return {id:p.id,label:p.label,kind:p.kind,meta:md,
                entity:entities[0]||entity,entities:entities,
                role:md.role||'',
                // declared renderable AND actually present in this payload
                renderable:p.renderable!==false&&!!entities.length,
                hiddenByDefault:p.default_visibility==='hidden',
                //: WHY it opens hidden, because the two reasons want different things once it is
                //: shown. See `dflt` and `unhide` below, and render/mesh_facts.far_side_interfaces.
                encloses:!!md.encloses_domain,
                coincident:md.coincident_with||''};});

    const KIND=thermal
      // a thermal model's regions are the file's parts, its boundary the sides of the air box
      ?{boundary:'Sides of the air box',region:'Parts',cellGroup:'Cell types',inspection:'Slices'}
      :{boundary:'Boundary patches',region:'Regions',cellGroup:'Cell types',
        inspection:'Inspection structures',poorCells:'Poor-quality cells'};
    // a thermal model's slices are drawn from the thermal panel, so they are not listed twice
    if(thermal)parts=parts.filter(p=>p.kind!=='inspection');
    const kindLabel=k=>KIND[k]||String(k).replace(/([A-Z])/g,' $1')
                                         .replace(/^./,c=>c.toUpperCase());

    // A part the backend opens HIDDEN is one that encloses everything else: shown
    // solid it is a wall of colour with the body trapped inside. It opens hidden AND
    // translucent, so one click brings it back as a shell you can see through. The
    // BACKEND decides which parts those are - from the case's own declared roles -
    // and "Restore full mesh" returns to exactly that.
    /* TWO REASONS TO OPEN HIDDEN, AND THEY WANT DIFFERENT THINGS WHEN SHOWN AGAIN.
       An enclosing wall comes back as a shell you can see through, which is the line above: one
       click on the outside of the plate must not seal the mesh inside it a second time.
       The far side of an interface the payload draws twice is not a wall at all. It is the
       surface the reader asked for - hidden only because another region draws the same sheet from
       the other side and two coincident sheets speckle - and at 18% the channel walls carved into
       the metal would be a ghost of the one thing in the metal worth looking at. So it comes back
       solid. */
    const dflt=p=>({hidden:!!(p.renderable&&p.hiddenByDefault),
                    opacity:p.hiddenByDefault&&!p.coincident?0.18:1});
    const st={};parts.forEach(p=>{
      const en=actorOf[p.entity],d=dflt(p);
      st[p.id]={hidden:d.hidden,opacity:d.opacity,entity:p.entity,
                base:en?en.actor.getProperty().getColor().slice():null};});
    let sel=null,isolated=false;

    /* ISOLATE MEANS SHOW THIS AND NOTHING ELSE, so whatever draws the isolated part comes back
       even when it opened hidden. An actor's visibility is decided by the state of the part that
       DRAWS it, not by the state of the part that was picked: on a conjugate mesh the metal
       region is drawn by six surfaces that all wrap the coolant and so all open hidden -
       correctly, or the first sight of the mesh is six grey rectangles. Isolating the metal used
       to leave an empty viewport, because the only state un-hidden was the region's own and the
       region paints nothing itself. */
    /* AND AN ENCLOSING WALL IS NOT WHAT THE READER ASKED FOR, when the part has anything else.
       The metal's outside is `solid_to_air`, which opens hidden because it wraps everything; the
       paragraph above brought it back on isolate because it was the metal's ONLY drawn surface
       and the alternative was an empty viewport. Once the payload also carries the channel walls
       carved into the metal, bringing it back costs twice: it stands between the camera and them,
       and - because the camera frames what is VISIBLE and the zoom goes where the camera is
       pointing - every cut lands on the OUTSIDE of the plate instead of in the channels.
       Measured on the delivered plate, the metal isolated and everything else identical, 42 wheel
       clicks and one deep pass: outside hidden, 220,804 real cells in a 7.70 mm view; outside
       shown, 2,209 in 4.13 mm. The coolant, for scale, is 228,022 in 7.83 mm.
       So an enclosing surface is left hidden WHEN SOMETHING ELSE DRAWS THE PART, and one click on
       its own chip brings it back as the shell it always was. When it is the only thing that
       draws the part, nothing has changed and it still comes back - which is the case on the
       page that carries one side of each interface, where the metal's outside is all it has.

       PER ACTOR AND BY THE SAME RULE `paint` USES, not per part. An actor's state belongs to the
       FIRST renderable part that names it, so asking "which parts draw this one" answers with
       parts whose state nothing reads - the metal region names its own surface, so a filter over
       parts found a non-enclosing one, concluded there was something else to show, and left the
       viewport empty on exactly the page this is meant to leave alone. */
    function unhide(id){
      const p=parts.find(x=>x.id===id);if(!p)return;
      st[id].hidden=false;
      const owners=p.entities.map(e=>parts.find(x=>x.renderable&&x.entities.indexOf(e)>=0))
                             .filter(Boolean);
      const inner=owners.filter(x=>!x.encloses);
      (inner.length?inner:owners).forEach(x=>{st[x.id].hidden=false;});}

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
      /* A WHOLE REGION, not a patch. `region:solid` isolated means the reader asked for the
         metal, and the metal's drawn surface is its OUTSIDE - the channel walls in it are
         `solid_to_fluid`, which this payload does not draw because the coolant draws the same
         surface from the other side. With the coolant off screen there is nothing to fight
         with, so the window can hand back the whole region. */
      isolatedRegion=(isolated&&selPart&&selPart.kind==='region'
                      &&String(selPart.id).indexOf('region:')===0)
        ?String(selPart.id).slice(7):null;
      entries.forEach(en=>{
        const p=parts.find(x=>x.renderable&&x.entities.indexOf(en.patch)>=0);
        const s=p?st[p.id]:null;if(!s)return;
        const pr=en.actor.getProperty(),
              isSel=!!(selPart&&selPart.entities.indexOf(en.patch)>=0);
        en.actor.setVisibility(!sliceOn&&!s.hidden&&(!isolated||isSel));
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
      /* A SEPARATE HOOK FROM `onPartsPainted`, which is spoken for, and fired on every paint
         rather than only on a visibility change - the listener compares the set itself, because
         paint also runs for a selection, which changes colour and nothing a window cares about. */
      if(onVisibleChanged)onVisibleChanged();
    }
    repaintParts=paint;

    // INSPECT - one part at a time: its OpenFOAM identity, its own statistics,
    // and the controls that apply to IT. Nothing is on screen for a part the
    // user has not selected, which is why the list stays a list.
    function inspect(p){
      if(!p){
        const off=parts.filter(x=>x.renderable&&st[x.id].hidden).map(x=>x.label);
        return `<div class="ins-empty">Select a part to inspect it.`
          +`<span>${parts.length} selectable entities were read from this case.</span>`
          +(off.length?`<span class="ins-off">Hidden: ${off.map(esc).join(', ')}</span>`:'')
          +`</div>`;
      }
      const rend=p.renderable,s=st[p.id];
      const rows=Object.entries(p.meta||{}).filter(([k])=>k!=='name'&&k!=='role')
        .filter(([,v])=>v!==null&&v!==undefined)
        .map(([k,v])=>`<div class="ins-cell"><dt class="ins-k">${esc(k.replace(/_/g,' '))}</dt>`
          +`<dd class="ins-v">${esc(Array.isArray(v)?v.join(', ')
             :(typeof v==='number'?v.toLocaleString():String(v)))}</dd></div>`).join('');
      return `<div class="ins-top"><span class="ins-nm">${esc(p.label)}</span>
          <span class="ins-badge">${esc(p.role||p.kind)}</span></div>
        <dl class="ins-grid">
          <div class="ins-cell"><dt class="ins-k">kind</dt>
            <dd class="ins-v">${esc(kindLabel(p.kind))}</dd></div>
          <div class="ins-cell"><dt class="ins-k">declared type</dt>
            <dd class="ins-v">${esc(p.role||'-')}</dd></div>
          ${rows}
        </dl>
        ${rend?`<div class="ins-acts">
          <button class="v-bnd-act on" data-a="hl">Highlighted</button>
          <button class="v-bnd-act${s.hidden?' on':''}" data-a="hide">${s.hidden?'Show':'Hide'}</button>
          <button class="v-bnd-act${isolated?' on':''}" data-a="iso">Isolate</button>
          <label class="v-bnd-op">Opacity
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
          if(a==='iso'){isolated=!isolated;if(isolated)unhide(sel);paint();refit();}
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
        /* PICKING ANOTHER PART WHILE ISOLATED re-isolates on it, so it has to be
           unhidden the same way the Isolate button does it - that is how a reader goes from
           the coolant to the metal, and it is the path that was landing on an empty view. */
        c.onclick=()=>{sel=(sel===p.id)?null:p.id;
                       if(!sel||!p.renderable)isolated=false;
                       else if(isolated)unhide(sel);
                       paint();};
        g.appendChild(c);});
      chips.appendChild(g);});

    // THE THERMAL TABLE shows a part alone (or the whole model again) through the same state
    partsApi={isolate:(entity)=>{
      // by the part's own name OR by any surface that draws it, so a caller holding either the
      // declared patch ("ymin") or a drawn one ("solid/ymin") reaches the same part
      const p=entity?parts.find(x=>x.renderable&&(x.entity===entity
                                   ||x.entities.indexOf(entity)>=0
                                   ||(x.meta&&x.meta.name===entity))):null;
      if(p){sel=p.id;isolated=true;unhide(p.id);}else{sel=null;isolated=false;}
      paint();refit();},
      repaint:()=>{paint();refit();}};
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
    hintEl.textContent=on?'Click a spot on the mesh to mark it · Drag still rotates'
                         :(activeMetric?HEAT_HINT:MOUSE_HINT);}
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
    if(e.target&&e.target.closest&&e.target.closest('.v-pin,.v-tools,.v-legend,.v-probe,.v-heatctl'))return;
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
    // with marks placed, sending them is the next step, so it takes the accent
    submitBtn.classList.toggle('primary',flags.length>0);
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
        +'<span class="fx fx-edit" title="edit note">✎</span>'
        +'<span class="fx fx-del" title="remove">✕</span>';
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
    // GROUPED, the way the figures are read: how big the mesh is, what its cells are, how good
    // they are. A zero that only says "nothing is wrong" (no skewed faces, a skew fraction of 0)
    // is folded into the figure it qualifies instead of taking a cell of its own.
    const SIZE=[['cells','Cells'],['faces','Faces'],['points','Points'],['regions','Regions']];
    // ALL SEVEN checkMesh counts. Three of them had no label here, so a mesh with wedges in it
    // listed its hexahedra and said nothing about the rest.
    const TYPES=[['hexahedra','Hexahedra'],['polyhedra','Polyhedra'],['prisms','Prisms'],
                 ['pyramids','Pyramids'],['tetrahedra','Tetrahedra'],['wedges','Wedges'],
                 ['tet_wedges','Tet wedges']];
    const QUALITY=[['max_non_ortho','Max non-orthogonality','°'],['max_skewness','Max skewness',''],
                   ['max_aspect_ratio','Max aspect ratio','']];
    // THE SIZE RANGE, as lengths rather than volumes, because that is what a mesh is judged by.
    // Drawn as its own group: on a two-scale part these two numbers and their ratio ARE the
    // mesh's story, and buried among the quality rows they read as incidental.
    const SPAN=[['min_cell_m','Smallest cell'],['max_cell_m','Largest cell']];
    const FOLDED=['avg_non_ortho','skew_faces','skew_fraction','cell_size_ratio',
                  'internal_faces','small_determinant_cells','concave_cells',
                  'low_quality_tet_faces',
                  'near_wall_layers','near_wall_levels','near_wall_cell_m',
                  'interface_faces_fluid','interface_faces_solid'];
    const KNOWN=[...SIZE,...TYPES,...QUALITY,...SPAN].map(r=>r[0])
                 .concat(FOLDED,['units','engine']);
    const isNum=v=>typeof v==='number'&&isFinite(v);
    // integers read as counts; a measured value to two decimals, and a very small one in
    // exponent form rather than a run of zeroes
    const show=v=>!isNum(v) ? esc(String(v))
      : Number.isInteger(v) ? v.toLocaleString()
      : (Math.abs(v)>0&&Math.abs(v)<0.001) ? v.toExponential(2)
      : v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
    const has=k=>k in q&&q[k]!==null&&q[k]!==undefined;
    const stat=([k,l])=>`<div class="mx-c mx-stat" data-key="${k}"><div class="mx-v">${show(q[k])}</div>`
      +`<div class="mx-k">${l}</div></div>`;
    // cell types: each as its share of the cells, so a hex-dominant mesh reads at a glance
    const total=isNum(q.cells)&&q.cells>0?q.cells:0;
    const type=([k,l])=>{const v=q[k],pct=total&&isNum(v)?Math.max(0,Math.min(100,100*v/total)):null;
      return `<div class="mx-c mx-row mx-type" data-key="${k}"><div class="mx-k">${l}</div>`
        +`<div class="mx-v">${show(v)}</div>`
        +(pct===null?'':`<div class="mx-pct">${pct>0&&pct<1?'&lt;1':Math.round(pct)}%</div>`
          +`<div class="mx-bar"><i style="width:${pct.toFixed(1)}%"></i></div>`)+`</div>`;};
    const row=(k,l,unit,sub)=>`<div class="mx-c mx-row mx-q" data-key="${esc(k)}"><div class="mx-k">${esc(l)}</div>`
      +`<div class="mx-v">${show(q[k])}${unit?`<small>${unit}</small>`:''}</div>`
      +(sub?`<div class="mx-sub">${sub}</div>`:'')+`</div>`;
    // the share of faces that are skewed, when there are any
    const frac=isNum(q.skew_fraction)&&q.skew_fraction>0?`(${Number((q.skew_fraction*100).toPrecision(2))}%)`:'';
    const ofFaces=isNum(q.faces)?`of ${q.faces.toLocaleString()}`:'';
    const skewNote=()=>{
      if(!isNum(q.skew_faces))return frac?`Skewed: ${frac.slice(1,-1)} of faces`:'';
      if(q.skew_faces===0)return 'No skewed faces';
      return [`${q.skew_faces.toLocaleString()} skewed face${q.skew_faces!==1?'s':''}`,ofFaces,frac]
        .filter(Boolean).join(' ');};
    const quality=[];
    if(has('max_non_ortho'))quality.push(row('max_non_ortho','Max non-orthogonality','°',
      has('avg_non_ortho')?`Mean ${show(q.avg_non_ortho)}°`:''));
    else if(has('avg_non_ortho'))quality.push(row('avg_non_ortho','Mean non-orthogonality','°'));
    // skewness: the max with the skewed faces under it; without a max, the count and its share
    // stand on their own - nothing the engine reported is dropped
    if(has('max_skewness'))quality.push(row('max_skewness','Max skewness','',skewNote()));
    else if(has('skew_faces'))quality.push(row('skew_faces','Skewed faces','',[ofFaces,frac].filter(Boolean).join(' ')));
    else if(has('skew_fraction'))quality.push(row('skew_fraction','Skew fraction',''));
    if(has('max_aspect_ratio'))quality.push(row('max_aspect_ratio','Max aspect ratio',''));
    // CELLS A SOLVER CANNOT INTEGRATE OVER. checkMesh names these only when it finds some, so
    // the zero case says so rather than leaving a reader to wonder whether it was looked for.
    /* THE TWO THE EXTENDED CHECKS FOUND. Both are shown as a share of the mesh as well as a
       count, because that is the difference between a defect and a texture: 3,676,061 concave
       cells out of 241,984,911 is 1.5% and is the staircase the octree cut, while 22 faces with a
       bad tet decomposition out of 752,277,309 is a defect with a location. Neither can be drawn -
       the concave set alone is 862 MB against a 400,000-triangle highlight budget - so the number
       is the only place they appear. */
    if(has('concave_cells'))quality.push(row('concave_cells','Concave cells','',
      q.concave_cells===0?'None'
        :(isNum(q.cells)&&q.cells>0?`${(100*q.concave_cells/q.cells).toPrecision(2)}% of cells`:'')));
    if(has('low_quality_tet_faces'))quality.push(row('low_quality_tet_faces',
      'Low-quality tet faces','',
      q.low_quality_tet_faces===0?'None':'checkMesh calls this an error'));
    if(has('small_determinant_cells'))quality.push(row('small_determinant_cells',
      'Cells with small determinant','',
      q.small_determinant_cells===0?'None'
        :(isNum(q.cells)&&q.cells>0?`${(100*q.small_determinant_cells/q.cells).toPrecision(2)}% of cells`:'')));

    /* A LENGTH, read the way an engineer says it. The backend carries metres because that is the
       mesh's unit; nobody discusses a microchannel in metres. mm down to a micron, then um. */
    const len=v=>!isNum(v)?esc(String(v))
      :Math.abs(v)>=1e-3?`${(v*1e3).toLocaleString(undefined,{maximumSignificantDigits:4})}<small>mm</small>`
      :`${(v*1e6).toLocaleString(undefined,{maximumSignificantDigits:4})}<small>&micro;m</small>`;
    const spanRow=([k,l])=>`<div class="mx-c mx-row mx-q" data-key="${k}"><div class="mx-k">${l}</div>`
      +`<div class="mx-v">${len(q[k])}</div></div>`;
    const span=SPAN.filter(([k])=>has(k)).map(spanRow);
    // the ratio is the number that matters on a graded mesh: how many octree levels it spans
    if(span.length===2&&isNum(q.cell_size_ratio))
      span.push(row('cell_size_ratio','Size ratio','&times;',
        `the delivered mesh spans ${Math.log2(q.cell_size_ratio).toFixed(1)} octree levels`));

    /* AT THE WALL - the third of the six readings the customer asked for by name, "number of
       boundary layers in the fluid". The answer on a microchannel conjugate mesh is none, and the
       reason this used to give was MEASURED FALSE: it said an extruded layer would leave every
       interface face without its partner. generateBoundaryLayers with optimiseLayer 0 kept all
       14,477 interface faces identical on both sides to 4e-18 m, at 99.97% coverage. Layers do
       not break conformality, and telling a customer they do is telling them something untrue
       about their own mesh.
       What actually rules them out is that a prism stack does not FIT: in a 98 um passage at an
       affordable cell size the stack takes the quality to the bar (non-orthogonality 13.82 ->
       64.95 against checkMesh's 65). Across a channel that narrow, across-the-channel IS
       near-wall, which is why the cells-across row below is the real answer to their question.
       A bare "0" reads as a failure, so the reason and what stands in its place are on the row.
       Measured and written to the manifest since the engine was built; it never reached a reader
       because the metric filter in render/mesh_facts dropped it. */
    const wall=[];
    if(has('near_wall_layers'))wall.push(row('near_wall_layers','Boundary layers','',
      q.near_wall_layers===0
        ? 'none: a prism stack does not fit across a passage this narrow without taking the cell '
          +'quality to its limit, so the near-wall resolution is the cells across the channel'
        : ''));
    if(has('near_wall_cell_m'))wall.push(
      `<div class="mx-c mx-row mx-q" data-key="near_wall_cell_m"><div class="mx-k">Cell at the wall</div>`
      +`<div class="mx-v">${len(q.near_wall_cell_m)}</div>`
      +(has('near_wall_levels')
        ? `<div class="mx-sub">graded refinement instead: ${show(q.near_wall_levels)} level`
          +`${q.near_wall_levels===1?'':'s'} from the background cell</div>`
        : '')+`</div>`);
    else if(has('near_wall_levels'))wall.push(row('near_wall_levels','Levels to the wall',''));
    // the interface itself: equal counts on the two sides ARE the conformality
    if(has('interface_faces_fluid')){
      const a=q.interface_faces_fluid,b=q.interface_faces_solid;
      wall.push(row('interface_faces_fluid','Interface faces','',
        isNum(b)?(a===b?'the same on both sides':`${show(b)} on the metal side`):''));
    }

    // anything the backend declared that this build has no name for still reaches the user
    const other=Object.keys(q).filter(k=>!KNOWN.includes(k)).map(k=>row(k,k.replace(/_/g,' '),''));
    const sizes=SIZE.filter(([k])=>has(k)),types=TYPES.filter(([k])=>has(k));
    const group=(title,body)=>body?`<div class="mx-group"><div class="mx-gh">${title}</div>${body}</div>`:'';
    if(!sizes.length&&!types.length&&!quality.length&&!span.length&&!wall.length&&!other.length)return;
    host.innerHTML=`<div class="mx"><div class="mx-h"><span>Delivered mesh</span>`
      +(q.engine?`<span class="eng">${esc(q.engine)}</span>`:'')
      +(q.units?`<span class="eng">${esc(q.units)}</span>`:'')
      +`</div>`
      +(sizes.length?`<div class="mx-size">${sizes.map(stat).join('')}</div>`:'')
      +group('Cell size',span.join(''))
      +group('Cell types',types.map(type).join(''))
      +group('Quality',quality.join(''))
      +group('At the wall',wall.join(''))
      +group('Other',other.join(''))
      +`</div>`;
  })();

  /* WHAT A PATCH'S face_count MEANS WHEN THE MESH IS TOO BIG TO DRAW WHOLE.
     Past the viewer's budget the server merges cells FOR DRAWING and ships `reduced`, which
     carries what was actually delivered. face_count is then the DRAWN count, and printing it as
     the delivered one tells a reader their mesh is twenty times coarser than the one they paid
     for - measured on the first customer cold plate, 608,781 drawn against 13,022,400 built.
     So the per-patch number comes from `reduced` when there is one, and the line says plainly
     that what is on screen is a merged view and at what size. */
  /* AND WHAT IT MEANS WHEN THE MESH IS TOO BIG TO DRAW WHOLE BUT NOTHING WAS APPROXIMATED.
     The exact route merges only faces that lay flat in the same plane, so there is NO resolution
     to state - stating one would invite the reader to discount a shape that is exact. It states
     the collapse instead, which is the thing that is actually true of it. */
  const reduced=_draw.block;
  const facesOf=p=>{
    const d=reduced&&reduced.patches&&reduced.patches[p.name];
    const n=d?d.delivered_faces:(p.delivered_faces||p.face_count||p.tri_count||0);
    return `${p.name}: ${(n||0).toLocaleString()} faces`;
  };
  const drawnNote=!reduced?''
    :_draw.exact
      ?` · drawn exactly (${(+reduced.drawn_polygons).toLocaleString()} polygons for all`
        +` ${(+reduced.delivered_faces).toLocaleString()} faces, nothing approximated)`
      :` · drawn merged at ${(reduced.resolution_m*1000).toLocaleString(undefined,{maximumFractionDigits:2})} mm`
        +` (${reduced.drawn_triangles.toLocaleString()} of ${reduced.delivered_faces.toLocaleString()} faces on screen)`;
  const statusEl=document.getElementById('v-status-'+job);
  statusEl.textContent=thermal&&surf.is_mesh
    // a thermal model says what it is in its own words: its parts, its cells, its power
    ?'your delivered thermal mesh · '+(thermal.summary||'')
    // so does an FEA mesh: its elements, its level and its named faces
    :surf.fea&&surf.is_mesh?'your delivered FEA mesh · '+(surf.fea.summary||'')
    :(surf.is_mesh?'your delivered mesh · '
                :'input surface - the mesh itself could not be rendered · ')
    +(surf.patches||[]).map(facesOf).join(' · ')+drawnNote;
  statusEl.title=statusEl.textContent;   // one line on screen; the whole of it on hover

  /* A DELIVERED THERMAL MODEL - its parts table, its quality in words, its ready-made slices
     and its own files (viewer/thermal_results.js). A slice is the delivered mesh cut by a plane,
     region by region, as the payload ships it: shown, it stands in for the surfaces, each part in
     its own colour and the air in grey, the camera square to it. */
  const sliceActors={};
  (function thermalResults(){
    const host=document.getElementById('v-thermal-'+job);
    if(!thermal||!host)return;
    (thermal.slices||[]).forEach(s=>{
      sliceActors[s.name]=(s.pieces||[]).map(pc=>{
        const pd=vtk.Common.DataModel.vtkPolyData.newInstance();
        pd.getPoints().setData(b64f32(pc.points_b64),3);
        const _slicePolys=b64u32(pc.polys_b64);
        pd.getPolys().setData(_slicePolys);
        const mapper=vtk.Rendering.Core.vtkMapper.newInstance();mapper.setInputData(pd);
        const actor=vtk.Rendering.Core.vtkActor.newInstance();actor.setMapper(mapper);
        const c=sliceColour(thermal,pc.region);
        const pr=actor.getProperty();pr.setColor(c[0],c[1],c[2]);
        setEdges(pr,_countPolys(_slicePolys),[0.16,0.16,0.15]);
        pr.setAmbient(0.55);pr.setDiffuse(0.55);
        if(pr.setBackfaceCulling)pr.setBackfaceCulling(false);
        actor.setVisibility(false);if(actor.setPickable)actor.setPickable(false);
        ren.addActor(actor);
        return {actor,region:pc.region};});});
    function showSlice(name){
      sliceOn=name&&sliceActors[name]?name:null;
      Object.entries(sliceActors).forEach(([n,list])=>list.forEach(a=>a.actor.setVisibility(n===sliceOn)));
      if(!sliceOn){
        // back to the model's own view, framed on what the parts panel shows
        orient(cam,(upSel&&upSel.value)||'+z',VIEW_DIR);
        if(partsApi)partsApi.repaint();else rw.render();
        return;}
      if(partsApi)partsApi.repaint();
      const s=(thermal.slices||[]).find(x=>x.name===sliceOn);
      // square to the cut: looking along its normal, up the model's z (or y for a level cut),
      // framed on the window the slice was made for (around a hot part), else the whole cut
      const b=[Infinity,-Infinity,Infinity,-Infinity,Infinity,-Infinity];
      sliceActors[sliceOn].forEach(a=>{const ab=a.actor.getBounds();
        for(let k=0;k<6;k+=2){b[k]=Math.min(b[k],ab[k]);b[k+1]=Math.max(b[k+1],ab[k+1]);}});
      const clip=Array.isArray(s.clip_box)&&s.clip_box.length===6?s.clip_box:null;
      if(clip)for(let k=0;k<6;k+=2){
        const lo=Math.max(b[k],clip[k]),hi=Math.min(b[k+1],clip[k+1]);
        if(hi>lo){b[k]=lo;b[k+1]=hi;}}
      const c=[(b[0]+b[1])/2,(b[2]+b[3])/2,(b[4]+b[5])/2],n=s.normal||[0,0,1];
      cam.setFocalPoint(c[0],c[1],c[2]);
      cam.setPosition(c[0]+n[0],c[1]+n[1],c[2]+n[2]);
      cam.setViewUp(...(Math.abs(n[2])>0.9?[0,1,0]:[0,0,1]));
      _clamping=true;ren.resetCamera(b);_clamping=false;rw.render();}
    host.innerHTML=thermalResultsHtml(job,thermal);
    thermalDownloads(document.querySelector('#viewer-'+job+' .v-dl'),job,thermal);
    const api=bindThermalResults(host,job,thermal,{
      isolate:name=>{if(sliceOn)showSlice(null);if(partsApi)partsApi.isolate(name);},
      slice:name=>showSlice(name)});
    window._vdbg=window._vdbg||{};
    window._vdbg[job+':thermal']={slice:name=>{api&&api.slice(name);showSlice(name);},
      sliceOn:()=>sliceOn,slices:()=>Object.fromEntries(Object.entries(sliceActors).map(([n,l])=>[n,l.length])),
      isolate:name=>{if(partsApi)partsApi.isolate(name);api&&api.pick(name);},
      visible:()=>entries.filter(e=>e.actor.getVisibility()).map(e=>e.patch),
      text:()=>host.innerText};
  })();

  /* A DELIVERED FEA MESH - its quality against each solver limit, the through-wall check, the
     plan in words, its hot spots and every named face (viewer/fea_results.js). A face picked there
     is shown alone through the parts panel; a hot spot also gets a marker where it is. */
  (function feaResults(){
    const fea=surf.fea||null,host=document.getElementById('v-fea-'+job);
    if(!fea||!host)return;
    let marker=null;
    function point(at){
      if(marker){ren.removeActor(marker);marker=null;}
      const S=vtk.Filters&&vtk.Filters.Sources&&vtk.Filters.Sources.vtkSphereSource;
      if(at&&at.length===3&&S){
        const src=S.newInstance({center:at,radius:Math.max(diag*0.012,1e-9),thetaResolution:20,phiResolution:14});
        const mapper=vtk.Rendering.Core.vtkMapper.newInstance();mapper.setInputConnection(src.getOutputPort());
        marker=vtk.Rendering.Core.vtkActor.newInstance();marker.setMapper(mapper);
        const pr=marker.getProperty();pr.setColor(_SEL[0],_SEL[1],_SEL[2]);pr.setAmbient(0.6);
        if(marker.setPickable)marker.setPickable(false);
        ren.addActor(marker);}
      rw.render();}
    host.innerHTML=feaResultsHtml(job,fea);
    feaDownloads(document.querySelector('#viewer-'+job+' .v-dl'),job,fea);
    const api=bindFeaResults(host,job,fea,{
      isolate:name=>{if(partsApi)partsApi.isolate(name);},
      point:at=>point(at)});
    window._vdbg=window._vdbg||{};
    window._vdbg[job+':fea']={isolate:name=>{if(partsApi)partsApi.isolate(name);api&&api.pick(name);},
      point:at=>point(at),marker:()=>!!marker,
      visible:()=>entries.filter(e=>e.actor.getVisibility()).map(e=>e.patch),
      text:()=>host.innerText};
  })();

  /* support/debug hook (read-only-ish; used by headless verification) */
  window._vdbg=window._vdbg||{};
  window._vdbg[job]={
    caps:()=>capRange(),
    dist:()=>{const f=cam.getFocalPoint(),q=cam.getPosition();
      return Math.hypot(q[0]-f[0],q[1]-f[1],q[2]-f[2]);},
    gridPx:()=>{const f=cam.getFocalPoint(),q=cam.getPosition();
      const d=Math.hypot(q[0]-f[0],q[1]-f[1],q[2]-f[2])||1e-12;
      return stats?stats.typ*(apiRW.getSize()[1]/(2*Math.tan(cam.getViewAngle()*Math.PI/360)))/d:null;},
    /* WHAT IS ACTUALLY ON SCREEN, against what the payload carries. The status line states the
       payload's own totals, which is the right thing for it to state and the wrong thing to
       measure a view by: a conjugate payload may carry both sides of an interface and draw one,
       so "did this change make the viewer draw more" is a question only the visible set answers. */
    drawn:()=>{const on=entries.filter(e=>e.actor.getVisibility());
      return {patches:on.map(e=>e.patch).sort(),
              polygons:on.reduce((a,e)=>a+e.nCells,0),
              triangles:on.reduce((a,e)=>a+e.nTris,0),
              payloadPolygons:entries.reduce((a,e)=>a+e.nCells,0),
              payloadTriangles:entries.reduce((a,e)=>a+e.nTris,0)};},
    fitDist:()=>_fitDist,stats:stats,diag:diag,size:()=>apiRW.getSize(),
    markers:()=>flags.length,up:()=>cam.getViewUp().slice(),
    upAxis:()=>upSel?upSel.value:'+z',setUp:axis=>setUp(axis),
    view:()=>({focal:cam.getFocalPoint().slice(),position:cam.getPosition().slice(),
               dir:cam.getDirectionOfProjection().slice()}),
    render:()=>rw.render()};

  /* PARAVIEW EXPORT - the same boundary and the same per-face numbers as a legacy VTK file,
     built by the API from the stored payload. Fetched with the API headers (a plain link would
     carry none) and handed to the browser as a download; the button says what it is doing. */
  (function vtkExport(){
    // a polyMesh, or the triangle surface of a tetrahedral engine's DELIVERED mesh (VMTK, Gmsh)
    if(surf.kind!=='polymesh'&&!(surf.kind==='stl'&&surf.is_mesh===true))return;
    const bar=document.querySelector('#viewer-'+job+' .v-dl');if(!bar)return;
    const a=document.createElement('a');a.className='v-dlbtn';a.href='#';a.id='v-vtk-'+job;
    /* WHAT THE FILE WILL ACTUALLY CONTAIN. `vtk_export` writes a quality SCALAR only for a
       metric it finds PER-FACE values for, in surface.quality_fields.patches. A MERGED
       surface has none: one drawn triangle stands for about 34 delivered faces and those
       faces do not share a value, so the payload carries the summary metrics and no arrays.
       The export is then patch_id and the geometry, and the old label promised a file the
       user would not get. Measured across the roster 2026-11-08: 7 of the 9 pages are
       reduced and exported patch_id alone; only the two section pages, which are unreduced,
       carried non_ortho, skewness and aspect_ratio. */
    const perFace=Object.keys((surf.quality_fields||{}).patches||{}).length>0;
    a.title=perFace?'the boundary with its per-face quality fields, for ParaView'
                   :'the boundary and its patch ids, for ParaView. This surface is merged, so '
                    +'it carries no per-face quality - a section page does.';
    a.innerHTML='<b>ParaView</b><span>'+(perFace?'.vtk with quality fields':'.vtk, patches only')+'</span>';
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

  /* DETAIL - the real cells of one place, when the drawn surface is merged.
     `fit_to_budget` clusters cells so a delivered mesh fits what a browser can carry: the first
     customer cold plate delivers 13,022,400 boundary faces on the coolant alone against a budget
     of 800,000 triangles, so about sixteen delivered faces become one drawn one. The zoom still
     lets a reader go right in, where the surface under their cursor is a merged approximation of
     the cells rather than the cells - every number they are given is the mesh's own and the
     surface is not. This asks the backend for a ball of the DELIVERED boundary around wherever
     the camera is looking, and swaps it in for the merged one while it is on, because two
     coincident surfaces at different resolutions fight for the same depth and neither reads.
     Off again and the merged actors come back exactly as they were. */
  /* INSPECT - the real cells of the place you CLICK.
     `fit_to_budget` clusters cells so a delivered mesh fits what a browser can carry: the first
     customer cold plate delivers 13,022,400 boundary faces on the coolant alone against a budget
     of 800,000 triangles, so about sixteen delivered faces become one drawn one. The zoom still
     lets a reader go right in, where the surface under their cursor is a merged approximation of
     the cells rather than the cells - every number they are given is the mesh's own and the
     surface is not. This asks the backend for a ball of the DELIVERED boundary and swaps it in for
     the merged one while it is on, because two coincident surfaces at different resolutions fight
     for the same depth and neither reads.

     WHAT CHANGED, AND WHY, after the owner of this product said he could not tell what the old
     button did. It was one control called "Detail", built `className='v-dlbtn'` and appended to
     `.v-dl` - the DOWNLOADS bar - so the instrument that reads the mesh was dressed as a download
     and filed next to ParaView. Three faults, none of them the cut itself:

       1. IT LOOKED LIKE A DOWNLOAD. Fixed by giving it its own class and its own home: the
          overlay on the canvas, with every other control that changes what the canvas shows.
       2. IT CUT WHERE THE CAMERA POINTED. The centre was `cam.getFocalPoint()`, so to read a
          place you had to first aim the entire camera at it. Now you click it. `pickCell` already
          existed for the region markers and the heatmap probe; this is its third reader.
       3. IT NEVER SAID HOW BIG ANYTHING WAS. On a part whose passages are 100-300 um and whose
          envelope is 170 mm, a reader with no legend cannot tell a channel from a fillet. There
          is now a size bar and a line that says what a pixel is worth and whether the surface is
          the mesh or the stand-in for it.

     AND THE CUT NO LONGER DIES ON A CAMERA MOVE. The old window cleared itself on the wheel,
     correctly, because its ball was cut where the camera WAS - move the camera and the ball was
     somewhere else. A ball cut at a point the reader chose is still that point's ball from any
     angle, so it now survives the camera and you can orbit the real cells. That is the whole
     reason to want them. */
  (function inspectWindow(){
    if(surf.kind!=='polymesh')return;
    if(!window.vtk)return;
    const box=document.getElementById('v-insp-box-'+job);if(!box)return;
    /* `a` IS THE ARM BUTTON AND `lbl` ITS READOUT, under the names the draw loop below already
       uses. The loop is unchanged and well tested; renaming its two outputs to suit a new shell
       would be churn in the one part of this window that never had a defect. */
    const a=document.getElementById('v-insp-arm-'+job);
    const lbl=document.getElementById('v-insp-read-'+job);
    const dial=document.getElementById('v-insp-dial-'+job);
    const rEl=document.getElementById('v-insp-r-'+job);
    const was='click the mesh';
    let shown=[];              // the detail's own actors
    let shownNames=[];         // and what each one is, so what was drawn can be read back
    let shownPainted=0;        // how many of them carry the metric's colours rather than a tint
    let shownMetric='';        // and which metric that is, so a test can tell grey from grey
    let hidden=[];             // the merged actors this turned off
    let centre=null;           // the point the reader picked; null until they pick one
    let armed=false;
    function clear(){
      shown.forEach(ac=>ren.removeActor(ac));shown=[];shownNames=[];
      shownPainted=0;shownMetric='';centre=null;
      hidden.forEach(h=>{
        // older shape (an actor) and the ghost shape (actor + what it was), so a half-applied
        // state from a reload cannot leave a surface stuck at a tenth opacity
        const en=h.en||h,pr=en.actor.getProperty();
        if(h.en){if(pr.setOpacity)pr.setOpacity(h.op);
                 if(pr.setEdgeVisibility)pr.setEdgeVisibility(h.ev);}
        else en.actor.setVisibility(true);});
      hidden=[];
      applyClip();applyXray();
      // aria-pressed is the MODE and setArmed owns it. It used to be set by a successful cut and
      // removed by this, so clearing one window turned the button off while the mode was still
      // on - the cursor stayed a crosshair and the next click cut again from a control that
      // looked idle.
      lbl.textContent=was;rw.render();legendSoon();}
    /* HOW BIG A BALL: the dial, geometric from four of the finest cells to half the part (see
       inspect.dialRadius). It used to be half the view's height, which tied the thing being read
       to the thing doing the reading - zoom to see the cells and the window you were reading
       changed size under you. */
    const fine=4*((stats&&stats.p10>0)?stats.p10:diag*0.002);
    function radiusNow(){return dialRadius(dial?dial.value:38,{fine,diag});}
    function showR(){if(rEl)rEl.textContent=fmtLen(radiusNow());}
    showR();

    /* FOLLOW: THE CUT TRACKS THE CAMERA.

       WHY THIS IS NOT A REVERSAL. The header above records that this window USED to cut at
       `cam.getFocalPoint()` and that it was changed to a click, because "to read a place you had
       to first aim the entire camera at it". That reasoning stands and the click is untouched.
       What was wrong was camera-cutting as the ONLY way in, not camera-cutting as such. Asked for
       "show everything, no corners cut", clicking one ball at a time is the corner being cut.

       WHAT IT CANNOT DO, said here so nobody tries. The whole drawn boundary at 1:1 is 19,599,615
       faces, and at the measured ~92 B of VRAM per delivered face that is about 1.8 GB - the
       context died below that with edges on (see _EDGE_MAX_POLYS). There is no setting that draws
       all of it at once on this class of machine. But a reader only ever LOOKS at one
       neighbourhood, and `boundary_index` was built for exactly that: a ball becomes the buckets
       it touches and nothing else is read. So this draws all of what is in front of you, at 1:1,
       and refreshes when you stop moving. Every cell is reachable; they are just not all on
       screen at once, which they could not be anyway at any screen size.

       WHEN IT ENGAGES is decided by how big a cell is ON SCREEN, not by a distance threshold,
       because the first is what the reader can actually see and the second depends on the part.
       `gridPx` already computes it. Below about 1.5 px a real cell is smaller than the pixel that
       would draw it: the merged surface is then the honest thing to show, and the round trip
       would be work nobody can see. The readout says which of the two states it is in and why,
       rather than looking idle. */
    const FOLLOW_MIN_PX=1.5;
    const FOLLOW_SETTLE_MS=260;
    //: How many times the window may halve itself down to fit. Two passes of the area rule land
    //: inside the cap from 77x over it, which is the worst case measured; the third is slack.
    const FOLLOW_FIT_PASSES=3;
    const fol=document.getElementById('v-insp-follow-'+job);
    let following=false,folTimer=null,folBusy=false,folKey='';
    let lastInRange=0,lastReturned=0,folFitR=0;
    function camDist(){const f=cam.getFocalPoint(),q=cam.getPosition();
      return Math.hypot(q[0]-f[0],q[1]-f[1],q[2]-f[2])||1e-12;}
    function viewRadius(){return camDist()*Math.tan(cam.getViewAngle()*Math.PI/360);}
    /* WHAT IS UNDER THE MIDDLE OF THE SCREEN, or null if that is empty space. `pickCell` reads
       nothing from the event but clientX and clientY, so the centre can be handed to it without
       a real one. It answers through a callback that is simply not called on a miss, hence the
       timeout: a Follow that never resolved would hold the busy lock for good. */
    function pickCentre(){
      return new Promise(function(res){
        let done=false;
        const fin=v=>{if(!done){done=true;res(v);}};
        try{
          const r=host.getBoundingClientRect();
          pickCell({clientX:r.left+r.width/2,clientY:r.top+r.height/2},
                   function(en,cid){fin(cellCenter(en,cid));});
        }catch(_e){fin(null);}
        setTimeout(function(){fin(null);},900);});}
    function cellPx(){
      if(!stats||!(stats.typ>0))return 0;
      return stats.typ*(apiRW.getSize()[1]/(2*Math.tan(cam.getViewAngle()*Math.PI/360)))/camDist();}
    async function followNow(){
      if(!following||folBusy)return;
      const px=cellPx();
      if(px<FOLLOW_MIN_PX){
        if(shown.length)dropShown();
        folKey='';
        lbl.textContent='zoom in for real cells - '+px.toFixed(1)+' px per cell';
        rw.render();return;}
      /* THE SAME VIEW IS NOT RE-FETCHED. An orbit that ends where it started, or a render
         triggered by something other than the camera, would otherwise cost a round trip and a
         redraw for an identical answer. */
      const hit=await pickCentre();
      const f=hit||cam.getFocalPoint(),r0=viewRadius();
      /* KEYED ON THE CAMERA, not on `f`. `f` is the point under the middle of the screen, which
         is where the cut is centred; the key is what the debounce and the re-ping in `finally`
         both compare, and that one has to describe the camera or the two never agree. They did
         not, once the screen-centre pick landed: every fit then scheduled another and none ever
         got past its first pass. */
      const fp=cam.getFocalPoint();
      const key=fp[0].toFixed(7)+','+fp[1].toFixed(7)+','+fp[2].toFixed(7)+'|'+r0.toFixed(7);
      if(key===folKey)return;
      folKey=key;folBusy=true;
      try{
        /* SHRINK UNTIL IT FITS, rather than hand back a sample and ask the reader to narrow it.
           Faces on a surface go as radius squared, so sqrt(returned / in_range) is the factor
           that makes the next ball fit. 0.92 is slack against the ball not being a sphere's
           worth of surface. */
        /* A BRACKET, NOT A FORMULA. `lo` is the largest radius known to fit, `hi` the smallest
           known to truncate, and the next guess is their geometric mean. Face density on this
           part is not uniform - measured at one point, 6 mm holds 2,804 faces and 19.42 mm holds
           3,482,098, because the larger ball reaches the channel array - so any rule that infers
           a radius from a count assumes something false. A bracket assumes nothing. */
        let r=r0,at=[f[0],f[1],f[2]],capSeen=0;
        let lo=0,hi=Infinity,bestR=0;
        for(let pass=0;pass<FOLLOW_FIT_PASSES;pass++){
          await recut(at,r);
          const last=pass===FOLLOW_FIT_PASSES-1;
          if(lastReturned===0){
            /* NOTHING THERE. Tested before the generic guard below, which used to catch this
               first and leave the message unreachable. */
            if(lastInRange===0){
              lbl.textContent=hit?'no cells in view':'nothing under the middle of the view';
              rw.render();}
            break;}
          if(lastInRange>lastReturned){
            capSeen=lastReturned;hi=r;
            if(last){
              /* NEVER LEAVE A SAMPLE ON SCREEN. Go back to the biggest ball that came back
                 whole, if one was found. */
              if(bestR>0){await recut(at,bestR);r=bestR;}
              break;}
            const nr=(lo>0)?Math.sqrt(lo*hi):r*0.92*Math.sqrt(lastReturned/lastInRange);
            if(!(nr>0)||nr<1e-9||nr>=r)break;
            r=nr;continue;}
          /* THE WHOLE BALL CAME BACK. Record it, and if it is far under the cap and there is a
             known-truncating radius above, try the geometric mean of the two. */
          lo=r;bestR=r;
          if(last)break;
          if(capSeen>0&&hi<Infinity&&lastReturned<capSeen*0.35){
            const nr=Math.sqrt(lo*hi);
            if(nr>r*1.05&&nr<hi*0.999){r=nr;continue;}}
          break;}
        folFitR=r;
      }
      catch(_e){folKey='';}
      finally{
        folBusy=false;
        /* WHERE THE CAMERA IS NOW, against where this fit was for. A fit is up to three round
           trips and the reader keeps scrolling through them; every ping that arrived in that
           window was dropped by the busy guard, so without this the last one - the one they
           stopped on - is the one that never gets drawn. */
        const g=cam.getFocalPoint(),gr=viewRadius();
        const now=g[0].toFixed(7)+','+g[1].toFixed(7)+','+g[2].toFixed(7)+'|'+gr.toFixed(7);
        if(following&&now!==folKey)folPing();}}
    function folPing(){
      if(!following)return;
      if(folTimer)clearTimeout(folTimer);
      folTimer=setTimeout(function(){folTimer=null;followNow();},FOLLOW_SETTLE_MS);}
    function setFollow(on){
      following=!!on;
      if(fol){fol.setAttribute('aria-pressed',following?'true':'false');
              fol.classList.toggle('on',following);}
      if(following){setArmed(false);setStream(false);setDeep(false);folKey='';followNow();}
      else{folKey='';if(folTimer){clearTimeout(folTimer);folTimer=null;}
           /* `clear`, NOT `dropShown`. `dropShown` removes what the mode drew and leaves
              `hidden` alone, so turning the mode off used to leave the merged surface ghosted at
              a tenth opacity for good. `clear` is the one that also puts that surface back. */
           clear();}}
    if(fol){
      fol.onclick=function(){setFollow(!following);};
      if(cam.onModified)cam.onModified(folPing);}

    /* GHOST ONCE PER OPEN WINDOW, not once per cut. `recut` drops the drawn actors but
       leaves `hidden` alone, so a second cut used to ghost the already-ghosted entries and
       record GHOST_OPACITY as their true opacity - after which closing the window left the
       merged surface at a tenth opacity for good, with nothing on screen to say why.

       THE REST OF THE MESH STAYS, AS A GHOST. It used to be switched off outright, on the
       grounds that two coincident surfaces at different resolutions fight for the same depth
       and neither reads. True, and the wrong trade: what a reader got was a scrap of cells
       floating in black with nothing around it and no way to tell where on a 178 mm plate
       they were looking. Asked what he was getting from it, the owner of this product said
       exactly that. At a tenth opacity and with its edges off the merged surface cannot
       compete with the cut - the cut is opaque and wins the depth test wherever they touch -
       but it still says where the cut IS. */
    function ghostOnce(){
      if(hidden.length)return;
      entries.forEach(en=>{
        if(!en.actor.getVisibility())return;
        const pr=en.actor.getProperty();
        hidden.push({en,op:pr.getOpacity?pr.getOpacity():1,
                     ev:pr.getEdgeVisibility?pr.getEdgeVisibility():true});
        if(pr.setOpacity)pr.setOpacity(GHOST_OPACITY);
        if(pr.setEdgeVisibility)pr.setEdgeVisibility(false);});}

    /* ONE WINDOW, FETCHED. Split out of `cut` so a tile can be asked for without taking the busy
       flag, the label and the camera move, which belong to a reader's own click and not to the
       thirty-odd tiles of a streaming pass. The refusal comes back as words because the backend
       refuses in words - too big to read, no workspace left on this machine - and the words are
       the useful part. */
    async function fetchTile(at,r){
      const q=`x=${at[0]}&y=${at[1]}&z=${at[2]}&radius=${r}`;
      const res=await fetch(`/api/v1/simulation/${job}/detail?${q}`,{headers:headers()});
      if(!res.ok){
        let why='';try{why=((await res.json())||{}).detail||'';}catch(_e){why='';}
        return {ok:false,why:why?String(why).slice(0,90):('detail '+res.status)};}
      return {ok:true,got:await res.json()};}

    /* ONE WINDOW'S PATCHES, DRAWN, accumulating into `shown`. `ix` says which colour a patch
       takes: the click path leaves it alone and gets the route's own order, the stream passes a
       name-keyed one so a patch is the same colour in every tile - the route sends only the
       patches that have a face in range, so tile order is not patch order. `edgeOff` is the
       whole-screen edge budget described below. */
    function drawCut(got,ix,edgeOff){
      let painted=0,paintedLabel='',paintedMetric='';
      (got.patches||[]).forEach((p,i)=>{
        const pd=vtk.Common.DataModel.vtkPolyData.newInstance();
        pd.getPoints().setData(b64f32(p.points_b64),3);
        pd.getPolys().setData(b64u32(p.polys_b64));
        const mapper=vtk.Rendering.Core.vtkMapper.newInstance();mapper.setInputData(pd);
        const ac=vtk.Rendering.Core.vtkActor.newInstance();ac.setMapper(mapper);
        const base=_PATCH_COLS[(ix?ix(p,i):i)%_PATCH_COLS.length],pr=ac.getProperty();
        pr.setColor(base[0],base[1],base[2]);
        /* THE COLOURS A CUT CAN HAVE THAT THE MAIN SURFACE CANNOT. Nothing here is merged -
           one delivered face is one polygon, in the order the route packed it - so a per-face
           number is a per-polygon number and painting it is honest. On the drawn surface it
           is not: the mesh is reduced to fit the triangle budget, one drawn face stands for
           about sixteen delivered ones, and a colour on it was measured on none of them.
           The values are checkMesh's own, and the bar comes with them: skewness is judged
           against 20 here, checkMesh's bar for a BOUNDARY face, not the internal 4. */
        const qb=p.quality_b64||{},qm=p.quality_metrics||{};
        /* WHICH METRIC. The one the reader picked, when they have picked one. When they have
           not - and on the meshes this exists for they CANNOT, because the payload carries no
           metrics block to make controls out of - the first the cut brought, in the order the
           route sends them. A colour nobody can reach is not a feature, and the button below
           names which metric it is showing so neither case is a guess. */
        const dm=(activeMetric&&qb[activeMetric])?activeMetric
                 :['non_ortho','skewness','aspect_ratio'].find(k=>qb[k]&&qm[k]);
        if(dm&&qb[dm]&&qm[dm]){
          const v=b64f32(qb[dm]);
          const rp=v.length===p.face_count?rampOf(qm[dm]):null;
          if(rp){
            const rgb=new Uint8Array(v.length*3);
            for(let k=0;k<v.length;k++)rp.color(v[k],rgb,k*3);
            pd.getCellData().setScalars(vtk.Common.Core.vtkDataArray.newInstance(
              {name:'quality',values:rgb,numberOfComponents:3}));
            mapper.setScalarModeToUseCellData();mapper.setColorModeToDirectScalars();
            if(mapper.setInterpolateScalarsBeforeMapping)
              mapper.setInterpolateScalarsBeforeMapping(false);
            mapper.setScalarVisibility(true);
            painted++;paintedLabel=qm[dm].label||dm;paintedMetric=dm;}}
        setEdges(pr,p.face_count||_countPolys(b64u32(p.polys_b64)));
        /* EDGES ARE A WHOLE-SCREEN BUDGET, NOT A PER-ACTOR ONE. `setEdges` caps one actor at
           750,000 polygons and every tile is far under that, but the stream puts dozens of them
           up and edge drawing is what loses the WebGL context - measured on this viewer at about
           1.9 million faces. So past one actor's worth of faces IN TOTAL the stream draws its
           tiles without edges, and takes the edges off the ones already up. */
        if(edgeOff&&pr.setEdgeVisibility)pr.setEdgeVisibility(false);
        pr.setAmbient(0.30);pr.setDiffuse(0.78);
        if(pr.setBackfaceCulling)pr.setBackfaceCulling(false);
        ren.addActor(ac);shown.push(ac);shownNames.push(String(p.name||''));});
      return {painted,paintedLabel,paintedMetric};}

    async function cut(at,rOverride){
      if(a.dataset.busy)return;
      a.dataset.busy='1';lbl.textContent='reading…';
      try{
        const r=(rOverride>0)?rOverride:radiusNow();
        centre=at.slice();
        const hit=await fetchTile(at,r);
        if(!hit.ok){lbl.textContent=hit.why;return;}
        const got=hit.got;
        /* WHAT THE BALL ACTUALLY HELD, kept so Follow can size its next one. The click path
           ignores these - a reader who picked a radius meant it. */
        lastInRange=+got.faces_in_range||0;lastReturned=+got.faces_returned||0;
        if(!(got.patches||[]).length){lbl.textContent='no cells there';return;}
        ghostOnce();
        const _dc=drawCut(got);
        const painted=_dc.painted,paintedLabel=_dc.paintedLabel,paintedMetric=_dc.paintedMetric;
        // SAID, NOT IMPLIED. A reader given 150,000 of 404,725 faces with no word about it reads
        // the gap as mesh that is not there.
        // SAID, so a reader knows whether the colours they are looking at are the metric they
        // picked or the flat palette. A cut of a mesh delivered before the fields were measured
        // draws grey, and grey that is not named reads as a clean mesh.
        shownPainted=painted;shownMetric=painted?paintedMetric:'';
        const col=painted?` · ${paintedLabel}`:'';
        // "zoom in" was the wrong instruction once the window stopped being the camera's: the way
        // to see all of a truncated window is to narrow the DIAL, which is what cutText says.
        lbl.textContent=cutText(got)+col;
        applyClip();
        rw.render();
        legendSoon();          // the legend now says "real cells"; it only watched the camera
        /* FRAME A CUT THAT IS TOO SMALL TO SEE. The window is a ball of a chosen size, and on a
           70 mm chip a 2.5 mm ball is forty pixels: the merged surface goes, the cells arrive,
           and what a reader sees is the mesh disappearing. So when the cut would come in under
           about a fifth of the view, the camera glides in and frames it - which is also the
           thing a reader was going to do by hand, badly, with a scroll wheel. Over that size it
           is left alone, because a reader who asked for a wide window meant to keep their view. */
        const onScreen=(2*r)/Math.max(perPx(),1e-12);
        if(onScreen<0.18*(host.clientHeight||600))glideTo(at,distFor(r));}
      catch(err){lbl.textContent='could not read there';console.warn('detail',err);}
      finally{delete a.dataset.busy;}}

    /* ONE CUT AT A TIME. `cut` adds actors; asked twice without this it would stack a second
       ball's cells over the first and leave both in `shown`, so the count on screen would be the
       sum of two windows and `clear` would be the only way back. The merged actors stay hidden
       across a re-cut on purpose - they are what the cut stands in front of. */
    function dropShown(){
      shown.forEach(ac=>ren.removeActor(ac));shown=[];shownNames=[];
      shownPainted=0;shownMetric='';}
    async function recut(at,r){dropShown();await cut(at,r);}

    /* ─── STREAMING: COVER THE SCREEN WITH REAL CELLS ──────────────────────────────────────────
       Inspect reads one ball where a reader clicks; Follow moves that ball to the middle of the
       view. Both answer "what is the mesh like HERE", and neither answers "show me the mesh" -
       which is the question that was actually being asked. The drawn surface answers it, but the
       drawn surface is MERGED: about sixteen delivered faces to one drawn polygon on this plate,
       so nothing a reader looks at on it is a delivered face.

       THE BUDGET IS THE SCREEN'S, NOT THE MESH'S, and that is the whole idea rather than a
       compromise. A 4K display is 8,294,400 pixels, so even at one pixel per face a screen runs
       out at eight million faces, and at a readable two or three pixels a face the ceiling is
       around two million - for ANY mesh. So a 317-million-cell plate and a 300-thousand-cell
       coupon stream exactly the same way; what differs is how much of each one screen holds, and
       the readout says which.

       HOW IT COVERS. The screen is cut into tiles and a ball of real cells is read into each one.
       Where the surface IS, per tile, comes from the drawn mesh's own cell centres projected in
       the browser - not from the renderer's picker, which answers one screen position per round
       trip and only for whatever is drawn in front, and this pass draws in front of itself, so by
       its second tile the picker would be reading its own output. A tile the route truncates is
       NOT drawn: it is split into four and each quarter read instead. Four splits take 4x4 to
       32x32, and whatever the budget could not reach is counted and said out loud. */
    //: how many delivered faces one pass puts on screen. 1.5 million is about 3 million triangles,
    //: and 3.81 million triangles is where this viewer was measured drawing at 55.2 fps.
    const STREAM_MAX_FACES=1500000;
    //: and how many round trips it will spend getting them. Each is capped server side at
    //: detail_window.MAX_RETURN_FACES, so a view that needs more tiles than this is a view too
    //: wide to read at real-cell resolution - which the readout then says.
    const STREAM_MAX_TILES=96;
    const STREAM_POOL=4;              // round trips in flight at once
    const STREAM_N0=4;                // the first grid: 4x4 of the screen
    const STREAM_GRID=32;            // and the finest it can split to, so three splits
    const STREAM_LEVELS=1+Math.round(Math.log2(STREAM_GRID/STREAM_N0));
    const STREAM_SETTLE_MS=320;
    const STREAM_SAMPLE_CAP=120000;   // cell centres kept per region, to find the front with
    /* PIXELS PER DELIVERED CELL before the mode will open, which is NOT the gate Follow uses.
       Follow's is 1.5 px of `stats.typ`, and typ is the AREA-WEIGHTED median face - on this
       delivery 100.631 um, exactly 4.00x the 25.149 um tenth percentile, which is the pitch-8
       lattice cell to within 0.03%. So Follow's gate is 0.37 px per lattice cell: not a cell
       anyone can see, and a view holding far more faces than the budget can draw. The budget is
       spent on face COUNT and the count is dominated by the fine faces, so this gate is on p10.

       MEASURED through the route the page uses, on four real points of the delivered plate, with
       a level-0 tile's ball at each candidate:
           px/cell   view height   tile ball   faces returned   whole   slowest
             1.5       16.77 mm     3.954 mm         18,199      4/4     1.39 s
             2.0       12.57 mm     2.966 mm          5,761      4/4     0.50 s
             3.0        8.38 mm     1.977 mm          1,866      4/4     0.23 s
       At 1.5 every tile comes back WHOLE at the first grid and a 4x4 pass draws about 291,000
       faces, five times inside the budget - so the threshold was right and the statistic under
       it was not. */
    const STREAM_MIN_PX=1.5;
    /* and how many pixels one DELIVERED cell covers, as against `cellPx`, which answers the same
       question about the drawn surface's own merged polygons. */
    function realCellPx(){
      const e=(stats&&stats.p10>0)?stats.p10:0;
      if(!(e>0))return cellPx();     // a payload with no cell_stats: fall back to the drawn one
      return e*(apiRW.getSize()[1]/(2*Math.tan(cam.getViewAngle()*Math.PI/360)))/camDist();}
    const str=document.getElementById('v-insp-stream-'+job);
    let streaming=false,strTimer=null,strBusy=false,strKey='';
    let strFaces=0,strTiles=0,strRead=0,strLeft=0,strDense=0;

    /* THE CAMERA'S THREE AXES, SQUARED UP. `getViewUp` is not promised to be square to the view
       direction - an orbit leaves it off - so the part of it running along the view is taken out
       before it is used as a screen axis, and the third axis is their cross product. */
    function camBasis(){
      const p=cam.getPosition(),f=cam.getFocalPoint(),u=cam.getViewUp();
      let dx=f[0]-p[0],dy=f[1]-p[1],dz=f[2]-p[2];
      const dl=Math.hypot(dx,dy,dz)||1;dx/=dl;dy/=dl;dz/=dl;
      const k=u[0]*dx+u[1]*dy+u[2]*dz;
      let ux=u[0]-dx*k,uy=u[1]-dy*k,uz=u[2]-dz*k;
      const ul=Math.hypot(ux,uy,uz)||1;ux/=ul;uy/=ul;uz/=ul;
      return {p,d:[dx,dy,dz],u:[ux,uy,uz],
              r:[dy*uz-dz*uy,dz*ux-dx*uz,dx*uy-dy*ux]};}

    /* THE DRAWN MESH'S OWN CELL CENTRES, thinned, cached on the region. This is the one thing a
       streaming pass needs that the server cannot give it: which point to ask about, per tile.
       They are already in the browser - the merged surface was built from them - so the whole
       question is answered without a round trip. */
    function samplesOf(en){
      if(en.strPts)return en.strPts;
      const step=Math.max(1,Math.ceil(en.nCells/STREAM_SAMPLE_CAP));
      const out=new Float32Array(Math.ceil(en.nCells/step)*3);let w=0;
      for(let c=0;c<en.nCells;c+=step){const q=cellCenter(en,c);
        out[w++]=q[0];out[w++]=q[1];out[w++]=q[2];}
      en.strPts=out.subarray(0,w);return en.strPts;}

    /* WHERE THE FRONT OF THE SURFACE IS, over a fine grid of the screen, in one pass over those
       samples. Each bin keeps the NEAREST sample that projected into it and how many did:
       nearest, because that is the face of the part the reader is looking at rather than its far
       side, and the count because an empty bin is empty screen and must not cost a round trip.
       A tile at any level is a block of these bins, so splitting one costs nothing. */
    function viewCloud(){
      const b=camBasis(),th=tanHalf(),G=STREAM_GRID,nb=G*G,sz=apiRW.getSize();
      const W=sz[0]||host.clientWidth||800,H=sz[1]||host.clientHeight||600;
      const asp=W/Math.max(H,1);
      const bz=new Float32Array(nb).fill(Infinity);
      const px=new Float32Array(nb),py=new Float32Array(nb),pz=new Float32Array(nb);
      const bn=new Int32Array(nb);let seen=0;
      entries.forEach(en=>{
        if(!en.actor.getVisibility())return;
        const s=samplesOf(en);
        for(let k=0;k<s.length;k+=3){
          const ax=s[k]-b.p[0],ay=s[k+1]-b.p[1],az=s[k+2]-b.p[2];
          const z=ax*b.d[0]+ay*b.d[1]+az*b.d[2];
          if(!(z>1e-9))continue;                      // behind the camera
          const u=(ax*b.r[0]+ay*b.r[1]+az*b.r[2])/(z*th*asp);
          if(u<-1||u>1)continue;
          const v=(ax*b.u[0]+ay*b.u[1]+az*b.u[2])/(z*th);
          if(v<-1||v>1)continue;
          let gx=(u+1)*0.5*G|0;if(gx<0)gx=0;if(gx>=G)gx=G-1;
          let gy=(1-v)*0.5*G|0;if(gy<0)gy=0;if(gy>=G)gy=G-1;
          const i=gy*G+gx;bn[i]++;seen++;
          if(z<bz[i]){bz[i]=z;px[i]=s[k];py[i]=s[k+1];pz[i]=s[k+2];}}});
      return {G,bz,px,py,pz,bn,b,th,asp,seen};}

    /* ONE TILE: its nearest sample, and whether it holds any at all. */
    function tileFront(c,x0,y0,x1,y1){
      let best=Infinity,bx=0,by=0,bzz=0,n=0;
      for(let y=y0;y<y1;y++)for(let x=x0;x<x1;x++){
        const i=y*c.G+x;if(!c.bn[i])continue;n+=c.bn[i];
        if(c.bz[i]<best){best=c.bz[i];bx=c.px[i];by=c.py[i];bzz=c.pz[i];}}
      return n?{at:[bx,by,bzz],z:best,n:n}:null;}

    /* AND HOW BIG A BALL COVERS IT. At the depth of its nearest sample the tile's rectangle is
       this wide and this tall in the part's own units, and a ball of half that diagonal covers the
       whole rectangle - so neighbouring tiles overlap and there is no seam between them. The
       centre is pushed a quarter of a radius away from the camera so the ball straddles the
       surface rather than sitting half in front of it: a cold plate seen face-on is thicker than
       one tile is wide, and a ball centred exactly on the front face reads only the front face. */
    function tileBall(c,f,x0,y0,x1,y1){
      const w=2*f.z*c.th*c.asp*(x1-x0)/c.G,h=2*f.z*c.th*(y1-y0)/c.G;
      const r=0.5*Math.hypot(w,h);
      return {r:r,at:[f.at[0]+c.b.d[0]*r*0.25,
                      f.at[1]+c.b.d[1]*r*0.25,
                      f.at[2]+c.b.d[2]*r*0.25]};}

    async function streamPass(key){
      const c=viewCloud();
      if(!c.seen)return {none:true};
      const step=c.G/STREAM_N0;
      let tiles=[];
      for(let gy=0;gy<STREAM_N0;gy++)for(let gx=0;gx<STREAM_N0;gx++)
        tiles.push([gx*step,gy*step,(gx+1)*step,(gy+1)*step]);
      const o={faces:0,drew:0,read:0,dense:0,split:0,left:0,range:0,painted:0,pLabel:'',pMetric:'',why:''};
      /* ONE PATCH, ONE COLOUR, ACROSS EVERY TILE. The route sends only the patches with a face in
         range, so the third patch of one tile is not the third patch of the next, and numbering
         them per tile would paint the same wall three different colours across a screen. */
      const colIx={};let colN=0;
      const ixOf=(p,i)=>{const k=String(p.name||i);
        if(colIx[k]===undefined)colIx[k]=colN++;return colIx[k];};
      let noEdges=false;
      ghostOnce();
      for(let L=0;L<STREAM_LEVELS&&tiles.length;L++){
        const next=[],want=[];
        tiles.forEach(q=>{const f=tileFront(c,q[0],q[1],q[2],q[3]);if(f)want.push({q:q,f:f});});
        let i=0;
        while(i<want.length){
          // the reader kept moving, or switched the mode off, while this pass was in flight
          if(!streaming||key!==strKey)return {aborted:true};
          if(o.faces>=STREAM_MAX_FACES||o.read>=STREAM_MAX_TILES){o.left+=want.length-i;break;}
          /* THE FIRST TILE GOES ALONE. The route parses a region's boundary on its first ball and
             answers from a cache after that - measured on this delivery at 5.42 s cold and 0.02 s
             warm, across six patches in four regions - so four cold tiles at once is four parses
             at once, for four times the memory in the API process and none of the benefit. One
             tile first warms what the rest of the pass needs. */
          const slab=want.slice(i,i+((o.read===0)?1:STREAM_POOL));i+=slab.length;
          const outs=await Promise.all(slab.map(async j=>{
            const b=tileBall(c,j.f,j.q[0],j.q[1],j.q[2],j.q[3]);
            try{return {j:j,res:await fetchTile(b.at,b.r)};}
            catch(_e){return {j:j,res:{ok:false,why:'could not read a tile'}};}}));
          o.read+=outs.length;
          outs.forEach(x=>{
            if(!x.res.ok){if(!o.why)o.why=x.res.why;return;}
            const got=x.res.got,ret=+got.faces_returned||0;
            /* HOW MUCH MESH IS IN VIEW AT ALL, from the first grid only - a split tile's ball
               covers ground its parent already counted, so counting the deeper levels too would
               count the same faces four times over. */
            if(L===0)o.range+=+got.faces_in_range||0;
            if(!ret||!(got.patches||[]).length)return;
            /* A TRUNCATED TILE IS NEVER DRAWN. What comes back then is a SAMPLE of that
               ball - the route shares its cap out across the patches - and a sample drawn beside
               whole neighbours does not read as a dense tile, it reads as holes in the mesh,
               which is the exact failure this mode came out of. So it is split into four and the
               quarters read instead; at the finest grid, where there is nothing left to split, it
               is counted and dropped. A tile of the finest grid is about fifty pixels by thirty,
               and one that truncates holds 150,000 faces in them - there is no sample of that a
               reader can read, so drawing one would only be inventing holes by hand. */
            const q=x.j.q,mx=(q[0]+q[2])>>1,my=(q[1]+q[3])>>1;
            if((+got.faces_in_range||0)>ret){
              if(mx>q[0]&&my>q[1]){
                next.push([q[0],q[1],mx,my],[mx,q[1],q[2],my],
                          [q[0],my,mx,q[3]],[mx,my,q[2],q[3]]);
                o.split++;}
              else o.dense++;
              return;}
            const d=drawCut(got,ixOf,noEdges);
            o.painted+=d.painted;
            if(d.painted&&!o.pLabel){o.pLabel=d.paintedLabel;o.pMetric=d.paintedMetric;}
            o.faces+=ret;o.drew++;});
          /* THE EDGE BUDGET IS THE SCREEN'S TOO. `setEdges` caps one actor at 750,000 polygons
             and every tile is far under that, but a pass puts dozens of them up and edge drawing
             is what loses the WebGL context - measured on this viewer at about 1.9 million faces.
             So once a pass is past one actor's worth in TOTAL, the rest of its tiles are drawn
             without edges and the ones already up have theirs taken away. */
          if(!noEdges&&o.faces>_EDGE_MAX_POLYS){
            noEdges=true;
            shown.forEach(ac=>{const pr=ac.getProperty();
              if(pr.setEdgeVisibility)pr.setEdgeVisibility(false);});}
          rw.render();}
        tiles=next;}
      o.left+=tiles.length;
      return o;}

    async function streamNow(){
      if(!streaming||strBusy)return;
      const cpx=realCellPx();
      if(cpx<STREAM_MIN_PX){
        if(shown.length)dropShown();
        strKey='';
        // NAMED, because this is the measure that matters and it is not the one the odometer
        // beside it reports: one drawn polygon on this delivery stands for about sixteen
        // delivered faces, so "px per cell" has to say WHICH cell.
        lbl.textContent='zoom in for real cells - '+cpx.toFixed(2)+' px per delivered cell';
        rw.render();return;}
      /* THE SAME VIEW IS NOT RE-STREAMED, and the key describes the CAMERA rather than any point
         on the mesh - the same thing Follow had to be corrected about, for the same reason: the
         debounce and the re-ping in `finally` both compare against this, so if it described
         anything else the two would never agree and every pass would schedule another. */
      const fp=cam.getFocalPoint(),r0=viewRadius();
      const key=fp[0].toFixed(7)+','+fp[1].toFixed(7)+','+fp[2].toFixed(7)+'|'+r0.toFixed(7);
      if(key===strKey)return;
      strKey=key;strBusy=true;a.dataset.busy='1';
      centre=null;lbl.textContent='streaming the view…';
      const t0=(window.performance&&performance.now)?performance.now():0;
      try{
        dropShown();
        const o=await streamPass(key);
        if(o.aborted)return;
        strFaces=o.faces||0;strTiles=o.drew||0;strRead=o.read||0;strLeft=o.left||0;
        strDense=o.dense||0;
        shownPainted=o.painted||0;shownMetric=o.painted?o.pMetric:'';
        if(o.none)lbl.textContent='nothing in view to read';
        /* READ BUT NOT DRAWN IS NOT THE SAME AS NOT THERE. Every tile truncating means
           the view holds more mesh than any tile of it can return whole, which is a real answer
           and the opposite of "no cells in view": the reader is looking at too much mesh, not at
           none of it. */
        else if(!o.drew)lbl.textContent=o.why||((o.split||o.dense)
          ?'too much mesh in view to read a tile of it whole - zoom in'
          :'no cells in view');
        else{
          const dt=((window.performance&&performance.now)?performance.now():0)-t0;
          /* WHAT IS ON SCREEN AGAINST WHAT IS THERE. A part-covered screen that only says
             how much it drew reads as the whole mesh, which is the one thing this viewer must
             never let a reader believe. The estimate divides the first grid's faces_in_range by
             how much those balls overlap - each covers its tile plus the corners, so
             pi*(w^2+h^2)/4 against w*h, which is pi/2 for a square tile - and is called "about"
             because that is what it is. */
          const inView=Math.round(o.range/1.5708);
          let s=o.faces.toLocaleString()+' real cells';
          if((o.left||o.dense)&&inView>o.faces)
            s+=' of about '+inView.toLocaleString()+' in view ('+
               Math.round(100*o.faces/inView)+'%)';
          s+=' in '+o.drew+(o.drew===1?' tile':' tiles');
          if(dt>0)s+=' · '+(dt/1000).toFixed(1)+' s';
          if(o.painted&&o.pLabel)s+=' · '+o.pLabel;
          // SAID, NOT IMPLIED: a view holding more mesh than a screen can show says so, and says
          // what to do about it, rather than letting a reader take a part-covered screen for the
          // whole mesh.
          if(o.left||o.dense)s+=' · zoom in for the rest';
          lbl.textContent=s;}
        applyClip();rw.render();legendSoon();}
      catch(_e){strKey='';lbl.textContent='could not stream the view';}
      finally{
        strBusy=false;delete a.dataset.busy;
        /* WHERE THE CAMERA IS NOW, against where this pass was for - the same re-ping Follow
           needs and for the same reason. A pass is up to twenty-four round trips and the reader
           keeps scrolling through them; every ping that arrived in that window was dropped by the
           busy guard, so without this the view they stopped on is the one never drawn. */
        const g=cam.getFocalPoint(),gr=viewRadius();
        const now=g[0].toFixed(7)+','+g[1].toFixed(7)+','+g[2].toFixed(7)+'|'+gr.toFixed(7);
        if(streaming&&now!==strKey)strPing();}}
    function strPing(){
      if(!streaming)return;
      if(strTimer)clearTimeout(strTimer);
      strTimer=setTimeout(function(){strTimer=null;streamNow();},STREAM_SETTLE_MS);}
    /* THE THREE MODES ARE ONE MODE. Inspect, Follow and Stream all own `shown` and all three draw
       in front of the same ghosted surface, so two of them on at once is two passes fighting over
       one set of actors. Turning one on turns the others off; only the `on` branch does that, so
       nothing here recurses. */
    function setStream(on){
      streaming=!!on;
      if(str){str.setAttribute('aria-pressed',streaming?'true':'false');
              str.classList.toggle('on',streaming);}
      if(streaming){setArmed(false);setFollow(false);setDeep(false);strKey='';streamNow();}
      else{strKey='';if(strTimer){clearTimeout(strTimer);strTimer=null;}
           strFaces=0;strTiles=0;strRead=0;strLeft=0;strDense=0;
           /* `clear`, NOT `dropShown`. `dropShown` removes what the mode drew and leaves
              `hidden` alone, so turning the mode off used to leave the merged surface ghosted at
              a tenth opacity for good. `clear` is the one that also puts that surface back. */
           clear();}}
    if(str){
      str.onclick=function(){setStream(!streaming);};
      if(cam.onModified)cam.onModified(strPing);}

    /* ─── DEEP: THE ZOOM KEEPS GOING ───────────────────────────────────────────────────────────
       "A 25 um cell on a 177 mm part is 0.09 px, so you cannot see your mesh in detail." The first
       half is true and the second does not follow - it is the iPad and Google Maps problem, and
       the answer there was never a bigger screen. It is DEPTH: every zoom level backed by real
       data at the resolution that level can show.

       WHY IT WORKS HERE AND WOULD NOT HAVE BEFORE. The surface this replaces is EXACT - coplanar
       faces merged into the rectangles they already tiled, so no vertex moved. The real cells and
       the surface are therefore the SAME GEOMETRY at different subdivisions, and swapping one for
       the other moves nothing a reader can see. Against the budget-clustered surface the same
       swap would shift the part by the 1.35% of area that route loses, and the mesh would visibly
       jump every time detail arrived. The lossless merge is what buys the seam-free handover; it
       is not a separate nicety.

       WHY IT REPLACES RATHER THAN GHOSTS. Inspect, Follow and Stream all draw a scrap of cells in
       front of a ghosted surface, because a scrap floating in black tells a reader nothing about
       where they are. This covers the WHOLE view, so there is nothing to orient against and two
       coincident surfaces would only fight for the same depth. The surface goes off entirely and
       comes straight back the moment the view stops fitting.

       WHERE IT TAKES OVER, measured on the delivered plate 2026-11-10 rather than chosen:

           view across   faces in view   px per cell   drawn
              12 mm        10,831,166        1.68      the exact surface
               6 mm         3,086,098        3.35      the exact surface
               4 mm         1,372,166        5.03      REAL CELLS
               2 mm           345,496       10.06      REAL CELLS

       So the handover lands at about 4 mm of a 177 mm part, where a cell is already 5 px - which
       is to say it becomes real at exactly the point a reader could first have seen that it was
       not. Round trip there is 2.3 s, and 0.66 s by 2 mm.

       IT NEVER SENDS A SAMPLE. A cut may be sampled because a reader opened it ON something and
       a sample still answers them. A sample of the WHOLE VIEW is a screen with holes in it, and
       nobody can tell a hole in the sample from a hole in the mesh. The route answers `fits:
       false` and sends nothing, and this keeps the surface up. */
    const DEEP_SETTLE_MS=300;
    //: Below this many pixels per DELIVERED cell, do not even ask. The route is the authority on
    //: whether a view fits - density varies far too much across this part to decide here - but a
    //: view this far out has never fitted and the round trip is 1.5 s of nothing. p10 and not
    //: typ, for the reason the streaming gate records: the budget is spent on face COUNT and the
    //: count is dominated by the fine faces.
    //:
    //: IT WAS 3.0 AND THAT WAS COSTING THE READER THE MESH. A pixel gate is a face-count estimate
    //: with the part's density divided out, and this part's density varies by better than two
    //: orders of magnitude - measured the same afternoon, a 5.68 mm view held 81,886 faces in one
    //: place while the recorded table has 3,086,098 in a 6 mm view in another. At 3.0 the coolant
    //: stayed a smooth surface at 8.3 mm (2.87 px per cell) in a view the route would have
    //: answered with every real cell, because nothing ever asked it. The floor now sits where a
    //: cell is still a visible thing - a cell and a half - and `depWorth` below carries the part
    //: of the job the floor was doing badly: it LEARNS this view's density from the route's own
    //: answer and stops asking once the next step out cannot fit. The floor guards the FIRST
    //: question, before there is anything to have learnt from.
    const DEEP_MIN_PX=1.5;
    //: How many view-widths the box reaches PAST what is under the middle of the screen. See
    //: `viewBox`: the camera sees forward, so a cube centred on the first surface it hits leaves
    //: the far half of a channel unread and the canvas full of holes.
    const DEEP_DEPTH=4.0;
    //: HOW FAR OVER THE ROUTE'S CEILING A PREDICTED COUNT MAY BE AND STILL BE WORTH ASKING.
    //:
    //: IT WAS 2.5 AND THAT IS FOUR WASTED QUESTIONS. Measured on the delivered plate,
    //: 2026-11-11, driving the wheel in from the fit and asking /window at every settle:
    //:
    //:     34.6 mm   4,573,668 faces in box   refused   (the first question; nothing learnt yet)
    //:     28.7 mm   3,723,777                refused   predicted 3.15 M, slack allowed 3.75 M
    //:     23.8 mm   2,861,907                refused   predicted 2.56 M
    //:     19.7 mm   1,906,000                refused   predicted 1.96 M
    //:     16.3 mm     818,905                FITS      predicted 1.31 M - 818,905 real cells
    //:
    //: Every refusal is a full bucket walk over a box holding millions of faces, so those three
    //: middle rows are seconds each of the reader's zoom spent on an answer the square law had
    //: already predicted would not fit. At 1.25 the same walk in asks at 34.6 (unlearnt), skips
    //: 28.7, 23.8 and 19.7, and asks at 16.3 - one refusal instead of four, same handover.
    //:
    //: NOT TIGHTER THAN 1.0, and not much looser. The square law holds on a SURFACE, which is
    //: what the index counts, so a prediction in the same place is sound - but it is a prediction
    //: of a count that varies by two orders of magnitude ACROSS this part, and the slack is what
    //: covers the reader zooming into a corner denser than the one measured. Panning is already
    //: covered by the centre test above (half a box width), so this only has to cover the
    //: density change along a straight zoom, which is small.
    const DEEP_SLACK=1.25;
    const dep=document.getElementById('v-insp-deep-'+job);
    let deepOn=false,depTimer=null,depBusy=false,depKey='';
    let depFaces=0,depFits=false,depInBox=0,depMs=0;
    //: What the route said its ceiling was, and the width/count pair it last measured. Faces on a
    //: surface go as the square of the width, so one honest answer is enough to predict the next
    //: question - and a prediction costs nothing, where the question costs a bucket walk over
    //: however much mesh is in the box.
    let depCap=0,depAcross=0,depSeen=-1,depSkipKey=null,depCentre=null;
    //: Bumped whenever the answer in flight stops being the answer anyone wants - the mode going
    //: off, or the reader changing which parts are on screen. A window is two awaits deep and
    //: both of them outlive a click.
    let depGen=0;

    /* THE PATCHES THE READER CANNOT SEE, as the window names them. Isolate the coolant and the
       metal is not on screen; without this the window hands it back anyway and draws it over the
       channels, because a box knows nothing about a parts panel. Worse than the drawing: the box
       is centred on whatever is under the middle of the screen, so once the plate's outer wall is
       drawn in front of the coolant the next window centres on THAT, and the one after it walks
       further out still. Measured before this list existed - isolate the coolant, zoom past the
       handover, and the window came back `solid/solid_to_air`, 400 faces, no coolant at all.

       VISIBILITY IS READ FROM `hidden` WHEN DEEP IS HOLDING IT, because by then this mode has
       switched the surface off itself and every actor would read as invisible. `hidden` is the
       record of what was on when it did. */
    function readerSees(){
      const on=Object.create(null);
      if(hidden.length)hidden.forEach(h=>{const en=h.en||h;on[en.patch]=1;});
      else entries.forEach(en=>{if(en.actor.getVisibility())on[en.patch]=1;});
      return on;}
    function deepSkip(){
      const on=readerSees(),out=[],seen=Object.create(null);
      entries.forEach(en=>{
        if(on[en.patch]||seen[en.patch])return;
        seen[en.patch]=1;out.push(en.patch);});
      return out.sort();}

    /* WHAT THE READER CAN SEE, which is what the window is actually told, and NOT the complement
       of what they cannot. A skip list can only name patches this viewer was handed, and a
       conjugate payload is not handed all of them: the plate's drawn set leaves out
       `solid/solid_to_fluid` and `solid/solid_to_chip`, the metal's side of two interfaces
       already drawn from the fluid's and the chip's. The window knows them, so a skip list that
       cannot name them got them back every time - two coincident sheets of 16,619,777 faces each,
       which a renderer cannot order and draws as a speckle of two materials across every channel
       wall. That speckle is what "the mesh looks wrong close up" was.

       Naming what IS on screen closes it without this viewer ever having to know a patch it was
       not given, and halves what travels on the way. */
    function deepKeep(){
      const on=readerSees(),out=[];
      Object.keys(on).forEach(k=>out.push(k));
      // the whole region, when that is what the reader isolated: a trailing slash says so, and
      // it is the only way to reach a patch this payload was never given
      if(isolatedRegion)out.push(isolatedRegion+'/');
      return out.sort();}

    /* IS IT WORTH ASKING. The floor above guards the first question; this guards every one after
       it, and unlike the floor it knows this view's own density. `depSeen` faces at `depAcross`
       across scales as the square, and the route refuses anything over `depCap`. The slack is
       generous on purpose: the estimate assumes the mesh is spread evenly through the box and it
       is not, so a near miss is worth the round trip and a mile out is not. */
    function depWorth(v){
      if(!(depCap>0&&depAcross>0&&depSeen>=0)||!(v&&v.across>0))return true;
      /* MOVED IS NOT ZOOMED. This part's face density varies by better than two orders of
         magnitude between the channel lattice and the plenum - 81,886 faces in one 5.68 mm view
         and 9,686,304 in another - so a count measured in one place predicts nothing about
         another, and a single dense refusal would otherwise wedge the mode off everywhere the
         reader panned to afterwards. Half a box width is the test: inside that the reader has
         zoomed, outside it they have gone somewhere else and this knows nothing about it. */
      if(depCentre){
        const c0=(v.lo[0]+v.hi[0])/2,c1=(v.lo[1]+v.hi[1])/2,c2=(v.lo[2]+v.hi[2])/2;
        if(Math.hypot(c0-depCentre[0],c1-depCentre[1],c2-depCentre[2])>0.5*depAcross)return true;}
      return depSeen*Math.pow(v.across/depAcross,2)<=depCap*DEEP_SLACK;}

    /* THE VIEW AS A WORLD-ALIGNED BOX. The index answers axis-aligned boxes and a frustum is
       neither axis-aligned nor a box, so this takes the oriented box around what the camera can
       see and then the world AABB of its eight corners. That OVER-covers when the camera sits at
       an angle - by up to root 3 - which is the right direction to be wrong in: it reads some
       mesh that is off screen, and never misses mesh that is on it.

       The depth is the larger of the two on-screen half-extents, so the box is roughly cubic. A
       flat slab would be cheaper and would stop at whatever faces the camera, leaving the inside
       of a channel - the thing a reader zoomed in to look at - unread. */
    function viewBox(at){
      const b=camBasis();
      const th=Math.tan(cam.getViewAngle()*Math.PI/360);
      const sz=apiRW.getSize(),asp=(sz&&sz[1]>0)?sz[0]/sz[1]:1;
      /* CENTRED ON THE MESH, AND SIZED AT THE MESH'S DEPTH - not at the focal point's.
         The focal point is wherever the camera happens to be aimed, and on this part that is
         usually empty space: `resetCamera` puts it at the centre of the bounding box, which for a
         cooled plate is inside the metal. A box there read 1,936 faces at a zoom the mesh has
         1.4 million in, and the mode engaged on an empty view. Same trap that made a focal-point
         cross-check report 1415.9% coverage earlier in this work.
         So the caller hands in what is actually under the middle of the screen, and the lateral
         half-extents are computed at THAT point's distance along the view direction. */
      const f=at||cam.getFocalPoint();
      const p=cam.getPosition();
      const dp=Math.abs((f[0]-p[0])*b.d[0]+(f[1]-p[1])*b.d[1]+(f[2]-p[2])*b.d[2])||camDist();
      const hy=dp*th,hx=hy*asp,hz=Math.max(hx,hy);
      /* DEEP, AND POINTING FORWARD. A cube on the picked point is not what a camera sees. What
         it sees carries on BEHIND the first surface - down the inside of a channel, past the far
         wall - while the space between the camera and that first surface is the part it has
         already flown through and is empty. A symmetric cube spends half its depth there and
         runs out of the other half early, and the screen then has holes in it where the surface
         receded past the back face. Measured on the coolant at the deepest zooms, with the cube:
         92.9% of the canvas drawn at 4.53 mm, 72.9% at 2.11 mm, 60.0% at 1.44 mm - and a reader
         cannot tell a hole in the window from a hole in the mesh, which is the one thing this
         route exists to avoid.

         So the box reaches a quarter of a width back and `DEEP_DEPTH` widths forward. It is
         affordable because it is bounded by the same cap as everything else and the counts at
         these zooms are three to five orders under it: a 1.44 mm view of the coolant holds about
         137 faces against a ceiling of 1,500,000. Where it is NOT affordable - the wide view of
         a dense lattice - the route refuses as it always did and the exact surface stays up. */
      const back=0.25*hz,fwd=DEEP_DEPTH*hz;
      const lo=[Infinity,Infinity,Infinity],hi=[-Infinity,-Infinity,-Infinity];
      for(let s0=-1;s0<=1;s0+=2)for(let s1=-1;s1<=1;s1+=2)for(let s2=0;s2<2;s2++)
        for(let k=0;k<3;k++){
          const v=f[k]+s0*hx*b.r[k]+s1*hy*b.u[k]+(s2?fwd:-back)*b.d[k];
          if(v<lo[k])lo[k]=v;
          if(v>hi[k])hi[k]=v;}
      return {lo,hi,across:2*Math.max(hx,hy)};}

    /* AIM THE ZOOM AT THE MESH. A dolly moves the camera a fraction of the way to its FOCAL
       POINT, and `resetCamera` leaves that at the centre of the visible bounding box - which on
       a cooled plate, and on the coolant alone, is a point in empty space inside the part. So
       the camera converges on somewhere the mesh is not, and the surface the reader is looking
       at never gets closer. Measured on the delivered coolant, zooming in from the fit:

           wheel clicks   camera to focal   camera to the MESH   view across, at the mesh
                     20        57.27 mm            72.02 mm              48.03 mm
                     36        12.46 mm            27.22 mm              18.15 mm
                     52         2.71 mm            17.46 mm              11.65 mm

       Thirty-two clicks of zoom moved the camera 54 mm closer to its target and 4 mm closer to
       the part. The mesh stayed about 17 mm away and the view across it barely moved, so the
       window kept asking for a box eleven millimetres wide - nine million faces, far past what
       one carries - and the real cells could never take over however long a reader scrolled.
       Every pixels-per-cell figure was quoting the same wrong distance and read 6.4x too
       optimistic with it.

       THE FIX MOVES NOTHING A READER CAN SEE. The focal point slides ALONG the view direction to
       the depth of whatever is under the middle of the screen; it stays on the same ray, so the
       view direction, the up vector and the view angle are all untouched and the rendered image
       is identical to the pixel. What changes is only what the next dolly converges on, and what
       `camDist` means - which is what every cell-size reading on this page is divided by.

       The deadband is what stops it chasing itself: `setFocalPoint` fires `onModified`, which
       pings this mode again, and without a "close enough" test the ping would re-aim and re-ping
       for ever. Within 2% it is already aimed and does nothing. */
    function aimAtMesh(hit){
      if(!hit)return false;
      const b=camBasis(),q=cam.getPosition(),f=cam.getFocalPoint();
      const along=a=>(a[0]-q[0])*b.d[0]+(a[1]-q[1])*b.d[1]+(a[2]-q[2])*b.d[2];
      const dh=along(hit),df=along(f);
      if(!(dh>0)||!(df>0))return false;
      if(Math.abs(dh-df)<=df*0.02)return false;
      cam.setFocalPoint(q[0]+b.d[0]*dh,q[1]+b.d[1]*dh,q[2]+b.d[2]*dh);
      return true;}

    /* OFF ENTIRELY, not to a tenth. Recorded in `hidden` in the bare-actor shape, which `clear`
       already knows how to put back - the same list the ghosting modes use, so one `clear`
       restores whichever of them was last on. */
    function hideSurface(){
      if(hidden.length)return;
      entries.forEach(en=>{
        if(!en.actor.getVisibility())return;
        hidden.push(en);
        en.actor.setVisibility(false);});}

    /* THE KEY CARRIES THE SKIP SET. It is what the debounce and the re-ping in `finally` both
       compare, so a key made of the box alone cannot tell "the same view" from "the same view of
       a different set of parts" - isolate the coolant without moving the camera and the mode sat
       on its last answer, which was the answer for the metal. */
    function depKeyOf(v,keep){
      return v.lo.concat(v.hi).map(x=>x.toFixed(7)).join(',')+'|'+keep.join('~');}

    async function deepNow(){
      if(!deepOn||depBusy)return;
      // THE CHEAP GATE FIRST, before the picker: the route is the authority on whether a view
      // fits, but a view this far out has never fitted and the pick plus the round trip is
      // seconds of nothing. The density gate is NOT here: it belongs below, on the box that
      // will actually be asked for.
      /* BOTH LISTS, because `keep` can now be wider than the screen. A whole-region entry says
         "every patch of this region", which is the only way to reach one the payload was never
         given - and it reaches the ones it WAS given and the reader has turned off as well. On
         this plate that is `solid/solid_to_air`: isolate the metal and its outside is left
         hidden, and without this the cut drew it straight back, which is the wall standing
         between the camera and the channels that the keep list was built to get rid of.
         `_visible_rows` applies keep first and then skip, so the pair reads as "the region,
         minus what is off". `skip` is a function of `keep` over a fixed set of actors, so the
         cache key below still only needs one of them. */
      const keep=deepKeep(),skip=deepSkip(),px=realCellPx();
      if(px<DEEP_MIN_PX||!keep.length){
        depKey=depKeyOf(viewBox(),keep);
        depFits=false;depFaces=0;
        if(shown.length||hidden.length)clear();
        lbl.textContent=keep.length?'zoom in and the real cells take over'
                                   :'nothing of the mesh is on screen';
        legendSoon();return;}
      // WHAT IS ACTUALLY UNDER THE MIDDLE OF THE SCREEN. The same picker Follow uses, and for
      // the same reason: the camera's focal point is not on the mesh.
      /* THE QUESTION THIS IS THE ANSWER TO. A window is two awaits deep - the pick, then the
         round trip - and a reader can turn the mode off or change which parts are on screen
         during either. Without this, `setDeep(false)` ran `clear()` and then the fetch landed,
         drew its cells and hid the surface again: Deep off, cells up, part gone. The same
         applies to `keep`, which is read before both awaits, so an answer for the coolant could
         be drawn after the reader had switched to the metal. */
      const gen=depGen;
      depBusy=true;
      const hit=await pickCentre();
      if(gen!==depGen||!deepOn){depBusy=false;return;}
      /* BEFORE THE BOX IS MEASURED, because the box and the gate are both read off the camera
         and this is the call that makes the camera's own distance mean the mesh. Re-aiming
         fires `onModified`, so the ping that follows is this mode's own and is what draws the
         view it has just made reachable. */
      aimAtMesh(hit);
      const v=viewBox(hit);
      const key=depKeyOf(v,keep);
      if(key===depKey){depBusy=false;return;}
      depKey=key;
      /* THE DENSITY GATE, ON THE BOX THAT WILL BE ASKED FOR. It was up with the floor, before the
         pick, where it compared a box built on the camera's FOCAL POINT against a width recorded
         from a box built on the picked point - two different boxes, so the prediction was against
         a width it had never measured and it let every question through. The focal point is not
         on the mesh (`viewBox` carries the measurements); that is the same trap, one level up. */
      if(!depWorth(v)){
        depFits=false;depFaces=0;
        if(shown.length||hidden.length)clear();
        lbl.textContent='zoom in and the real cells take over';
        legendSoon();depBusy=false;return;}
      const t0=(window.performance&&performance.now)?performance.now():0;
      if(a)a.dataset.busy='1';
      try{
        const q=`x0=${v.lo[0]}&y0=${v.lo[1]}&z0=${v.lo[2]}`
               +`&x1=${v.hi[0]}&y1=${v.hi[1]}&z1=${v.hi[2]}`
               +`&keep=${encodeURIComponent(keep.join(','))}`
               +`&skip=${encodeURIComponent(skip.join(','))}`;
        const res=await fetch(`/api/v1/simulation/${job}/window?${q}`,{headers:headers()});
        if(!res.ok){
          let why='';try{why=((await res.json())||{}).detail||'';}catch(_e){why='';}
          lbl.textContent=why?String(why).slice(0,90):('window '+res.status);
          depFits=false;return;}
        const got=await res.json();
        // the answer outlived its question: `finally` still clears the busy flag
        if(gen!==depGen||!deepOn)return;
        depInBox=+got.faces_in_box||0;
        // WHAT THIS VIEW IS LIKE, from the route rather than from an assumption. Recorded on
        // every answer, fitting or not, because a refusal carries the count too and a refusal is
        // exactly the case the next prediction has to get right.
        depCap=+got.max_faces||depCap;
        // NOT FROM AN EMPTY ANSWER. `depWorth` scales a count by the square of the width, and
        // zero scales to zero at every width - so one empty box would say "worth asking" for
        // ever and the gate would be dead. An empty box is routine now that a keep list can
        // leave a region out entirely: the reader isolates the coolant, the middle of the
        // screen lands past the channels, and the answer is a legitimate fits-with-nothing-in-it
        // rather than the metal. Keep the last REAL pair; `depSeen` stays -1 until one arrives,
        // which is what lets the first question through under the floor.
        if(depInBox>0){depAcross=v.across;depSeen=depInBox;
                       depCentre=[(v.lo[0]+v.hi[0])/2,(v.lo[1]+v.hi[1])/2,
                                  (v.lo[2]+v.hi[2])/2];}
        if(got.fits&&(got.patches||[]).length){
          dropShown();
          hideSurface();
          /* EDGES ARE BUDGETED ACROSS THE WHOLE SCREEN, not per actor. `setEdges` caps one actor
             at _EDGE_MAX_POLYS and every patch here is far under it, but this puts several up at
             once and edge drawing is what loses the WebGL context - measured on this viewer at
             about 1.9 M faces. At the zoom this engages, the view is 1.37 M faces and climbing
             as the reader goes wider, so the cap is checked against the TOTAL. */
          const edgeOff=(+got.faces_returned||0)>_EDGE_MAX_POLYS;
          drawCut(got,null,edgeOff);
          /* THE ROUTE'S OWN COUNT, not drawCut's. `painted` counts the patches that got a quality
             ramp, which is what the cut label wants and is emphatically not a number of cells -
             it read "1 real cells" for a window carrying nearly two thousand. */
          depFaces=+got.faces_returned||0;
          depFits=true;
          depMs=t0?Math.round(((performance.now()-t0))):0;
          lbl.textContent=`${depFaces.toLocaleString()} real cells · the whole view`
                          +(depMs?` · ${(depMs/1000).toFixed(1)} s`:'');
        }else{
          depFits=false;depFaces=0;
          // STRAIGHT BACK TO THE SURFACE, and `clear` rather than `dropShown`, because
          // `dropShown` leaves `hidden` alone and the surface would stay switched off.
          if(shown.length||hidden.length)clear();
          lbl.textContent=depInBox
            ? `${depInBox.toLocaleString()} cells in view · zoom in and they become real`
            : 'nothing of the mesh in view';}
        applyClip();rw.render();legendSoon();}
      catch(err){depFits=false;lbl.textContent='could not read this view';
                 console.warn('deep',err);}
      finally{
        if(a)delete a.dataset.busy;
        depBusy=false;
        // where the camera is NOW against where this answer was for: every ping that arrived
        // during the round trip was dropped by the busy guard, including the one they stopped on
        const now=depKeyOf(viewBox(hit),deepKeep());
        if(deepOn&&now!==depKey)depPing();}}

    function depPing(){
      if(!deepOn)return;
      if(depTimer)clearTimeout(depTimer);
      depTimer=setTimeout(function(){depTimer=null;deepNow();},DEEP_SETTLE_MS);}
    function setDeep(on){
      deepOn=!!on;
      if(dep){dep.setAttribute('aria-pressed',deepOn?'true':'false');
              dep.classList.toggle('on',deepOn);}
      depGen++;
      if(deepOn){setArmed(false);setFollow(false);setStream(false);
                 depKey='';depSkipKey=deepKeep().join('~');
                 depCap=0;depAcross=0;depSeen=-1;depCentre=null;deepNow();}
      else{depKey='';depSkipKey=null;if(depTimer){clearTimeout(depTimer);depTimer=null;}
           depFaces=0;depFits=false;depInBox=0;
           depCap=0;depAcross=0;depSeen=-1;depCentre=null;
           clear();}}
    /* THE READER CHANGED WHAT THEY CAN SEE. Three things are wrong at once and all three are
       this mode's: the cells on screen are a window for the old set, the density this mode learnt
       was measured on the old set, and - the one that is actually visible - `paint` has just set
       every actor's visibility from the parts panel, which turns the surface this mode switched
       off back ON, underneath the cells it drew. That last one is what a reader sees as a smooth
       surface with patches of mesh scattered across it.

       So: drop the cells, forget the record of what was hidden (paint is the authority now),
       forget the density, and ask again. Keyed on the set itself because `paint` also runs for a
       selection, and re-asking on a colour change would be a round trip for nothing. */
    onVisibleChanged=function(){
      if(!deepOn){depSkipKey=null;hidden=[];return;}
      /* THE STALE RECORD GOES FIRST, and that ordering is the whole of it. `hidden` is this
         mode's note of which actors IT switched off; `paint` has just set every one of them
         from the parts panel, so the note describes a state that no longer exists. Two things
         went wrong while it stood:

         `readerSees` prefers `hidden` when it is non-empty, so `deepKeep` kept answering with
         the set from BEFORE the reader touched anything - the comparison below could never see
         a change, and isolating a different part while Deep was on did nothing at all. Measured:
         switch from the coolant to the metal and the keep list stayed the coolant's, the window
         stayed the coolant's, and the cells on screen stayed the coolant's.

         And `hideSurface` begins `if(hidden.length)return;`, so a stale note also meant it could
         never run again. The surface `paint` had just turned back on stayed on, underneath the
         cells - which is a smooth surface with patches of mesh scattered over it, and is what
         this whole repair started from. */
      hidden=[];
      const k=deepKeep().join('~');
      if(k===depSkipKey){
        /* SAME PARTS. The cells up are still the right cells, so this does not spend a round
           trip - it just puts the surface back down under them. `paint` runs for a selection
           too, and re-asking on a colour change would be a fetch for nothing. */
        if(depFits&&shown.length)hideSurface();
        rw.render();return;}
      depSkipKey=k;depGen++;
      if(shown.length)dropShown();
      depCap=0;depAcross=0;depSeen=-1;depCentre=null;
      depKey='';depPing();};

    if(dep){
      dep.onclick=function(){setDeep(!deepOn);};
      if(cam.onModified)cam.onModified(depPing);
      /* ON BY DEFAULT, BUT ONLY ON AN EXACT SURFACE. A mode a reader has to find is not an answer
         to "the viewer should show me my mesh"; they should simply zoom and have it happen. But
         the handover is only invisible where the surface it replaces is the mesh - on a
         budget-clustered one the part would shift by the 1.35% of area that route loses every
         time detail arrived, and a jump nobody asked for is worse than a button nobody found.
         So the payload decides, and the gate above means a view this far out costs nothing.

         AFTER THIS TURN OF THE LOOP, and not inline, because `setDeep` turns the other modes off
         and `setArmed` reads `activeMetric` - which is declared with `let` in the heatmap block
         further down this file and is therefore in its temporal dead zone while the viewer is
         still wiring itself up. Inline, this threw "Cannot access 'activeMetric' before
         initialization" and the whole viewer failed to load with a message about downloads. The
         three modes have always called each other safely because until now nothing called any of
         them before a reader clicked. */
      if(_draw.exact)setTimeout(function(){if(deepOn)return;setDeep(true);},0);}

    /* ARMED IS A MODE, and it says so three ways - the button's own state, the cursor, and the
       hint under the canvas - because a mode a reader cannot see is a mode they will click out of
       by accident and blame the mesh for. */
    function setArmed(on){
      armed=!!on;
      a.setAttribute('aria-pressed',armed?'true':'false');
      box.dataset.armed=armed?'1':'0';
      host.style.cursor=armed?'crosshair':'';
      const h=document.getElementById('v-hint-'+job);
      if(h)h.textContent=armed?'Click a spot to read its real cells · Drag still rotates'
                              :(activeMetric?HEAT_HINT:MOUSE_HINT);
      if(!armed)clear();else lbl.textContent=was;
      rw.render();}
    a.onclick=e=>{e.preventDefault();setArmed(!armed);};

    /* THE CLICK. `pickCell` is the viewer's own picker - the region markers and the heatmap probe
       read the mesh through it already, this is its third reader - and `cellCenter` turns the face
       it returns into the point the ball is cut around. A drag is not a click: the orbit moves the
       pointer, and without this test every rotation that ended over the mesh fired a cut. */
    let dxy=null;
    host.addEventListener('pointerdown',e=>{dxy=[e.clientX,e.clientY];},true);
    host.addEventListener('click',e=>{
      if(!armed)return;
      if(e.target&&e.target.closest&&
         e.target.closest('.v-insp,.v-tools,.v-legend,.v-legend-scale,.v-probe,.v-flags'))return;
      if(dxy&&Math.hypot(e.clientX-dxy[0],e.clientY-dxy[1])>4)return;
      pickCell(e,(en,cid)=>{recut(cellCenter(en,cid));});},true);
    // Esc leaves the mode the way Esc leaves every mode
    host.setAttribute('tabindex','-1');
    document.addEventListener('keydown',e=>{
      if(e.key==='Escape'&&(armed||shown.length)&&document.getElementById('viewer-'+job))
        setArmed(false);});

    /* THE DIAL moves the label on every tick and the CUT only when it settles. A re-cut per tick
       is a request per tick: the window the plate reads from is 1.66 GB of index on the server
       and a slider dragged across its range is forty of them. */
    if(dial){
      dial.addEventListener('input',showR);
      dial.addEventListener('change',()=>{showR();if(centre)recut(centre.slice());});}

    /* THE SHELL, AND WHY IT ADDS THE METAL RATHER THAN FADING IT. A cooled solid is a block with
       passages cut through it, so the metal's every patch but the coupling is a face of that
       block - `encloses_domain` is how the engine says so, and `mesh_facts` opens exactly those
       HIDDEN, because the first thing a reader should see of a conjugate mesh is the coolant and
       not six grey rectangles sealing it in.

       Which makes "fade the metal" a control with nothing to do: measured on the plate, a slider
       taken to 85% changed not one pixel, because what it was fading was already not drawn. The
       useful move is the other one - put the metal BACK, see-through, so the channels are seen
       inside the block they are cut from. So this is an opacity, plainly: off draws no shell,
       right draws a solid one, and anywhere between is the view worth having.

       It leaves a cut alone. While real cells are up the merged actors are deliberately hidden,
       and a shell switched on over them would hang in front of the thing being read. */
    const ENCLOSE=new Set((surf.patches||[]).filter(p=>p&&p.encloses_domain)
                                            .map(p=>String(p.name)));
    const xEl=document.getElementById('v-insp-xray-'+job);
    const xLbl=document.getElementById('v-insp-xrayr-'+job);
    function applyXray(){
      const v=xEl?(+xEl.value)/100:0;
      if(xLbl)xLbl.textContent=v?Math.round(v*100)+'%':'off';
      const cutUp=shown.length>0;
      entries.forEach(en=>{
        if(ENCLOSE.size&&!ENCLOSE.has(en.patch))return;   // only the wrapper, where there is one
        const pr=en.actor.getProperty();
        if(v>0){
          if(en._shellWas===undefined)en._shellWas=en.actor.getVisibility();
          if(!cutUp)en.actor.setVisibility(true);
          if(pr.setOpacity)pr.setOpacity(Math.max(0.03,v));
        }else{
          if(pr.setOpacity)pr.setOpacity(1);
          if(en._shellWas!==undefined){
            if(!cutUp)en.actor.setVisibility(en._shellWas);
            delete en._shellWas;}}});}
    if(xEl)xEl.addEventListener('input',()=>{applyXray();rw.render();});

    /* CLIP. A plane, slid through the part, with everything on one side of it not drawn. Nothing
       is removed and nothing is measured differently: `addClippingPlane` is a mapper setting, so
       the mesh on the server and the counts on screen are untouched and sliding it back shows
       exactly what was there. The cut's own actors get the same plane, or an inspect window
       opened inside a clipped part would hang in front of the opening. */
    let clipAx='',clipFlip=false;
    const clipEl=document.getElementById('v-insp-clip-'+job);
    function applyClip(){
      const ms=entries.map(en=>en.actor.getMapper()).concat(shown.map(ac=>ac.getMapper()));
      ms.forEach(m=>{if(m&&m.removeAllClippingPlanes)m.removeAllClippingPlanes();});
      if(!clipAx)return;
      const k={x:0,y:1,z:2}[clipAx];
      const lo=_bs[k*2],hi=_bs[k*2+1],t=clipEl?(+clipEl.value)/100:0.5;
      const o=[0,0,0];o[k]=lo+(hi-lo)*t;
      const n=[0,0,0];n[k]=clipFlip?-1:1;
      const pl=vtk.Common.DataModel.vtkPlane.newInstance({origin:o,normal:n});
      ms.forEach(m=>{if(m&&m.addClippingPlane)m.addClippingPlane(pl);});}

    /* AIM AT WHAT IS LEFT, which is the whole of a bug that made Clip unusable the moment anyone
       zoomed.

       `resetCamera` puts the focal point at the centre of the bounding box, and `applyClip` puts
       the plane at the midpoint of that same box - so at the slider's default the two are the SAME
       POINT. The camera dollies toward its focal point, so zooming after a clip converged the
       camera onto the clip plane itself, and everything beyond the plane is exactly what was
       removed. Measured on the delivered coolant, drawn canvas against wheel clicks:

           clicks      no clip     clip on z
                0       16.26%        11.66%
               20       83.63%        33.37%
               40      100.00%         3.23%

       A reader clipped to see inside, zoomed to see detail, and watched the mesh evaporate. The
       clip was innocent of the emptiness and guilty of where it left the camera pointing.

       So when the clip moves, the focal point moves to the middle of the half that SURVIVES, and
       the camera moves with it - same direction, same distance, so nothing about the view changes
       except what the zoom is now converging on. Only on a clip change: `applyClip` is also called
       after every cut and from `clear`, and moving a reader's camera on either of those would be
       the camera wandering off on its own. */
    function reaimForClip(){
      if(_clamping)return;
      const k={x:0,y:1,z:2}[clipAx];
      const f=cam.getFocalPoint().slice(),p=cam.getPosition().slice();
      if(k===undefined){
        // clip off: back to the box's own centre, so the next zoom behaves as it always did
        const c=[(_bs[0]+_bs[1])/2,(_bs[2]+_bs[3])/2,(_bs[4]+_bs[5])/2];
        cam.setFocalPoint(c[0],c[1],c[2]);
        cam.setPosition(p[0]+c[0]-f[0],p[1]+c[1]-f[1],p[2]+c[2]-f[2]);
        return;}
      const lo=_bs[k*2],hi=_bs[k*2+1],t=clipEl?(+clipEl.value)/100:0.5;
      const at=lo+(hi-lo)*t;
      // `normal` points INTO the half that is kept, so flip decides which side survives
      const keepLo=clipFlip?lo:at,keepHi=clipFlip?at:hi;
      const want=(keepLo+keepHi)/2;
      // a sliver is not worth aiming into; leave the camera where it is rather than shoving it
      // into a hundred-micron gap at the end of the slider's travel
      if(!(keepHi-keepLo>1e-6*Math.max(hi-lo,1e-12)))return;
      const d=want-f[k];
      if(!isFinite(d)||Math.abs(d)<1e-12)return;
      f[k]+=d;p[k]+=d;
      cam.setFocalPoint(f[0],f[1],f[2]);
      cam.setPosition(p[0],p[1],p[2]);}
    box.querySelectorAll('.v-insp-c[data-ax]').forEach(b=>{
      b.onclick=()=>{
        clipAx=b.dataset.ax||'';
        box.querySelectorAll('.v-insp-c[data-ax]').forEach(o=>
          o.setAttribute('aria-pressed',o===b?'true':'false'));
        if(clipEl)clipEl.disabled=!clipAx;
        applyClip();reaimForClip();clampCam();rw.render();legendSoon();};});
    if(clipEl)clipEl.addEventListener('input',()=>{
      applyClip();reaimForClip();clampCam();rw.render();legendSoon();});
    const flipB=document.getElementById('v-insp-flip-'+job);
    if(flipB)flipB.onclick=()=>{clipFlip=!clipFlip;applyClip();reaimForClip();clampCam();
                                rw.render();legendSoon();};

    /* THE HELP IS THE FEATURE, on the evidence: the control worked and was unreadable, so the
       words that say what it does earn their place beside it rather than in a document nobody
       opens next to a 3D view. */
    const helpB=document.getElementById('v-insp-help-'+job);
    const helpBox=document.getElementById('v-insp-helpbox-'+job);
    if(helpB&&helpBox)helpB.onclick=()=>{
      const on=helpBox.hidden;helpBox.hidden=!on;
      helpB.setAttribute('aria-expanded',on?'true':'false');};

    /* THE GLIDE IS THIS WINDOW'S OWN, which is a decision and not an oversight. The heatmap has
       one - `flyTo`, eased, turning about a single axis, cancelled by any input - and it is the
       better glide. It is also unreachable here: the heatmap returns early on a payload with
       neither per-patch quality arrays nor hotspots, so every line below that return, `flyTo`
       among them, is never defined on exactly that payload. The chip IS that payload. Borrowing
       it would have given a camera that glides on the plate and silently does nothing on the
       chip - the same cross-closure fault that has already cost this file `tanHalf` and `rampOf`,
       both of which threw into a button's label. So this one is short, plain and always defined.

       The BEARING IS KEPT: the camera looks the way it already looked and moves in. On a part
       that is one repeated pattern end to end, choosing a new angle for the reader is how they
       lose track of where they are. */
    let glide=0,glideSeq=0;
    const EASE=t=>t<0.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2;
    /** Taking the mouse ends the glide. A camera that keeps moving while a reader is dragging it
     *  is a camera fighting its owner, and on the long walk below it is also the stop button. */
    function stopGlide(){glideSeq++;if(glide){cancelAnimationFrame(glide);glide=0;}}
    host.addEventListener('pointerdown',stopGlide,{capture:true});
    host.addEventListener('wheel',stopGlide,{capture:true,passive:true});
    function glideTo(at,dist){
      cancelAnimationFrame(glide);
      const token=++glideSeq;
      const f0=cam.getFocalPoint().slice(),p0=cam.getPosition().slice();
      const v=[p0[0]-f0[0],p0[1]-f0[1],p0[2]-f0[2]];
      const d0=Math.hypot(v[0],v[1],v[2])||1e-9;
      for(let k=0;k<3;k++)v[k]/=d0;
      const r=capRange(),d1=Math.max(r[0],Math.min(r[1],dist||d0));
      const calm=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const dur=calm?0:620,t0=performance.now();
      return new Promise(res=>{
        const step=()=>{
          if(token!==glideSeq){glide=0;res(false);return;}
          const t=dur?Math.min(1,(performance.now()-t0)/dur):1,e=EASE(t);
          const d=d0*Math.pow(d1/d0,e);
          const f=[0,1,2].map(k=>f0[k]+(at[k]-f0[k])*e);
          // the same guard flyTo uses: the clamp must not re-enter while the camera is mid-move
          _clamping=true;
          try{cam.setFocalPoint(f[0],f[1],f[2]);
              cam.setPosition(f[0]+v[0]*d,f[1]+v[1]*d,f[2]+v[2]*d);
              cam.orthogonalizeViewUp();}
          finally{_clamping=false;}
          clampCam();ren.resetCameraClippingRange();rw.render();legendSoon();
          if(t<1)glide=requestAnimationFrame(step);else{glide=0;res(true);}};
        step();});}
    /** How far back to sit so a ball of radius r fills about 45% of the view's height. */
    function distFor(r){return Math.max(r,1e-12)/(0.45*Math.max(tanHalf(),1e-6));}

    /* WORST turns a figure into a place, which is the one thing a number like "83.6 degrees"
       cannot do for a reader on its own. The payload already carries the places: the hotspots are
       the worst faces per metric with an x/y/z taken off the index's own centroids
       (boundary_quality._sidecar_hotspots), measured on the faces the mesher wrote. So this needs
       no new measurement, no new endpoint and no second opinion - it takes the top of that list,
       glides there, and cuts. The metric is the one the reader picked if they have picked one. */
    const HOTS=((surf.quality_fields||{}).hotspots)||[];
    const METRIC_NAME={non_ortho:'non-orthogonality',skewness:'skewness',
                       aspect_ratio:'aspect ratio'};
    const worstB=document.getElementById('v-insp-worst-'+job);
    if(worstB){
      if(!HOTS.length){worstB.disabled=true;
        worstB.title='this mesh shipped no hot spots - nothing measured over a bar';}
      worstB.onclick=async()=>{
        const pool=activeMetric?HOTS.filter(h=>h.metric===activeMetric):[];
        const list=(pool.length?pool:HOTS).slice().sort((p,q)=>(q.value||0)-(p.value||0));
        const h=list[0];if(!h)return;
        const at=[h.x,h.y,h.z];
        setArmed(true);
        lbl.textContent='flying to the worst face…';
        await glideTo(at,distFor(radiusNow()));
        await recut(at);
        const nm=METRIC_NAME[h.metric]||h.metric;
        const v=Math.round((+h.value||0)*100)/100;
        lbl.textContent=lbl.textContent+' · worst '+nm+' '+v+(h.patch?' on '+h.patch:'');};}

    /* FLY walks the focal point along the part's longest axis in even hops, so a channel network
       can be FOLLOWED instead of orbited - on a plate that is mostly one repeated pattern, an
       orbit tells a reader almost nothing about what changes along it.
       Any drag, wheel or Fit cancels the glide (flyTo's own rule), and a cancelled hop resolves
       false - so taking the mouse ends the tour rather than fighting it for the camera. */
    const flyB=document.getElementById('v-insp-fly-'+job);
    let flying=false;
    if(flyB)flyB.onclick=async()=>{
      if(flying){flying=false;stopGlide();return;}
      /* THE WHOLE PART, NOT ONE BALL. The camera's zoom-out cap is computed from what is
         currently VISIBLE (capRange, off the visible set), so walking the long axis with a cut
         up holds the camera inside that cut's few millimetres: measured on the chip, an
         eight-hop walk of a 78 mm part ended at 2.22 um a pixel. Putting the merged surface
         back first gives the walk the part's own envelope. */
      if(shown.length)clear();
      flying=true;flyB.textContent='Stop';flyB.setAttribute('aria-pressed','true');
      try{
        const k=[0,1,2].reduce((b,i)=>
          (_bs[i*2+1]-_bs[i*2])>(_bs[b*2+1]-_bs[b*2])?i:b,0);
        const lo=_bs[k*2],hi=_bs[k*2+1],span=hi-lo;
        const mid=[0,1,2].map(i=>(_bs[i*2]+_bs[i*2+1])/2);
        const STOPS=7;
        for(let s=0;s<=STOPS&&flying;s++){
          const at=mid.slice();at[k]=lo+span*(s/STOPS);
          const ok=await glideTo(at,distFor(span/(2*STOPS)));
          if(!ok)break;}
      }finally{
        flying=false;flyB.textContent='Fly';flyB.removeAttribute('aria-pressed');}};

    /* THE SIZE LEGEND, which is the answer to "I cannot tell what I am looking at". A bar whose
       LABEL is a round number (see inspect.scaleBar), the worth of one pixel, and whether the
       surface under the cursor is the mesh or the merged stand-in for it. Coalesced onto one
       animation frame because a camera fires modified on every mouse move of an orbit. */
    const MERGE=_draw.factor,EXACT=_draw.exact;
    const sBar=document.getElementById('v-scale-i-'+job);
    const sLbl=document.getElementById('v-scale-l-'+job);
    const sOdo=document.getElementById('v-scale-o-'+job);
    function perPx(){
      const f=cam.getFocalPoint(),p=cam.getPosition();
      const d=Math.hypot(p[0]-f[0],p[1]-f[1],p[2]-f[2]);
      return (2*d*tanHalf())/(host.clientHeight||600);}
    let legendQ=0;
    function legend(){
      const m=perPx(),sb=scaleBar(m,140);
      if(sBar)sBar.style.width=Math.max(6,Math.min(220,sb.px))+'px';
      if(sLbl)sLbl.textContent=sb.label;
      if(sOdo)sOdo.textContent=odometerText({mPerPx:m,merged:MERGE,real:!!shown.length,
                                             exact:EXACT});}
    function legendSoon(){
      if(legendQ)return;
      legendQ=requestAnimationFrame(()=>{legendQ=0;legend();});}
    legend();
    if(cam.onModified)cam.onModified(legendSoon);
    new ResizeObserver(legendSoon).observe(host);

    // a drag on a slider is not an orbit, and a click on the overlay is not a click on the mesh
    shield(box);
    const sc=document.getElementById('v-scale-'+job);if(sc)shield(sc);

    closeDetail=()=>{if(shown.length){clear();setArmed(false);}};
    // `hidden` ALONE IS ENOUGH to have taken the mesh off screen: a pass that fits with nothing
    // in the box hides the surface and shows no cells, and that is exactly the state a re-frame
    // must not be computed from.
    showSurfaceAgain=()=>{if(shown.length||hidden.length)clear();};
    /* WHAT A READER CAN ACTUALLY SEE, which is what all three of this window's defects came
       down to: which patches are drawn - a budget that drops a region drops it from here -
       and whether the merged surface this stands in front of is still hidden. */
    window._vdbg[job].detail=()=>({button:a,shown:shown.length,names:shownNames.slice(),
                                   hidden:hidden.length,radius:radiusNow(),
                                   // the mode, the point it was cut around and the instruments,
                                   // so the browser tier can drive this without pixel picking
                                   armed,centre:centre?centre.slice():null,
                                   clip:clipAx||null,xray:xEl?+xEl.value:0,
                                   scale:sLbl?sLbl.textContent:'',odometer:sOdo?sOdo.textContent:'',
                                   cut:at=>recut(at),arm:on=>setArmed(on),
                                   follow:on=>setFollow(on),following:()=>following,
                                   // the streaming mode, and what its last pass covered. A pass
                                   // is keyed on the camera and will not repeat itself, so a test
                                   // that wants one asks for it by clearing that key.
                                   stream:on=>setStream(on),streaming:()=>streaming,
                                   streamAgain:()=>{strKey='';return streamNow();},
                                   streamFaces:()=>strFaces,streamTiles:()=>strTiles,
                                   streamRead:()=>strRead,streamLeft:()=>strLeft,
                                   streamDense:()=>strDense,
                                   // the deep-zoom mode: whether the view fits as real cells,
                                   // how many it drew, and the box it asked for - so a test can
                                   // drive the handover without a camera setter
                                   deep:on=>setDeep(on),deepOn:()=>deepOn,
                                   deepAgain:()=>{depKey='';return deepNow();},
                                   deepFits:()=>depFits,deepFaces:()=>depFaces,
                                   deepInBox:()=>depInBox,deepBox:()=>viewBox(),
                                   // what the reader cannot see, which is what the window is
                                   // told to leave out, and what the route taught us about this
                                   // view's own density
                                   deepSkip:()=>deepSkip(),deepKeep:()=>deepKeep(),
                                   deepCap:()=>depCap,deepAcross:()=>depAcross,
                                   deepAim:async()=>aimAtMesh(await pickCentre()),
                                   // where the pick lands and how far down the view it is,
                                   // against where the camera is aimed - the two depths that
                                   // decide the box and the pixels-per-cell gate
                                   deepProbe:async()=>{
                                     const h=await pickCentre(),b=camBasis();
                                     const q=cam.getPosition(),f=cam.getFocalPoint();
                                     const dot=a=>Math.abs((a[0]-q[0])*b.d[0]
                                                          +(a[1]-q[1])*b.d[1]
                                                          +(a[2]-q[2])*b.d[2]);
                                     return {hit:h,focal:f,pos:q,camDist:camDist(),
                                             dpFocal:dot(f),dpHit:h?dot(h):null,
                                             nearFar:cam.getClippingRange?
                                                     cam.getClippingRange():null,
                                             acrossFocal:viewBox().across,
                                             acrossHit:h?viewBox(h).across:null};},
                                   deepSeen:()=>depSeen,
                                   deepWorth:v=>depWorth(v||viewBox()),
                                   realCellPx:()=>realCellPx(),
                                   cellPx:()=>cellPx(),viewRadius:()=>viewRadius(),
                                   fitRadius:()=>folFitR,inRange:()=>lastInRange,
                                   returned:()=>lastReturned,
                                   // whether what is on screen carries the METRIC'S colours or
                                   // the flat per-patch tint: both are colours, and a test that
                                   // only counted actors could not tell them apart
                                   painted:shownPainted,metric:shownMetric});
  })();

  /* QUALITY HEATMAP - the boundary coloured by the quality of the cell behind each face.
     The payload carries one float per drawn polygon (render/face_quality.py, aligned with the
     surface reader's own filter) and the bar the engine's gate judges against. The
     delivered-mesh FIGURES are the controls: the non-orthogonality, skewness and
     aspect-ratio numbers become clickable when fields exist for them - clicking a number shows where it
     lives. Nothing here touches the geometry: colouring is cell scalars on the same immutable
     actors, and turning it off restores exactly the property colours the parts panel set. */
  let activeMetric=null,lastProbe=null;
  (function heatmap(){
    const qf=surf.quality_fields;
    /* NO PER-FACE ARRAYS IS NOT NO HEATMAP. On a mesh whose cells were merged to fit the drawing
       budget the engine ships the figures and the hotspots without `patches`: one drawn face
       stands for several delivered ones, so a colour on it was measured on none of them, but a
       hotspot is a place and a number taken on the faces the mesher wrote and merging the drawing
       moves neither. The areas panel runs on hotspots alone (buildAreas makes an area per loose
       hotspot) and paintFaces already turns scalars off where there is nothing to paint - so this
       asked for `patches` only because it had never been given a payload without them, and a
       reader of the biggest mesh got no "where to look" at all. */
    if(!qf||!qf.metrics||!window.vtk)return;
    const META={non_ortho:{fact:'max_non_ortho',b64:'non_ortho_b64'},
                skewness:{fact:'max_skewness',b64:'skewness_b64'},
                aspect_ratio:{fact:'max_aspect_ratio',b64:'aspect_ratio_b64'}};
    const avail=Object.keys(META).filter(m=>qf.metrics[m]);
    // decode once. An array that does not match the polygon count is dropped, not trusted:
    // a misaligned field would paint every face with its neighbour's number.
    entries.forEach(en=>{const p=(qf.patches||{})[en.patch];en.q={};if(!p)return;
      avail.forEach(m=>{if(!p[META[m].b64])return;const a=b64f32(p[META[m].b64]);
        if(a.length===en.nCells)en.q[m]=a;});
      if(p.cell_faces_b64){const cf=b64u8(p.cell_faces_b64);if(cf.length===en.nCells)en.cellFaces=cf;}
      /* HOW MANY DELIVERED FACES ONE DRAWN POLYGON STANDS FOR. Absent on the budget route, which
         cannot know. Present on the lossless one, which merged them and so does: the value above
         is then the WORST of that many, which is an upper bound a reader can act on and is NOT
         the value of the face under their cursor. The chip's flat top is ONE polygon covering
         544,272 faces; painted by its single worst cell and read as a face's own number, it says
         the chip is ruined. The probe says `worst of N` wherever N is more than one. */
      if(p.faces_per_polygon_b64){const fp=b64u32(p.faces_per_polygon_b64);
        if(fp.length===en.nCells)en.nFaces=fp;}});
    if(!entries.some(en=>Object.keys(en.q).length)&&!(qf.hotspots||[]).length)return;

    /* one ramp per metric: calm below the bar, warm approaching it, red past it. The colours
       are computed here and handed to the mapper as direct per-face RGB, and the legend is
       built from the SAME stops - so the bar on screen is the bar in the colours, and nothing
       depends on which lookup-table classes the vendored bundle happens to export. */
    /* the console's original ramp, its calm blues about 30% lighter (Areen, 2026-10-01):
       steel below, the wireframe blue through the calm range, warming to amber and
       international orange at the bar, crimson past it. The colour is the surface's own - nothing is drawn on top of the mesh. */
    const ramp=m=>rampOf(qf.metrics[m]);
    function fmt(m,v){const u=qf.metrics[m].unit||'';return (u==='°'?v.toFixed(1):v.toFixed(2))+u;}

    /* PROBLEM AREAS. The faces over the bar, and those near it (80% of it and up), gathered into
       places, so a mesh with three bad corners reads as three areas and not as a few hundred faces.
       The worst face left seeds an area and every bad face within a radius of it joins: ten typical
       faces of what is on screen, or 2% of its size, whichever is larger - so the same rule groups a
       nozzle's lip and an airliner's wing root. A grid of that radius keeps it one pass however many
       faces there are, and seeding worst first puts the list in order as it is built. A payload
       hotspot (a face over the bar) that no drawn area holds - a bad cell inside the volume - still
       makes an area of its own. Nothing here is drawn on the mesh: a click glides the camera there
       and the heatmap's own colour shows the cells. */
    const NEAR=0.8,SHOW_AREAS=5,MAX_AREAS=200;
    const NEUTRAL=[84,87,92];        // "only bad cells": the flat grey every face below 80% rests in
    const shown=()=>entries.filter(en=>en.actor.getVisibility());
    const shownSig=()=>shown().map(en=>en.patch).join('|')+'#'+(upSel?upSel.value:'');
    const faceSize=(en,cid)=>{const v=newell(en,cid);return Math.sqrt(0.5*Math.hypot(v[0],v[1],v[2]));};
    const dot=(a,b)=>a[0]*b[0]+a[1]*b[1]+a[2]*b[2];
    const unit=v=>{const l=Math.hypot(v[0],v[1],v[2])||1;return [v[0]/l,v[1]/l,v[2]/l];};
    function camDist(){const f=cam.getFocalPoint(),q=cam.getPosition();
      return Math.hypot(q[0]-f[0],q[1]-f[1],q[2]-f[2])||1e-12;}
    function unionBounds(list){if(!list.length)return null;
      const b=[Infinity,-Infinity,Infinity,-Infinity,Infinity,-Infinity];
      list.forEach(en=>{const e=en.pd.getBounds();
        for(let k=0;k<6;k+=2){b[k]=Math.min(b[k],e[k]);b[k+1]=Math.max(b[k+1],e[k+1]);}});
      return b;}
    function diagOf(list){const b=unionBounds(list);if(!b)return diag;
      return Math.max(Math.hypot(b[1]-b[0],b[3]-b[2],b[5]-b[4]),1e-9);}
    function faceTyp(vis){            // the median drawn face of the parts on screen, sampled
      const tot=vis.reduce((a,en)=>a+en.nCells,0),step=Math.max(1,Math.floor(tot/3000)),s=[];
      vis.forEach(en=>{for(let c=0;c<en.nCells;c+=step)s.push(faceSize(en,c));});
      s.sort((a,b)=>a-b);
      return (s.length&&s[s.length>>1])||(stats&&stats.typ)||diag*0.005;}
    const roleOf=en=>(((surf.patches||[]).find(p=>p.name===en.patch)||{}).type||'')+' '+en.patch;
    const isIn=en=>/inlet|inflow/i.test(roleOf(en)),isOut=en=>/outlet|outflow|exit/i.test(roleOf(en));

    /* WHERE IT IS, in words a person would use, read from the drawn parts' own box and the up axis
       the viewer is using - nothing is guessed past what the case says. Ahead and behind are said
       only when the case names an inlet and an outlet: between them the walls of an internal flow
       read "inlet end" / "outlet end", and a body standing in the stream reads "front" (toward the
       inlet) / "rear". "tip" is the far end of something only a small share of the part reaches - a
       wing tip, a fin top, a probe's point. "upper" / "lower" are thirds along the up axis. */
    function placeFrame(vis){
      const box=en=>/farfield|freestream|symmetry/i.test(roleOf(en));
      let ref=vis.filter(en=>!isIn(en)&&!isOut(en)&&!box(en));if(!ref.length)ref=vis;
      const tot=ref.reduce((a,en)=>a+en.pts.length/3,0),step=Math.max(1,Math.floor(tot/20000)),P=[];
      ref.forEach(en=>{const n=en.pts.length/3;for(let i=0;i<n;i+=step)P.push([en.pts[i*3],en.pts[i*3+1],en.pts[i*3+2]]);});
      const range=ax=>{let lo=Infinity,hi=-Infinity;P.forEach(p=>{const d=dot(p,ax);if(d<lo)lo=d;if(d>hi)hi=d;});
        return [lo,hi];};
      const mid=list=>{const b=unionBounds(list);return [(b[0]+b[1])/2,(b[2]+b[3])/2,(b[4]+b[5])/2];};
      const up=upVector(upSel?upSel.value:'+z');
      const ins=entries.filter(isIn),outs=entries.filter(isOut);
      let stream=null,internal=false,stR=null;
      if(ins.length&&outs.length&&P.length){const a=mid(ins),b=mid(outs),d=[b[0]-a[0],b[1]-a[1],b[2]-a[2]];
        if(Math.hypot(d[0],d[1],d[2])>0){stream=unit(d);stR=range(stream);const span=(stR[1]-stR[0])||1;
          // the inlet and the outlet close the walls' own ends: the flow runs inside them
          internal=dot(a,stream)>=stR[0]-0.15*span&&dot(b,stream)<=stR[1]+0.15*span;}}
      const axes=[[1,0,0],[0,1,0],[0,0,1]].filter(ax=>!stream||Math.abs(dot(ax,stream))<0.9)
        .map(ax=>({ax,r:range(ax),isUp:Math.abs(dot(ax,up))>0.9}));
      const big=Math.max(1e-30,...axes.map(a=>a.r[1]-a.r[0]),stR?stR[1]-stR[0]:0);
      return {P,up,upR:P.length?range(up):null,stream,stR,internal,axes,big};}
    function hintOf(c,F,nrm,port){
      if(!F.P.length)return '';
      const t=(r,v)=>(v-r[0])/((r[1]-r[0])||1);
      const words=[];
      if(F.stream&&!port){const s=t(F.stR,dot(c,F.stream));
        if(s<0.3)words.push(F.internal?'inlet end':'front');
        else if(s>0.7)words.push(F.internal?'outlet end':'rear');}
      let tipUp=false;
      for(const a of F.axes){const s=t(a.r,dot(c,a.ax));if(s>0.05&&s<0.95)continue;
        // the bottom of the part along the up axis is where it rests, not a tip
        if(a.isUp&&(dot(a.ax,F.up)>0?s<=0.08:s>=0.92))continue;
        const span=(a.r[1]-a.r[0])||1,hi=s>=0.92,cut=hi?a.r[1]-0.08*span:a.r[0]+0.08*span;
        let n=0;F.P.forEach(p=>{const d=dot(p,a.ax);if(hi?d>=cut:d<=cut)n++;});
        if(n<0.04*F.P.length){words.push('tip');tipUp=a.isUp;break;}}
      // a level patch says which way it faces; anything else, which third of the height it is in
      const fu=nrm?dot(nrm,F.up):0;
      if(Math.abs(fu)>0.85)words.push(fu>0?'top side':'underside');
      // (a part with no height to speak of - a flat plate - has no upper or lower)
      else if(!tipUp&&F.upR&&F.upR[1]-F.upR[0]>0.05*F.big){const s=t(F.upR,dot(c,F.up));
        if(s>0.67)words.push('upper');else if(s<0.33)words.push('lower');}
      return words.slice(0,2).join(' · ')||'middle';}

    function buildAreas(m){
      const md=qf.metrics[m],lim=md.limit,out={list:[],total:0,offScreen:false};
      if(!(lim>0))return out;
      const cut=NEAR*lim,vis=shown(),vi=new Set(vis);
      // a part that is not on screen still decides whether the whole mesh is clear
      entries.forEach(en=>{if(vi.has(en)||out.offScreen)return;const v=en.q[m];
        if(v)for(let i=0;i<v.length;i++)if(v[i]>=cut){out.offScreen=true;break;}});
      const onScreen=h=>!h.patch||vis.some(en=>en.patch===h.patch);
      const hot=(qf.hotspots||[]).filter(h=>h.metric===m&&h.value>lim);
      if(hot.some(h=>!onScreen(h)))out.offScreen=true;
      const hs=hot.filter(onScreen);
      let n=0;vis.forEach(en=>{const v=en.q[m];if(v)for(let i=0;i<v.length;i++)if(v[i]>=cut)n++;});
      if(!n&&!hs.length)return out;
      const typ=faceTyp(vis),R=Math.max(10*typ,0.02*diagOf(vis)),R2=R*R;
      // the candidates, flat: value, part, face, centre
      const val=new Float32Array(n),ent=new Uint16Array(n),cid=new Uint32Array(n),pos=new Float64Array(n*3);
      let k=0;vis.forEach(en=>{const v=en.q[m];if(!v)return;const ei=entries.indexOf(en);
        for(let i=0;i<v.length;i++)if(v[i]>=cut){const p=cellCenter(en,i);
          val[k]=v[i];ent[k]=ei;cid[k]=i;pos[k*3]=p[0];pos[k*3+1]=p[1];pos[k*3+2]=p[2];k++;}});
      // a grid of the radius: an area's members all sit in the 27 grid cells around its seed
      const b=unionBounds(vis)||[0,0,0,0,0,0];
      const cellOf=(x,y,z)=>[Math.floor((x-b[0])/R),Math.floor((y-b[2])/R),Math.floor((z-b[4])/R)];
      const key=(i,j,l)=>((i+2)*4099+(j+2))*4099+(l+2);
      const grid=new Map();
      for(let q=0;q<n;q++){const g=cellOf(pos[q*3],pos[q*3+1],pos[q*3+2]),kk=key(g[0],g[1],g[2]);
        let a=grid.get(kk);if(!a){a=[];grid.set(kk,a);}a.push(q);}
      const order=Array.from({length:n},(_,q)=>q).sort((a,c)=>val[c]-val[a]);
      const taken=new Uint8Array(n),areas=[];
      const fresh=(seed,worst,inside)=>({seed,worst,over:0,near:0,hot:0,sum:[0,0,0],nrm:[0,0,0],nabs:0,
                                         firm:!inside,sizes:[],by:{},m:[]});
      for(const s of order){if(taken[s])continue;if(areas.length>=MAX_AREAS)break;
        const sx=pos[s*3],sy=pos[s*3+1],sz=pos[s*3+2],g=cellOf(sx,sy,sz),a=fresh([sx,sy,sz],val[s],false);
        for(let i=-1;i<=1;i++)for(let j=-1;j<=1;j++)for(let l=-1;l<=1;l++){
          const cell=grid.get(key(g[0]+i,g[1]+j,g[2]+l));if(!cell)continue;
          for(const q of cell){if(taken[q])continue;
            const dx=pos[q*3]-sx,dy=pos[q*3+1]-sy,dz=pos[q*3+2]-sz;if(dx*dx+dy*dy+dz*dz>R2)continue;
            taken[q]=1;a.m.push(q);
            if(val[q]>lim)a.over++;else a.near++;
            a.sum[0]+=pos[q*3];a.sum[1]+=pos[q*3+1];a.sum[2]+=pos[q*3+2];
            const en=entries[ent[q]],nv=newell(en,cid[q]),sg=outSign(en),nm=Math.hypot(nv[0],nv[1],nv[2]);
            a.nrm[0]+=nv[0]*sg;a.nrm[1]+=nv[1]*sg;a.nrm[2]+=nv[2]*sg;a.nabs+=nm;
            if(!en.outFirm)a.firm=false;
            if(a.sizes.length<400)a.sizes.push(Math.sqrt(0.5*nm));
            a.by[en.patch]=(a.by[en.patch]||0)+1;}}
        areas.push(a);}
      // the hotspots: one inside a drawn area tells it its worst cell; the rest make their own
      const loose=[];
      hs.forEach(h=>{
        const a=areas.find(a=>(h.x-a.seed[0])**2+(h.y-a.seed[1])**2+(h.z-a.seed[2])**2<=R2*1.44);
        if(!a){loose.push(h);return;}
        if(h.value>a.worst)a.worst=h.value;
        a.hot++;});
      areas.forEach(a=>{if(a.worst>lim&&!a.over)a.over=a.hot||1;});
      loose.sort((x,y)=>y.value-x.value);
      const used=new Uint8Array(loose.length);
      loose.forEach((h,i)=>{if(used[i]||areas.length>=MAX_AREAS)return;
        const a=fresh([h.x,h.y,h.z],h.value,true);
        loose.forEach((g,j)=>{if(used[j])return;
          if((g.x-h.x)**2+(g.y-h.y)**2+(g.z-h.z)**2>R2)return;used[j]=1;a.over++;
          a.sum[0]+=g.x;a.sum[1]+=g.y;a.sum[2]+=g.z;const pn=g.patch||'inside the mesh';a.by[pn]=(a.by[pn]||0)+1;});
        areas.push(a);});
      // worst first; between equals, the one with more cells over
      areas.sort((x,y)=>(y.worst-x.worst)||(y.over-x.over)||(y.near-x.near));
      const F=placeFrame(vis);
      out.total=areas.length;
      out.list=areas.slice(0,SHOW_AREAS).map((a,i)=>{
        const cnt=a.m.length||a.over||1,c=[a.sum[0]/cnt,a.sum[1]/cnt,a.sum[2]/cnt];
        // the place the camera centres: the area's own face nearest its middle, so the focus sits on
        // the surface even when the area curves round a corner
        let r=0,best=-1,bd=Infinity;
        a.m.forEach(q=>{const dd=Math.hypot(pos[q*3]-c[0],pos[q*3+1]-c[1],pos[q*3+2]-c[2]);
          if(dd>r)r=dd;if(dd<bd){bd=dd;best=q;}});
        if(best<0)bd=0;
        const at=best>=0?[pos[best*3],pos[best*3+1],pos[best*3+2]]:c;
        a.sizes.sort((x,y)=>x-y);
        const size=a.sizes.length?a.sizes[a.sizes.length>>1]:typ;
        // the part it is on: the one holding most of it, and a second one holding a fair share
        const parts=Object.entries(a.by).sort((x,y)=>y[1]-x[1]);
        const patch=parts.length>1&&parts[1][1]>=0.25*cnt?`${parts[0][0]}, ${parts[1][0]}`
                                                           :(parts[0]||['inside the mesh'])[0];
        const nl=Math.hypot(a.nrm[0],a.nrm[1],a.nrm[2]),flat=a.nabs?nl/a.nabs:0;
        let normal=flat>0.3?[a.nrm[0]/nl,a.nrm[1]/nl,a.nrm[2]/nl]:null;
        if(!normal&&best>=0)normal=cellNormal(entries[ent[best]],cid[best]);
        const port=parts.length&&entries.some(en=>en.patch===parts[0][0]&&(isIn(en)||isOut(en)));
        return {index:i,patch,hint:hintOf(c,F,a.firm&&flat>0.6?normal:null,port),
                over:a.over,near:a.near,worst:a.worst,centre:at,radius:Math.max(r+bd,size),size,
                normal,firm:a.firm};});
      return out;}

    /* THE GLIDE. The camera glides to an area over FLY_MS - focal point straight, distance
       geometric so a long zoom feels even, direction turned about one axis - rather than jumping,
       so the viewer keeps their bearings. Any drag, wheel, Fit or change of the up axis takes the
       camera back at once. */
    const FLY_MS=1000;
    let fly=null;
    function cancelFly(){if(fly){const f=fly;fly=null;f.resolve(false);}}
    const ease=t=>t<0.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2;
    function flyTo(tf,dir,dist){
      cancelFly();
      const f0=cam.getFocalPoint().slice(),p0=cam.getPosition().slice();
      const v0=[p0[0]-f0[0],p0[1]-f0[1],p0[2]-f0[2]],d0=Math.hypot(v0[0],v0[1],v0[2])||1e-9;
      for(let k=0;k<3;k++)v0[k]/=d0;
      // the turn: about the axis square to both directions (about the view-up for a half turn)
      const ang=Math.acos(Math.max(-1,Math.min(1,dot(v0,dir))));
      let ax=[v0[1]*dir[2]-v0[2]*dir[1],v0[2]*dir[0]-v0[0]*dir[2],v0[0]*dir[1]-v0[1]*dir[0]];
      if(Math.hypot(ax[0],ax[1],ax[2])<1e-6){const u=cam.getViewUp(),d=dot(u,v0);
        ax=[u[0]-d*v0[0],u[1]-d*v0[1],u[2]-d*v0[2]];}
      ax=unit(ax);
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

    /* WHERE THE CAMERA STOPS: the area in the middle of the view, close enough that its own faces
       read as cells - 20 px across when the area is small, never under 12 px when it is wide - and
       seen from the side it faces, keeping the bearing the viewer already has. */
    function areaDist(a){
      const H=host.clientHeight||600,t=tanHalf(),at=px=>a.size*H/(2*t*px);
      const d=Math.min(at(12),Math.max(at(20),1.3*a.radius/t)),r=capRange();
      return Math.max(r[0],Math.min(r[1],d));}
    // never straight down the up axis: the view would lose which way is up
    function offUp(v){const u=cam.getViewUp(),du=dot(v,u);
      if(Math.abs(du)<=0.94)return v;
      let h=[v[0]-du*u[0],v[1]-du*u[1],v[2]-du*u[2]];
      if(Math.hypot(h[0],h[1],h[2])<1e-3){const fw=cam.getDirectionOfProjection(),df=dot(fw,u);
        h=[-(fw[0]-df*u[0]),-(fw[1]-df*u[1]),-(fw[2]-df*u[2])];}
      h=unit(h);return unit(v.map((x,k)=>Math.sign(du)*0.94*u[k]+0.34*h[k]));}
    // the drawn faces' centres and reach (centre to farthest corner), once per part, for the
    // line-of-sight test
    function geom(en){if(!en.geo){const C=new Float32Array(en.nCells*3),Rr=new Float32Array(en.nCells),off=cellOffsets(en);
        for(let c=0;c<en.nCells;c++){const p=cellCenter(en,c),o=off[c],n=en.polys[o];let r=0;
          C[c*3]=p[0];C[c*3+1]=p[1];C[c*3+2]=p[2];
          for(let j=1;j<=n;j++){const q=en.polys[o+j]*3;
            r=Math.max(r,Math.hypot(en.pts[q]-p[0],en.pts[q+1]-p[1],en.pts[q+2]-p[2]));}
          Rr[c]=r;}
        en.geo={C,R:Rr};}
      return en.geo;}
    // does the sight line p + t v, t0 < t < t1, pass through drawn face c? (its triangle fan)
    function hits(en,c,p,v,t0,t1){
      const o=cellOffsets(en)[c],n=en.polys[o],P=en.pts,a=en.polys[o+1]*3;
      for(let j=2;j<n;j++){const b=en.polys[o+j]*3,e=en.polys[o+j+1]*3;
        const e1=[P[b]-P[a],P[b+1]-P[a+1],P[b+2]-P[a+2]],e2=[P[e]-P[a],P[e+1]-P[a+1],P[e+2]-P[a+2]];
        const h=[v[1]*e2[2]-v[2]*e2[1],v[2]*e2[0]-v[0]*e2[2],v[0]*e2[1]-v[1]*e2[0]],det=dot(e1,h);
        if(det===0)continue;
        const w=[p[0]-P[a],p[1]-P[a+1],p[2]-P[a+2]],u=dot(w,h)/det;if(u<0||u>1)continue;
        const q=[w[1]*e1[2]-w[2]*e1[1],w[2]*e1[0]-w[0]*e1[2],w[0]*e1[1]-w[1]*e1[0]],k=dot(v,q)/det;
        if(k<0||u+k>1)continue;
        const t=dot(e2,q)/det;if(t>t0&&t<t1)return true;}
      return false;}
    // would a part on screen stand between the area and a camera d along v? The line starts a
    // hair off the area's own face, so only something in front of it counts
    function blocked(p,v,d,keep){
      const q=[p[0]+v[0]*d,p[1]+v[1]*d,p[2]+v[2]*d],pad=0.05*diag;
      for(const en of shown()){const b=en.pd.getBounds();
        if(Math.max(p[0],q[0])+pad<b[0]||Math.min(p[0],q[0])-pad>b[1]||Math.max(p[1],q[1])+pad<b[2]
           ||Math.min(p[1],q[1])-pad>b[3]||Math.max(p[2],q[2])+pad<b[4]||Math.min(p[2],q[2])-pad>b[5])continue;
        const {C,R}=geom(en);
        for(let c=0;c<en.nCells;c++){
          const x=C[c*3]-p[0],y=C[c*3+1]-p[1],z=C[c*3+2]-p[2],t=x*v[0]+y*v[1]+z*v[2],r=R[c];
          if(t+r<=keep||t-r>=d)continue;
          const ex=x-t*v[0],ey=y-t*v[1],ez=z-t*v[2];
          if(ex*ex+ey*ey+ez*ez>r*r)continue;
          if(hits(en,c,p,v,keep,d))return true;}}
      return false;}
    function areaDir(a,d){
      const f=cam.getFocalPoint(),p=cam.getPosition();
      let v=unit([p[0]-f[0],p[1]-f[1],p[2]-f[2]]),nrm=a.normal?a.normal.slice():null;
      if(nrm){let dp=dot(v,nrm);
        // a part with no firm outside (a flat inlet disc) is seen from whichever side faces us
        if(dp<0&&!a.firm){nrm=nrm.map(x=>-x);dp=-dp;}
        if(dp<0){v=v.map((x,k)=>x-2*dp*nrm[k]);dp=-dp;}
        if(dp<0.7)v=unit(v.map((x,k)=>x+(0.7-dp)*nrm[k]));}
      // the bearing the viewer has, unless something stands in the way: then the clear view
      // nearest to it, turning about the area's face - over to its other side too when the part
      // has no firm outside
      const ring=n=>{const t1=unit(Math.abs(n[0])<0.9?[0,-n[2],n[1]]:[-n[2],0,n[0]]);
        const t2=[n[1]*t1[2]-n[2]*t1[1],n[2]*t1[0]-n[0]*t1[2],n[0]*t1[1]-n[1]*t1[0]],out=[n];
        [30,60].forEach(deg=>{const ca=Math.cos(deg*Math.PI/180),sa=Math.sin(deg*Math.PI/180);
          for(let k=0;k<8;k++){const ph=k*Math.PI/4;
            out.push(unit([0,1,2].map(i=>n[i]*ca+(t1[i]*Math.cos(ph)+t2[i]*Math.sin(ph))*sa)));}});
        return out;};
      const n=nrm||v,cands=ring(n).concat(nrm&&a.firm?[]:ring(n.map(x=>-x)));
      cands.sort((x,y)=>dot(y,v)-dot(x,v));
      const keep=0.05*a.size;
      for(const c of [v].concat(cands)){const w=offUp(c);if(!blocked(a.centre,w,d,keep))return w;}
      return offUp(v);}

    let legend=null,probeEl=null,selArea=-1,onlyBad=false,curRp=null;
    let areaSet={list:[],total:0,offScreen:false},areaSig='';
    function hideProbe(){if(probeEl){probeEl.remove();probeEl=null;}}
    function goArea(i){
      const a=areaSet.list[i];if(!activeMetric||!a)return Promise.resolve(false);
      selArea=i;markArea();hideProbe();
      const d=areaDist(a);
      return flyTo(a.centre,areaDir(a,d),d);}
    function markArea(){if(!legend)return;
      legend.querySelectorAll('.v-area').forEach(b=>{
        b.setAttribute('aria-pressed',String(Number(b.dataset.index)===selArea));});}

    // the worst value, to the precision that tells it apart from the limit
    function fmtWorst(m,v){const md=qf.metrics[m],u=md.unit||'';
      const dv=Math.abs(v-md.limit),dp=u==='°'?(dv<0.01?3:dv<0.1?2:1):(dv<0.001?4:dv<0.01?3:2);
      return v.toFixed(dp)+u;}
    function areasHtml(m){
      const S=areaSet;
      if(!S.list.length)return `<div class="v-areas"><p class="ar-none">`
        +(S.offScreen?'No problem areas on the parts shown'
                     :'No problem areas: every cell is well inside the limit')+`</p></div>`;
      const count=a=>[a.over?`<span class="ar-over">${a.over.toLocaleString()} over the limit</span>`:'',
                      a.near?`${a.near.toLocaleString()} near`+(a.over?'':' the limit'):''].filter(Boolean).join(' · ');
      return `<div class="v-areas" role="group" aria-label="Problem areas">`
        +`<div class="ar-h"><span class="ar-ht">Problem areas</span>`
          +`<button class="v-only" type="button" aria-pressed="${onlyBad}" `
          +`title="Grey out every face below ${esc(fmt(m,NEAR*qf.metrics[m].limit))} (80% of the limit)">Only bad cells</button></div>`
        +S.list.map(a=>`<button class="v-area" type="button" data-index="${a.index}" aria-pressed="${a.index===selArea}" `
            +`title="Go to area ${a.index+1}">`
          +`<span class="ar-i">${a.index+1}</span>`
          +`<span class="ar-b"><span class="ar-t"><span class="ar-p">${esc(a.patch)}</span>`
            +(a.hint?`<span class="ar-w"> · ${esc(a.hint)}</span>`:'')+`</span>`
            +`<span class="ar-c">${count(a)}</span></span>`
          +`<span class="ar-v">${esc(fmtWorst(m,a.worst))}</span></button>`).join('')
        +(S.total>S.list.length?`<p class="ar-n">The ${S.list.length} worst of ${S.total.toLocaleString()} areas</p>`:'')
        +`</div>`;}
    function wireAreas(){if(!legend)return;
      legend.querySelectorAll('.v-area').forEach(b=>{b.onclick=()=>goArea(Number(b.dataset.index));});
      const ob=legend.querySelector('.v-only');if(ob)ob.onclick=()=>setOnlyBad(!onlyBad);}
    // what is on screen changed (a part hidden, shown or isolated; the up axis): the areas follow
    function refreshAreas(){const m=activeMetric;if(!m)return;
      const sig=m+'#'+shownSig();if(sig===areaSig)return;
      cancelFly();areaSet=buildAreas(m);areaSig=sig;selArea=-1;
      if(!areaSet.list.length&&onlyBad){onlyBad=false;paintFaces(m,curRp);}
      const el=legend&&legend.querySelector('.v-areas');
      if(el){el.outerHTML=areasHtml(m);wireAreas();}
      rw.render();}
    onPartsPainted=refreshAreas;
    if(upSel)upSel.addEventListener('change',()=>{cancelFly();refreshAreas();});

    /* ONLY BAD CELLS: every face below 80% of the bar goes one flat grey, so the clusters stand
       out at full view; off, the scale's colours come back. A viewer's preference - nothing in
       the payload changes. */
    function paintFaces(m,rp){const cut=m?NEAR*qf.metrics[m].limit:0;
      entries.forEach(en=>{const mp=en.actor.getMapper();
        if(rp&&en.q[m]){
          const v=en.q[m],rgb=new Uint8Array(v.length*3);
          for(let i=0;i<v.length;i++){
            if(onlyBad&&!(v[i]>=cut)){rgb[i*3]=NEUTRAL[0];rgb[i*3+1]=NEUTRAL[1];rgb[i*3+2]=NEUTRAL[2];}
            else rp.color(v[i],rgb,i*3);}
          en.pd.getCellData().setScalars(vtk.Common.Core.vtkDataArray.newInstance(
            {name:'quality',values:rgb,numberOfComponents:3}));
          mp.setScalarModeToUseCellData();mp.setColorModeToDirectScalars();
          if(mp.setInterpolateScalarsBeforeMapping)mp.setInterpolateScalarsBeforeMapping(false);
          mp.setScalarVisibility(true);}
        else mp.setScalarVisibility(false);});}
    function setOnlyBad(b){
      onlyBad=!!b&&!!activeMetric&&areaSet.list.length>0;
      if(activeMetric&&curRp){paintFaces(activeMetric,curRp);rw.render();}
      const bt=legend&&legend.querySelector('.v-only');
      if(bt)bt.setAttribute('aria-pressed',String(onlyBad));
      return onlyBad;}

    function setMetric(m){
      if(m===activeMetric)m=null;
      cancelFly();
      /* THE COLOURS GO ON THE MERGED SURFACE, which the detail window hides while it is open -
         so painting a metric with the detail up left a legend saying "max 27.0 deg" over a mesh
         with no colour on it anywhere. Measured in a browser on the delivered coupon. The merged
         surface comes back, which is the same thing any camera move already does. */
      if(m&&closeDetail)closeDetail();
      activeMetric=m;onlyBad=false;selArea=-1;
      const rp=m?ramp(m):null;curRp=rp;
      paintFaces(m,rp);
      if(legend){legend.remove();legend=null;}
      hideProbe();
      areaSet=m?buildAreas(m):{list:[],total:0,offScreen:false};
      areaSig=m?m+'#'+shownSig():'';
      // the edges darken while the faces carry the data, so every cell still reads, and are handed
      // back through the parts panel when it is switched off, so a selected part keeps its
      // selection edge
      edgeCol=rp?_EDGE_HEAT:_EDGE;
      if(repaintParts)repaintParts();
      else entries.forEach(en=>{en.actor.getProperty().setEdgeColor(edgeCol[0],edgeCol[1],edgeCol[2]);});
      if(rp){const md=qf.metrics[m];
        legend=document.createElement('div');legend.className='v-legend';
        // an instrument scale: the bar stands upright beside the viewport, the limit is a
        // tick with its value, the top of the bar is a third past the limit; the problem areas
        // follow under it
        legend.innerHTML=`<span class="lg-h"><b>${esc(md.label)}</b>`
            +`<button class="x" type="button" title="turn colouring off" aria-label="Turn colouring off">✕</button></span>`
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
          +areasHtml(m);
        legend.querySelector('.x').onclick=()=>setMetric(null);
        wireAreas();
        // a press or a scroll on the scale is not the start of an orbit or a zoom
        shield(legend);
        host.appendChild(legend);}
      if(!markMode)hintEl.textContent=rp?HEAT_HINT:MOUSE_HINT;
      document.querySelectorAll(`#v-facts-${job} .mx-c.live, #v-heatctl-${job} a`).forEach(c=>{
        const on=c.dataset.metric===m;c.classList.toggle('on',on);
        if(!c.classList.contains('mx-c'))return;
        c.setAttribute('aria-pressed',String(on));
        const t=c.querySelector('.mx-show-t');if(t)t.textContent=on?'Showing':'Show on mesh';});
      rw.render();}

    // the figures are the controls - each one that has a field says so, with a small swatch of the
    // colour scale; a payload with no figures block gets a plain text one
    const facts=document.getElementById('v-facts-'+job);
    let wired=0;
    if(facts)avail.forEach(m=>{
      const cell=[...facts.querySelectorAll('.mx-c')].find(c=>c.dataset.key===META[m].fact);
      if(!cell)return;
      cell.classList.add('live');cell.dataset.metric=m;cell.title='Colour the mesh by this';
      cell.setAttribute('role','button');cell.tabIndex=0;cell.setAttribute('aria-pressed','false');
      cell.insertAdjacentHTML('beforeend',
        '<span class="mx-show"><i class="mx-ramp" aria-hidden="true"></i><span class="mx-show-t">Show on mesh</span></span>');
      cell.onclick=()=>setMetric(m);wired++;});
    if(wired<avail.length){
      const ctl=document.createElement('div');ctl.className='v-heatctl';ctl.id='v-heatctl-'+job;
      ctl.innerHTML='Colour by '+avail.map(m=>`<a data-metric="${m}">${esc(qf.metrics[m].label)}</a>`).join(' · ');
      ctl.querySelectorAll('a').forEach(a=>{a.onclick=()=>setMetric(a.dataset.metric);});
      shield(ctl);host.appendChild(ctl);}

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
      shield(probeEl);                 // a press on the card is not the start of an orbit
      host.appendChild(probeEl);}
    function showProbe(en,cid){
      hideProbe();
      const role=((surf.patches||[]).find(p=>p.name===en.patch)||{}).type||'';
      const cf=en.cellFaces?en.cellFaces[cid]:null;
      const have=avail.filter(m=>en.q[m]);
      const over=have.filter(m=>en.q[m][cid]>qf.metrics[m].limit);
      lastProbe={patch:en.patch,cell:cid,cellFaces:cf,over,
                 values:Object.fromEntries(have.map(m=>[m,en.q[m][cid]]))};
      const nf=en.nFaces?en.nFaces[cid]:1;
      const rows=have.map(m=>probeRow(m,en.q[m][cid],nf)).join('');
      probeEl=document.createElement('div');probeEl.className='v-probe';
      probeEl.innerHTML=`<div class="pk">${esc(en.patch)} · `
        +(nf>1?`polygon ${cid.toLocaleString()}, ${nf.toLocaleString()} delivered faces`
              :`face ${cid.toLocaleString()}`)+`</div>`
        +`<div class="pr-rows">${rows}</div>`
        +(cf!=null?`<div class="pr-cell">cell behind it: ${cf}-face ${shapeOf(cf)}</div>`:'')
        +(over.length?`<div class="why">likely: ${esc(why(cf,role))}</div>`:'');
      probeActs(()=>placeMarker(en,cid));}
    // one reading: what it is, its value, and where it sits against the gate's bar
    function probeRow(m,v,nFaces){const md=qf.metrics[m],bad=v>md.limit;
      /* A BOUND IS NOT A MEASUREMENT OF THE THING UNDER THE CURSOR, and the difference has to be
         on the card rather than in a docstring. Where one drawn polygon stands for many delivered
         faces the number is the worst of them: "over the limit" then means at least one face in
         there is, not that this is it. */
      const many=nFaces>1;
      return `<div class="pr"><span class="pr-k">${esc(md.label)}</span>`
        +`<span class="pr-v">${esc(fmt(m,v))}${many?' <small>worst of '
            +nFaces.toLocaleString()+'</small>':''}</span>`
        +`<span class="pr-s ${bad?'over':'ok'}">`
        +(bad?(many?'at least one face over':'over'):(many?'none over':'under'))
        +` the ${esc(fmt(m,md.limit))} limit</span></div>`;}
    host.addEventListener('pointerup',e=>{
      if(markMode||!activeMetric||!downXY)return;
      if(e.target&&e.target.closest&&e.target.closest('.v-probe,.v-legend,.v-pin,.v-heatctl,.v-tools'))return;
      if(Math.hypot(e.clientX-downXY[0],e.clientY-downXY[1])>6)return;
      pickCell(e,(en,cid)=>showProbe(en,cid));},true);

    /* support/debug hook - lets the browser tier drive the heatmap without pixel picking; the demo
       recorder clicks the real buttons. `extras` counts what is drawn besides the mesh's own parts
       and the user's marks: the heatmap draws nothing on top of the mesh, so it stays 0.
       areas() is the problem-area list on screen (index i is the button with data-index i, shown
       as number i+1); goArea(i) glides there and resolves true when the camera lands (false when a
       drag, wheel, Fit or another area took it first); only(b) sets "Only bad cells". */
    const areaOut=a=>({index:a.index,patch:a.patch,hint:a.hint,over:a.over,near:a.near,worst:a.worst,
                       centre:a.centre.slice(),radius:a.radius,cellSize:a.size});
    window._vdbg[job+':heat']={metrics:avail,set:setMetric,active:()=>activeMetric,
      areas:()=>areaSet.list.map(areaOut),totalAreas:()=>areaSet.total,goArea:i=>goArea(i),
      selected:()=>selArea,only:b=>setOnlyBad(b===undefined?!onlyBad:b),onlyBad:()=>onlyBad,
      flying:()=>!!fly,focal:()=>cam.getFocalPoint().slice(),dist:()=>camDist(),
      // how many CSS pixels area i's typical face spans at the camera's distance now
      cellPx:i=>{const a=areaSet.list[i];if(!a)return null;
        return a.size*(host.clientHeight||600)/(2*camDist()*tanHalf());},
      probe:(patch,cid)=>{const en=entries.find(t=>t.patch===patch);if(en)showProbe(en,cid);return lastProbe;},
      last:()=>lastProbe,legend:()=>!!legend,wired:()=>wired,
      extras:()=>ren.getActors().filter(a=>!entries.some(en=>en.actor===a)
        &&!flags.some(f=>f.actors.includes(a))).length,
      edges:()=>{const pp=entries[0].actor.getProperty();return {on:!!pp.getEdgeVisibility(),color:pp.getEdgeColor()};},
      colorOf:(patch,cid)=>{const en=entries.find(t=>t.patch===patch);if(!en)return null;
        const sc=en.pd.getCellData().getScalars();if(!sc||!en.actor.getMapper().getScalarVisibility())return null;
        const a=sc.getData();return [a[cid*3],a[cid*3+1],a[cid*3+2]];}};
  })();
}
