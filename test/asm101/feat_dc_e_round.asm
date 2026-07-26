* A DC E (single-precision) constant ROUNDS at the 24-bit fraction
* boundary -- it is not the truncated msw of the 56-bit double
* conversion: flight FPMUPMTU +0294 'DC E'0.015'' is 3F3D70A4 (ours
* truncated to 3F3D70A3); OI30 FCMLINIT FCM26P04 'DC E'26.041667''
* = 421A0AAB confirms the rule (truncation gives ...AA).  The D
* (double) conversion is unchanged: D'0.015' stays 3F3D70A3 D70A3D71
* (rounded half-up at the 56-bit fraction).
T7       CSECT
E15      DC    E'0.015'
E26      DC    E'26.041667'
D15      DC    D'0.015'
R1       EQU   1
         LE    R1,=E'0.1'
         LTORG
         END
