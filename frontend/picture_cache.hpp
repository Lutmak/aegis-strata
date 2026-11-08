// UnbiasedStrata: a picture encoded once is a picture encoded once.
//
// A chat client resends the whole conversation on every turn, pictures included, and the vision encoder is the slow
// part of a request (USTRAT-15: 6 s a screenshot on 16 CPU threads, 18 s on 6; the engine then reuses the prompt's
// KV prefix in a fraction of a second). Upstream Strata's Python server kept the encoder's output per picture in an
// LRU of 64 files keyed by the SHA-256 of the picture's bytes (serve/server.py, class Vision). This is that cache
// for the C++ front end, in memory: mtmd already names every bitmap by the SHA-256 of its bytes, so the key is the
// chunk's id and the rows are the projector's output (n_tok rows of the model's embedding width).
//
// Entries are shared pointers: a request keeps the rows it is writing even when the cache lets go of them, so
// nothing is ever evicted from under a caller (upstream's #1072). Two limits, entries and bytes; the newest entry is
// always kept, even alone over the byte limit, so a request never fails for cache room.
//
// Header-only, free of llama.cpp, so frontend/tests/picture_cache_test.cpp runs it without a model.
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <list>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace strata_frontend {

struct PictureRows {
    int32_t n_tok = 0;   // rows
    int32_t nx    = 0;   // the grid the rows make (columns)
    int32_t ny    = 0;   // (lines)
    int32_t width = 0;   // floats a row
    std::vector<float> embd;   // n_tok * width

    size_t bytes() const { return sizeof(PictureRows) + embd.size() * sizeof(float); }
};

class PictureCache {
public:
    using Rows = std::shared_ptr<const PictureRows>;

    struct Stats {
        uint64_t hits = 0;
        uint64_t misses = 0;
        uint64_t evictions = 0;
    };

    explicit PictureCache(size_t max_entries = 64, size_t max_bytes = size_t(1) << 30) {
        configure(max_entries, max_bytes);
    }

    // the limits; drops what is held. 0 entries turns the cache off: put() hands the rows back uncached.
    void configure(size_t max_entries, size_t max_bytes) {
        std::lock_guard<std::mutex> lock(mu_);
        max_entries_ = max_entries;
        max_bytes_   = max_bytes;
        items_.clear();
        order_.clear();
        bytes_ = 0;
    }

    bool   enabled()     const { return max_entries_ > 0; }
    size_t max_entries() const { return max_entries_; }
    size_t max_bytes()   const { return max_bytes_; }

    // the rows of a picture seen before, now the most recently used; nullptr for a picture not held
    Rows find(const std::string & key) {
        std::lock_guard<std::mutex> lock(mu_);
        auto it = items_.find(key);
        if (it == items_.end()) {
            ++stats_.misses;
            return nullptr;
        }
        order_.splice(order_.end(), order_, it->second.pos);
        ++stats_.hits;
        return it->second.rows;
    }

    // remembers the rows under `key` (replacing an older entry of that key) and returns them
    Rows put(const std::string & key, PictureRows rows) {
        Rows shared = std::make_shared<const PictureRows>(std::move(rows));
        if (!enabled() || key.empty()) {
            return shared;
        }
        std::lock_guard<std::mutex> lock(mu_);
        drop_locked(key);
        order_.push_back(key);
        items_[key] = Item{shared, std::prev(order_.end())};
        bytes_ += shared->bytes();
        evict_locked();
        return shared;
    }

    size_t size() const {
        std::lock_guard<std::mutex> lock(mu_);
        return items_.size();
    }

    size_t bytes() const {
        std::lock_guard<std::mutex> lock(mu_);
        return bytes_;
    }

    Stats stats() const {
        std::lock_guard<std::mutex> lock(mu_);
        return stats_;
    }

    // The key the engine's conversation cache takes (img_keys=<hex>,...): the first 64 bits of the SHA-256 hex id
    // mtmd gives a bitmap. 0 for an id that is not at least 16 hex digits, and 0 is never sent.
    static uint64_t engine_key(const std::string & id) {
        if (id.size() < 16) {
            return 0;
        }
        const std::string head = id.substr(0, 16);
        char * end = nullptr;
        const unsigned long long v = std::strtoull(head.c_str(), &end, 16);
        return (end == head.c_str() + 16) ? (uint64_t) v : 0;
    }

    static std::string hex(uint64_t v) {
        char b[17];
        std::snprintf(b, sizeof b, "%016llx", (unsigned long long) v);
        return b;
    }

    // "<hex>,<hex>,..." for the GENI line; empty when any key is 0 (the engine then keys on the rows themselves)
    static std::string hex_list(const std::vector<uint64_t> & keys) {
        std::string out;
        for (uint64_t k : keys) {
            if (k == 0) {
                return std::string();
            }
            out += (out.empty() ? "" : ",") + hex(k);
        }
        return out;
    }

private:
    struct Item {
        Rows rows;
        std::list<std::string>::iterator pos;   // its place in order_
    };

    void drop_locked(const std::string & key) {
        auto it = items_.find(key);
        if (it == items_.end()) {
            return;
        }
        bytes_ -= it->second.rows->bytes();
        order_.erase(it->second.pos);
        items_.erase(it);
    }

    // the least recently used go first, down to the limits; the newest entry stays whatever its size
    void evict_locked() {
        while (items_.size() > 1 && (items_.size() > max_entries_ || bytes_ > max_bytes_)) {
            drop_locked(order_.front());
            ++stats_.evictions;
        }
    }

    mutable std::mutex mu_;
    size_t max_entries_ = 0;
    size_t max_bytes_   = 0;
    size_t bytes_       = 0;
    Stats  stats_;
    std::list<std::string> order_;   // least recently used first
    std::unordered_map<std::string, Item> items_;
};

} // namespace strata_frontend
