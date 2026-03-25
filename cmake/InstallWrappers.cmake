# InstallWrappers.cmake — Configure and install wrapper scripts

set(WRAPPER_ASM101S_DIR "${ASM101S_DIR}")
set(WRAPPER_XCOM_I_DIR "${XCOM_I_DIR}")
set(WRAPPER_PYTHON_CMD "${SDL_VENV_PYTHON}")

# _configure_wrapper(<template> <output-name>)
#   Configures a .in template into build/bin/<output-name> and makes it executable.
function(_configure_wrapper TEMPLATE OUTPUT_NAME)
    configure_file(
        "${CMAKE_SOURCE_DIR}/cmake/templates/${TEMPLATE}"
        "${CMAKE_BINARY_DIR}/bin/${OUTPUT_NAME}"
        @ONLY
    )
    file(CHMOD "${CMAKE_BINARY_DIR}/bin/${OUTPUT_NAME}"
         PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE
                     GROUP_READ GROUP_EXECUTE
                     WORLD_READ WORLD_EXECUTE)
endfunction()

# Build-tree wrappers: point at the build directory so they work before install
set(WRAPPER_BINDIR "${CMAKE_BINARY_DIR}/halsfc")
set(WRAPPER_LIBDIR "${CMAKE_BINARY_DIR}/lib")
_configure_wrapper(halsc.in        halsc)

set(WRAPPER_BINDIR "${CMAKE_BINARY_DIR}/bin")
set(WRAPPER_LIBDIR "${CMAKE_BINARY_DIR}/lib")
_configure_wrapper(asm101s.sh.in   asm101s)
_configure_wrapper(lnk101.sh.in   lnk101)
_configure_wrapper(gpc-batch.sh.in gpc-batch)
_configure_wrapper(gpc-debug.sh.in gpc-debug)

# Install-tree wrappers: reconfigure with install-prefix paths
set(WRAPPER_BINDIR "${SDL_INSTALL_BINDIR}")
set(WRAPPER_LIBDIR "${SDL_INSTALL_LIBDIR}")

function(_configure_install_wrapper TEMPLATE OUTPUT_NAME)
    configure_file(
        "${CMAKE_SOURCE_DIR}/cmake/templates/${TEMPLATE}"
        "${CMAKE_BINARY_DIR}/install-bin/${OUTPUT_NAME}"
        @ONLY
    )
endfunction()

_configure_install_wrapper(halsc.in        halsc)
_configure_install_wrapper(asm101s.sh.in   asm101s)
_configure_install_wrapper(lnk101.sh.in   lnk101)

install(
    PROGRAMS
        "${CMAKE_BINARY_DIR}/install-bin/halsc"
        "${CMAKE_BINARY_DIR}/install-bin/asm101s"
        "${CMAKE_BINARY_DIR}/install-bin/lnk101"
    DESTINATION "${SDL_INSTALL_BINDIR}"
    COMPONENT wrappers
)

if(EXISTS "${SIM_DIR}/package.json")
    _configure_install_wrapper(gpc-batch.sh.in gpc-batch)
    _configure_install_wrapper(gpc-debug.sh.in gpc-debug)
    install(
        PROGRAMS
            "${CMAKE_BINARY_DIR}/install-bin/gpc-batch"
            "${CMAKE_BINARY_DIR}/install-bin/gpc-debug"
        DESTINATION "${SDL_INSTALL_BINDIR}"
        COMPONENT wrappers
    )
endif()

# Install HAL/S-FC support files
install_halsfc_support()
