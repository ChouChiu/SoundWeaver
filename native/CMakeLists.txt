cmake_minimum_required(VERSION 3.16)
project(soundweaver_native LANGUAGES C CXX)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# ---- 1. Audio Vivid Decoder (`vivid`) ------------------------------------------------
file(GLOB AV3A_DEC_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/av3a/avs3Decoder/src/*.c")
file(GLOB AV3A_COM_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/av3a/libavs3_common/*.c")
file(GLOB AV3A_DBG_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/av3a/libavs3_debug/*.c")

add_executable(vivid
    "${CMAKE_CURRENT_SOURCE_DIR}/av3a/vivid.c"
    ${AV3A_DEC_SOURCES}
    ${AV3A_COM_SOURCES}
    ${AV3A_DBG_SOURCES}
)
target_include_directories(vivid PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/av3a"
    "${CMAKE_CURRENT_SOURCE_DIR}/av3a/avs3Decoder/include"
    "${CMAKE_CURRENT_SOURCE_DIR}/av3a/libavs3_common"
    "${CMAKE_CURRENT_SOURCE_DIR}/av3a/libavs3_debug"
)
if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(vivid PRIVATE -O2 -Wno-pointer-sign -Wno-macro-redefined -Wno-unused-result)
endif()
if(UNIX)
    target_link_libraries(vivid PRIVATE m)
endif()

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

# ---- 3. Audio Vivid Binaural Renderer (`vivid_binaural`) -----------------------------
set(BUILD_TESTING OFF CACHE BOOL "" FORCE)
set(LIBSAMPLERATE_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LIBSAMPLERATE_INSTALL OFF CACHE BOOL "" FORCE)

add_subdirectory(binaural/metadata)
add_subdirectory(binaural/renderer_core)
add_subdirectory(binaural/avs3_api)

add_executable(vivid_binaural "${CMAKE_CURRENT_SOURCE_DIR}/binaural/vivid_binaural.cpp")
target_link_libraries(vivid_binaural PRIVATE metadata renderer)
target_include_directories(vivid_binaural PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/binaural/metadata/inc"
    "${CMAKE_CURRENT_SOURCE_DIR}/binaural/utils/libbw64"
    "${CMAKE_CURRENT_SOURCE_DIR}/binaural/avs3_api"
    "${CMAKE_CURRENT_SOURCE_DIR}/binaural/renderer_core/api"
)

# ---- 4. MPEG-H 3D Audio / 360RA Decoder (`ia_mpeghd_testbench`) ----------------------
set(LC_LEVEL_4 ON CACHE BOOL "Enables low complexity level 4 decoder" FORCE)
add_subdirectory(mpegh)
