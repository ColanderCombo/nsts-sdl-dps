* An X constant with no explicit length occupies ceil(digits/2) bytes
* rounded UP to a halfword, with the value right-justified -- AP-101S
* stores hex constants halfword-wide.  Ground truth: DC X'0' -> 0000,
* DC X'F' -> 000F.  A single hex digit used to assemble as one byte (08),
* which became 0x0800 once the next item forced halfword alignment,
* instead of 0x0008.  Multi-digit constants (X'0011', X'AAAAAAAA') are
* unaffected (their byte length is already even).
T        CSECT
A0       DC    X'8'
A1       DC    X'F'
A2       DC    X'0011'
A3       DC    X'AAAAAAAA'
         END
