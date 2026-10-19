# Responsibility: Verify a run that repaired a customer's CAD says so and hands the file over,
#                 whether or not a mesh ever arrived.
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

#: What the API returns for a run whose repair succeeded and whose mesh then failed. This is the
#: real shape: `terminalResult` maps every artifact row into `files`, so the repaired STEP and the
#: report are already on the page by the time the result card is drawn.
_REPAIRED_BUT_UNMESHED = """{
  pass:false, attempts:3, files:[
    {type:'repair_report', label:'Repair report', size:2048, url:'/d/report'},
    {type:'repaired_cad',  label:'Repaired geometry', size:29208, url:'/d/repaired'}]}"""


def test_repair_deliverables_are_picked_out_of_whatever_the_run_delivered(live):
    out = live.evaluate("""(async () => {
      const { repairDeliverables } = await import('/static/js/render/repair.js');
      return {
        // THE REPAIRED FILE LEADS. A customer whose CAD was fixed wants the geometry first and
        // the explanation second.
        both: repairDeliverables([
          {type:'repair_report', label:'r'}, {type:'repaired_cad', label:'c'}]).map(f => f.type),
        // A mesh run that repaired nothing has no repair panel to draw.
        meshOnly: repairDeliverables([{type:'mesh_bundle'}, {type:'video'}]).map(f => f.type),
        // Evidence with no geometry is still worth handing over: it explains what was found.
        reportOnly: repairDeliverables([{type:'repair_report'}]).map(f => f.type),
        empty: repairDeliverables([]).map(f => f.type),
        missing: repairDeliverables(null).map(f => f.type),
      };
    })()""")

    assert out["both"] == ["repaired_cad", "repair_report"], \
        f"the repaired geometry did not lead the panel: {out['both']}"
    assert out["meshOnly"] == [], "a run that repaired nothing offered a repair download"
    assert out["reportOnly"] == ["repair_report"]
    assert out["empty"] == [] and out["missing"] == []


def test_a_failed_run_that_repaired_the_geometry_still_offers_it(live):
    # THE REGRESSION THIS PINS. The whole deliverables panel used to be opened only when a mesh
    # was shown, so a run whose repair SUCCEEDED and whose mesh then failed told the customer
    # "Run ended - failed" and silently withheld the file we had already produced for them.
    out = live.evaluate("""(async () => {
      const { openRepairPanel } = await import('/static/js/render/repair.js');
      const anchor = document.createElement('div');
      document.body.appendChild(anchor);
      const drawn = openRepairPanel('job-1', anchor, __DATA__);
      const panel = document.getElementById('repair-job-1');
      const links = [...panel.querySelectorAll('a[download]')].map(
        a => ({href: a.getAttribute('href'), text: a.textContent.trim()}));
      // Asked twice - a re-render, a reconnect - it must not stack two panels on the page.
      openRepairPanel('job-1', anchor, __DATA__);
      const count = document.querySelectorAll('#repair-job-1').length;
      const text = panel.textContent;
      anchor.remove();
      return {drawn: !!drawn, links, count, text};
    })()""".replace("__DATA__", _REPAIRED_BUT_UNMESHED))

    assert out["drawn"] is True and out["count"] == 1, \
        f"the panel was drawn {out['count']} times"
    assert [link["href"] for link in out["links"]] == ["/d/repaired", "/d/report"], \
        f"the downloads are wrong or out of order: {out['links']}"
    # It must name what happened in the customer's terms. "Run ended - failed" is the mesh's
    # verdict; this panel is the only place the repair is mentioned at all.
    assert "repair" in out["text"].lower(), f"the panel never mentions the repair: {out['text']}"


def test_a_run_with_nothing_repaired_draws_no_panel(live):
    out = live.evaluate("""(async () => {
      const { openRepairPanel } = await import('/static/js/render/repair.js');
      const anchor = document.createElement('div');
      document.body.appendChild(anchor);
      const drawn = openRepairPanel('job-2', anchor,
        {pass:false, files:[{type:'mesh_bundle', url:'/d/mesh'}]});
      const found = document.querySelectorAll('#repair-job-2').length;
      anchor.remove();
      return {drawn: !!drawn, found};
    })()""")

    assert out["drawn"] is False and out["found"] == 0, \
        "a failed run that repaired nothing drew an empty repair panel"
