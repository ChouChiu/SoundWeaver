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
#include "ext/simd/simd_utils.h"
#include <array>

namespace avs3renderer {

class Spectrum {
private:
    Eigen::Array<float, kNumOctaveBands, 1> coefficients_;

public:
    EIGEN_MAKE_ALIGNED_OPERATOR_NEW
    Spectrum(float coeff = 0.) {
        coefficients_.fill(coeff);
    }

    Spectrum(Eigen::Array<float, kNumOctaveBands, 1> coeff) : coefficients_(coeff) {
    }

    Spectrum(const float* coeff) {
        for (size_t band = 0; band < kNumOctaveBands; ++band) {
            coefficients_[band] = coeff[band];
        }
    }
    ~Spectrum() {
    }

    void fill(float coeff) {
        coefficients_.fill(coeff);
    }

    float& operator[](unsigned int i) {
        assert(i < kNumOctaveBands);
        return coefficients_[i];
    }

    const float& operator[](unsigned int i) const {
        assert(i < kNumOctaveBands);
        return coefficients_[i];
    }

    Spectrum operator+(const Spectrum& other) const {
        Spectrum ret;
        ret.coefficients_ = other.coefficients_ + this->coefficients_;
        return ret;
    }

    Spectrum operator-(const Spectrum& other) const {
        Spectrum ret;
        for (size_t i = 0; i < kNumOctaveBands; ++i) {
            ret[i] = (*this)[i] - other[i];
        }
        return ret;
    }

    Spectrum operator*(const Spectrum& other) const {
        Spectrum ret;
        for (size_t i = 0; i < kNumOctaveBands; ++i) {
            ret[i] = (*this)[i] * other[i];
        }
        return ret;
    }

    Spectrum operator*(const float& other) const {
        Spectrum ret;
        for (size_t i = 0; i < kNumOctaveBands; ++i) {
            ret[i] = (*this)[i] * other;
        }
        return ret;
    }

    Spectrum operator/(const float& other) const {
        assert(other != 0);
        Spectrum ret;
        for (size_t i = 0; i < kNumOctaveBands; ++i) {
            ret[i] = (*this)[i] / other;
        }
        return ret;
    }

    Spectrum operator/(const Spectrum& other) const {
        Spectrum ret;
        for (size_t i = 0; i < kNumOctaveBands; ++i) {
            if (other[i] == 0) {
                if ((*this)[i] == 0) {
                    ret[i] = 0;
                } else {
                    assert(1);
                }
            } else {
                ret[i] = (*this)[i] / other[i];
            }
        }
        return ret;
    }

    Spectrum& operator+=(const Spectrum& other) {
        for (size_t i = 0; i < kNumOctaveBands; ++i) {
            coefficients_[i] += other[i];
        }
        return *this;
    }

    Spectrum& operator*=(const Spectrum& other) {
        for (size_t i = 0; i < kNumOctaveBands; ++i) {
            coefficients_[i] *= other[i];
        }
        return *this;
    }

    Spectrum& operator*=(float scalar) {
        for (size_t i = 0; i < kNumOctaveBands; ++i) {
            coefficients_[i] *= scalar;
        }
        return *this;
    }

    bool operator==(const Spectrum& other) const {
        for (size_t i = 0; i < kNumOctaveBands; ++i) {
            if (std::abs(coefficients_[i] - other[i]) > kFloatTolerance)
                return false;
        }
        return true;
    }

    Spectrum& operator=(const float* coeffs) {
        for (size_t i = 0; i < kNumOctaveBands; ++i) {
            coefficients_[i] = coeffs[i];
        }
        return *this;
    }

    Spectrum Sqrt() const {
        Spectrum ret(*this);
        for (size_t i = 1; i < kNumOctaveBands; ++i) {
            assert(ret[i] >= 0);
            ret[i] = std::sqrt(ret[i]);
        }
        return ret;
    }
};

}  // namespace avs3renderer
