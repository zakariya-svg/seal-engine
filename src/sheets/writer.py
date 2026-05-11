"""Google Sheets writer - appends lead rows to a target spreadsheet."""
from __future__ import annotations

import os
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from src.utils.logger import get_logger

logger = get_logger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Expected column order in the sheet.
LEAD_COLUMNS = [
    "company_name",
    "website",
    "industry",
    "company_size",
    "contact_name",
    "contact_title",
    "contact_email",
    "linkedin_url",
    "source",
    "score",
    "notes",
    "created_at",
]


class SheetsWriter:
    def __init__(self, spreadsheet_id: str | None = None, worksheet_name: str = "Leads"):
        self.spreadsheet_id = spreadsheet_id or os.environ["LEADS_SPREADSHEET_ID"]
        self.worksheet_name = worksheet_name
        self._ws: gspread.Worksheet | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> gspread.Worksheet:
        if self._ws is not None:
            return self._ws

        creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials/service_account.json")
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(self.spreadsheet_id)

        try:
            ws = sheet.worksheet(self.worksheet_name)
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(self.worksheet_name, rows=1000, cols=len(LEAD_COLUMNS))
            ws.append_row(LEAD_COLUMNS)
            logger.info("Created worksheet '%s' with headers.", self.worksheet_name)

        self._ws = ws
        return ws

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append_lead(self, lead: dict[str, Any]) -> None:
        """Append a single lead row. Missing columns default to empty string."""
        if os.getenv("DRY_RUN", "false").lower() == "true":
            logger.info("[DRY RUN] Would write lead: %s", lead.get("company_name"))
            return

        row = [str(lead.get(col, "")) for col in LEAD_COLUMNS]
        ws = self._connect()
        ws.append_row(row, value_input_option="USER_ENTERED")
        logger.info("Appended lead: %s <%s>", lead.get("company_name"), lead.get("contact_email"))

    def append_leads(self, leads: list[dict[str, Any]]) -> None:
        """Batch-append multiple lead rows."""
        for lead in leads:
            self.append_lead(lead)
        logger.info("Appended %d leads.", len(leads))

    def ensure_headers(self) -> None:
        """Ensure the first row contains the expected headers."""
        ws = self._connect()
        existing = ws.row_values(1)
        if existing != LEAD_COLUMNS:
            ws.insert_row(LEAD_COLUMNS, index=1)
            logger.info("Header row inserted/updated.")
