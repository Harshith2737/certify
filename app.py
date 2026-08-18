#!/usr/bin/env python3
"""Generate personalized event certificates and email them in bulk.

Workflow:
1. Validate participant CSV data.
2. Render each certificate from an HTML template using Jinja2.
3. Convert the rendered HTML to a print-ready A4 landscape PDF using Playwright.
4. Send personalized emails with the certificate attached.
5. Log progress, warnings, and totals.

Usage:
    python app.py --dry-run
    python app.py
"""

import argparse
import os
import re
import smtplib
import sys
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from jinja2 import Template
from playwright.sync_api import sync_playwright


class Colors:
    """ANSI color helpers for terminal logging."""

    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    RESET = "\033[0m"


def log_info(message: str) -> None:
    print(f"{Colors.BLUE}[INFO]{Colors.RESET} {message}")


def log_success(message: str) -> None:
    print(f"{Colors.GREEN}[SUCCESS]{Colors.RESET} {message}")


def log_warning(message: str) -> None:
    print(f"{Colors.YELLOW}[WARN]{Colors.RESET} {message}")


def log_error(message: str) -> None:
    print(f"{Colors.RED}[ERROR]{Colors.RESET} {message}")


def sanitize_filename(value: str) -> str:
    """Convert participant names into safe filenames for the generated PDFs."""
    clean = re.sub(r"[^A-Za-z0-9]+", "_", value.strip())
    clean = clean.strip("_")
    return clean if clean else "Participant"


def load_configuration() -> dict:
    """Load SMTP configuration from environment variables."""
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")
    config = {
        "smtp_host": os.getenv("SMTP_HOST"),
        "smtp_port": os.getenv("SMTP_PORT"),
        "sender_email": os.getenv("SMTP_SENDER_EMAIL"),
        "app_password": os.getenv("SMTP_APP_PASSWORD"),
    }

    missing = [key for key, value in config.items() if not value]
    if missing:
        raise ValueError(
            "Missing SMTP environment variables: " + ", ".join(missing)
        )

    try:
        config["smtp_port"] = int(config["smtp_port"])
    except ValueError as exc:
        raise ValueError("SMTP_PORT must be an integer.") from exc

    return config


def validate_email(email: str) -> bool:
    """Simple validation for email format."""
    pattern = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
    return bool(re.fullmatch(pattern, email.strip()))


def load_participants(csv_path: Path) -> list[dict]:
    """Read the CSV file and keep only valid rows."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Participants CSV not found: {csv_path}")

    dataframe = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    required_columns = {"Name", "Email"}
    missing = required_columns - set(dataframe.columns)
    if missing:
        raise ValueError(
            f"CSV is missing required columns: {', '.join(sorted(missing))}"
        )

    valid_rows: list[dict] = []
    invalid_rows = 0

    for index, row in dataframe.iterrows():
        name = str(row.get("Name", "")).strip()
        email = str(row.get("Email", "")).strip()

        if not name or not email:
            invalid_rows += 1
            log_warning(
                f"Skipping row {index + 2}: empty Name or Email value."
            )
            continue

        if not validate_email(email):
            invalid_rows += 1
            log_warning(
                f"Skipping row {index + 2}: invalid email format for '{name}' -> '{email}'."
            )
            continue

        valid_rows.append({"Name": name, "Email": email})

    log_info(f"Loaded {len(valid_rows)} valid participant records from {csv_path.name}.")
    if invalid_rows:
        log_warning(f"Skipped {invalid_rows} invalid row(s) during validation.")

    return valid_rows


def render_certificate_html(template_path: Path, participant_name: str, assets_dir: Path) -> str:
    """Inject participant name into the certificate HTML template and expose asset URLs as file:// paths."""
    template_content = template_path.read_text(encoding="utf-8")
    template = Template(template_content)
    rendered = template.render(PARTICIPANT_NAME=participant_name)

    asset_root = assets_dir.resolve().as_posix()
    rendered = rendered.replace('src="assets/', f'src="file://{asset_root}/')
    rendered = rendered.replace("src='assets/", f"src='file://{asset_root}/")
    return rendered


def generate_certificate_pdf(html_content: str, output_pdf: Path) -> None:
    """Use Playwright Chromium to generate a high-resolution A4 landscape PDF."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=2)
        try:
            page.set_content(html_content, wait_until="load")
            page.emulate_media(media="print")
            page.pdf(
                path=str(output_pdf),
                format="A4",
                landscape=True,
                print_background=True,
                prefer_css_page_size=True,
                margin={
                    "top": "0.2in",
                    "right": "0.2in",
                    "bottom": "0.2in",
                    "left": "0.2in",
                },
            )
        finally:
            browser.close()


def build_email_message(participant_name: str, recipient_email: str, sender_email: str, pdf_path: Path) -> MIMEMultipart:
    """Create a personalized email with the attached certificate."""
    message = MIMEMultipart()
    message["From"] = sender_email
    message["To"] = recipient_email
    message["Subject"] = "Certificate of Participation | 3-Day Ideathon | DATAZOIDS"

    body = f"""Dear {participant_name},

