#!/usr/bin/env python3
"""
Profile HAL/S LSP components to identify performance bottlenecks.
"""

import cProfile
import pstats
import io
import os
import time
import sys
from pathlib import Path
from typing import Dict, Any

# Add the extension directory to path
sys.path.insert(0, str(Path(__file__).parent))

from hals_semantic_parser import HALSParser
from hals_lsp_server import HALSLanguageServer


def profile_single_file_parse(file_path: Path, iterations: int = 1) -> Dict[str, Any]:
    """Profile parsing a single file."""
    text = file_path.read_text(encoding='utf-8', errors='replace')
    parser = HALSParser()
    
    # Warmup
    parser.parse(text)
    
    # Profile
    pr = cProfile.Profile()
    pr.enable()
    
    start = time.perf_counter()
    for _ in range(iterations):
        parser.parse(text)
    elapsed = time.perf_counter() - start
    
    pr.disable()
    
    # Get stats
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats('cumulative')
    ps.print_stats(20)
    
    return {
        'file': str(file_path),
        'size_bytes': len(text),
        'lines': text.count('\n'),
        'iterations': iterations,
        'total_time': elapsed,
        'time_per_parse': elapsed / iterations,
        'profile': s.getvalue(),
        'symbols': len(parser.get_symbols()),
    }


def profile_workspace_index(oi_root: Path) -> Dict[str, Any]:
    """Profile building the workspace index."""
    server = HALSLanguageServer()
    
    # Count files first
    subdirs = ['SSSRC', 'APPLSRC', 'INCL80']
    file_count = 0
    total_bytes = 0
    for subdir in subdirs:
        root = oi_root / subdir
        if root.exists():
            for f in root.rglob('*'):
                if f.is_file() and not f.name.startswith('.'):
                    file_count += 1
                    try:
                        total_bytes += f.stat().st_size
                    except OSError:
                        pass
    
    print(f"Indexing {file_count} files ({total_bytes / 1024:.1f} KB) in {oi_root}")
    
    # Profile the index build
    pr = cProfile.Profile()
    pr.enable()
    
    start = time.perf_counter()
    index = server._build_workspace_index(oi_root)
    elapsed = time.perf_counter() - start
    
    pr.disable()
    
    # Get stats
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats('cumulative')
    ps.print_stats(30)
    
    return {
        'oi_root': str(oi_root),
        'file_count': file_count,
        'total_bytes': total_bytes,
        'time_seconds': elapsed,
        'files_per_second': file_count / elapsed if elapsed > 0 else 0,
        'symbol_defs': len(index.get('symbol_defs', {})),
        'include_by_norm': len(index.get('include_by_norm', {})),
        'profile': s.getvalue(),
    }


def profile_parser_methods(text: str) -> Dict[str, float]:
    """Profile individual parser methods."""
    parser = HALSParser()
    
    results = {}
    
    # Preprocess multiline
    start = time.perf_counter()
    for _ in range(100):
        processed = parser._preprocess_multiline(text)
    results['_preprocess_multiline'] = (time.perf_counter() - start) / 100
    
    # Remove comments
    start = time.perf_counter()
    for _ in range(100):
        cleaned = parser._remove_comments(processed)
    results['_remove_comments'] = (time.perf_counter() - start) / 100
    
    # Parse programs
    start = time.perf_counter()
    for _ in range(100):
        parser._parse_programs(cleaned)
    results['_parse_programs'] = (time.perf_counter() - start) / 100
    
    # Parse procedures
    start = time.perf_counter()
    for _ in range(100):
        parser._parse_procedures(cleaned)
    results['_parse_procedures'] = (time.perf_counter() - start) / 100
    
    # Parse functions
    start = time.perf_counter()
    for _ in range(100):
        parser._parse_functions(cleaned)
    results['_parse_functions'] = (time.perf_counter() - start) / 100
    
    # Parse declares
    start = time.perf_counter()
    for _ in range(100):
        parser._parse_declares(cleaned)
    results['_parse_declares'] = (time.perf_counter() - start) / 100
    
    # Parse structures
    start = time.perf_counter()
    for _ in range(100):
        parser._parse_structures(cleaned)
    results['_parse_structures'] = (time.perf_counter() - start) / 100
    
    # Parse replaces
    start = time.perf_counter()
    for _ in range(100):
        parser._parse_replaces(cleaned)
    results['_parse_replaces'] = (time.perf_counter() - start) / 100
    
    # Parse references
    start = time.perf_counter()
    for _ in range(100):
        parser._parse_references(cleaned)
    results['_parse_references'] = (time.perf_counter() - start) / 100
    
    return results


