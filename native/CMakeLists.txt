cmake_minimum_required(VERSION 3.16)
project(soundweaver_native LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# ---- 1. Audio Vivid Frame Decoder ------------------------------------------------
file(GLOB AV3A_DEC_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/av3a/avs3Decoder/src/*.c")
file(GLOB AV3A_COM_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/av3a/libavs3_common/*.c")
file(GLOB AV3A_DBG_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/av3a/libavs3_debug/*.c")

add_library(av3a_decoder_core STATIC ${AV3A_DEC_SOURCES} ${AV3A_COM_SOURCES} ${AV3A_DBG_SOURCES})
target_include_directories(av3a_decoder_core PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/av3a/avs3Decoder/include"
    "${CMAKE_CURRENT_SOURCE_DIR}/av3a/libavs3_common"
    "${CMAKE_CURRENT_SOURCE_DIR}/av3a/libavs3_debug"
)
if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(av3a_decoder_core PRIVATE -O2 -Wno-pointer-sign -Wno-macro-redefined -Wno-unused-result)
endif()
if(UNIX)
    target_link_libraries(av3a_decoder_core PUBLIC m)
endif()

add_executable(soundweaver_av3a "av3a/av3a_worker.c")
target_link_libraries(soundweaver_av3a PRIVATE av3a_decoder_core)

# ---- 2. Audio Vivid Encoder (`vivid_encode`) -----------------------------------------
file(GLOB AV3A_ENC_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/av3a/avs3Encoder/src/*.c")
file(GLOB AV3A_ENC_COM_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/av3a/libavs3_common_enc/*.c")

add_executable(vivid_encode
    "${CMAKE_CURRENT_SOURCE_DIR}/av3a/vivid_encode.c"
    ${AV3A_ENC_SOURCES}
    ${AV3A_ENC_COM_SOURCES}
)
target_include_directories(vivid_encode PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/av3a/avs3Encoder/include"
    "${CMAKE_CURRENT_SOURCE_DIR}/av3a/libavs3_common_enc"
)
if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(vivid_encode PRIVATE -O2 -Wno-pointer-sign -Wno-format -Wno-unused-variable -Wno-unused-but-set-variable -Wno-unused-result -Wno-incompatible-pointer-types)
endif()
if(UNIX)
    target_link_libraries(vivid_encode PRIVATE m)
endif()

add_custom_command(TARGET vivid_encode POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${CMAKE_CURRENT_SOURCE_DIR}/av3a/model.bin"
        "$<TARGET_FILE_DIR:vivid_encode>/model.bin"
)

# ---- 3. MPEG-H 3D Audio / 360RA Decoder (`ia_mpeghd_testbench`) ----------------------
set(LC_LEVEL_4 ON CACHE BOOL "Enables low complexity level 4 decoder" FORCE)
add_subdirectory(mpegh)

add_executable(soundweaver_mpegh "mpegh/soundweaver_worker.c")
target_link_libraries(soundweaver_mpegh PRIVATE ia_mpeghd_lib)
if(UNIX)
    target_link_libraries(soundweaver_mpegh PRIVATE m)
endif()
