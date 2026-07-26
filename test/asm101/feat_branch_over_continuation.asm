         MACRO                                                          00000000
         SETTER &V                                                      00000000
         GBLA  &GG                                                      00000000
&GG      SETA  &V                                                       00000000
         MEND                                                           00000000
         MACRO                                                          00000000
         CONTBR &P1,&P2                                                 00000000
         GBLA  &GG                                                      00000000
.NOTSUBL AIF   ('&P2' EQ '' OR '&P2' EQ 'OR' OR '&P2' EQ 'AND' OR '&P2'X00000000
               EQ 'ORIF' OR '&P2' EQ 'ANDIF').SGLOPR                    00000000
         MNOTE 0,'took FALLTHROUGH'                                     00000000
         MEXIT                                                          00000000
.SGLOPR  SETTER 9                                                       00000000
         MNOTE 0,'after SETTER GG=[&GG]'                                00000000
         MEND                                                           00000000
T        CSECT                                                          00000000
         CONTBR (NZ)                                                    00000000
         END                                                            00000000
