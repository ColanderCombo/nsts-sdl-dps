* SPON/SPOFF store-protect capture -> ' PROT' control cards.
* PROTA interleaves per-halfword (the real BFS PSA shape); the state is ON
* at its end, so PROTB (a new CSECT) opens protected -- the state persists
* across CSECT boundaries.  PROTC manages protection but protects nothing.
PROTA    CSECT
         DC    X'1111'            unprotected (initial state off)
         SPON
         DC    X'2222'            protected
         DC    X'3333'            protected
         SPOFF
         DC    X'4444'            unprotected
         SPON
         DC    X'5555'            protected to end of csect
PROTB    CSECT
         DC    X'6666'            protected (state carried over)
         SPOFF
         DC    X'7777'            unprotected
PROTC    CSECT
         SPOFF
         DC    X'8888'            unprotected, csect still "managed"
         END
