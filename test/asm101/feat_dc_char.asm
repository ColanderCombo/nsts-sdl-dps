* C-type (character) DC: was an unimplemented stub that emitted no              
* object code at all.  Covers: plain, explicit length (pad/truncate),           
* duplication, and the '' escaped-quote.  EBCDIC: A-Z = C1.. , blank=40         
* quote ' = 7D.                                                                 
DCCHAR   CSECT                                                                  
C0       DC    C'XY'                                                            
C1       DC    CL4'AB'                                                          
C2       DC    CL1'AB'                                                          
C3       DC    3C'Z'                                                            
C4       DC    C'A''B'                                                          
         END                                                                    
