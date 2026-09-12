#!/usr/bin/env python3
"""Automated TECHNOVANZA 6.0 / SIH 2026 certificate mailing.

Supports:
- one personalized email per participant with their individual certificate
- an additional team ZIP for each team leader
- alphabetical team ordering
- controlled batches with an explicit PROCEED confirmation
- dry-run mode (no SMTP connection)
- CSV audit logs

Expected participant-level CSV columns:
Team Name,SIH-PSID,Name,Email,Role,Team Leader,Leader Email,Certificate_File,Team_Zip

Role should be "Team Lead" for the leader and "Team Member" for everyone else.
"Certificate_File" is the path to the individual's PDF.
"Team_Zip" is the path to the ZIP containing the team's PDFs.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from dotenv import load_dotenv

SUBJECT_MEMBER = "Certificate of Participation | TECHNOVANZA 6.0 – SIH 2026 | {team}"
SUBJECT_LEADER = "Team Certificates | TECHNOVANZA 6.0 – SIH 2026 | {team}"

MEMBER_BODY = """Dear {name},

Greetings from CMR Technical Campus!

Congratulations on your participation in TECHNOVANZA 6.0, the Internal Hackathon conducted as part of the Smart India Hackathon (SIH) 2026 initiative at CMR Technical Campus on 12th September 2026.

Team Details:
Team Name: {team}
Problem Statement ID: {psid}

Your participation, teamwork, and contribution towards the development of an innovative solution are sincerely appreciated.

Please find your individual Certificate of Participation attached with this email for your records.

We appreciate your efforts and wish you continued success in your academic, technical, and innovation pursuits.

Warm regards,

Dr. A. Mahendar
Program Convener, SPOC – Smart India Hackathon (SIH)
Convener – Institution's Innovation Council (IIC)
CMR Technical Campus
Hyderabad
"""

LEADER_BODY = """Dear {name},

Greetings from CMR Technical Campus!

Congratulations on successfully participating in TECHNOVANZA 6.0, the Internal Hackathon conducted as part of the Smart India Hackathon (SIH) 2026 initiative at CMR Technical Campus on 12th September 2026.

As the Team Leader of {team}, we are pleased to share the team's certificates of participation.

Team Details:
Team Name: {team}
Problem Statement ID: {psid}

Team Members:
{members}

Attached with this email is a ZIP folder containing the individual certificates of all members of your team. Your own individual certificate is also included in the ZIP file.

Please retain the ZIP folder for your team's records and future reference.

We sincerely appreciate your team's enthusiasm, collaboration, problem-solving efforts, and contribution to the innovation process.

Wishing you and your team continued success in your future academic, technical, and innovation pursuits.

Warm regards,

