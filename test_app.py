from pathlib import Path

import pytest
from pypdf import PdfReader

from app import (
    ensure_required_files,
    generate_certificate_pdf,
    load_participants,
    render_certificate_html,
)


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
TEMPLATE = ROOT / "certificate.html"


def test_canonical_participants_have_unique_ids():
    participants = load_participants(ROOT / "participants.csv")
    ids = [participant["Certificate_ID"] for participant in participants]
    assert len(ids) == len(set(ids))
    assert all(ids)


def test_duplicate_ids_fail_before_generation(tmp_path):
    csv_path = tmp_path / "participants.csv"
    csv_path.write_text(
        "Name,Email,Certificate_ID\n"
        "One,one@example.com,DUP-1\n"
        "Two,two@example.com,DUP-1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate Certificate_ID"):
        load_participants(csv_path)


def test_template_renders_participant_and_certificate_id():
    participant = {"Name": "Test Participant", "Certificate_ID": "TEST-001"}
    rendered = render_certificate_html(TEMPLATE, participant, ASSETS)
    assert "Test Participant" in rendered
    assert "TEST-001" in rendered
    assert 'src="assets/cmrtc_logo.png.jpeg"' in rendered
    assert 'src="assets/sig_coordinator.png"' in rendered
    assert "url('assets/datazoids_logo.png.jpeg')" in rendered


def test_required_assets_load_and_pdf_is_a4_landscape(tmp_path):
    ensure_required_files(TEMPLATE, ASSETS)
    participant = {"Name": "Test Participant", "Certificate_ID": "TEST-001"}
    output_pdf = tmp_path / "Certificate_TEST-001.pdf"
    generate_certificate_pdf(
        render_certificate_html(TEMPLATE, participant, ASSETS),
        output_pdf,
        TEMPLATE.parent,
        ASSETS,
    )
    page = PdfReader(str(output_pdf)).pages[0]
    assert len(PdfReader(str(output_pdf)).pages) == 1
    assert float(page.mediabox.width) > float(page.mediabox.height)
    assert abs(float(page.mediabox.width) - 841.9) < 1
    assert abs(float(page.mediabox.height) - 595.3) < 1
