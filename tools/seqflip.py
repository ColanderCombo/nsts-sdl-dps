#!/usr/bin/env python3
"""Move leading PDS sequence numbers to the end of each card.

Mainframe PDS listing files as exported from virtualagc often have the
8-digit sequence number in columns 1-8 followed by the card content.
This rewrites each line into classic card-deck form: content in
columns 1-72 (blank padded), sequence number in columns 73-80.

Usage:
    seqflip.py FILE [FILE ...]        # write FILE.seq alongside each input
    seqflip.py -i FILE [FILE ...]     # rewrite files in place
    seqflip.py --check FILE ...       # report convertibility, change nothing
"""

import argparse
import re
import sys

SEQ_RE = re.compile(rb"^(\d{8}| {8})(.*?)(\r?\n?)$")


def flip_line(line, lineno, path, warnings):
    eol = line[len(line.rstrip(b"\r\n")):]
    if not line.rstrip():
        return eol
    m = SEQ_RE.match(line)
    if not m:
        raise ValueError(f"{path}:{lineno}: no leading 8-digit sequence number")
    seq, body, _ = m.groups()
    body = body.rstrip()
    if seq.isspace():
        # unnumbered card: no sequence field to carry over
        return body + eol
    if len(body) > 72:
        warnings.append(
            f"{path}:{lineno}: content is {len(body)} chars; "
            f"sequence placed past column 80"
        )
        return body + seq + eol
    return body.ljust(72) + seq + eol


def flip_file(path, warnings):
    with open(path, "rb") as f:
        lines = f.readlines()
    return b"".join(
        flip_line(line, n, path, warnings) for n, line in enumerate(lines, 1)
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", metavar="FILE")
    ap.add_argument("-i", "--in-place", action="store_true",
                    help="rewrite files in place instead of writing FILE.seq")
    ap.add_argument("--check", action="store_true",
                    help="only report whether files are convertible")
    args = ap.parse_args()

    status = 0
    for path in args.files:
        warnings = []
        try:
            converted = flip_file(path, warnings)
        except ValueError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            status = 1
            continue
        for w in warnings:
            print(f"WARN: {w}", file=sys.stderr)
        if args.check:
            print(f"ok: {path}")
            continue
        out = path if args.in_place else path + ".seq"
        with open(out, "wb") as f:
            f.write(converted)
        print(f"{path} -> {out}")
    return status


if __name__ == "__main__":
    sys.exit(main())
