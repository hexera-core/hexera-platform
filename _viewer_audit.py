#!/usr/bin/env python3
"""EVERY CONTROL ON THE EXACT VIEWER, DRIVEN AND MEASURED.

Rehaan's ask was "almost all functions there rn are buggy and ass - rework them to do them right
or just cut them if they aren't". This answers the first half of that: it drives each control the
way a reader would and records what observably happened, so "buggy" becomes a named list instead
of an impression. It changes nothing.
"""
import base64
import io
import json
import pathlib
import sys
import time

import numpy as np
from PIL import Image

ROOT = pathlib.Path("/home/rehaan/hexera-platform-semi")
sys.path.insert(0, str(ROOT / "tests/ui"))
import _chrome  # noqa: E402

JOB = "corintis-exact"
D = "window._vdbg['%s']" % JOB
OUT = pathlib.Path("/tmp/audit")
OUT.mkdir(exist_ok=True)
BG = np.array([9, 14, 19])

ROWS = []


def row(control, verdict, evidence):
    ROWS.append((control, verdict, evidence))
    print("%-22s %-7s %s" % (control, verdict, evidence), flush=True)


br = _chrome.Browser(_chrome.find_chrome())
page = br.new_page()


def patient(expr, limit=1800):
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            if page.evaluate("(() => { try { return !!(%s); } catch(e){ return false; } })()"
                             % expr, timeout=25):
                return time.time() - t0
        except Exception:
            pass
        time.sleep(2.0)
    raise SystemExit("gave up on " + expr)


def ev(expr, timeout=900):
    last = None
    for a in range(40):
        try:
            return page.evaluate(expr, timeout=timeout)
        except Exception as exc:
            last = exc
            time.sleep(2.0)
    raise last


def tryev(expr, timeout=900):
    """Evaluate, but hand back ('ERR', message) instead of raising."""
    try:
        return ev(expr, timeout=timeout)
    except Exception as exc:
        return {"__err": "%s: %s" % (type(exc).__name__, str(exc)[:160])}


def send(m, p, t=300):
    for _ in range(6):
        try:
            return page._send(m, p, timeout=t)
        except Exception:
            time.sleep(2.0)


def wheel(x, y, n):
    for _ in range(n):
        send("Input.dispatchMouseEvent",
             {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": -120,
              "modifiers": 0, "pointerType": "mouse"})


def draw_pct(tag):
    for _ in range(8):
        try:
            r = send("Page.captureScreenshot", {"format": "png"})
            raw = base64.b64decode(r["data"])
            (OUT / (tag + ".png")).write_bytes(raw)
            im = np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"))[320:800, 0:940]
            d = np.abs(im.astype(np.int16) - BG[None, None, :]).max(axis=2)
            return 100.0 * float((d > 10).mean())
        except Exception:
            time.sleep(3.0)
    return -1.0


def idle(limit=420):
    t0 = time.time()
    while time.time() - t0 < limit:
        if not ev("(() => !!document.getElementById('v-insp-arm-%s').dataset.busy)()" % JOB):
            return True
        time.sleep(2.0)
    return False


def exists(eid):
    return ev("(() => !!document.getElementById('%s-%s'))()" % (eid, JOB))


def click(eid):
    return ev("(() => { const b=document.getElementById('%s-%s'); if(!b) return 'missing';"
              " if(b.disabled) return 'disabled'; b.click(); return 'clicked'; })()" % (eid, JOB))


# ---------------------------------------------------------------- 1. load
page._send("Page.navigate", {"url": "http://localhost:8787/exact"})
secs = patient("%s && %s.detail" % (D, D))
time.sleep(6)
row("page load", "PASS", "payload + detail handle in %.1f s" % secs)

box = ev("""(() => { const r = document.getElementById('v-canvas-%s').getBoundingClientRect();
  return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)}; })()""" % JOB)

# ---------------------------------------------------------------- 2. the badge
badge = ev("(() => document.getElementById('v-count-%s').textContent)()" % JOB)
dr = ev("(() => %s.drawn())()" % D)
row("cell-count badge",
    "PASS" if badge and badge.strip() else "FAIL",
    "%r; drawn %s polys over %d patches" % (badge, "{:,}".format(dr["polygons"]), len(dr["patches"])))

