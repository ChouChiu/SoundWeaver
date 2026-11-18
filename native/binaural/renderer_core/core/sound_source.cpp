/* Copyright 2021 Beijing Zitiao Network Technology Co.,
* Licensed under the Apache License, Version 2.0 (the "License");
* you may not use this file except in compliance with the License.
* You may obtain a copy of the License at
*
* http://www.apache.org/licenses/LICENSE-2.0
*
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

#include "sound_source.h"
#include "core/utils.h"

namespace avs3renderer {

namespace {

Spectrum NormalizeEnergy(const Spectrum& energy_at_one_meter, const float& detection_sphere_radius) {
    return energy_at_one_meter * 4.0f / (detection_sphere_radius * detection_sphere_radius);
}
}  // namespace

SoundSource::SoundSource(const Point3f& position,
                         const Vector3f& front,
                         const Vector3f& up,
                         const Spectrum& energy,
                         int source_id,
                         float volumetric_size)
    : id_(source_id >= 0 ? source_id : IdGenerator()),
      energy_(energy),
      to_world_(2, Transform4f::Identity()),
      to_world_read_index_(0),
      volumetric_size_(volumetric_size) {
    SetPose(position, front, up);
}

void SoundSource::SetPosition(float x, float y, float z) {
    auto write_idx = 1 - to_world_read_index_.load();
    to_world_.at(write_idx) = to_world_.at(1 - write_idx);
    to_world_.at(write_idx)(0, 3) = x;
    to_world_.at(write_idx)(1, 3) = y;
    to_world_.at(write_idx)(2, 3) = z;
    to_world_read_index_.store(write_idx);
}

Point3f SoundSource::Position() const {
    auto read_idx = to_world_read_index_.load();
    return {to_world_.at(read_idx)(0, 3), to_world_.at(read_idx)(1, 3), to_world_.at(read_idx)(2, 3)};
}
void SoundSource::SetPose(const Point3f& position, const Vector3f& front, const Vector3f& up) {
    auto write_idx = 1 - to_world_read_index_.load();
    to_world_.at(write_idx) = ToWorldMatrix(position, front, up);
    to_world_read_index_.store(write_idx);
}
int SoundSource::IdGenerator() {
    static int id = 0;
    return id++;
}

OmniSoundSource::OmniSoundSource(const Transform4f& to_world,
                                 const Spectrum& energy_at_one_meter,
                                 const float& rt_detection_radius,
                                 int source_id,
                                 float volumetric_size)
    : SoundSource(to_world, NormalizeEnergy(energy_at_one_meter, rt_detection_radius), source_id, volumetric_size),
      Sphere(SoundSource::Position(), rt_detection_radius),
      normalize_factor_(4.0f / (rt_detection_radius * rt_detection_radius)) {
}

OmniSoundSource::OmniSoundSource(const Point3f& position,
                                 const Vector3f& front,
                                 const Vector3f& up,
                                 const Spectrum& energy_at_one_meter,
                                 const float& rt_detection_radius,
                                 int source_id,
                                 float volumetric_size)
    : SoundSource(position,
                  front,
                  up,
                  NormalizeEnergy(energy_at_one_meter, rt_detection_radius),
                  source_id,
                  volumetric_size),
      Sphere(position, rt_detection_radius),
      normalize_factor_(4.0f / (rt_detection_radius * rt_detection_radius)) {
    center_ = Position();
}

void OmniSoundSource::SetPosition(float x, float y, float z) {
    SoundSource::SetPosition(x, y, z);
    center_ = Position();
}

void OmniSoundSource::SetPose(const Point3f& position, const Vector3f& front, const Vector3f& up) {
    SoundSource::SetPose(position, front, up);
    center_ = Position();
}

}  // namespace avs3renderer