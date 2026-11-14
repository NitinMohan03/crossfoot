"""The dataset's daily rate table, read strictly. Used only for an advice in a foreign currency.

`fx_rates.csv` is `date,USD,EUR`: foreign units per pound, six decimal places, one row for
every calendar day (spec 01 §3.5). A rate becomes integer parts per million -- the
representation `ref.fx_rates` stores and every other server uses -- through the same
`rate_ppm` the reference loader applies, so the two cannot round differently. A row that is not
a date and one rate per currency is `malformed_source`, naming the file and the row: a rate
read wrongly would restate every foreign advice in sterling and nothing downstream could tell.
"""

from datetime import date

from crossfoot.db.reference import rate_ppm
from crossfoot.mcp.common.errors import MALFORMED_SOURCE, ToolError

DATE_COLUMN = "date"

#: (day, currency) -> foreign units per GBP, in parts per million.
Rates = dict[tuple[date, str], int]


def parse_rates(name: str, text: str) -> Rates:
    """Every rate in the table, keyed by day and currency."""

    def bad(row: int, why: str) -> ToolError:
        where = f"{name} row {row}" if row else name
        return ToolError(MALFORMED_SOURCE, f"{where}: {why}", file=name, row=row)

    rows = [row.removesuffix("\r") for row in text.split("\n")]
    while rows and rows[-1] == "":
        rows.pop()
    if not rows:
        raise bad(0, "the rate table is empty")
    heading = rows[0].split(",")
    currencies = heading[1:]
    if heading[0] != DATE_COLUMN or not currencies:
        raise bad(0, "the heading is not `date` followed by currency codes")
    if any(len(code) != 3 or not code.isalpha() or not code.isupper() for code in currencies):
        raise bad(0, "a currency column is not a three-letter code")
    if len(set(currencies)) != len(currencies):
        raise bad(0, "a currency column appears twice")

    rates: Rates = {}
    seen: set[date] = set()
    for number, row in enumerate(rows[1:], 1):
        fields = row.split(",")
        if len(fields) != len(heading):
            raise bad(number, f"expected {len(heading)} fields, found {len(fields)}")
        try:
            day = date.fromisoformat(fields[0])
        except ValueError as exc:
            raise bad(number, f"{fields[0]!r} is not an ISO date") from exc
        if day in seen:
            raise bad(number, f"{day.isoformat()} appears twice")
        seen.add(day)
        for currency, raw in zip(currencies, fields[1:], strict=True):
            try:
                ppm = rate_ppm(raw)
            except (ValueError, ArithmeticError) as exc:
                raise bad(number, f"{raw!r} is not a rate with at most six decimal places") from exc
            if ppm <= 0:
                raise bad(number, f"{raw!r} is not a positive rate")
            rates[(day, currency)] = ppm
    return rates