# ---------------------------------------------------------------- 3. Fit
fit0 = ev("(() => %s.dist())()" % D)
wheel(box["x"], box["y"], 18)
time.sleep(2)
mid = ev("(() => %s.dist())()" % D)
click("v-fitbtn")
time.sleep(4)
fit1 = ev("(() => %s.dist())()" % D)
back = abs(fit1 - fit0) / max(fit0, 1e-12)
row("Fit", "PASS" if back < 0.05 else "FAIL",
    "dist %.4f -> zoomed %.4f -> Fit %.4f (%.1f%% off)" % (fit0, mid, fit1, back * 100))

# ---------------------------------------------------------------- 4. the part views
views = []
for key, label in (("", "All"), ("region:fluid", "Coolant"),
                   ("region:solid", "Metal"), ("region:chip", "Chip")):
    sel = 'header [data-view="%s"]' % key
    ok = ev("(() => { const b=document.querySelector(%s); if(b){b.click(); return true;}"
            " return false; })()" % json.dumps(sel))
    if not ok:
        views.append("%s MISSING" % label)
        continue
    time.sleep(4)
    click("v-fitbtn")
    time.sleep(5)
    pct = draw_pct("view_" + label.lower())
    d = ev("(() => %s.drawn())()" % D)
    views.append("%s %.1f%% canvas / %s polys" % (label, pct, "{:,}".format(d["polygons"])))
row("part views (4)",
    "FAIL" if any("MISSING" in v for v in views) else "PASS",
    "; ".join(views))

# back to All
ev("(() => { const b=document.querySelector('header [data-view=\"\"]');"
   " if(b)b.click(); return 1; })()")
time.sleep(3)
click("v-fitbtn")
time.sleep(4)

# ---------------------------------------------------------------- 5. Inspect
# `detail()` is a SNAPSHOT: `armed` on it is the value at the moment it was called, so arming
# and reading the flag need two calls. And the cut has to land ON the mesh - the camera's focal
# point after Fit is the bounding box's centre, which on this plate is a millimetre of solid
# metal with no boundary face anywhere near it, so cutting there correctly finds nothing. The
# window's own `deepProbe` reports what is under the middle of the screen, which is a real face.
st = tryev("(() => { %s.detail().arm(true); return {armed:%s.detail().armed}; })()" % (D, D))
cut = tryev("(async () => { const d=%s.detail(); const p=await d.deepProbe();"
            " if(!p.hit) return {__err:'nothing under the middle of the screen to cut'};"
            " await d.cut(p.hit); const e=%s.detail();"
            " return {shown:e.shown, names:e.names, hidden:e.hidden, at:p.hit,"
            " painted:e.painted, metric:e.metric}; })()" % (D, D), timeout=2400)
idle()
if isinstance(cut, dict) and "__err" in cut:
    row("Inspect (arm+cut)", "FAIL", cut["__err"])
else:
    row("Inspect (arm+cut)", "PASS" if cut.get("shown", 0) > 0 else "FAIL",
        "armed=%s; %d actors %s; hid %d merged; painted %d (%s)"
        % (st.get("armed"), cut.get("shown", 0),
           [n.split("/")[-1] for n in (cut.get("names") or [])][:4],
           cut.get("hidden", 0), cut.get("painted", 0), cut.get("metric") or "-"))

# ---------------------------------------------------------------- 6. the radius dial
dl = tryev("""(() => { const e=document.getElementById('v-insp-dial-%s');
  const r=document.getElementById('v-insp-r-%s'); if(!e||!r) return {__err:'dial missing'};
  const was=r.textContent; e.value=String(Math.max(1,+e.min||1));
  e.dispatchEvent(new Event('input',{bubbles:true}));
  const lo=r.textContent; e.value=String(+e.max||100);
  e.dispatchEvent(new Event('input',{bubbles:true}));
  const hi=r.textContent; return {was:was, lo:lo, hi:hi}; })()""" % (JOB, JOB))
if "__err" in dl:
    row("radius dial", "FAIL", dl["__err"])
else:
    row("radius dial", "PASS" if dl["lo"] != dl["hi"] else "FAIL",
        "readout %s -> min %s -> max %s" % (dl["was"], dl["lo"], dl["hi"]))

# ---------------------------------------------------------------- 7. restore the full mesh
rs = tryev("(() => { const d=%s.detail(); d.arm(false); return 1; })()" % D)
time.sleep(1)
ev("(() => { const b=document.getElementById('v-bnd-restore-%s'); if(b)b.click(); return 1; })()"
   % JOB)
