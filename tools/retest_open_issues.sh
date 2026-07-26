#!/bin/sh

lnk101 GV7RTL.obj -o GV7RTL.fcm --json-symbols GV7RTL.json --external-syms ../csects-G9.json
fcmcmp --csect-table ../csects-G9.json GV7RTL.json GV7RTL.fcm ../G9.fcm

lnk101 DKFCCOL.obj -o DKFCCOL.fcm --json-symbols DKFCCOL.json --external-syms ../csects-G9.json
fcmcmp --csect-table ../csects-G9.json DKFCCOL.json DKFCCOL.fcm ../G9.fcm

lnk101 DKFCM0.obj -o DKFCM0.fcm --json-symbols DKFCM0.json --external-syms ../csects-G9.json
fcmcmp --csect-table ../csects-G9.json DKFCM0.json DKFCM0.fcm ../G9.fcm

lnk101 VXMMIDVA.obj -o VXMMIDVA.fcm --json-symbols VXMMIDVA.json --external-syms ../csects-G9.json
fcmcmp --csect-table ../csects-G9.json VXMMIDVA.json VXMMIDVA.fcm ../G9.fcm

lnk101 GIZM50.obj -o GIZM50.fcm --json-symbols GIZM50.json --external-syms ../csects-G9.json
fcmcmp --csect-table ../csects-G9.json GIZM50.json GIZM50.fcm ../G9.fcm

lnk101 -o linked-S2.fcm --json-symbols symbols-S2.json --external-syms ../csects-S2.json -f \
    objects/SPEPSP.obj objects/PU7MUP.obj objects/PU6MUP.obj objects/DSPSPC.obj \
    objects/DPDSPC.obj objects/PU3MUP.obj objects/PU2MUP.obj objects/SBDITEM.obj \
    objects/SAMITEM.obj objects/PU5MUP.obj objects/PU1MUP.obj objects/PU8MUP.obj \
    objects/SMNCLN.obj objects/RXYCIN.obj objects/PU4MUP.obj objects/SSRRCO.obj \
    objects/RSCSIN.obj objects/RFPPOS.obj objects/RWPPHC.obj objects/RPHCTF.obj \
    objects/RKGKIN.obj objects/RJSHAN.obj objects/RYECNV.obj objects/RBMHDW.obj \
    objects/RUDKYB.obj objects/RTVTOT.obj objects/RRPRRA.obj objects/ROVKYB.obj \
    objects/RASAUT.obj objects/PMCCYC.obj objects/SPRPRB.obj objects/SPCPPC.obj \
    objects/PMOSTA.obj objects/SBSBACKS.obj objects/SSSSTAND.obj objects/DKFCM4.obj \
    objects/SSOSPDAT.obj objects/SDTSM.obj objects/PCINIT.obj objects/SACPMU.obj \
    objects/SSSIBYP.obj objects/SPPPRECO.obj objects/STMTAB.obj objects/SRESTO.obj \
    objects/DGEGSEER.obj objects/DGOGSECO.obj objects/DKFCCOL.obj objects/DKFCM0.obj \
    objects/DMZLOG.obj objects/DIMICC.obj objects/DMAMAC.obj objects/AIDDEU.obj
fcmcmp --csect-table ../csects-S2.json symbols-S2.json linked-S2.fcm ../S2.fcm

find . -maxdepth 1 -type f \( -name 'GV7RTL.*.json' -o -name 'DKFCCOL.*.json' -o -name 'DKFCM0.*.json' -o -name 'VXMMIDVA.*.json' -o -name 'GIZM50.*.json' -o -name 'linked-S2.*.json' -o -name 'symbols-S2.*.json' -o -name 'GV7RTL.json' -o -name 'DKFCCOL.json' -o -name 'DKFCM0.json' -o -name 'VXMMIDVA.json' -o -name 'GIZM50.json' -o -name 'symbols-S2.json' \) -print | tar czf retest_results.tar.gz -T -
