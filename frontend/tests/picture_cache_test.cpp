// UnbiasedStrata: frontend/picture_cache.hpp without a model.
//
//   c++ -std=c++20 -pthread -I frontend frontend/tests/picture_cache_test.cpp -o picture_cache_test && ./picture_cache_test
//
// Cases:
//   1. a miss, then a hit that returns the same rows; the counters
//   2. least recently used goes first: a find() refreshes an entry, the entry count limit evicts the stalest
//   3. the byte limit evicts; the newest entry stays even alone over the limit
//   4. rows a caller holds outlive their eviction; a put() of a held key replaces without double counting
//   5. off (0 entries) and an empty key: put() hands the rows back and holds nothing
//   6. engine_key, hex and hex_list: mtmd's SHA-256 id becomes the engine's 64-bit key; a bad id is 0 and sends nothing
//   7. many threads putting and finding at once keep the invariants
#include "picture_cache.hpp"

#include <cstdio>
#include <string>
#include <thread>
#include <vector>

using strata_frontend::PictureCache;
using strata_frontend::PictureRows;

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); ++failures; } } while (0)

static PictureRows rows_of(int32_t n_tok, int32_t width, float fill) {
    PictureRows r;
    r.n_tok = n_tok;
    r.nx    = n_tok;
    r.ny    = 1;
    r.width = width;
    r.embd.assign((size_t) n_tok * (size_t) width, fill);
    return r;
}

static std::string key_of(int i) {
    return "k" + std::to_string(i);
}

int main() {
    // 1. miss, hit, counters
    {
        PictureCache c(4, 1 << 20);
        CHECK(c.enabled() && c.size() == 0 && c.bytes() == 0);
        CHECK(c.find("a") == nullptr);
        auto put = c.put("a", rows_of(3, 2, 1.5f));
        CHECK(put != nullptr && put->n_tok == 3 && put->embd.size() == 6 && put->embd[5] == 1.5f);
        auto hit = c.find("a");
        CHECK(hit == put);
        CHECK(c.size() == 1 && c.bytes() == put->bytes());
        const auto s = c.stats();
        CHECK(s.hits == 1 && s.misses == 1 && s.evictions == 0);
    }
    // 2. LRU order and the entry limit
    {
        PictureCache c(3, 1 << 20);
        for (int i = 0; i < 3; ++i) c.put(key_of(i), rows_of(1, 1, (float) i));
        CHECK(c.find(key_of(0)) != nullptr);          // k0 is now the most recent; k1 the stalest
        c.put(key_of(3), rows_of(1, 1, 3.0f));        // over the limit: k1 goes
        CHECK(c.size() == 3);
        CHECK(c.find(key_of(1)) == nullptr);
        CHECK(c.find(key_of(0)) != nullptr && c.find(key_of(2)) != nullptr && c.find(key_of(3)) != nullptr);
        CHECK(c.stats().evictions == 1);
    }
    // 3. the byte limit; the newest stays
    {
        const size_t one = rows_of(4, 4, 0.f).bytes();
        PictureCache c(100, one * 2 + one / 2);       // room for two
        c.put("a", rows_of(4, 4, 0.f));
        c.put("b", rows_of(4, 4, 0.f));
        CHECK(c.size() == 2 && c.bytes() == 2 * one);
        c.put("c", rows_of(4, 4, 0.f));               // a goes
        CHECK(c.size() == 2 && c.find("a") == nullptr && c.find("b") != nullptr);
        auto big = c.put("big", rows_of(64, 64, 0.f)); // alone over the limit: kept, everything else gone
        CHECK(c.size() == 1 && c.find("big") == big && c.bytes() == big->bytes());
        CHECK(c.bytes() > c.max_bytes());
    }
    // 4. a held entry outlives eviction; replacing a key
    {
        PictureCache c(1, 1 << 20);
        auto a = c.put("a", rows_of(2, 2, 7.f));
        c.put("b", rows_of(2, 2, 8.f));               // a evicted
        CHECK(c.find("a") == nullptr);
        CHECK(a->embd[0] == 7.f && a.use_count() == 1);
        auto b2 = c.put("b", rows_of(5, 5, 9.f));     // same key, new rows
        CHECK(c.size() == 1 && c.bytes() == b2->bytes() && c.find("b") == b2);
        CHECK(c.stats().evictions == 1);
    }
    // 5. off, and an empty key
    {
        PictureCache off(0, 1 << 20);
        CHECK(!off.enabled());
        auto r = off.put("a", rows_of(1, 1, 0.f));
        CHECK(r != nullptr && r->n_tok == 1 && off.size() == 0 && off.find("a") == nullptr);
        PictureCache on(4, 1 << 20);
        auto e = on.put("", rows_of(1, 1, 0.f));
        CHECK(e != nullptr && on.size() == 0);
        on.configure(0, 0);
        CHECK(!on.enabled() && on.size() == 0 && on.bytes() == 0);
    }
    // 6. keys for the engine
    {
        const std::string sha = "0123456789abcdefFEDCBA98765432100123456789abcdef0123456789abcdef";
        CHECK(PictureCache::engine_key(sha) == 0x0123456789abcdefULL);
        CHECK(PictureCache::engine_key("FFFFFFFFFFFFFFFF") == ~0ULL);
        CHECK(PictureCache::engine_key("0123456789abcde") == 0);          // 15 digits
        CHECK(PictureCache::engine_key("0123456789abcdeg00") == 0);       // not hex
        CHECK(PictureCache::engine_key("") == 0);
        CHECK(PictureCache::hex(0x0123456789abcdefULL) == "0123456789abcdef");
        CHECK(PictureCache::hex(1) == "0000000000000001");
        CHECK(PictureCache::hex_list({1, 0x10}) == "0000000000000001,0000000000000010");
        CHECK(PictureCache::hex_list({1, 0, 2}).empty());
        CHECK(PictureCache::hex_list({}).empty());
    }
    // 7. threads
    {
        PictureCache c(8, 1 << 20);
        std::vector<std::thread> threads;
        for (int t = 0; t < 8; ++t) {
            threads.emplace_back([&c, t] {
                for (int i = 0; i < 500; ++i) {
                    const std::string k = key_of((t * 7 + i) % 20);
                    auto r = c.find(k);
                    if (r == nullptr) r = c.put(k, rows_of(1 + i % 3, 2, (float) t));
                    if (r->embd.empty()) std::abort();
                }
            });
        }
        for (auto & th : threads) th.join();
        CHECK(c.size() <= 8 && c.size() >= 1);
        const auto s = c.stats();
        CHECK(s.hits + s.misses == 8 * 500);
        size_t sum = 0;
        for (int i = 0; i < 20; ++i) {
            if (auto r = c.find(key_of(i))) sum += r->bytes();
        }
        CHECK(sum == c.bytes());
    }

    if (failures == 0) {
        std::printf("picture_cache_test: all checks passed\n");
        return 0;
    }
    std::fprintf(stderr, "picture_cache_test: %d failure(s)\n", failures);
    return 1;
}
