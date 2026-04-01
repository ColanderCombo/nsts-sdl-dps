# BuildRuntime.cmake - CMake module for assembling runtime library files
#
# 1. Find all .asm files in RUNASM and ZCONASM directories
# 2. Assemble each file using asm101s
# 3. Installs the .obj files to lib/RUN and lib/ZCON

set(RUNASM_DIR "${HALSFC_SRC_DIR}/RUNASM")
set(ZCONASM_DIR "${HALSFC_SRC_DIR}/ZCONASM")
set(RUNMAC_DIR "${HALSFC_SRC_DIR}/RUNMAC")

# Assemble a single .asm file
function(assemble_file ASM_FILE OUTPUT_DIR INSTALL_DIR STAMP_VAR)
    get_filename_component(ASM_NAME "${ASM_FILE}" NAME_WE)
    get_filename_component(ASM_BASENAME "${ASM_FILE}" NAME)
    
    # sreplace # with _HASH_ (CMake doesn't allow # in OUTPUT paths)
    string(REPLACE "#" "_HASH_" SANITIZED_NAME "${ASM_NAME}")
    
    # The actual output file preserves the original name (with #)
    set(OBJ_FILE "${OUTPUT_DIR}/${ASM_NAME}.obj")
    # Stamp file uses sanitized name for CMake tracking
    set(STAMP_FILE "${OUTPUT_DIR}/${SANITIZED_NAME}.stamp")
    
    # Build the assembler command - use stamp file for CMake tracking
    # The actual .obj file has the original name (may contain #)
    # Note: --tolerable=4 allows MNOTE severity 4 warnings from macros
    # Uses the venv-installed asm101s package via "python -m asm101s"
    add_custom_command(
        OUTPUT "${STAMP_FILE}"
        COMMAND ${CMAKE_COMMAND} -E make_directory "${OUTPUT_DIR}"
        COMMAND ${CMAKE_COMMAND} -E env PYTHONUTF8=1
                "${SDL_VENV_PYTHON}" -m asm101s
                --object=${ASM_NAME}.obj
                "--library=${RUNMAC_DIR}"
                --tolerable=4
                "${ASM_FILE}"
        COMMAND ${CMAKE_COMMAND} -E touch "${STAMP_FILE}"
        WORKING_DIRECTORY "${OUTPUT_DIR}"
        DEPENDS "${ASM_FILE}"
        COMMENT "[ASM101S] Assembling ${ASM_BASENAME}..."
        VERBATIM
    )
    
    # Append stamp file to the tracking list
    set(${STAMP_VAR} ${${STAMP_VAR}} "${STAMP_FILE}" PARENT_SCOPE)
endfunction()

# Build all runtime library files
function(build_runtime_library)
    set(RUNTIME_STAMP_FILES "")
    
    # Output directories in build tree
    set(RUN_BUILD_DIR "${CMAKE_BINARY_DIR}/lib/runtime/RUN")
    set(ZCON_BUILD_DIR "${CMAKE_BINARY_DIR}/lib/runtime/ZCON")
    
    # Create output directories
    file(MAKE_DIRECTORY "${RUN_BUILD_DIR}")
    file(MAKE_DIRECTORY "${ZCON_BUILD_DIR}")
    
    #-------------------------------------------------------------------------
    # RUNASM - HAL/S Runtime
    #-------------------------------------------------------------------------
    if(EXISTS "${RUNASM_DIR}")
        file(GLOB RUNASM_FILES "${RUNASM_DIR}/*.asm")
        list(LENGTH RUNASM_FILES RUNASM_COUNT)
        message(STATUS "Found ${RUNASM_COUNT} assembly files in RUNASM")
        
        foreach(ASM_FILE ${RUNASM_FILES})
            assemble_file("${ASM_FILE}" "${RUN_BUILD_DIR}" "RUN" RUNTIME_STAMP_FILES)
        endforeach()
    else()
        message(WARNING "RUNASM directory not found: ${RUNASM_DIR}")
    endif()
    
    #-------------------------------------------------------------------------
    # ZCONASM - ZCON's that point to the runtime functions
    #-------------------------------------------------------------------------
    if(EXISTS "${ZCONASM_DIR}")
        file(GLOB ZCONASM_FILES "${ZCONASM_DIR}/*.asm")
        list(LENGTH ZCONASM_FILES ZCONASM_COUNT)
        message(STATUS "Found ${ZCONASM_COUNT} assembly files in ZCONASM")
        
        foreach(ASM_FILE ${ZCONASM_FILES})
            assemble_file("${ASM_FILE}" "${ZCON_BUILD_DIR}" "ZCON" RUNTIME_STAMP_FILES)
        endforeach()
    else()
        message(WARNING "ZCONASM directory not found: ${ZCONASM_DIR}")
    endif()
    
    # Create the runtime target
    add_custom_target(runtime ALL
        DEPENDS ${RUNTIME_STAMP_FILES}
        COMMENT "Building runtime library"
    )
    
    # install(CODE) to handle files with # in their names
    install(CODE "
        file(GLOB RUN_OBJ_FILES \"${RUN_BUILD_DIR}/*.obj\")
        file(INSTALL \${RUN_OBJ_FILES} DESTINATION \"\${CMAKE_INSTALL_PREFIX}/lib/runtime/RUN\")
        file(GLOB ZCON_OBJ_FILES \"${ZCON_BUILD_DIR}/*.obj\")
        file(INSTALL \${ZCON_OBJ_FILES} DESTINATION \"\${CMAKE_INSTALL_PREFIX}/lib/runtime/ZCON\")
    " COMPONENT runtime)
endfunction()
