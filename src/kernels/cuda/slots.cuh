// src/kernels/cuda/slots.cuh - where the matrix, the column and the output of a slots launch's matrix j are: a
// list the host wrote (MmvqSlots), or experts the card picked (MmvqIds).  The slots kernels take either.
#pragma once

#include "strata/kernels/native_mmvq.hpp"

#include <cstddef>
#include <cstdint>

namespace strata::kernels {

struct SlotPtrs {
    const void* w;
    const void* x;
    float* y;
};

__device__ __forceinline__ SlotPtrs slot_ptrs(const MmvqSlots& s, int j) { return {s.w[j], s.x[j], s.y[j]}; }

__device__ __forceinline__ SlotPtrs slot_ptrs(const MmvqIds& s, int j) {
    return {static_cast<const uint8_t*>(s.table[s.ids[j]]) + s.offset,
            static_cast<const uint8_t*>(s.x) + std::size_t(j / s.x_every) * s.x_stride, s.y + std::size_t(j) * s.y_stride};
}

}  // namespace strata::kernels