def main():
    import argparse
    
    arg_parser = argparse.ArgumentParser(description='Profile HAL/S LSP components')
    arg_parser.add_argument('--file', type=Path, help='Profile single file parsing')
    arg_parser.add_argument('--oi-root', type=Path, help='Profile workspace indexing')
    arg_parser.add_argument('--methods', type=Path, help='Profile individual parser methods on file')
    arg_parser.add_argument('--sample', type=int, default=10, help='Sample N files from OI root')
    
    args = arg_parser.parse_args()
    
    if args.file:
        print(f"\n=== Profiling single file: {args.file} ===\n")
        result = profile_single_file_parse(args.file, iterations=10)
        print(f"File: {result['file']}")
        print(f"Size: {result['size_bytes']} bytes, {result['lines']} lines")
        print(f"Symbols found: {result['symbols']}")
        print(f"Time per parse: {result['time_per_parse']*1000:.2f} ms")
        print(f"\nTop functions by cumulative time:")
        print(result['profile'])
    
    elif args.oi_root:
        print(f"\n=== Profiling workspace index: {args.oi_root} ===\n")
        result = profile_workspace_index(args.oi_root)
        print(f"Files indexed: {result['file_count']}")
        print(f"Total size: {result['total_bytes'] / 1024:.1f} KB")
        print(f"Time: {result['time_seconds']:.2f} seconds")
        print(f"Rate: {result['files_per_second']:.1f} files/second")
        print(f"Symbol defs: {result['symbol_defs']}")
        print(f"Include mappings: {result['include_by_norm']}")
        print(f"\nTop functions by cumulative time:")
        print(result['profile'])
    
    elif args.methods:
        print(f"\n=== Profiling parser methods on: {args.methods} ===\n")
        text = args.methods.read_text(encoding='utf-8', errors='replace')
        print(f"File size: {len(text)} bytes, {text.count(chr(10))} lines")
        results = profile_parser_methods(text)
        print("\nMethod timings (ms per call, avg of 100):")
        for method, time_ms in sorted(results.items(), key=lambda x: -x[1]):
            print(f"  {method}: {time_ms*1000:.3f} ms")
    
    else:
        # Default: sample files from an OI root. Set HALS_OI_ROOT to point at one
        # (e.g. .../pfs/code/OI340600), or pass --file PATH.
        oi_root = Path(os.environ.get('HALS_OI_ROOT', ''))
        if oi_root.exists():
            print(f"\n=== Sampling {args.sample} files from {oi_root} ===\n")
            
            # Collect sample files
            sample_files = []
            for subdir in ['SSSRC', 'APPLSRC']:
                root = oi_root / subdir
                if root.exists():
                    for f in root.iterdir():
                        if f.is_file() and not f.name.startswith('.'):
                            sample_files.append(f)
                            if len(sample_files) >= args.sample:
                                break
                if len(sample_files) >= args.sample:
                    break
            
            total_time = 0
            for f in sample_files:
                result = profile_single_file_parse(f, iterations=1)
                print(f"{f.name}: {result['time_per_parse']*1000:.2f} ms ({result['lines']} lines, {result['symbols']} symbols)")
                total_time += result['time_per_parse']
            
            print(f"\nTotal for {len(sample_files)} files: {total_time*1000:.2f} ms")
            print(f"Average: {total_time/len(sample_files)*1000:.2f} ms per file")
        else:
            print("Set HALS_OI_ROOT to an OI root directory (e.g. .../pfs/code/OI340600), or pass --file PATH.")


if __name__ == '__main__':
    main()
