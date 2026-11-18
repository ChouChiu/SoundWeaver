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

Set-PSDebug -Trace 2

$archs_ = "x64"
$types_ = "Release"

$vs_version = "vs2019";
$src_dir = Get-Location;
$dist_dir = "$(Get-Location)\dist";

#start build
$build_dir = "$src_dir\build_${archs_}_$types_"

if ((Test-Path -Path "$build_dir")) {
    Remove-Item "$build_dir" -Recurse;
}
New-Item -ItemType Directory -Path "$build_dir"

Write-Debug "build_dir:${build_dir}"

if (-not (Test-Path -Path "$dist_dir")) {
    New-Item -ItemType Directory -Path "$dist_dir"
}

Write-Host "start cmake set on dist_dir:${dist_dir}"

cmake -G "Visual Studio 16 2019" -A $archs_ `
-DCMAKE_INSTALL_PREFIX:PATH=$dist_dir `
-DCMAKE_BUILD_TYPE:STRING=$types_ `
-S "$src_dir" -B "$build_dir" `

if (-not $?) {
    Write-Error "Cmake configuration errors";
    exit -1;
}

Write-Debug "start cmake build"
cmake --build "$build_dir" --config $types_ --target install -j8;
if(-not $?) {
	Write-Error "CMake building errors";
	exit -1;
}
if ((Test-Path -Path "$build_dir")) {
    Remove-Item "$build_dir" -Recurse;
}


