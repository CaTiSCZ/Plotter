import subprocess
import os

def run_csvgen(output_file, **kwargs):
    """Run csvgen.py with specified arguments."""
    cmd = ["python", "csvgen.py", output_file]
    
    if kwargs.get('header'):
        cmd.append("-H")
    if kwargs.get('num_columns'):
        cmd.extend(["-C", str(kwargs['num_columns'])])
    if kwargs.get('num_samples'):
        cmd.extend(["-n", str(kwargs['num_samples'])])
    if kwargs.get('period'):
        cmd.extend(["-p", str(kwargs['period'])])
    if kwargs.get('amplitude') is not None:
        cmd.extend(["-a", str(kwargs['amplitude'])])
    if kwargs.get('offset') is not None:
        cmd.extend(["-o", str(kwargs['offset'])])
    if kwargs.get('float'):
        cmd.append("-f")
    if kwargs.get('delimiter'):
        cmd.extend(["-d", kwargs['delimiter']])
    if kwargs.get('decimal_comma'):
        cmd.append("-c")
    if kwargs.get('align'):
        cmd.append("-A")
    
    print(f"Generating: {output_file}")
    subprocess.run(cmd, check=True)

def main():
    # Create output directory if it doesn't exist
    output_dir = "."
    os.makedirs(output_dir, exist_ok=True)
    
    print("Generating test CSV files...\n")
    
    # 1. All combinations of header/delimiter/decimal
    print("=== Basic combinations ===")
    
    # Comma delimiter (always with decimal point)
    run_csvgen(f"{output_dir}/header_comma.csv", 
               header=True, delimiter=",")
    run_csvgen(f"{output_dir}/noheader_comma.csv", 
               header=False, delimiter=",")
    
    # Semicolon with decimal point
    run_csvgen(f"{output_dir}/header_semi_dot.csv", 
               header=True, delimiter=";", float=True, amplitude=1000, offset=0)
    run_csvgen(f"{output_dir}/noheader_semi_dot.csv", 
               header=False, delimiter=";", float=True, amplitude=1000, offset=0)
    
    # Semicolon with decimal comma
    run_csvgen(f"{output_dir}/header_semi_comma.csv", 
               header=True, delimiter=";", decimal_comma=True, float=True, amplitude=1000, offset=0)
    run_csvgen(f"{output_dir}/noheader_semi_comma.csv", 
               header=False, delimiter=";", decimal_comma=True, float=True, amplitude=1000, offset=0)
    
    # 2. Special cases (all with header, semicolon, decimal point)
    print("\n=== Special cases ===")
    
    # Float range 0-1
    run_csvgen(f"{output_dir}/float_0to1.csv", 
               header=True, delimiter=";", float=True, 
               amplitude=0.5, offset=0.5)
    
    # Float range -1 to 1
    run_csvgen(f"{output_dir}/float_-1to1.csv", 
               header=True, delimiter=";", float=True, 
               amplitude=1.0, offset=0.0)
    
    # Only 300 samples
    run_csvgen(f"{output_dir}/samples_300.csv", 
               header=True, delimiter=";", num_samples=300)
    
    # Only 1 signal
    run_csvgen(f"{output_dir}/signals_1.csv", 
               header=True, delimiter=";", num_columns=1)
    
    # Only 2 signals
    run_csvgen(f"{output_dir}/signals_2.csv", 
               header=True, delimiter=";", num_columns=2)
    
    # 6 signals
    run_csvgen(f"{output_dir}/signals_6.csv", 
               header=True, delimiter=";", num_columns=6, align=True)
    
    # 8 signals
    run_csvgen(f"{output_dir}/signals_8.csv", 
               header=True, delimiter=";", num_columns=8, align=True)
    
    # 12 signals
    run_csvgen(f"{output_dir}/signals_12.csv", 
               header=True, delimiter=";", num_columns=12, align=True)
    
    # Aligned columns
    run_csvgen(f"{output_dir}/aligned.csv", 
               header=True, delimiter=";", float=True, 
               amplitude=1000, offset=0, align=True)
    
    print("\n=== Generation complete ===")
    print(f"Total files generated: 15")
    print(f"Output directory: {output_dir}/")

if __name__ == '__main__':
    main()
