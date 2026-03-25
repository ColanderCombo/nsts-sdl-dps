#!/usr/bin/env python3

import numpy as np
from halsfmt import write6, save

MM = np.array([[11.0, 12.0, 13.0],
               [21.0, 22.0, 23.0],
               [31.0, 32.0, 33.0]])

C = 2.0
I = 0  # HAL/S I=1 → Python row 0
J = 1  # HAL/S J=2 → Python row 1

output = ""

# M = MM; M(I,*) = C * MM(I,*)
M = MM.copy()
M[I, :] = C * MM[I, :]
output += write6(M)

# M = MM; M(I,*) = MM(I,*) + C * MM(J,*)
M = MM.copy()
M[I, :] = MM[I, :] + C * MM[J, :]
output += write6(M)

# Exchange rows I and J
M = MM.copy()
TEMP = M[I, :].copy()
M[I, :] = M[J, :]
M[J, :] = TEMP
output += write6(M)

save(output)
