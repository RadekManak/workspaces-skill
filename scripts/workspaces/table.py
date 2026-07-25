"""Fixed-width text tables for CLI output.

A table is described by its columns -- `(HEADER, width)` pairs, where a
`None` width means "last column, no padding or truncation". Keeping the
header and its rows derived from one column list is the point: they can no
longer drift apart when a column is added or resized.
"""


def _cell(value, width):
    text = "" if value is None else str(value)
    if width is None:
        return text
    return f"{text[:width]:{width}}"


def format_header(columns):
    return " ".join(_cell(name, width) for name, width in columns)


def format_row(columns, values):
    return " ".join(_cell(value, width) for (_, width), value in zip(columns, values))


def print_table(columns, rows):
    """Print a header plus one line per row, each row a sequence of cell values."""
    print(format_header(columns))
    for values in rows:
        print(format_row(columns, values))