time.sleep(3)
after = ev("(() => { const d=%s.detail(); return {shown:d.shown, hidden:d.hidden}; })()" % D)
pct_all = draw_pct("restored")
row("restore full mesh",
    "PASS" if after["shown"] == 0 and after["hidden"] == 0 else "FAIL",
    "shown %d hidden %d; %.1f%% canvas" % (after["shown"], after["hidden"], pct_all))

# ---------------------------------------------------------------- 8. Deep zoom
wheel(box["x"], box["y"], 42)
time.sleep(3)
dp = tryev("(async () => { const d=%s.detail(); d.deep(true); await d.deepAgain();"
           " const e=%s.detail();"
           " return {on:e.deepOn(), fits:e.deepFits(), faces:e.deepFaces(),"
           " across:+(e.deepBox().across*1000).toFixed(3), px:e.realCellPx(),"
           " keep:e.deepKeep(), cap:e.deepCap()}; })()" % (D, D), timeout=2400)
idle()
if "__err" in dp:
    row("Deep zoom", "FAIL", dp["__err"])
else:
    pct_deep = draw_pct("deep")
    row("Deep zoom", "PASS" if dp.get("fits") and dp.get("faces", 0) > 0 else "FAIL",
        "fits=%s %s real faces across %.3f mm, %.2f px/cell, cap %s; %.1f%% canvas"
        % (dp.get("fits"), "{:,}".format(dp.get("faces") or 0), dp.get("across") or 0,
           dp.get("px") or 0, dp.get("cap"), pct_deep))

# ---------------------------------------------------------------- 9. Follow
fl = tryev("(async () => { const d=%s.detail(); d.deep(false); d.follow(true);"
           " await new Promise(r=>setTimeout(r,1200));"
           " const e=%s.detail();"
           " return {following:e.following(), inRange:e.inRange(), returned:e.returned(),"
           " shown:e.shown}; })()" % (D, D), timeout=2400)
idle()
time.sleep(4)
fl2 = tryev("(() => { const e=%s.detail();"
            " return {following:e.following(), inRange:e.inRange(), returned:e.returned(),"
            " shown:e.shown}; })()" % D)
ev("(() => { %s.detail().follow(false); return 1; })()" % D)
if "__err" in fl:
    row("Follow", "FAIL", fl["__err"])
else:
    good = fl.get("following") and (fl2.get("returned", 0) > 0 or fl2.get("shown", 0) > 0)
    row("Follow", "PASS" if good else "FAIL",
        "following=%s; inRange %s returned %s shown %s"
        % (fl.get("following"), fl2.get("inRange"), fl2.get("returned"), fl2.get("shown")))

# ---------------------------------------------------------------- 10. Stream
sm = tryev("(async () => { const d=%s.detail(); d.stream(true); await d.streamAgain();"
           " const e=%s.detail();"
           " return {streaming:e.streaming(), faces:e.streamFaces(), tiles:e.streamTiles(),"
           " read:e.streamRead(), left:e.streamLeft(), dense:e.streamDense(),"
           " shown:e.shown}; })()" % (D, D), timeout=2400)
idle()
ev("(() => { %s.detail().stream(false); return 1; })()" % D)
if "__err" in sm:
    row("Stream", "FAIL", sm["__err"])
else:
    row("Stream", "PASS" if sm.get("faces", 0) > 0 else "FAIL",
        "streaming=%s faces %s tiles %s read %s left %s dense %s"
        % (sm.get("streaming"), sm.get("faces"), sm.get("tiles"), sm.get("read"),
           sm.get("left"), sm.get("dense")))

# ---------------------------------------------------------------- 11. Worst
ev("(() => { const b=document.getElementById('v-bnd-restore-%s'); if(b)b.click(); return 1; })()"
   % JOB)
time.sleep(2)
wst_state = ev("""(() => { const b=document.getElementById('v-insp-worst-%s');
  if(!b) return {missing:true};
  return {disabled:!!b.disabled, title:b.title||''}; })()""" % JOB)
if wst_state.get("missing"):
    row("Worst", "FAIL", "button missing")
elif wst_state.get("disabled"):
    row("Worst", "CUT?", "disabled: %r" % wst_state.get("title"))
else:
    click("v-insp-worst")
    time.sleep(18)
    idle()
    w = ev("(() => { const d=%s.detail();"
           " return {shown:d.shown, read:document.getElementById('v-insp-read-%s').textContent,"
           " focal:%s.view().focal}; })()" % (D, JOB, D))
    row("Worst", "PASS" if w["shown"] > 0 and "worst" in (w["read"] or "") else "FAIL",
        "%d actors; readout %r" % (w["shown"], w["read"]))

