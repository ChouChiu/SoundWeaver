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


add_executable(file-render file_render.cpp)

target_link_libraries(file-render PUBLIC
        metadata
        renderer)

target_include_directories(file-render PUBLIC
        ../metadata/inc
        ../utils/libbw64
        ../avs3_api)

add_executable(binaural-renderer binaural_renderer.cpp)

target_link_libraries(binaural-renderer
        metadata
        renderer)

target_include_directories(binaural-renderer PUBLIC
        ../metadata/inc
        ../utils/libbw64
        ../avs3_api)

install(TARGETS file-render
        binaural-renderer
        RUNTIME DESTINATION ${CMAKE_SOURCE_DIR}/dist/bin/render)
