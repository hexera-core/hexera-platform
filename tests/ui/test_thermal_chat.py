# Responsibility: Verify a whole thermal model's chat reads in its own words in a real browser: the
# declaration shows as "Confirmed on the thermal model panel" without the intake's enum words, and
# the brief opens with the job sheet; an ordinary part's declaration and brief are unchanged.
# Boundaries: the console's own modules imported in the page; the declaration and the job sheet are
# the application's (application/geometry_confirmation, application/thermal_wording).
from __future__ import annotations

import json
import re

import pytest

pytestmark = pytest.mark.ui

FORBIDDEN = re.compile(r"(?<![a-z])(?:inlet|outlet|sure|triangle|sticker)", re.I)


def _declaration() -> str:
    from meshpipeline.api.v1.geometry import ConfirmIn
    from meshpipeline.application.geometry_confirmation import confirmation_message

    return confirmation_message(ConfirmIn(
        input_kind="solid-assembly", flow="internal", part="Set-top box, vented", openings=[],
        size_mm=[180.0, 150.0, 80.0], unit="mm", unit_fixed=True, regions=["Case", "PCB", "air"],
        check_mode="thermal", detail="standard"))


def test_the_thermal_declaration_shows_as_confirmed_on_the_panel(live):
    from meshpipeline.application.geometry_confirmation import CONFIRMED_MARK

    generic = f"{CONFIRMED_MARK} the file is the fluid volume itself, input_kind fluid-domain; the fluid flows through it."
    shown = live.evaluate(f"""(async () => {{
      const {{ displayText }} = await import('/static/js/render/geometry_form.js');
      return [displayText({json.dumps(_declaration())}), displayText({json.dumps(generic)})];
    }})()""")
    thermal, plain = shown
    assert thermal.startswith("Confirmed on the thermal model panel: “Set-top box, vented”, read from the file")
    assert "input_kind" not in thermal and "mesh_fidelity" not in thermal
    assert "for a conjugate heat-transfer solve" in thermal and "Detail level: Standard" in thermal
    assert not FORBIDDEN.search(thermal) and "flows through" not in thermal
    assert plain.startswith("Confirmed on the picture: the file is the fluid volume itself")


def test_the_brief_of_a_thermal_model_opens_with_its_job_sheet(live):
    sheet = ("Mesh “Set-top box, vented” (Flotherm, 8 parts + air, 8.7 W, 35 °C ambient) for a conjugate "
             "heat-transfer solve. Snap-grid hex mesh, Standard: up to 2M cells, at least 2 cells through "
             "every layer. Delivered as an OpenFOAM multi-region case plus the thermal model.")
    brief = {"check_mode": "thermal", "job_sheet": sheet, "purpose": "conjugate_heat_transfer",
             "purpose_label": "Conjugate heat transfer", "engine": "snapgrid",
             "engine_label": "Snap-grid multi-region (ECXML)", "input_kind": "solid-assembly",
             "mesh_fidelity_label": "Standard (your choice)", "label": "set-top box thermal mesh",
             "checks": [{"id": "boundary_roles_valid", "label": "The air box's sides are kept as the file has them",
                         "status": "pass", "detail": ""}]}
    out = live.evaluate(f"""(async () => {{
      const {{ Stage }} = await import('/static/js/render/stage.js');
      Stage.brief({json.dumps(brief)});
      const card = [...document.querySelectorAll('.req-card')].pop();
      return {{text: card.innerText, sheet: card.querySelector('.rc-sheet')?.textContent || '',
               keys: [...card.querySelectorAll('.rc-k')].map(e => e.textContent)}};
    }})()""")
    assert out["sheet"] == sheet
    assert out["keys"] == ["Engine", "Detail level", "Case"]
    assert "Job sheet" in out["text"] and "Structured brief" not in out["text"]
    assert not FORBIDDEN.search(out["text"])