# ---------------------------------------------------------------- 12. Fly
ev("(() => { const b=document.getElementById('v-bnd-restore-%s'); if(b)b.click(); return 1; })()"
   % JOB)
time.sleep(2)
f0 = ev("(() => %s.view().focal)()" % D)
fstate = click("v-insp-fly")
time.sleep(14)
f1 = ev("(() => %s.view().focal)()" % D)
lbl_fly = ev("(() => { const b=document.getElementById('v-insp-fly-%s');"
             " return b?b.textContent:null; })()" % JOB)
ev("(() => { const b=document.getElementById('v-insp-fly-%s');"
   " if(b&&b.textContent==='Stop')b.click(); return 1; })()" % JOB)
time.sleep(3)
moved = max(abs(a - b) for a, b in zip(f0, f1))
row("Fly", "PASS" if fstate == "clicked" and moved > 1e-6 else "FAIL",
    "%s; focal moved %.6g m; button read %r mid-tour" % (fstate, moved, lbl_fly))

# ---------------------------------------------------------------- 13. Clip
cl = tryev("""(() => { const bs=[...document.querySelectorAll(
    '#v-insp-box-%s .v-insp-c[data-ax]')];
  if(!bs.length) return {__err:'clip axis buttons missing'};
  const axes=bs.map(b=>b.dataset.ax);
  const pos=document.getElementById('v-insp-clip-%s');
  const b=bs.find(b=>b.dataset.ax==='z')||bs.find(b=>b.dataset.ax);
  b.click();
  return {axes:axes, set:b.dataset.ax, posDisabled:pos?!!pos.disabled:null}; })()""" % (JOB, JOB))
if "__err" in cl:
    row("Clip", "FAIL", cl["__err"])
else:
    time.sleep(4)
    pct_clip = draw_pct("clip")
    cstate = ev("(() => %s.detail().clip)()" % D)
    posd = ev("(() => { const p=document.getElementById('v-insp-clip-%s');"
              " return p?{disabled:!!p.disabled, value:p.value}:null; })()" % JOB)
    # slide the plane and check the picture changes
    ev("""(() => { const p=document.getElementById('v-insp-clip-%s');
      if(p && !p.disabled){ p.value='20';
        p.dispatchEvent(new Event('input',{bubbles:true})); } return 1; })()""" % JOB)
    time.sleep(4)
    pct_slid = draw_pct("clip_slid")
    fstate2 = click("v-insp-flip")
    time.sleep(4)
    pct_flip = draw_pct("clip_flipped")
    ev("""(() => { const b=[...document.querySelectorAll('#v-insp-box-%s .v-insp-c[data-ax]')]
      .find(b=>b.dataset.ax===''); if(b)b.click(); return 1; })()""" % JOB)
    time.sleep(3)
    pct_off = draw_pct("clip_off")
    row("Clip", "PASS" if cstate and abs(pct_clip - pct_off) > 1.0 else "FAIL",
        "axes %s; picked %r -> state %r; position %s; canvas %.1f%% clipped / %.1f%% off"
        % (cl["axes"], cl["set"], cstate, posd, pct_clip, pct_off))
    row("Clip position", "PASS" if abs(pct_slid - pct_clip) > 0.5 else "FAIL",
        "slider 50 -> 20: %.1f%% -> %.1f%% canvas" % (pct_clip, pct_slid))
    row("Clip flip", "PASS" if fstate2 == "clicked" and abs(pct_flip - pct_slid) > 0.5 else "FAIL",
        "%s; %.1f%% -> %.1f%%" % (fstate2, pct_slid, pct_flip))

# ---------------------------------------------------------------- 14. X-ray
xr = tryev("""(() => { const e=document.getElementById('v-insp-xray-%s');
  if(!e) return {__err:'xray control missing'};
  const l=document.getElementById('v-insp-xrayr-%s');
  const was=e.value, lwas=l?l.textContent:null; e.value='60';
  e.dispatchEvent(new Event('input',{bubbles:true}));
  return {was:was, now:e.value, lwas:lwas, lnow:l?l.textContent:null}; })()""" % (JOB, JOB))
if "__err" in xr:
    row("X-ray", "FAIL", xr["__err"])
