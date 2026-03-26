import numpy as np
import argparse

def generate_signals(num_samples, period, amplitude, offset, num_columns=4):
    """Generate signals with optional phase shifts for multiple columns."""
    t = np.arange(num_samples)
    
    # Calculate number of phase groups (every 4 signals form a group)
    num_groups = int(np.ceil(num_columns / 4))
    phase_shift = 2 * np.pi / num_groups if num_groups > 1 else 0
    
    signals = []
    signal_names = []
    signal_types = ['sin', 'cos', 'triangle', 'saw']
    
    for col_idx in range(num_columns):
        # Determine signal type and phase group
        signal_type = signal_types[col_idx % 4]
        phase_group = col_idx // 4
        
        # Calculate phase with group offset
        phase = 2 * np.pi * t / period + phase_group * phase_shift
        
        # Generate signal based on type
        if signal_type == 'sin':
            signal = amplitude * np.sin(phase) + offset
        elif signal_type == 'cos':
            signal = amplitude * np.cos(phase) + offset
        elif signal_type == 'triangle':
            signal = amplitude * (2 / np.pi * np.arcsin(np.sin(phase))) + offset
        elif signal_type == 'saw':
            signal = amplitude * ((phase / np.pi) % 2 - 1) + offset
        
        signals.append(signal)
        
        # Generate name with phase suffix if multiple groups
        if num_groups > 1:
            phase_deg = int(phase_group * 360 / num_groups)
            signal_names.append(f"{signal_type}_{phase_deg}")
        else:
            signal_names.append(signal_type)
    
    return signals, signal_names

def format_value(val, use_int, decimal_comma):
    """Format a single value."""
    if use_int:
        return str(int(round(val)))
    else:
        s = f"{val:.6f}"
        if decimal_comma:
            s = s.replace('.', ',')
        return s

def main():
    parser = argparse.ArgumentParser(description="Generate CSV file with sin, cos, triangle, and saw signals.")
    parser.add_argument("output_file", type=str, help="Output CSV file name")
    parser.add_argument("-H", "--header", action="store_true", help="Include header row with signal names")
    parser.add_argument("-C", "--num-columns", type=int, default=4, help="Number of columns/signals (default: %(default)d)")
    parser.add_argument("-n", "--num-samples", type=int, default=5000, help="Number of samples (default: %(default)d)")
    parser.add_argument("-p", "--period", type=int, default=500, help="Period in samples (default: %(default)d)")
    parser.add_argument("-a", "--amplitude", type=float, default=32767, help="Amplitude (default: %(default)g)")
    parser.add_argument("-o", "--offset", type=float, default=0, help="Offset - 0 for centered, amplitude for positive only (default: %(default)g)")
    parser.add_argument("-f", "--float", action="store_true", help="Output as float (default: int)")
    parser.add_argument("-d", "--delimiter", type=str, default=",", help="Column delimiter (default: '%(default)s')")
    parser.add_argument("-c", "--decimal-comma", action="store_true", help="Use decimal comma instead of point (only for float output)")
    parser.add_argument("-A", "--align", action="store_true", help="Align columns with spaces")
    
    args = parser.parse_args()
    
    # Generate signals
    signals, signal_names = generate_signals(
        args.num_samples, args.period, args.amplitude, args.offset, args.num_columns
    )
    
    # Format data
    use_int = not args.float
    data_lines = []
    
    for i in range(args.num_samples):
        values = [format_value(sig[i], use_int, args.decimal_comma) for sig in signals]
        data_lines.append(values)
    
    # Write to file
    with open(args.output_file, 'w') as f:
        # Separator for aligned output
        sep = args.delimiter + ' ' if args.align else args.delimiter
        
        # Header
        if args.header:
            if args.align:
                # Calculate max widths
                max_widths = [len(h) for h in signal_names]
                for line in data_lines:
                    for i, val in enumerate(line):
                        max_widths[i] = max(max_widths[i], len(val))
                # Write aligned header
                header_str = sep.join(h.rjust(max_widths[i]) for i, h in enumerate(signal_names))
                f.write(header_str.rstrip() + '\n')
            else:
                f.write(sep.join(signal_names) + '\n')
        
        # Data
        if args.align:
            # Calculate max widths if not already done
            if not args.header:
                max_widths = [0] * args.num_columns
                for line in data_lines:
                    for i, val in enumerate(line):
                        max_widths[i] = max(max_widths[i], len(val))
            
            # Write aligned data
            for line in data_lines:
                line_str = sep.join(val.rjust(max_widths[i]) for i, val in enumerate(line))
                f.write(line_str.rstrip() + '\n')
        else:
            # Write normal data
            for line in data_lines:
                f.write(sep.join(line) + '\n')
    
    print(f"Generated {args.num_samples} samples in '{args.output_file}'")
    print(f"  Columns: {args.num_columns} ({', '.join(signal_names)})")
    print(f"  Period: {args.period} samples")
    print(f"  Amplitude: {args.amplitude}, Offset: {args.offset}")
    print(f"  Format: {'float' if args.float else 'int'}, Delimiter: '{args.delimiter}'")

if __name__ == '__main__':
    main()
