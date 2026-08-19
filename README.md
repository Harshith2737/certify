# Certificate Generator

`participants.csv` is the canonical participant source. Every row must have a unique `Certificate_ID`; generated files are named only by that ID.

Install dependencies and the browser once:

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Run the required smoke test, then the full dry run:

```bash
python app.py --test-certificate
python app.py --dry-run
```

Use `python app.py --dry-run` to generate without sending email. Real email sending requires `SMTP_HOST`, `SMTP_PORT`, `SMTP_SENDER_EMAIL`, and `SMTP_APP_PASSWORD` in an untracked `.env`; never commit credentials.

Run automated checks with `pytest`.