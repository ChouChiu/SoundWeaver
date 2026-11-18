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


## Project options
option(BUILD_SHARED_LIB "Build shared library" OFF)

## Add platform marco
if (${CMAKE_SYSTEM_NAME} STREQUAL "Darwin")
    if ("${DARWIN_TARGET_OS_NAME}" STREQUAL "ios")
        add_compile_definitions(BUILD_IOS)
    else ()
        add_compile_definitions(BUILD_MAC)
    endif ()
elseif (${CMAKE_SYSTEM_NAME} STREQUAL "Linux")
    add_compile_definitions(BUILD_LINUX)
    set(CMAKE_POSITION_INDEPENDENT_CODE ON)
elseif (${CMAKE_SYSTEM_NAME} STREQUAL "Windows")
    # Otherwise you will meet "'M_PI': undeclared identifier"
    add_compile_definitions(_USE_MATH_DEFINES)
    add_compile_definitions(BUILD_WIN)
elseif (${CMAKE_SYSTEM_NAME} STREQUAL "Android")
    add_compile_definitions(BUILD_ANDROID)
endif ()
