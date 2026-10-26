"""Save every new BOM flood warning text, nationwide, for parser testing.

Usage (cron, every 30 minutes):
    python tools/capture_warnings.py OUT_DIR

BOM's FTP folder holds only current products, so each run lists the warning
text files (IDx followed by five digits, any state including WA and NT) and
saves any whose content has not been seen before. Files the integration's
parser recognises as flood warnings go to OUT_DIR/flood/; text that mentions
flooding but is NOT recognised goes to OUT_DIR/unrecognised/, which is the
interesting pile: wording the parser may be missing.
"""
from __future__ import annotations

import ftplib
import hashlib
import io
import os
import re
import sys
from datetime import UTC, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from custom_components.bom_flood import analysis as A

NAME_RE = re.compile(r"^ID[A-Z]\d{5}\.txt$")


def _looks_like_flood_warning(text: str) -> bool:
    """A warning or watch (not a forecast) that mentions flooding anywhere."""
    head = "\n".join(text.splitlines()[:12]).lower()
    is_warning = ("warning" in head or "watch" in head) and "forecast" not in head and "marine" not in head
    return is_warning and re.search(r"\bflood", text, re.IGNORECASE) is not None


def main() -> int:
    out = sys.argv[1]
    for sub in ("flood", "unrecognised"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)
    ftp = ftplib.FTP("ftp.bom.gov.au", timeout=60)
    ftp.login()
    ftp.cwd("/anon/gen/fwo/")
    saved = 0
    try:
        for name in sorted(n for n in ftp.nlst("ID*.txt") if NAME_RE.match(n)):
            buf = io.BytesIO()
            ftp.retrbinary(f"RETR {name}", buf.write)
            text = buf.getvalue().decode("utf-8", errors="replace")
            digest = hashlib.sha1(text.encode()).hexdigest()[:10]
            pid = name[:-4]
            if A.parse_flood_warning(pid, text) is not None:
                sub = "flood"
            elif _looks_like_flood_warning(text):
                sub = "unrecognised"
            else:
                continue
            if any(f.endswith(f"_{digest}.txt") for f in os.listdir(os.path.join(out, sub))):
                continue
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%MZ")
            with open(os.path.join(out, sub, f"{pid}_{stamp}_{digest}.txt"), "w") as fh:
                fh.write(text)
            saved += 1
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            ftp.close()
    if saved:
        print(f"{datetime.now(UTC).isoformat()} saved {saved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