Dr. A. Mahendar
Program Convener, SPOC – Smart India Hackathon (SIH)
Convener – Institution's Innovation Council (IIC)
CMR Technical Campus
Hyderabad
"""

@dataclass(frozen=True)
class Participant:
    team: str
    psid: str
    name: str
    email: str
    role: str
    leader: str
    leader_email: str
    certificate: Path
    team_zip: Path


def normalize_role(value: str) -> str:
    v = value.strip().casefold()
    return "Team Lead" if v in {"team lead", "team leader", "leader", "lead"} else "Team Member"


def valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value.strip()))


def load_participants(path: Path) -> list[Participant]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"Team Name", "SIH-PSID", "Name", "Email", "Role", "Team Leader", "Leader Email", "Certificate_File", "Team_Zip"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Missing required CSV columns: {', '.join(sorted(missing))}")
        out: list[Participant] = []
        for line_no, row in enumerate(reader, 2):
            name = row["Name"].strip()
            email = row["Email"].strip()
            leader_email = row["Leader Email"].strip()
            if not name or not email:
                raise ValueError(f"Row {line_no}: Name and Email are required")
            if not valid_email(email):
                raise ValueError(f"Row {line_no}: invalid participant email: {email}")
            if not valid_email(leader_email):
                raise ValueError(f"Row {line_no}: invalid leader email: {leader_email}")
            out.append(Participant(
                team=row["Team Name"].strip(),
                psid=row["SIH-PSID"].strip(),
                name=name,
                email=email,
                role=normalize_role(row["Role"]),
                leader=row["Team Leader"].strip(),
                leader_email=leader_email,
                certificate=Path(row["Certificate_File"].strip()),
                team_zip=Path(row["Team_Zip"].strip()),
            ))
    out.sort(key=lambda p: (p.team.casefold(), p.psid.casefold(), 0 if p.role == "Team Lead" else 1, p.name.casefold()))
    return out


def build_message(p: Participant, from_email: str, members: list[str]) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = p.email
    msg["Message-ID"] = make_msgid(domain=from_email.split("@", 1)[-1])
    if p.role == "Team Lead":
        msg["Subject"] = SUBJECT_LEADER.format(team=p.team)
        body = LEADER_BODY.format(name=p.name, team=p.team, psid=p.psid, members="\n".join(f"{i}. {n}" for i, n in enumerate(members, 1)))
    else:
        msg["Subject"] = SUBJECT_MEMBER.format(team=p.team)
        body = MEMBER_BODY.format(name=p.name, team=p.team, psid=p.psid)
    msg.set_content(body)
    if not p.certificate.is_file():
        raise FileNotFoundError(f"Individual certificate not found: {p.certificate}")
    msg.add_attachment(p.certificate.read_bytes(), maintype="application", subtype="pdf", filename=p.certificate.name)
    if p.role == "Team Lead":
        if not p.team_zip.is_file():
            raise FileNotFoundError(f"Team ZIP not found: {p.team_zip}")
        msg.add_attachment(p.team_zip.read_bytes(), maintype="application", subtype="zip", filename=p.team_zip.name)
    return msg


def get_config() -> dict[str, object]:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_SENDER_EMAIL", "SMTP_APP_PASSWORD"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise ValueError("Missing SMTP settings: " + ", ".join(missing))
    return {"host": os.environ["SMTP_HOST"], "port": int(os.environ["SMTP_PORT"]), "sender": os.environ["SMTP_SENDER_EMAIL"], "password": os.environ["SMTP_APP_PASSWORD"]}


def send_message(msg: EmailMessage, cfg: dict[str, object]) -> None:
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as server:
        server.starttls()
        server.login(cfg["sender"], cfg["password"])
        server.send_message(msg)


def group_members(participants: list[Participant]) -> dict[tuple[str, str], list[str]]:
    groups: dict[tuple[str, str], list[str]] = {}
    for p in participants:
        groups.setdefault((p.team, p.psid), []).append(p.name)
    return groups


def write_log(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["Team", "PSID", "Name", "Email", "Role", "Subject", "Certificate", "Team_Zip", "Status", "Error"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="TECHNOVANZA 6.0 / SIH 2026 mailer")
    ap.add_argument("--csv", type=Path, required=True)
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--batch", type=int, default=1, help="1-based batch number")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--leaders-only", action="store_true")
    ap.add_argument("--members-only", action="store_true")
    ap.add_argument("--no-confirm", action="store_true", help="Skip PROCEED prompt; use only after testing")
    ap.add_argument("--log", type=Path, default=Path("output_pdfs/email_batches/sih_2026_mail_log.csv"))
    args = ap.parse_args()
    if args.batch_size <= 0 or args.batch <= 0:
        ap.error("--batch-size and --batch must be positive")
    if args.leaders_only and args.members_only:
        ap.error("Choose only one of --leaders-only or --members-only")

    participants = load_participants(args.csv)
    if args.leaders_only:
        participants = [p for p in participants if p.role == "Team Lead"]
    elif args.members_only:
        participants = [p for p in participants if p.role != "Team Lead"]

    groups = group_members(participants)
    start = (args.batch - 1) * args.batch_size
    batch = participants[start:start + args.batch_size]
    if not batch:
        print(f"No records in batch {args.batch}.")
        return 0

    print(f"Batch {args.batch}: {len(batch)} email(s)")
    for i, p in enumerate(batch, 1):
        kind = "LEADER + ZIP" if p.role == "Team Lead" else "MEMBER"
        print(f"  {i:02d}. {p.team} | {p.name} | {p.email} | {kind}")

    cfg = None if args.dry_run else get_config()
    if not args.no_confirm:
        answer = input("Type PROCEED to send this batch: ").strip().upper()
        if answer != "PROCEED":
            print("Cancelled. No email was sent.")
            return 0

    rows: list[dict[str, str]] = []
    for p in batch:
        msg = None
        try:
            msg = build_message(p, str(cfg["sender"]) if cfg else "Dr. A. Mahendar", groups.get((p.team, p.psid), [p.name]))
            if args.dry_run:
                status = "DRY_RUN_READY"
                error = ""
            else:
                assert cfg is not None
                send_message(msg, cfg)
                status = "SMTP_ACCEPTED"
                error = ""
                print(f"Sent: {p.email}")
        except Exception as exc:
            status = "ERROR"
            error = str(exc)
            print(f"ERROR: {p.email}: {exc}", file=sys.stderr)
        rows.append({
            "Team": p.team,
            "PSID": p.psid,
            "Name": p.name,
            "Email": p.email,
            "Role": p.role,
            "Subject": msg["Subject"] if msg else "",
            "Certificate": str(p.certificate),
            "Team_Zip": str(p.team_zip) if p.role == "Team Lead" else "",
            "Status": status,
            "Error": error,
        })

    write_log(args.log, rows)
    print(f"Audit log: {args.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
