# InstallWrappers.cmake — Configure and install wrapper scripts

set(WRAPPER_ASM101S_DIR "${ASM101S_DIR}")
set(WRAPPER_XCOM_I_DIR "${XCOM_I_DIR}")
set(WRAPPER_PYTHON_CMD "${SDL_VENV_PYTHON}")

# On Linux, Electron requires either unprivileged user namespaces or a
# SUID chrome-sandbox.  When neither is available, pass --no-sandbox.
set(ELECTRON_SANDBOX_FLAGS "")
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    execute_process(
        COMMAND sysctl -n kernel.unprivileged_userns_clone
        OUTPUT_VARIABLE _userns_clone
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET
        RESULT_VARIABLE _userns_rc
    )
    if(_userns_rc EQUAL 0 AND _userns_clone STREQUAL "0")
        set(ELECTRON_SANDBOX_FLAGS "--no-sandbox")
        message(STATUS "Unprivileged user namespaces disabled — Electron wrappers will use --no-sandbox")
    endif()
endif()

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

# Symlink HALSFC support files into build/lib/ so halsc can find them
file(MAKE_DIRECTORY "${CMAKE_BINARY_DIR}/lib")
foreach(_support ERRORLIB ACCESS)
    if(EXISTS "${HALSFC_SRC_DIR}/${_support}" AND NOT EXISTS "${CMAKE_BINARY_DIR}/lib/${_support}")
        file(CREATE_LINK "${HALSFC_SRC_DIR}/${_support}" "${CMAKE_BINARY_DIR}/lib/${_support}" SYMBOLIC)
    endif()
endforeach()

set(WRAPPER_BINDIR "${CMAKE_BINARY_DIR}/bin")
set(WRAPPER_LIBDIR "${CMAKE_BINARY_DIR}/lib")
_configure_wrapper(asm101s.sh.in   asm101s)
_configure_wrapper(lnk101.sh.in   lnk101)
_configure_wrapper(gpc-batch.sh.in gpc-batch)
_configure_wrapper(gpc-dbg.sh.in   gpc-dbg)
_configure_wrapper(gpc-dump.sh.in  gpc-dump)
_configure_wrapper(gpc-gui.sh.in   gpc-gui)
_configure_wrapper(fcmcmp.sh.in    fcmcmp)
_configure_wrapper(rldanalyze.sh.in rldanalyze)
_configure_wrapper(ibmobjdump.sh.in ibmobjdump)

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
    _configure_install_wrapper(gpc-dbg.sh.in   gpc-dbg)
    _configure_install_wrapper(gpc-dump.sh.in  gpc-dump)
    _configure_install_wrapper(gpc-gui.sh.in   gpc-gui)
    install(
        PROGRAMS
            "${CMAKE_BINARY_DIR}/install-bin/gpc-batch"
            "${CMAKE_BINARY_DIR}/install-bin/gpc-dbg"
            "${CMAKE_BINARY_DIR}/install-bin/gpc-dump"
            "${CMAKE_BINARY_DIR}/install-bin/gpc-gui"
        DESTINATION "${SDL_INSTALL_BINDIR}"
        COMPONENT wrappers
    )
endif()

# Install HAL/S-FC support files
install_halsfc_support()
