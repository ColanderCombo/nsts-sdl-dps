* Nested-sublist macro arguments and multilevel &SYSLIST subscripts.
* From virtualagc/virtualagc#1331: ASM101S mangled the nested sublist
* (10,(&A,&B,&C),30) into (10,((,(100,((,,200),(,,300))),)),30).  The
* HLASM rule (Assembler H GC26-3758-3 p.13, "Multilevel sublists in
* macro instruction operands") is that nesting survives verbatim and
* extra subscripts index into it; expected values below were verified
* against z390's mz390 in the issue thread.
         GBLA    &A,&B,&C
&A       SETA    100
&B       SETA    200
&C       SETA    300
         MACRO
         RON
         LCLA    &I
&I       SETA    1
.LOOP    ANOP
         MNOTE   '&SYSLIST(&I)'
&I       SETA    &I+1
         AIF     (&I LE N'&SYSLIST).LOOP
         MEND
         MACRO
         SHOW
         MNOTE   'SYSLIST(1)     = >&SYSLIST(1)<'
         MNOTE   'SYSLIST(2)     = >&SYSLIST(2)<'
         MNOTE   'SYSLIST(2,1)   = >&SYSLIST(2,1)<'
         MNOTE   'SYSLIST(2,2)   = >&SYSLIST(2,2)<'
         MNOTE   'SYSLIST(2,2,1) = >&SYSLIST(2,2,1)<'
         MNOTE   'SYSLIST(2,2,3) = >&SYSLIST(2,2,3)<'
         MNOTE   'SYSLIST(2,9)   = >&SYSLIST(2,9)<'
         MNOTE   'SYSLIST(9)     = >&SYSLIST(9)<'
         MNOTE   'SYSLIST(1,1)   = >&SYSLIST(1,1)<'
         MEND
         MACRO
         COUNTS
         LCLA    &N1,&N2,&N3,&N4
&N1      SETA    N'&SYSLIST
&N2      SETA    N'&SYSLIST(1)
&N3      SETA    N'&SYSLIST(2)
&N4      SETA    N'&SYSLIST(3)
         MNOTE   'N SYSLIST=&N1 N(1)=&N2 N(2)=&N3 N(3)=&N4'
         MEND
T        CSECT
         RON     1,2,3
         RON     1,(10,20,30),3
         RON     1,(10,(&A,&B,&C),30),3
         SHOW    1,(10,(100,200,300),30),3
* An omitted element still counts: N'(A,,C) and N'(A,B,) are both 3.
         COUNTS  X,(A,,C),(A,B,)
         END
