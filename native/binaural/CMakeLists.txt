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


cmake_minimum_required(VERSION 3.14)
project(avs3_render)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_STANDARD 14)
include(GNUInstallDirs)
add_compile_options(-w)

add_subdirectory(metadata)
add_subdirectory(renderer_core)
add_subdirectory(avs3_api)
add_subdirectory(example)