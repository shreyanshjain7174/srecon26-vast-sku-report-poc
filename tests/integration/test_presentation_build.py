from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).parents[2]


def test_claim_gated_deck_builds_with_sixteen_slides_and_notes() -> None:
    subprocess.run(
        ["node", "scripts/build_talk_deck.js"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = REPOSITORY_ROOT / "artifacts/presentation/srecon26-llm-hpa-evidence-poc.pptx"
    manifest = json.loads((REPOSITORY_ROOT / "artifacts/presentation/DECK-MANIFEST.json").read_text())

    assert manifest["slide_count"] == 16
    assert manifest["evidence_verdict"]["sha256"]
    with zipfile.ZipFile(output) as archive:
        slides = [name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
        notes = [name for name in archive.namelist() if name.startswith("ppt/notesSlides/notesSlide") and name.endswith(".xml")]
    assert len(slides) == 16
    assert len(notes) == 16
