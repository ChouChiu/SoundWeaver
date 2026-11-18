#Copyright 2021 Beijing Zitiao Network Technology Co.,
#Licensed under the Apache License, Version 2.0 (the "License");
#you may not use this file except in compliance with the License.
#You may obtain a copy of the License at
#
#http://www.apache.org/licenses/LICENSE-2.0
#
#Unless required by applicable law or agreed to in writing, software
#distributed under the License is distributed on an "AS IS" BASIS,
#WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#See the License for the specific language governing permissions and
#limitations under the License.


cmake_minimum_required(VERSION 3.12)
project(renderer_core)
set(CMAKE_CXX_STANDARD 11)

include(cmake/options.cmake)

set(SOURCE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/)
set(API_DIR ${CMAKE_CURRENT_SOURCE_DIR}/api)

set(API
        api/avs3_audio.cpp
        )

set(CORE
        core/listener.cpp
        core/sound_source.cpp
        )

set(AURALIZATION
        core/ambisonic_encoder.cpp
        core/ambisonic_binaural_decoder.cpp
        ambisonics/SHEval.cpp
        core/aligned_allocator.h
        core/ambisonic_rotator.cpp
        core/fft_manager.cpp
        core/ramp_processor.cpp
        core/spherical_harmonic_hrir.cpp
        core/static_convolver.cpp)

set(SOURCE
        ${API}
        ${CORE}
        ${AURALIZATION}
        )

# Third party libraries
set(EIGEN_INCLUDE_DIR ${SOURCE_DIR}/ext/eigen-3.3.7)

set(PFFFT_DIR ${SOURCE_DIR}/ext/pffft/)
set(PFFFT_INCLUDE_DIR ${PFFFT_DIR})
add_library(PffftObj OBJECT ${PFFFT_DIR}/pffft.c)

set(HRTFS_DIR ${SOURCE_DIR}/ext/hrtf_database/)
set(HRTFS_INCLUDE_DIR ${HRTFS_DIR})
add_library(HrtfsObj OBJECT ${HRTFS_DIR}/hrtf_assets.cc)

set(AUDIO_SIMD_DIR ${SOURCE_DIR}/ext/simd/)
add_library(AudioSimdObj OBJECT ${AUDIO_SIMD_DIR}/simd_utils.cc)

set(LIBSAMPLERATE_DIR ${SOURCE_DIR}/ext/libsamplerate)
set(LIBSAMPLERATE_INCLUDE_DIR ${LIBSAMPLERATE_DIR}/include/)
add_subdirectory(${LIBSAMPLERATE_DIR})

set(THIRD_PARTY_INCLUDES
        ${EIGEN_INCLUDE_DIR}
        ${PFFFT_INCLUDE_DIR}
        ${HRTFS_INCLUDE_DIR}
        ${AUDIO_SIMD_DIR}
        ${LIBSAMPLERATE_INCLUDE_DIR}
        )

set(THIRD_PARTY_LIBS SampleRate::samplerate)

if (${CMAKE_SYSTEM_NAME} STREQUAL "Darwin" OR ${CMAKE_SYSTEM_NAME} STREQUAL "Linux")
    list(APPEND THIRD_PARTY_LIBS pthread)
endif ()

add_library(RendererCoreObj OBJECT ${SOURCE})

target_include_directories(RendererCoreObj PRIVATE ${API_DIR})
target_include_directories(RendererCoreObj PRIVATE ${SOURCE_DIR})
target_include_directories(RendererCoreObj PRIVATE ${THIRD_PARTY_INCLUDES})

set(OBJECTS
        $<TARGET_OBJECTS:RendererCoreObj>
        $<TARGET_OBJECTS:PffftObj>
        $<TARGET_OBJECTS:HrtfsObj>
        $<TARGET_OBJECTS:AudioSimdObj>
        )

if (BUILD_SHARED_LIB)
    add_library(RendererCore SHARED ${OBJECTS})
else ()
    add_library(RendererCore STATIC ${OBJECTS})
endif (BUILD_SHARED_LIB)

set_target_properties(RendererCore PROPERTIES INTERFACE_INCLUDE_DIRECTORIES "${CMAKE_CURRENT_SOURCE_DIR}")

target_link_libraries(RendererCore ${THIRD_PARTY_LIBS})
target_include_directories(RendererCore PUBLIC ${API_DIR})

add_library(RendererCore::RendererCore ALIAS RendererCore)