Congratulations on successfully participating in the 3-Day Ideathon organized by the CSE - Data Science Department (DATAZOIDS) in association with IIC at CMR Technical Campus (CMRTC).

Your certificate of participation is attached herewith. We are proud to celebrate your participation and the effort you invested in this initiative.

Warm regards,
DATAZOIDS Team
CSE - Data Science Department
CMR Technical Campus (CMRTC)
"""

    message.attach(MIMEText(body, "plain"))

    with pdf_path.open("rb") as certificate_file:
        attachment = MIMEApplication(certificate_file.read(), _subtype="pdf")

    attachment.add_header(
        "Content-Disposition",
        'attachment',
        filename=f"Certificate_{sanitize_filename(participant_name)}.pdf",
    )
    message.attach(attachment)
    return message


def send_email_with_attachment(
    smtp_host: str,
    smtp_port: int,
    sender_email: str,
    app_password: str,
    recipient_email: str,
    participant_name: str,
    pdf_path: Path,
) -> None:
    """Send a single email using SMTP with STARTTLS security."""
    message = build_email_message(participant_name, recipient_email, sender_email, pdf_path)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(sender_email, app_password)
        server.send_message(message)


def ensure_required_files(template_path: Path, assets_dir: Path) -> None:
    """Validate required template and image assets before processing."""
    if not template_path.exists():
        raise FileNotFoundError(f"Certificate template not found: {template_path}")

    required_assets = [
        "cmrtc_logo.png",
        "iic_logo.png",
        "datazoids_badge.png",
        "sig_coordinator.png",
        "sig_hod.png",
        "sig_director.png",
    ]
    missing = [name for name in required_assets if not (assets_dir / name).exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing asset files in {assets_dir}: {', '.join(missing)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate and email certificates for participants.")
    parser.add_argument("--csv", type=Path, default=Path(__file__).resolve().parent / "participants.csv")
    parser.add_argument("--template", type=Path, default=Path(__file__).resolve().parent / "certificate.html")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "output_pdfs")
    parser.add_argument("--assets-dir", type=Path, default=Path(__file__).resolve().parent / "assets")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate PDFs without sending emails.",
    )
    args = parser.parse_args()

    summary = {"total_processed": 0, "emails_sent": 0, "failed": 0}

    try:
        ensure_required_files(args.template, args.assets_dir)
        args.output_dir.mkdir(parents=True, exist_ok=True)

        participants = load_participants(args.csv)
        if not participants:
            log_warning("No valid participant records found. Nothing to process.")
            return 0

        smtp_config = None if args.dry_run else load_configuration()

        for participant in participants:
            summary["total_processed"] += 1
            participant_name = participant["Name"]
            recipient_email = participant["Email"]
            sanitized_name = sanitize_filename(participant_name)
            output_pdf = args.output_dir / f"Certificate_{sanitized_name}.pdf"

            try:
                rendered_html = render_certificate_html(args.template, participant_name, args.assets_dir)
                generate_certificate_pdf(rendered_html, output_pdf)
                log_success(f"Generated PDF for {participant_name}: {output_pdf.name}")

                if args.dry_run:
                    log_info(f"Dry-run enabled: email not sent to {recipient_email}.")
                    continue

                send_email_with_attachment(
                    smtp_config["smtp_host"],
                    smtp_config["smtp_port"],
                    smtp_config["sender_email"],
                    smtp_config["app_password"],
                    recipient_email,
                    participant_name,
                    output_pdf,
                )
                summary["emails_sent"] += 1
                log_success(f"Email sent to {recipient_email}.")
                time.sleep(2)

            except Exception as exc:
                summary["failed"] += 1
                log_error(f"Failed for {participant_name}: {exc}")

        print("\n" + "=" * 70)
        print("Execution Summary")
        print("=" * 70)
        for key, value in summary.items():
            print(f"{key}: {value}")
        print("=" * 70)
        return 0

    except Exception as exc:
        log_error(f"Fatal error: {exc}")
        print("\n" + "=" * 70)
        print("Execution Summary")
        print("=" * 70)
        for key, value in summary.items():
            print(f"{key}: {value}")
        print("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
