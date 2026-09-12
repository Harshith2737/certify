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
import csv
import os
import re
import smtplib
import sys
import tempfile
import time
from datetime import datetime
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


REQUIRED_ASSETS = (
    "cmrtc_logo.png.jpeg",
    "iic_logo.png",
    "ds_dept_logo.png.jpeg",
    "datazoids_logo.png.jpeg",
    "sig_coordinator.png",
    "sig_hod.png.jpeg",
    "sig_director.png.jpeg",
)


def log_info(message: str) -> None:
    print(f"{Colors.BLUE}[INFO]{Colors.RESET} {message}")


def log_success(message: str) -> None:
    print(f"{Colors.GREEN}[SUCCESS]{Colors.RESET} {message}")


def log_warning(message: str) -> None:
    print(f"{Colors.YELLOW}[WARN]{Colors.RESET} {message}")


def log_error(message: str) -> None:
    print(f"{Colors.RED}[ERROR]{Colors.RESET} {message}")


def sanitize_filename(value: str) -> str:
    """Convert an identifier into a safe filename component."""
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
    """Read the canonical CSV file and keep only valid rows."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Participants CSV not found: {csv_path}")

    dataframe = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    required_columns = {"Name", "Email", "Certificate_ID"}
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
        certificate_id = str(row.get("Certificate_ID", "")).strip()

        if not name or not email or not certificate_id:
            invalid_rows += 1
            log_warning(
            f"Skipping row {index + 2}: Name, Email, and Certificate_ID are required."
            )
            continue

        if not validate_email(email):
            invalid_rows += 1
            log_warning(
                f"Skipping row {index + 2}: invalid email format for '{name}' -> '{email}'."
            )
            continue

        valid_rows.append(
            {"Name": name, "Email": email, "Certificate_ID": certificate_id}
        )

    duplicate_ids = sorted(
        certificate_id
        for certificate_id in {row["Certificate_ID"] for row in valid_rows}
        if sum(row["Certificate_ID"] == certificate_id for row in valid_rows) > 1
    )
    if duplicate_ids:
        raise ValueError(
            "Duplicate Certificate_ID values found; generation stopped safely: "
            + ", ".join(duplicate_ids)
        )

    log_info(f"Loaded {len(valid_rows)} valid participant records from {csv_path.name}.")
    if invalid_rows:
        log_warning(f"Skipped {invalid_rows} invalid row(s) during validation.")

    return valid_rows


def render_certificate_html(
    template_path: Path, participant: dict, assets_dir: Path
) -> str:
    """Render the template while retaining relative asset paths for file navigation."""
    template_content = template_path.read_text(encoding="utf-8")
    template = Template(template_content)
    return template.render(
        PARTICIPANT_NAME=participant["Name"],
        NAME=participant["Name"],
        CERTIFICATE_ID=participant["Certificate_ID"],
    )


def generate_certificate_pdf(
    html_content: str, output_pdf: Path, html_base_dir: Path, assets_dir: Path
) -> None:
    """Navigate to a temporary file so Chromium resolves relative assets reliably."""
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    failed_asset_requests: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=2)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".html", dir=html_base_dir, delete=False
            ) as temporary_html:
                temporary_html.write(html_content)
                temporary_html_path = Path(temporary_html.name)

            def record_failed_request(request) -> None:
                if request.url.startswith(assets_dir.resolve().as_uri()):
                    failed_asset_requests.append(request.url)

            page.on("requestfailed", record_failed_request)
            page.goto(temporary_html_path.as_uri(), wait_until="load")
            page.wait_for_function(
                """() => Array.from(document.querySelectorAll('img')).every(img => img.complete)"""
            )
            broken_images = page.locator("img").evaluate_all(
                """images => images.filter(img => img.naturalWidth === 0).map(img => img.alt)"""
            )
            watermark_image = page.locator(".watermark").evaluate(
                "element => getComputedStyle(element).backgroundImage"
            )
            if broken_images:
                raise RuntimeError("Required certificate images failed to load: " + ", ".join(broken_images))
            if watermark_image == "none":
                raise RuntimeError("Certificate watermark background failed to resolve.")
            if failed_asset_requests:
                raise RuntimeError("Certificate asset requests failed: " + ", ".join(failed_asset_requests))
            page.emulate_media(media="print")
            page.pdf(
                path=str(output_pdf),
                format="A4",
                landscape=True,
                print_background=True,
                prefer_css_page_size=True,
                scale=0.99,
                margin={
                    "top": "0",
                    "right": "0",
                    "bottom": "0",
                    "left": "0",
                },
            )
        finally:
            if "temporary_html_path" in locals():
                temporary_html_path.unlink(missing_ok=True)
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
        filename=pdf_path.name,
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


def send_email_batch_with_tracking(
    smtp_host: str,
    smtp_port: int,
    sender_email: str,
    app_password: str,
    recipient_email: str,
    participant_name: str,
    certificate_id: str,
    pdf_path: Path,
) -> dict:
    """Send a single email and capture detailed SMTP response information."""
    result = {
        "Participant_Name": participant_name,
        "Recipient_Email": recipient_email,
        "Certificate_ID": certificate_id,
        "PDF_Filename": pdf_path.name,
        "SMTP_Status": "NOT_ATTEMPTED",
        "SMTP_Error": "",
        "Message_ID": "",
        "Timestamp": datetime.now().isoformat(),
    }

    try:
        message = build_email_message(participant_name, recipient_email, sender_email, pdf_path)
        
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(sender_email, app_password)
            
            # sendmail returns (refused_recipients, response_code, response_string)
            refused = server.sendmail(
                sender_email,
                recipient_email,
                message.as_string()
            )
            
            # Capture Message-ID from the message
            if "Message-ID" in message:
                result["Message_ID"] = message["Message-ID"]
            
            # Check for refused recipients
            if refused:
                # refused is a dict of email -> (code, msg)
                result["SMTP_Status"] = "SMTP_REJECTED"
                for email, (code, msg) in refused.items():
                    result["SMTP_Error"] = f"Code {code}: {msg.decode('utf-8', errors='ignore')}"
                    log_error(f"SMTP rejection for {email}: {result['SMTP_Error']}")
            else:
                result["SMTP_Status"] = "SMTP_ACCEPTED"
                log_success(f"Email sent to {recipient_email}.")
        
    except smtplib.SMTPAuthenticationError as exc:
        result["SMTP_Status"] = "SMTP_ERROR"
        result["SMTP_Error"] = f"Authentication failed: {str(exc)}"
        log_error(f"SMTP authentication error: {str(exc)}")
    except smtplib.SMTPException as exc:
        result["SMTP_Status"] = "SMTP_ERROR"
        result["SMTP_Error"] = f"SMTP error: {str(exc)}"
        log_error(f"SMTP error: {str(exc)}")
    except Exception as exc:
        result["SMTP_Status"] = "SMTP_ERROR"
        result["SMTP_Error"] = str(exc)
        log_error(f"Email sending error: {str(exc)}")
    
    return result


def ensure_required_files(template_path: Path, assets_dir: Path) -> None:
    """Fail fast when the template or any required asset is unavailable."""
    if not template_path.exists():
        raise FileNotFoundError(f"Certificate template not found: {template_path}")

    missing = [name for name in REQUIRED_ASSETS if not (assets_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"Required certificate assets are missing in {assets_dir}: {', '.join(missing)}"
        )


def write_status_report(path: Path, rows: list[dict]) -> None:
    """Persist generation and email outcomes without mutating participant source data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["Certificate_ID", "Name", "Email", "Certificate_Status", "Email_Status", "Error"]
    with path.open("w", newline="", encoding="utf-8") as status_file:
        writer = csv.DictWriter(status_file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_batch_log(path: Path, results: list[dict]) -> None:
    """Write batch email sending results with detailed SMTP tracking."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "Participant_Name",
        "Recipient_Email",
        "Certificate_ID",
        "PDF_Filename",
        "SMTP_Status",
        "SMTP_Error",
        "Message_ID",
        "Timestamp",
    ]
    with path.open("w", newline="", encoding="utf-8") as batch_file:
        writer = csv.DictWriter(batch_file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)


def write_batch_summary(path: Path, batch_number: int, results: list[dict]) -> None:
    """Write summary of batch email sending campaign."""
    path.parent.mkdir(parents=True, exist_ok=True)
    
    attempted = len(results)
    smtp_accepted = sum(1 for r in results if r["SMTP_Status"] == "SMTP_ACCEPTED")
    smtp_rejected = sum(1 for r in results if r["SMTP_Status"] == "SMTP_REJECTED")
    smtp_error = sum(1 for r in results if r["SMTP_Status"] == "SMTP_ERROR")
    not_attempted = sum(1 for r in results if r["SMTP_Status"] == "NOT_ATTEMPTED")
    
    summary_entry = {
        "Batch_Number": batch_number,
        "Total_Attempted": attempted,
        "SMTP_Accepted": smtp_accepted,
        "SMTP_Rejected": smtp_rejected,
        "SMTP_Error": smtp_error,
        "Not_Attempted": not_attempted,
        "Timestamp": datetime.now().isoformat(),
    }
    
    # Append to existing summary or create new
    fields = [
        "Batch_Number",
        "Total_Attempted",
        "SMTP_Accepted",
        "SMTP_Rejected",
        "SMTP_Error",
        "Not_Attempted",
        "Timestamp",
    ]
    
    if path.exists():
        # Append to existing
        with path.open("a", newline="", encoding="utf-8") as summary_file:
            writer = csv.DictWriter(summary_file, fieldnames=fields)
            writer.writerow(summary_entry)
    else:
        # Create new
        with path.open("w", newline="", encoding="utf-8") as summary_file:
            writer = csv.DictWriter(summary_file, fieldnames=fields)
            writer.writeheader()
            writer.writerow(summary_entry)


def process_email_batch(
    batch_number: int,
    participants_batch: list[dict],
    smtp_config: dict,
    output_dir: Path,
    csv_path: Path,
) -> dict:
    """Process a batch of participants: generate PDFs and send emails with detailed tracking."""
    batch_results = []
    summary = {
        "batch_number": batch_number,
        "total_processed": 0,
        "smtp_accepted": 0,
        "smtp_rejected": 0,
        "smtp_error": 0,
        "not_attempted": 0,
        "pdf_errors": 0,
    }
    
    # Load original CSV to verify email addresses
    original_df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    csv_emails = {row["Certificate_ID"]: row["Email"] for _, row in original_df.iterrows()}
    
    log_info(f"\n{'='*70}")
    log_info(f"BATCH {batch_number} - Processing {len(participants_batch)} participants")
    log_info(f"{'='*70}")
    
    # First pass: show participants that will be processed
    log_info("\nParticipants to be sent in this batch:")
    for idx, participant in enumerate(participants_batch, 1):
        cert_id = participant["Certificate_ID"]
        name = participant["Name"]
        email = participant["Email"]
        log_info(f"  {idx}. {name} | {email} | Certificate ID: {cert_id}")
    
    # Verify SMTP configuration
    log_info("\nVerifying SMTP configuration...")
    if not all([smtp_config.get("smtp_host"), smtp_config.get("smtp_port"), 
                smtp_config.get("sender_email")]):
        log_error("SMTP configuration is incomplete.")
        return summary
    log_success("SMTP configuration verified (credentials not displayed).")
    
    # Ask for confirmation before sending
    log_warning("\n⚠️  Ready to send emails to the above participants.")
    log_info("Type 'PROCEED' to continue or press Ctrl+C to cancel:")
    try:
        user_input = input("> ").strip().upper()
        if user_input != "PROCEED":
            log_warning("Batch sending cancelled by user.")
            return summary
    except KeyboardInterrupt:
        log_warning("\nBatch sending cancelled by user.")
        return summary
    
    log_info(f"\nSending emails for Batch {batch_number}...")
    
    # Send emails for each participant in batch
    for participant in participants_batch:
        name = participant["Name"]
        email = participant["Email"]
        cert_id = participant["Certificate_ID"]
        sanitized_id = sanitize_filename(cert_id)
        pdf_path = output_dir / f"Certificate_{sanitized_id}.pdf"
        
        summary["total_processed"] += 1
        
        # Validate PDF exists
        if not pdf_path.exists():
            log_error(f"PDF not found for {name} ({cert_id}): {pdf_path}")
            batch_results.append({
                "Participant_Name": name,
                "Recipient_Email": email,
                "Certificate_ID": cert_id,
                "PDF_Filename": pdf_path.name,
                "SMTP_Status": "NOT_ATTEMPTED",
                "SMTP_Error": f"PDF file not found: {pdf_path}",
                "Message_ID": "",
                "Timestamp": datetime.now().isoformat(),
            })
            summary["pdf_errors"] += 1
            continue
        
        # Validate email matches CSV
        if cert_id in csv_emails and csv_emails[cert_id] != email:
            log_error(f"Email mismatch for {name} ({cert_id}): CSV has {csv_emails[cert_id]}, participant has {email}")
            batch_results.append({
                "Participant_Name": name,
                "Recipient_Email": email,
                "Certificate_ID": cert_id,
                "PDF_Filename": pdf_path.name,
                "SMTP_Status": "NOT_ATTEMPTED",
                "SMTP_Error": f"Email mismatch: CSV={csv_emails[cert_id]}, participant={email}",
                "Message_ID": "",
                "Timestamp": datetime.now().isoformat(),
            })
            summary["not_attempted"] += 1
            continue
        
        # Send email with tracking
        log_info(f"Sending to {name} ({email})...")
        result = send_email_batch_with_tracking(
            smtp_config["smtp_host"],
            smtp_config["smtp_port"],
            smtp_config["sender_email"],
            smtp_config["app_password"],
            email,
            name,
            cert_id,
            pdf_path,
        )
        batch_results.append(result)
        
        # Update summary
        if result["SMTP_Status"] == "SMTP_ACCEPTED":
            summary["smtp_accepted"] += 1
        elif result["SMTP_Status"] == "SMTP_REJECTED":
            summary["smtp_rejected"] += 1
        elif result["SMTP_Status"] == "SMTP_ERROR":
            summary["smtp_error"] += 1
        else:
            summary["not_attempted"] += 1
        
        time.sleep(1)
    
    # Write batch log
    batch_dir = output_dir / "email_batches"
    batch_log_path = batch_dir / f"batch_{batch_number:03d}.csv"
    write_batch_log(batch_log_path, batch_results)
    log_success(f"Batch log written to: {batch_log_path}")
    
    # Write/append to summary
    summary_path = batch_dir / "email_batch_summary.csv"
    write_batch_summary(summary_path, batch_number, batch_results)
    log_success(f"Summary updated: {summary_path}")
    
    # Print detailed report
    print("\n" + "="*70)
    print(f"BATCH {batch_number} - DETAILED RESULTS")
    print("="*70)
    for result in batch_results:
        print(f"\nParticipant: {result['Participant_Name']}")
        print(f"  Email: {result['Recipient_Email']}")
        print(f"  Certificate ID: {result['Certificate_ID']}")
        print(f"  PDF: {result['PDF_Filename']}")
        print(f"  SMTP Status: {result['SMTP_Status']}")
        if result["SMTP_Error"]:
            print(f"  SMTP Error: {result['SMTP_Error']}")
        if result["Message_ID"]:
            print(f"  Message-ID: {result['Message_ID']}")
        print(f"  Timestamp: {result['Timestamp']}")
    
    print("\n" + "="*70)
    print(f"BATCH {batch_number} - SUMMARY")
    print("="*70)
    print(f"Total Processed: {summary['total_processed']}")
    print(f"SMTP Accepted: {summary['smtp_accepted']}")
    print(f"SMTP Rejected: {summary['smtp_rejected']}")
    print(f"SMTP Error: {summary['smtp_error']}")
    print(f"Not Attempted: {summary['not_attempted']}")
    print(f"PDF Errors: {summary['pdf_errors']}")
    print("="*70)
    
    return summary


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
    parser.add_argument(
        "--test-certificate",
        action="store_true",
        help="Generate only the first valid certificate and never send email.",
    )
    parser.add_argument(
        "--batch-send",
        action="store_true",
        help="Send emails in controlled batches (max 10 per batch) with detailed tracking.",
    )
    parser.add_argument(
        "--batch-number",
        type=int,
        default=1,
        help="Batch number to process (for batch-send mode).",
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        default=Path(__file__).resolve().parent / "output_pdfs" / "certificate_status.csv",
    )
    args = parser.parse_args()

    summary = {"total_participants": 0, "valid_participants": 0, "certificates_generated": 0, "emails_sent": 0, "failures": 0, "duplicate_ids": 0, "invalid_records": 0}
    status_rows = []

    try:
        ensure_required_files(args.template, args.assets_dir)
        args.output_dir.mkdir(parents=True, exist_ok=True)

        participants = load_participants(args.csv)
        summary["valid_participants"] = len(participants)
        summary["total_participants"] = sum(1 for _ in pd.read_csv(args.csv, dtype=str, keep_default_na=False).itertuples())
        summary["invalid_records"] = summary["total_participants"] - summary["valid_participants"]
        if not participants:
            log_warning("No valid participant records found. Nothing to process.")
            return 0

        # Batch sending mode
        if args.batch_send:
            smtp_config = load_configuration()
            batch_size = 10
            batch_start_idx = (args.batch_number - 1) * batch_size
            batch_end_idx = batch_start_idx + batch_size
            batch_participants = participants[batch_start_idx:batch_end_idx]
            
            if not batch_participants:
                log_error(f"Batch {args.batch_number} is empty. Total participants: {len(participants)}")
                return 1
            
            log_info(f"Processing Batch {args.batch_number} ({len(batch_participants)} participants)")
            
            # Generate PDFs for the batch first
            for participant in batch_participants:
                participant_name = participant["Name"]
                recipient_email = participant["Email"]
                certificate_id = sanitize_filename(participant["Certificate_ID"])
                output_pdf = args.output_dir / f"Certificate_{certificate_id}.pdf"
                
                try:
                    if not output_pdf.exists():
                        rendered_html = render_certificate_html(args.template, participant, args.assets_dir)
                        generate_certificate_pdf(rendered_html, output_pdf, args.template.parent, args.assets_dir)
                        summary["certificates_generated"] += 1
                        log_success(f"Generated PDF for {participant_name}: {output_pdf.name}")
                    else:
                        log_info(f"PDF already exists for {participant_name}: {output_pdf.name}")
                        summary["certificates_generated"] += 1
                except Exception as exc:
                    summary["failures"] += 1
                    log_error(f"Failed to generate PDF for {participant_name}: {exc}")
            
            # Send emails for the batch
            batch_result = process_email_batch(
                args.batch_number,
                batch_participants,
                smtp_config,
                args.output_dir,
                args.csv,
            )
            
            summary["emails_sent"] = batch_result["smtp_accepted"]
            
            print("\n" + "="*70)
            print("Batch Sending Complete")
            print("="*70)
            print(f"Batch Number: {args.batch_number}")
            print(f"Next batch command: python app.py --batch-send --batch-number {args.batch_number + 1}")
            print("="*70)
            return 0
        
        # Standard mode (generate all and optionally send all)
        smtp_config = None if args.dry_run or args.test_certificate else load_configuration()
        selected_participants = participants[:1] if args.test_certificate else participants

        for participant in selected_participants:
            participant_name = participant["Name"]
            recipient_email = participant["Email"]
            certificate_id = sanitize_filename(participant["Certificate_ID"])
            output_pdf = args.output_dir / f"Certificate_{certificate_id}.pdf"
            status = {"Certificate_ID": participant["Certificate_ID"], "Name": participant_name, "Email": recipient_email, "Certificate_Status": "Failed", "Email_Status": "Not attempted", "Error": ""}

            try:
                rendered_html = render_certificate_html(args.template, participant, args.assets_dir)
                generate_certificate_pdf(rendered_html, output_pdf, args.template.parent, args.assets_dir)
                summary["certificates_generated"] += 1
                status["Certificate_Status"] = "Generated"
                status["Email_Status"] = "Skipped (test mode)" if args.test_certificate else "Skipped (dry-run)" if args.dry_run else "Pending"
                log_success(f"Generated PDF for {participant_name}: {output_pdf.name}")

                if args.dry_run or args.test_certificate:
                    log_info(f"Dry-run enabled: email not sent to {recipient_email}.")
                else:
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
                    status["Email_Status"] = "Sent"
                    log_success(f"Email sent to {recipient_email}.")
                    time.sleep(2)

            except Exception as exc:
                summary["failures"] += 1
                status["Error"] = str(exc)
                log_error(f"Failed for {participant_name}: {exc}")
            status_rows.append(status)

        write_status_report(args.status_file, status_rows)

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