else:
    time.sleep(4)
    pct_x = draw_pct("xray")
    xs = ev("(() => %s.detail().xray)()" % D)
    ev("""(() => { const e=document.getElementById('v-insp-xray-%s'); e.value='%s';
      e.dispatchEvent(new Event('input',{bubbles:true})); return 1; })()""" % (JOB, xr["was"]))
    time.sleep(3)
    pct_x0 = draw_pct("xray_off")
    row("X-ray", "PASS" if xs and abs(pct_x - pct_x0) > 0.3 else "FAIL",
        "slider %s -> %s (label %r -> %r), state %r; canvas %.1f%% vs %.1f%%"
        % (xr["was"], xr["now"], xr.get("lwas"), xr.get("lnow"), xs, pct_x, pct_x0))

# ---------------------------------------------------------------- 15. quality heatmap
hm = tryev("""(() => { const h=window._vdbg['%s:heat'];
  if(!h) return {__err:'no heat handle'};
  return {metrics:h.metrics, active:h.active()}; })()""" % JOB)
if "__err" in hm:
    row("quality heatmap", "FAIL", hm["__err"])
else:
    got = []
    for m in (hm["metrics"] or []):
        r = tryev("""(() => { const h=window._vdbg['%s:heat']; h.set(%s);
          return {active:h.active()}; })()""" % (JOB, json.dumps(m)))
        time.sleep(3)
        p = draw_pct("heat_" + str(m))
        got.append("%s%s %.1f%%" % (m, "" if r.get("active") == m else "(NOT SET)", p))
    row("quality heatmap", "PASS" if got else "FAIL",
        "%d metrics: %s" % (len(hm["metrics"] or []), "; ".join(got)))
    ev("(() => { window._vdbg['%s:heat'].set(null); return 1; })()" % JOB)

# ---------------------------------------------------------------- 16. ParaView export
vt = tryev("""(async () => { const b=document.getElementById('v-vtk-%s');
  if(!b) return {__err:'export button missing'};
  const href=b.dataset.href||b.getAttribute('data-href')||'';
  return {label:b.textContent.trim(), disabled:!!b.disabled, href:href}; })()""" % JOB)
if "__err" in vt:
    row("ParaView export", "FAIL", vt["__err"])
else:
    row("ParaView export", "PASS" if not vt["disabled"] else "FAIL",
        "button %r disabled=%s" % (vt["label"], vt["disabled"]))

# ---------------------------------------------------------------- 17. Help
hp = tryev("""(() => { const b=document.getElementById('v-insp-help-%s');
  const bx=document.getElementById('v-insp-helpbox-%s');
  if(!b||!bx) return {__err:'help missing'};
  const before=bx.hidden||getComputedStyle(bx).display==='none';
  b.click();
  const after=bx.hidden||getComputedStyle(bx).display==='none';
  const txt=(bx.textContent||'').trim().slice(0,70);
  b.click();
  return {before:before, after:after, txt:txt}; })()""" % (JOB, JOB))
if "__err" in hp:
    row("Help", "FAIL", hp["__err"])
else:
    row("Help", "PASS" if hp["before"] != hp["after"] else "FAIL",
        "hidden %s -> %s; %r" % (hp["before"], hp["after"], hp["txt"]))

# ---------------------------------------------------------------- 18. scale legend
lg = tryev("""(() => ({bar:(document.getElementById('v-scale-i-%s')||{}).style?.width||null,
  label:(document.getElementById('v-scale-l-%s')||{}).textContent||null,
  odo:(document.getElementById('v-scale-o-%s')||{}).textContent||null}))()"""
           % (JOB, JOB, JOB))
row("scale legend", "PASS" if lg.get("label") else "FAIL",
    "bar %s label %r odometer %r" % (lg.get("bar"), lg.get("label"), lg.get("odo")))

# ---------------------------------------------------------------- errors
print("")
print("page errors:", page.page_errors[-6:], flush=True)
print("console errors:", page.console_errors[-6:], flush=True)
print("")
print("%-22s %-7s %s" % ("CONTROL", "VERDICT", "EVIDENCE"))
for c, v, e in ROWS:
    print("%-22s %-7s %s" % (c, v, e))
bad = [c for c, v, _ in ROWS if v != "PASS"]
print("")
print("NOT PASSING: %s" % (", ".join(bad) if bad else "none"))
(OUT / "audit.json").write_text(json.dumps(
    [{"control": c, "verdict": v, "evidence": e} for c, v, e in ROWS], indent=1))
br.close()
