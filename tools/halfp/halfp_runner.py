"""Long-lived subprocess wrapper around the halfp_driver binary.

Spawns the driver once and shuttles command lines through it. Used so a
~60k-conversion test run doesn't fork the binary 60k times.
"""

import subprocess
from typing import Optional


class HalfpDriver:
    def __init__(self, driver_path):
        self._proc = subprocess.Popen(
            [driver_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )

    def close(self):
        if self._proc.stdin:
            self._proc.stdin.close()
        self._proc.wait(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _exchange(self, line: str) -> str:
        assert self._proc.stdin and self._proc.stdout
        self._proc.stdin.write(line + "\n")
        self._proc.stdin.flush()
        out = self._proc.stdout.readline()
        return out.rstrip("\n")

    # Reverse (hex -> decimal):
    def dp_to_string(self, hex16: str) -> str:
        return self._exchange("D " + hex16)

    def sp_to_string(self, hex8: str) -> str:
        return self._exchange("S " + hex8)

    # Forward (decimal -> hex):
    def dp_from_string(self, decimal: str) -> str:
        return self._exchange("d " + decimal)

    def sp_from_string(self, decimal: str) -> str:
        return self._exchange("s " + decimal)

    # MONITOR(12)-style formatting (the runtime print path):
    def dp_to_hal_string(self, hex16: str) -> str:
        return self._exchange("M " + hex16)

    def sp_to_hal_string(self, hex8: str) -> str:
        return self._exchange("m " + hex8)

    # Arithmetic primitives (DP, bit-exact to ibm_dp_*):
    def dp_neg(self, hex16: str) -> str:
        return self._exchange("_ " + hex16)

    def dp_arith(self, op: str, a: str, b: str) -> str:
        cmd = {'+': '+', '-': '-', '*': 'x', '/': '/', '**': '^'}[op]
        return self._exchange(f"{cmd} {a} {b}")

    def to_string(self, value) -> str:
        """Dispatch by FPValue.type_class."""
        if value.type_class == "DP":
            return self.dp_to_string(value.hex)
        return self.sp_to_string(value.hex)

    def from_string(self, type_class: str, decimal: str) -> str:
        if type_class == "DP":
            return self.dp_from_string(decimal)
        return self.sp_from_string(decimal)
