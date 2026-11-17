# Responsibility: Verify in a real browser that the reviewer's pictures are in view on the run box's review row while it runs and after it ends, without opening the row's folded steps, and that a replayed picture (no bytes) draws nothing.
# Boundaries: the console's own modules in the page, fed through the one event dispatch; no network.
from __future__ import annotations

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

# two different 1x1 PNGs: the renderer tells pictures apart by their bytes
RED = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
BLUE = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPj/HwADBwIAMCbHYQAAAABJRU5ErkJggg=="

SETUP = """
  const { Stage } = await import('/static/js/render/stage.js');
  const { dispatch } = await import('/static/js/core/events.js');
  Stage.mount();
  let cur = null;
  const ev = (e) => { cur = dispatch(e, Stage, cur); };
  const seen = (el) => { if (!el) return false; const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
  const pictures = () => { const row = document.querySelector('.proc [data-row=review]');
    const shots = [...document.querySelectorAll('.proc .e-shot')];
    return { count: shots.length, inView: shots.filter(seen).length,
             grid: seen(document.querySelector('.proc .e-shots')),
             folded: !!row && !row.classList.contains('open'),
             inReviewRow: shots.every(s => s.closest('.tl-node') === row) }; };
"""


def test_the_reviewers_pictures_are_in_view_without_opening_the_row(live):
    out = live.evaluate("(async () => {" + SETUP + f"""
      Stage.ensureProc();
      ev({{type:'stage', stage:'executor'}});
      ev({{type:'check', stage:'executor', statement:'Every element has positive volume', ok:true}});
      ev({{type:'stage', stage:'reviewer'}});
      ev({{type:'note', stage:'reviewer', text:'Inspecting the mesh against your brief', tone:'info'}});
      ev({{type:'screenshot', stage:'reviewer', image:'{RED}'}});
      ev({{type:'tool_call', stage:'reviewer', id:'c1', agent:'reviewer', public_label:'Adjusted inspection view', status:'started'}});
      ev({{type:'screenshot', stage:'reviewer', image:'{BLUE}'}});
      ev({{type:'screenshot', stage:'reviewer', image:'{BLUE}'}});
      const running = pictures();
      ev({{type:'verdict', stage:'reviewer', verdict:'PASS', summary:''}});
      Stage.final({{pass:true, createdAt:'2026-11-13T02:51:00Z', endedAt:'2026-11-13T03:04:42Z', attempts:1}});
      return {{running, ended: pictures()}};
    }})()""", timeout=60)
    for when in ("running", "ended"):
        p = out[when]
        # the row's steps stay folded, and the pictures are seen anyway - the grid sits outside the fold
        assert p["folded"], when
        assert p["count"] == 2 and p["inView"] == 2 and p["grid"], (when, p)
        assert p["inReviewRow"], when
    assert_clean(live, "a review with pictures")


def test_a_replayed_picture_with_no_bytes_draws_nothing(live):
    out = live.evaluate("(async () => {" + SETUP + """
      Stage.ensureProc();
      ev({type:'stage', stage:'reviewer'});
      ev({type:'screenshot', stage:'reviewer'});
      return pictures();
    })()""", timeout=60)
    assert out["count"] == 0 and not out["grid"]
