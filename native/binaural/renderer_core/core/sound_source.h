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

#pragma once
#include "core/definitions.h"
#include "spectrum.h"
#include "shapes.h"
#include <atomic>

namespace avs3renderer {

class SoundSource {
public:
    EIGEN_MAKE_ALIGNED_OPERATOR_NEW
    SoundSource()
        : id_(IdGenerator()), to_world_(2, Transform4f::Identity()), energy_(Spectrum(1.0f)), volumetric_size_(0.f), to_world_read_index_(0) {
    }
    SoundSource(const Transform4f& to_world, const Spectrum& energy, int source_id = -1, float volumetric_size = 0.f)
        : id_(source_id >= 0? source_id : IdGenerator()), to_world_(2, to_world), energy_(energy), volumetric_size_(volumetric_size), to_world_read_index_(0) {
    }
    SoundSource(const Point3f& position, const Vector3f& front, const Vector3f& up, const Spectrum& energy, int source_id = -1, float volumetric_size = 0.f);
    virtual void SetPosition(float x, float y, float z);
    virtual Point3f Position() const;
    virtual void SetPose(const Point3f& position, const Vector3f& front, const Vector3f& up);
    virtual Spectrum Energy(const Vector3f& direction, bool normalized) const = 0;
    virtual Spectrum energy() {
        return energy_;
    }
    int id() const {
        return id_;
    };
    float volumetric_size() const {
        return volumetric_size_;
    }
    void set_volumetric_size(float size) {
        volumetric_size_ = size;
    }

protected:
    std::vector<Transform4f> to_world_;
    std::atomic<int> to_world_read_index_;
    Spectrum energy_;
    std::atomic<float> volumetric_size_;

private:
    static int IdGenerator();
    int id_;
};

class OmniSoundSource : public SoundSource, public Sphere {
public:
    EIGEN_MAKE_ALIGNED_OPERATOR_NEW
    explicit OmniSoundSource(const Transform4f& to_world = Transform4f::Identity(),
                             const Spectrum& energy_at_one_meter = Spectrum(1.0),
                             const float& rt_detection_radius = 0.1f, int source_id = -1, float volumetric_size = 0.f);

    OmniSoundSource(const Point3f& position,
                    const Vector3f& front,
                    const Vector3f& up,
                    const Spectrum& energy_at_one_meter = Spectrum(1.0),
                    const float& rt_detection_radius = 0.1f,
                    int source_id = -1,
                    float volumetric_size = 0.f);
    void SetPosition(float x, float y, float z) override;
    void SetPose(const Point3f& position, const Vector3f& front, const Vector3f& up) override;
    Spectrum Energy(const Vector3f& direction, bool normalized) const override {
        return normalized ? energy_ : energy_ / normalize_factor_;
    }

private:
    float normalize_factor_;
};

}  // namespace avs3renderer
