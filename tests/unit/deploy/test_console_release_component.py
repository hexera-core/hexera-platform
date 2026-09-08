# Responsibility: Verify Gate C builds, smokes and records the console alongside app and mesh.
# Boundaries: it reads the validation script's declarations; it runs no docker build.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
VALIDATE = REPO / "devtools" / "release" / "validate.sh"
PUBLISH = REPO / "devtools" / "release" / "publish.sh"


def test_console_is_a_built_component():
    text = VALIDATE.read_text(encoding="utf-8")
    assert "[console]=console" in text, (
        "the component-to-target map does not name the console")
    assert "for comp in app mesh console;" in text, (
        "the build loop does not cover the console, so no console image is ever validated")


def test_console_image_is_smoked_before_it_is_recorded():
    text = VALIDATE.read_text(encoding="utf-8")
    assert "console image: serves its health route" in text, (
        "an image that builds and cannot serve is a green gate and a broken deploy")


def test_console_is_written_into_the_record():
    text = VALIDATE.read_text(encoding="utf-8")
    assert '"console":' in text, "the components JSON does not declare the console"
    assert '"console-service"' in text, (
        "the console component does not name the workload it becomes")


def test_publication_needs_no_per_component_change():
    text = PUBLISH.read_text(encoding="utf-8")
    assert "for comp in ${REC_COMPONENTS}" in text, (
        "publication stopped iterating the record's components; adding one would now "
        "silently not publish it")
