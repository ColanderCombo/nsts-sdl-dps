#!/usr/bin/env python3

import math
from halsfmt import write6, save

cases = [
    (1, -5, 6),        # real: roots 3, 2 (exact)
    (1, -6, 9),        # real: double root 3 (D=0, exact)
    (1, 2, 5),         # complex: -1 ± 2i (exact)
    (3, 4, -10),       # real: irrational roots (original p.37 example)
    (1, 0, -4),        # real: roots ±2 (B=0, exact)
    (-2, 8, -6),       # real: roots 1, 3 (negative A, exact)
    (2, 3, 5),         # complex: -0.75 ± √31/4 i (irrational)
    (2, -10, 0),       # real: roots 5, 0 (C=0, exact)
    (0, 0, 0),         # quit
]

output = ""

for A, B, C in cases:
    A, B, C = float(A), float(B), float(C)

    # WRITE(6) ;
    output += write6()

    # WRITE(6) 'Enter A, B, C (0,0,0 to quit):';
    output += write6('Enter A, B, C (0,0,0 to quit):')

    # READ(5) A, B, C;  -- no output

    # IF A = 0 AND B = 0 AND C = 0 THEN ...
    if A == 0 and B == 0 and C == 0:
        output += write6('Quitting ...')
        break

    # D = B**2 - 4 A C;
    D = B**2 - 4 * A * C

    if D >= 0:
        # D = D**0.5;
        D = math.sqrt(D)
        # ROOT1 = (-B + D) / (2 A);
        ROOT1 = (-B + D) / (2 * A)
        # ROOT2 = (-B - D) / (2 A);
        ROOT2 = (-B - D) / (2 * A)

        # WRITE(6) 'Real roots of', A, 'X**2 +', B, 'X +', C,
        #          'are:', ROOT1, ROOT2;
        output += write6('Real roots of', A, 'X**2 +', B, 'X +', C,
                         'are:', ROOT1, ROOT2)

        # WRITE(6) 'Check:',
        #         A ROOT1**2 + B ROOT1 + C,
        #         A ROOT2**2 + B ROOT2 + C;
        check1 = A * ROOT1**2 + B * ROOT1 + C
        check2 = A * ROOT2**2 + B * ROOT2 + C
        output += write6('Check:', check1, check2)
    else:
        # TEMPORARY RE, IM, RE2, IM2;
        # D = (-D)**0.5;
        D = math.sqrt(-D)
        # RE = -B / (2 A);
        RE = -B / (2 * A)
        # IM = D / (2 A);
        IM = D / (2 * A)

        # WRITE(6) 'Complex roots of', A, 'X**2 +', B, 'X +',
        #          C, 'are:', RE, '+/-', IM, 'i';
        output += write6('Complex roots of', A, 'X**2 +', B, 'X +',
                         C, 'are:', RE, '+/-', IM, 'i')

        # RE2 = RE**2 - IM**2;
        RE2 = RE**2 - IM**2
        # IM2 = 2 RE IM;
        IM2 = 2 * RE * IM

        # WRITE(6) 'Check:', A RE2 + B RE + C, '+/-',
        #          A IM2 + B IM, 'i';
        check_re = A * RE2 + B * RE + C
        check_im = A * IM2 + B * IM
        output += write6('Check:', check_re, '+/-', check_im, 'i')

save(output)
