# Responsibility: Verify Gate C builds, smokes and records the admin console alongside the others.
# Boundaries: it reads the validation script's declarations; it runs no docker build.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
VALIDATE = REPO / "devtools" / "release" / "validate.sh"
PUBLISH = REPO / "devtools" / "release" / "publish.sh"


def test_admin_is_a_built_component():
    text = VALIDATE.read_text(encoding="utf-8")
    assert "[admin]=admin" in text, "the component-to-target map does not name the admin console"
    assert "for comp in app mesh console admin;" in text, (
        "the build loop does not cover the admin console, so no admin image is ever validated")


def test_admin_image_is_smoked_before_it_is_recorded():
    assert "admin image: serves its health route" in VALIDATE.read_text(encoding="utf-8"), (
        "an image that builds and cannot serve is a green gate and a broken deploy")


def test_admin_is_written_into_the_record():
    text = VALIDATE.read_text(encoding="utf-8")
    assert '"admin":' in text
    assert '"admin-service"' in text, "the admin component does not name the workload it becomes"


def test_publication_needs_no_per_component_change():
    assert "for comp in ${REC_COMPONENTS}" in PUBLISH.read_text(encoding="utf-8")
