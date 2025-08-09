import os
import sys
import csv
from collections import Counter

GB = 1024 * 1024 * 1024

def detect_csv_header(lines):
    """Detects the number of header rows in a CSV file given as list of lines."""
    sniffer = csv.Sniffer()
    for i in range(1, len(lines) + 1):
        try:
            has_header = sniffer.has_header(''.join(lines[:i]))
            if has_header:
                return i
        except Exception:
            continue
    return 0  # Fallback: assume no header row

def count_data_rows(file_path, max_header_length = 10):
    """Returns number of data rows (excluding header, which may have >1 row)."""
    file_size = os.path.getsize(file_path)
    if file_size <= GB:
        # Small enough, load whole file into memory
        with open(file_path, encoding='utf-8') as f:
            lines = f.readlines()
        header_test_lines = min(len(lines), max_header_length)
        header_rows = detect_csv_header(lines[:header_test_lines])
        return max(0, len(lines) - header_rows)
    else:
        # Too big, process line by line (original method)
        with open(file_path, newline='', encoding='utf-8') as f:
            # Get header rows by reading first 10 lines
            sample = []
            try:
                for _ in range(max_header_length):
                    sample.append(next(f))
            except StopIteration:
                pass  # file has less than 10 lines
            header_rows = detect_csv_header(sample)
            # Count remaining lines (data rows)
            data_rows = sum(1 for _ in f)
        return max(0, data_rows + len(sample) - header_rows)

def main(folder='.'):
    csv_files = [f for f in os.listdir(folder) if f.lower().endswith('.csv') and os.path.isfile(os.path.join(folder, f))]
    if not csv_files:
        print("Nebyl nalezen žádný CSV soubor.")
        return

    csv_count = len(csv_files)
    print(f"Nalezeno {csv_count} CSV souborů.")

    row_counts = {}
    for i, filename in enumerate(csv_files, 1):
        try:
            count = count_data_rows(os.path.join(folder, filename))
            row_counts[filename] = count
            print(f"{i:4}/{csv_count} {filename}: {count} řádků")
        except Exception as e:
            print(f"Chyba při zpracování {filename}: {e}")

    if not row_counts:
        print("Nebyl nalezen žádný validní CSV soubor.")
        return

    count_freq = Counter(row_counts.values())
    majority_count, _ = count_freq.most_common(1)[0]

    # Find files with a differing row count
    outliers = {fname: cnt for fname, cnt in row_counts.items() if cnt != majority_count}

    print(f"\nPočet řádků většiny: {majority_count}")
    if outliers:
        for fname, cnt in outliers.items():
            print(f"{fname}: {cnt}")
    else:
        print("Všechny soubory mají stejný počet řádků.")

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '.')
