#
# CSECT naming conventions
#   ref.USA-001556/p.164
#
#   CCNNNNNN
#
#   CC = Code
#   NNNNNN = HAL/S Compilation Unit Name
#       Underscores Removed
#       First Six Characters
#
#   CC
#     $ = process entry
#     
#     $0NNNNNN = PROGRAM
#     $cNNNNNN = TASK
#       c = 1-F, max 15 TASKs
#     #CNNNNNN = COMSUB
#     anNNNNNN = Internal Procedure
#       a = A-M
#       n = 0-9, max 130 procedures
#     aaNNNNNN = Library Routine
#       a = A-Z
#     @cNNNNNN = STACK
#       c = 0-9,A-Z
#     #DNNNNNN = DECLARE Data
#     #PNNNNNN = COMPOOL Data
#     #ZNNNNNN = ZCON to COMSUM or REMOTE Data
#     #QNNNNNN = ZCON to Library Routine
#     #0NNNNNN = Bank 0
#     #ENNNNNN = Process Directory Entry (PDE)
#     #LNNNNNN = Library Data For Library Routine
#     #XNNNNNN = EXCLUSIVE Data
#     ##NNNNNN = Simulation Data File (SDF)
#     @@NNNNNN = TEMPLATE
#
# CSECT Placement
#
#   CODE: $0, $1, A1, AA -> Sector >= 2
#   DATA: @0, #D, #0, #P, #E, #L, #X -> Sector ∈ 0,1
#                          REMOTE #P -> Any Sector 
#                     REMOTE_DATA #P -> Sector = 7
#   ZCON:                     #Z, #Q -> First 2K of Sector = 0

# 
#
