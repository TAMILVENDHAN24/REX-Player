cmake_minimum_required(VERSION 3.22.1)

project("rex_player_native" C)

# QuickJS-NG CLI used by yt-dlp for YouTube n/sig challenges.
# Same build as the REX Ytdlp add-on: packaged as an executable libqjs.so in the
# native library dir so Android permits running it as a subprocess
# (requires jniLibs.useLegacyPackaging = true, already set in app/build.gradle.kts).
set(QUICKJS_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party/quickjs")

add_executable(
    qjs_runtime
    "${QUICKJS_DIR}/dtoa.c"
    "${QUICKJS_DIR}/gen/repl.c"
    "${QUICKJS_DIR}/gen/standalone.c"
    "${QUICKJS_DIR}/libregexp.c"
    "${QUICKJS_DIR}/libunicode.c"
    "${QUICKJS_DIR}/qjs.c"
    "${QUICKJS_DIR}/quickjs-libc.c"
    "${QUICKJS_DIR}/quickjs.c"
)
set_target_properties(
    qjs_runtime
    PROPERTIES
    OUTPUT_NAME "qjs"
    PREFIX "lib"
    SUFFIX ".so"
    C_STANDARD 11
    C_STANDARD_REQUIRED ON
    C_EXTENSIONS ON
    ENABLE_EXPORTS TRUE
)
target_include_directories(qjs_runtime PRIVATE "${QUICKJS_DIR}")
target_compile_definitions(qjs_runtime PRIVATE _GNU_SOURCE QUICKJS_NG_BUILD)
target_compile_options(qjs_runtime PRIVATE -funsigned-char
    "$<$<CONFIG:Release>:-ffunction-sections>"
    "$<$<CONFIG:Release>:-fdata-sections>"
    "$<$<CONFIG:Release>:-fvisibility=hidden>")
target_link_options(qjs_runtime PRIVATE
    "$<$<CONFIG:Release>:-Wl,-O1>"
    "$<$<CONFIG:Release>:-Wl,--gc-sections>"
    "$<$<CONFIG:Release>:-Wl,--icf=safe>"
    "$<$<CONFIG:Release>:-Wl,-z,max-page-size=16384>")
target_link_libraries(qjs_runtime dl m)
