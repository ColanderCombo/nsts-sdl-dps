* An F/H constant with a decimal exponent is an INTEGER (value = mantissa
* x 10^exp), not a fraction of full scale: FCMCBLKS 'FPM15MIN DC F'900E6''
* assembles to 35A4E900 in the IBM OI30 listing; the fraction path clamped
* it to 7FFFFFFF (and FPMCVTFX F'1800E6' to the same).
TESTFE   CSECT
M15      DC    F'900E6'
M30      DC    F'1800E6'
HK       DC    H'2E3'
NEG      DC    F'-15E2'
FRC      DC    F'0.5'
         END
