#!/usr/bin/env python3
"""
rldanalyze <sym.json> <our.fcm> <baseline.fcm>
"""

import json
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Optional

import typer

from .addr import (
  Addr,
  AddrDisp,
  sector_decode,
  decode_zcon_hw1,
  format_zcon_fields,
  rld_flag_type_name,
)

app = typer.Typer(
  help="Follow undefined RLDs",
  add_completion=False,
  no_args_is_help=True,
  rich_markup_mode=None,
  pretty_exceptions_enable=False,
)


def analyze_rld(rld, our_value, baseline_value):
  length = rld["length"]
  sign = rld["sign"]
  direction = rld["direction"]
  existing = rld["existing"]  # unrelocated value from TXT

  result = {
    "symbol": rld["symbol"],
    "imageOffset": rld["imageOffset"],
    "flags": rld["flags"],
    "flagType": rld_flag_type_name(rld["flags"]),
    "length": length,
    "section": rld["section"],
    "sectionOffset": rld["sectionOffset"],
    "module": rld["module"],
    "existing": existing,
    "ourValue": our_value,
    "baselineValue": baseline_value,
  }
  mask = (1 << (length * 8)) - 1

  if direction == 0:
    reloc_value = (baseline_value - existing) & mask
  else:  # direction=1
    reloc_value = (existing - baseline_value) & mask

  if sign:
    target_raw = (-reloc_value) & mask
  else:
    target_raw = reloc_value

  result["targetRaw"] = target_raw
  result["targetHW"] = sector_decode(target_raw) if length == 2 else target_raw

  return result


def build_memory_map(csect_data):
  regions = []
  for name, info in csect_data.items():
    if "start" in info:
      regions.append((info["start"], info["end"], name, info.get("type", "")))
  regions.sort()
  return regions


def find_gaps(regions, max_addr=None):
  if not regions:
    return []
  if max_addr is None:
    max_addr = max(r[1] for r in regions)
  # Coalesce overlapping/adjacent:
  merged = []
  for start, end, name, typ in regions:
    if merged and start <= merged[-1][1] + 1:
      merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    else:
      merged.append((start, end))
  gaps = []
  prev_end = -1
  for start, end in merged:
    if start > prev_end + 1:
      gaps.append((prev_end + 1, start - 1))
    prev_end = max(prev_end, end)
  return gaps


def lookup_region(regions, hw_addr):
  import bisect

  starts = [r[0] for r in regions]
  idx = bisect.bisect_right(starts, hw_addr) - 1
  if idx >= 0:
    start, end, name, typ = regions[idx]
    if start <= hw_addr <= end:
      return (name, typ, hw_addr - start)
  return None


def lookup_gap(gaps, hw_addr):
  for gs, ge in gaps:
    if gs <= hw_addr <= ge:
      return (gs, ge)
  return None


def read_baseline_hw(baseline_image, hw_addr):
  off = Addr.from_hw(hw_addr).bytes
  if off + 1 < len(baseline_image):
    return (baseline_image[off] << 8) | baseline_image[off + 1]
  return None


def follow_zcon(baseline_image, zcon_hw_addr, regions, gaps):
  hw0 = read_baseline_hw(baseline_image, zcon_hw_addr)
  hw1 = read_baseline_hw(baseline_image, zcon_hw_addr + 1)
  if hw0 is None or hw1 is None:
    return None

  result = {
    "zconAddr": zcon_hw_addr,
    "hw0": hw0,
    "hw1": hw1,
    **decode_zcon_hw1(hw1),
  }

  target_hw = sector_decode(hw0)
  result["targetHW"] = target_hw

  region = lookup_region(regions, target_hw) if regions else None
  gap = lookup_gap(gaps, target_hw) if gaps else None
  if region:
    name, typ, off = region
    result["targetRegion"] = f"{name}+{off:X}" if off else name
    result["targetType"] = typ
  elif gap:
    gs, ge = Addr.from_hw(gap[0]), Addr.from_hw(gap[1])
    result["targetRegion"] = f"UNALLOCATED (gap {gs.x}–{ge.x})"
    result["targetType"] = "GAP"
  else:
    result["targetRegion"] = "UNKNOWN"
    result["targetType"] = "?"

  return result


def scan_gap_data(baseline_image, gap_start, gap_end, equiv_set):
  hits = []
  for hw in range(gap_start, gap_end + 1):
    val = read_baseline_hw(baseline_image, hw)
    if val is None:
      break
    if val not in equiv_set:
      hits.append((hw, val))
  return hits


