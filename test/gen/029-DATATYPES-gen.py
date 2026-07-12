#!/usr/bin/env python3

import numpy as np
from numpy.linalg import det, inv
from halsfmt import write6, save


S = 5.0
I = 6

V = np.array([10.0, 11.0, 12.0])
X = np.array([5.0, 6.0, 2.0])
Y = np.array([1.0, 1.0, 1.0])

M = np.array([[20.0, 21.0, 22.0],
              [23.0, 24.0, 25.0],
              [26.0, 27.0, 28.0]])

M2 = np.array([[0.0, 0.3], [-0.1, 0.4]])
V1 = np.array([1.0, -1.0, 1.0])
V2 = np.array([0.5, 0.6])
M1A = np.array([[1.0, 1.0, 2.0], [0.5, -0.5, 1.0]])
M2A = np.array([[0.0, 0.5], [0.0, 1.0], [0.0, 1.0]])
M3 = np.array([[0.5, 1.0], [0.0, 1.0], [0.2, 0.4]])

A2A = np.array([[3.0, 7.0], [1.0, -4.0]])
A2B = np.array([[0.5, 1.0], [-0.5, 0.0]])
A3A = np.array([[-2.0, 2.0, -3.0], [-1.0, 1.0, 3.0], [2.0, 0.0, -1.0]])
A4A = np.array([[1,2,3,4],[5,6,7,8],[9,10,11,12],[13,14,15,16]], dtype=float)
A4B = np.array([[1,2,3,4],[8,5,6,7],[9,12,10,11],[13,14,16,15]], dtype=float)
A5A = np.array([[1,2,3,4,1],[8,5,6,7,2],[9,12,10,11,3],
                [13,14,16,15,4],[10,8,6,4,2]], dtype=float)
A6A = np.array([[-1.0, 1.5], [1.0, -1.0]])
A23A = np.array([[1.0, 0.0, 3.0], [2.0, 0.0, 4.0]])


output = ""

# WRITE(6);  -- blank line
output += write6()

output += write6('Stuff related to "Programming in HAL/S", p. 29.')

# S = V . V  (dot product)
S_vv = np.dot(V, V)
output += write6('V =', V)
output += write6('V.V =', S_vv)  # string trailing space + scalar leading sign space = 2 spaces

# VV = V * V  (cross product)
VV_cross = np.cross(V, V)
output += write6('V*V =', VV_cross)

output += write6('X =', X)
output += write6('Y =', Y)

# X.Y = dot product
output += write6('X.Y =', np.dot(X, Y))

# X*Y = cross product
output += write6('X*Y =', np.cross(X, Y))

# VV = V M  (vector times matrix)
VV_vm = V @ M
output += write6('M =', M)
output += write6('V M =', VV_vm)

# MM = V V  (outer product)
MM_outer = np.outer(V, V)
output += write6('V V =', MM_outer)

# MM = M M  (matrix multiply)
MM_mm = M @ M
output += write6('M M =', MM_mm)

# VV = V S  (vector * scalar -- but S was overwritten to V.V = 365)
VV_vs = V * S_vv
output += write6('V S =', VV_vs)

# blank line
output += write6()

output += write6('DATATYPES TEST')

# I = 10; WRITE(6) 1.5E-2 I, ' (SCALAR times INTEGER, should be 0.15.)'
# HAL/S: scalar * integer → integer result. 0.015 * 10 = 0.15 → truncated to 0.
I_val = 10
output += write6(int(0.015 * I_val), ' (SCALAR times INTEGER, should be 0.15.)')

# S = 1.5; WRITE(6) S M2 ...
S = 1.5
SM2 = S * M2
output += write6(SM2,
    ' SCALAR times MATRIX, should be 0.0 0.45 -0.15 0.6.')

output += write6(M2 * S,
    ' MATRIX times SCALAR, should be 0.0 0.45 -0.15 0.6.')

output += write6(np.outer(V1, V2),
    ' VECTOR outer product, should be 0.5 0.6 -0.5 -0.6 0.5 0.6.')

output += write6(np.outer(V2, V1),
    ' VECTOR outer product, should be 0.5 -0.5 0.6 0.6 -0.6 0.6.')

output += write6(M1A @ M2A,
    ' MATRIX times MATRIX, should be 0.0 3.5 0.0 0.75')

output += write6(M2A @ M1A,
    ' MATRIX times MATRIX, should be 0.25 -0.25 0.5 0.5 -0.5',
    ' 1.0 0.5 -0.5 1.0')

output += write6(V1 @ M3,
    ' VECTOR times MATRIX, should be 0.7 0.4.')

# Determinants section
output += write6()
output += write6('Stuff related to matrix determinants.')
output += write6(det(A2A), ' 2x2 determinant should be -19.')
output += write6(det(A3A), ' 3x3 determinant should be 18.0.')
output += write6(det(A4A), ' 4x4 determinant should be 0.0.')
output += write6(det(A4B), ' 4x4 determinant should be -348.0.')
output += write6(det(A5A), ' 5x5 determinant should be -240.0.')

# Inverses section
output += write6()
output += write6('Stuff related to matrix inverses.')

A6B = inv(A6A)
output += write6(A6B, ' 2x2 inverse should be 2.0 3.0 2.0 2.0')
output += write6(A6A @ A6B, ' should be 2x2 identity.')
output += write6(A6B @ A6A, ' should be 2x2 identity.')

A3I = inv(A3A)
output += write6(A3A @ A3I, ' should be 3x3 identity.')
output += write6(A3I @ A3A, ' should be 3x3 identity.')

output += write6('Should see similar singular-matrix error as below ...')
# A4A is singular (det=0), but we still compute inverse (roundoff)
try:
    A4I_singular = inv(A4A)
except np.linalg.LinAlgError:
    pass
output += write6('... but due to roundoff error, perhaps not!')

A4I = inv(A4B)
A4C = inv(A4B)  # A4B**(-1)
output += write6(A4B @ A4I, ' should be 4x4 identity.')
output += write6(A4I @ A4B, ' should be 4x4 identity.')
output += write6(A4C @ A4B, ' should be 4x4 identity.')

A5I = inv(A5A)
output += write6(A5A @ A5I, ' should be 5x5 identity.')
output += write6(A5I @ A5A, ' should be 5x5 identity.')

# Exponentiation section
output += write6()
output += write6('Stuff related to matrix "exponentiation".')

# A2B**2 = A2B @ A2B
output += write6(A2B @ A2B, ' should be -0.25 0.5 -0.25 -0.5.')

# A2B**(-1) = inv(A2B)
output += write6(inv(A2B), ' should be 0.0 -2.0 1.0 1.0.')

# A2B**0 = identity
output += write6(np.eye(2), ' should be 1.0 0.0 0.0 1.0.')

# A2B**T = transpose
output += write6(A2B.T, ' should be 0.5 -0.5 1.0 0.0.')

# A23A**T = transpose of 2x3
output += write6(A23A.T, ' should be 1.0 2.0 0.0 0.0 3.0 4.0.')

save(output)
