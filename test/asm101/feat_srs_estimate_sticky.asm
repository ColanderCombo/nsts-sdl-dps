* A FORWARD branch condenses to the SRS short form only when its
* SELF-CONDENSED displacement (the distance after the branch itself
* shrinks a halfword) is under 54 -- not the full 56-halfword field.
* Flight FCMBMAN+95 keeps 'BC 07,#@LB27' RS at exactly 54 (C7F7 0036)
* while its 8 twins condense; FPMOPSCN+C condenses at 53 (DCD4) and
* FIOPDHF+13 at 48 (DBC0); no SRS branch displacement >= 0x38 exists
* anywhere in the flight image.  Layout mirrors FCMBMAN: the first
* branch's final self-condensed displacement is exactly 54 (RS, the
* very flight bytes C7F7 0036); the 8 twins land well inside the
* window and condense.
TESTSRS  CSECT
         BC    07,FAR
         BC    07,FAR
         BC    07,FAR
         BC    07,FAR
         BC    07,FAR
         BC    07,FAR
         BC    07,FAR
         BC    07,FAR
         BC    07,FAR
         DS    46H
FAR      DS    0H
         BCR   07,0
         END