@app.command()
def main(
  sym_json: Annotated[Path, typer.Argument(exists=True)],
  our_fcm: Annotated[Path, typer.Argument(exists=True)],
  baseline_fcm: Annotated[Path, typer.Argument(exists=True)],
  csect_table: Annotated[Optional[Path], typer.Option("--csect-table")] = None,
  show_gaps: Annotated[bool, typer.Option("--show-gaps")] = False,
  show_csects: Annotated[bool, typer.Option("--show-csects")] = False,
  scan_gaps: Annotated[bool, typer.Option("--scan-gaps")] = False,
  equiv: Annotated[str, typer.Option("--equiv")] = "0000,C6C6,C9FB",
  json_out: Annotated[Optional[Path], typer.Option("--json", "-j")] = None,
):

  equiv_set = frozenset()
  if equiv:
    vals = [int(v, 16) for v in equiv.split(",") if v.strip()]
    equiv_set = frozenset(vals)

  with open(sym_json) as f:
    sym_data = json.load(f)

  unresolved = sym_data.get("unresolvedRelocations", [])
  if not unresolved:
    print("No unresolved relocations found in symbol table.")
    raise typer.Exit(0)

  our_image = our_fcm.read_bytes()
  baseline_image = baseline_fcm.read_bytes()

  # Load csect table if provided
  regions = []
  gaps = []
  if csect_table:
    with open(csect_table) as f:
      regions = build_memory_map(json.load(f))
    gaps = find_gaps(regions)

  section_map = {}  # name -> hw address
  for s in sym_data.get("sections", []):
    section_map[s["name"]] = s["address"]

  sym_addrs = sorted((s["address"], s["name"]) for s in sym_data.get("symbols", []))
  sym_hws = [a for a, _ in sym_addrs]

  import bisect

  def resolve_hw(hw):
    idx = bisect.bisect_right(sym_hws, hw) - 1
    if idx >= 0:
      addr, name = sym_addrs[idx]
      disp = hw - addr
      if disp == 0:
        return name
      if disp < 0x1000:
        return f"{name}+{disp:X}"
    return None

  def resolve_hw_any(hw):
    sym = resolve_hw(hw)
    if sym:
      return sym
    region = lookup_region(regions, hw) if regions else None
    if region:
      name, typ, off = region
      label = f"{name}+{off:X}" if off else name
      return f"{label} ({typ})" if typ else label
    gap = lookup_gap(gaps, hw) if gaps else None
    if gap:
      gs, ge = Addr.from_hw(gap[0]), Addr.from_hw(gap[1])
      return f"UNALLOCATED (gap {gs.x}–{ge.x})"
    return None

  results = []
  for rld in unresolved:
    offset = rld["imageOffset"]
    length = rld["length"]

    if offset + length > len(our_image):
      print(f"WARNING: offset {offset:06X} beyond our FCM (size={len(our_image)})")
      continue

    our_val = int.from_bytes(our_image[offset : offset + length], "big")

    if offset + length > len(baseline_image):
      print(f"WARNING: offset {offset:06X} beyond baseline (size={len(baseline_image)})")
      continue

    bl_val = int.from_bytes(baseline_image[offset : offset + length], "big")

    analysis = analyze_rld(rld, our_val, bl_val)

    # Annotate target address
    target_hw = analysis["targetHW"]
    analysis["targetAnnotation"] = resolve_hw_any(target_hw)

    # For ZCONs, follow the pointer in the baseline
    ft = rld["flags"] & 0x70
    if ft in (0x10, 0x50) or (ft == 0x00 and regions):
      region = lookup_region(regions, target_hw) if regions else None
      if region and "ZCON" in (region[1] or ""):
        zcon_info = follow_zcon(baseline_image, target_hw, regions, gaps)
        if zcon_info:
          analysis["zcon"] = zcon_info

    results.append(analysis)

  by_symbol = defaultdict(list)
  for r in results:
    by_symbol[r["symbol"]].append(r)

  gap_data = {}  # (gap_start, gap_end) -> list of (hw_addr, hw_value)
  if scan_gaps and gaps:
    for gs, ge in gaps:
      hits = scan_gap_data(baseline_image, gs, ge, equiv_set)
      if hits:
        gap_data[(gs, ge)] = hits

  # Print memory map if requested
  if regions and (show_csects or show_gaps or scan_gaps):
    n_data_gaps = len(gap_data)
    print(f"\nMemory map: {len(regions)} csects, {len(gaps)} unallocated gap(s)"
          + (f", {n_data_gaps} with data" if scan_gaps else ""))
    print("─" * 78)
    if show_csects or scan_gaps:
      entries = []
      if show_csects:
        for start, end, name, typ in regions:
          entries.append((start, "csect", start, end, name, typ))
      for gs, ge in gaps:
        entries.append((gs, "gap", gs, ge, None, None))
      entries.sort()
      name_width = max((len(n) for _, _, _, _, n, _ in entries if n), default=8)
      for _, kind, start, end, name, typ in entries:
        size = end - start + 1
        if kind == "gap":
          hits = gap_data.get((start, end))
          if hits and size <= 2:
            # Small gap with data: dump inline
            hw_str = " ".join(f"{v:04X}" for _, v in hits)
            print(f"  {start:05X}–{end:05X}  ({size:5d} HW)  {'*** GAP ***':{name_width}s}  data: {hw_str}  <---")
          elif hits:
            print(f"  {start:05X}–{end:05X}  ({size:5d} HW)  {'*** GAP ***':{name_width}s}  {len(hits)} HW of data  <---")
          elif scan_gaps:
            if show_csects:
              print(f"  {start:05X}–{end:05X}  ({size:5d} HW)  {'*** GAP ***':{name_width}s}")
          else:
            print(f"  {start:05X}–{end:05X}  ({size:5d} HW)  {'*** GAP ***':{name_width}s}")
        else:
          typ_str = f"  [{typ}]" if typ else ""
          print(f"  {start:05X}–{end:05X}  ({size:5d} HW)  {name:{name_width}s}{typ_str}")
    else:
      for gs, ge in gaps:
        size = ge - gs + 1
        print(f"  gap {gs:05X}–{ge:05X}  ({size} HW)")
    print()

  print(f"{len(results)} unresolved relocations for {len(by_symbol)} undefined csect(s)")
  print()
  print("=" * 78)

  for sym_name in sorted(by_symbol):
    entries = by_symbol[sym_name]
    print(f"\n  CSECT: {sym_name}")
    print(f"  {'─' * 72}")

    target_addrs = set()

    for r in entries:
      img_addr = Addr(r["imageOffset"])  # byte offset -> .x = hw
      sec_off = AddrDisp(r["sectionOffset"])  # byte offset -> .hw
      ft = r["flagType"]
      flags_hex = f"{r['flags']:02X}"
      sign_str = "-" if r["flags"] & 0x80 else "+"
      dir_str = "sub" if r.get("direction") else "add"

      sec_hw = section_map.get(r["section"])
      sec_ctx = (f"{r['section']} (@ {Addr.from_hw(sec_hw).x})" if sec_hw is not None else r["section"])

      print(f"    @ {img_addr.x}  {sec_ctx} +{sec_off.hw:04X}")
      print(f"      flags={flags_hex} ({ft}, {sign_str}, {dir_str})  len={r['length']}  existing={r['existing']:0{r['length']*2}X}")
      print(f"      our={r['ourValue']:0{r['length']*2}X}  baseline={r['baselineValue']:0{r['length']*2}X}  -> target={r['targetRaw']:0{r['length']*2}X}", end="",)

      if r["length"] == 2 and r["targetRaw"] & 0x8000:
        print(f"  [S1:{r['targetRaw'] & 0x7FFF:04X}]", end="")
      print()

      annotation = r.get("targetAnnotation")
      if annotation:
        print(f"      -> {annotation}")

      # Print ZCON follow info
      zcon = r.get("zcon")
      if zcon:
        zcon_addr = Addr.from_hw(zcon["zconAddr"])
        target = Addr.from_hw(zcon["targetHW"])
        print(f"      -> ZCON @ {zcon_addr.x}: [{zcon['hw0']:04X}, {zcon['hw1']:04X}]  {format_zcon_fields(zcon)}")
        print(f"         code target: {target.x}  " f"-> {zcon['targetRegion']}")

      target_addrs.add(r["targetHW"])

    if len(target_addrs) == 1:
      hw = target_addrs.pop()
      annotation = resolve_hw_any(hw) or "UNKNOWN REGION"
      print(f"\n  ==> Consistent target: {Addr.from_hw(hw).x}  [{annotation}]")
    elif len(target_addrs) > 1:
      addrs_str = ", ".join(Addr.from_hw(a).x for a in sorted(target_addrs))
      print(f"\n  ==> Multiple targets: {addrs_str}")
      print(f"      (expected for ZCON)")

  print("\n" + "=" * 78)

  # Summary
  all_targets = defaultdict(set)
  for r in results:
    all_targets[r["targetHW"]].add(r["symbol"])

  print(f"\nDiscovered csect addresses:")
  for hw in sorted(all_targets):
    syms = ", ".join(sorted(all_targets[hw]))
    annotation = resolve_hw_any(hw) or "UNKNOWN"
    print(f"  {Addr.from_hw(hw).x} <- {syms}  [{annotation}]")

    # If this address is a ZCON, show where it points
    if regions:
      region = lookup_region(regions, hw)
      if region and "ZCON" in (region[1] or ""):
        zcon_info = follow_zcon(baseline_image, hw, regions, gaps)
        if zcon_info:
          target = Addr.from_hw(zcon_info["targetHW"])
          print(
            f"         ZCON [{zcon_info['hw0']:04X},{zcon_info['hw1']:04X}] "
            f"{format_zcon_fields(zcon_info)} "
            f"-> {target.x}  "
            f"{zcon_info['targetRegion']}")

  if json_out:
    with open(json_out, "w") as f:
      json.dump({"results": results,
                 "summary": { 
                   hex(addr): { "csects": sorted(syms),
                                "annotation": resolve_hw_any(addr),
                   } for addr, syms in all_targets.items()
                  },
                 }, f, indent=2,)
    print(f"\nsaved to: {json_out}")


if __name__ == "__main__":
    app()
