"""CSV file loader with automatic delimiter and header detection."""

import numpy as np


def load_csv(file_path: str) -> tuple[np.ndarray, list[str]]:
    """Load CSV file with automatic delimiter and header detection.

    Supports:
    - Comma or semicolon delimiters (auto-detected)
    - Decimal comma (when semicolon delimiter)
    - Optional header row (auto-detected)

    Returns:
        tuple: (data array shape [rows, cols], column names list)
    """
    with open(file_path, 'r') as f:
        first_line = f.readline().strip()

    # Detect delimiter: semicolon has priority (used with decimal comma)
    delimiter = ';' if ';' in first_line else ','

    # Split first line
    first_values = [v.strip() for v in first_line.split(delimiter)]

    # Detect if first line is header (try to parse first value as float)
    has_header = False
    try:
        test_val = first_values[0].replace(',', '.')
        float(test_val)
    except (ValueError, IndexError):
        has_header = True

    # Prepare converter for decimal comma when using semicolon delimiter
    if delimiter == ';':
        converter = lambda s: float(s.strip().replace(',', '.'))
    else:
        converter = lambda s: float(s.strip())

    # Load data
    if has_header:
        column_names = first_values
        data = np.loadtxt(
            file_path, delimiter=delimiter, skiprows=1,
            converters={i: converter for i in range(len(column_names))},
        )
    else:
        data = np.loadtxt(
            file_path, delimiter=delimiter,
            converters={i: converter for i in range(len(first_values))},
        )
        num_cols = data.shape[1] if data.ndim > 1 else 1
        column_names = [str(i) for i in range(num_cols)]

    # Ensure 2D even for single column
    if data.ndim == 1:
        data = data.reshape(-1, 1)

    return data, column_names
