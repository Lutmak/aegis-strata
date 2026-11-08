// UnbiasedStrata: stands in for the ui.h that llama.cpp's tools/ui generates at build time. tools/server/server-http.cpp
// includes it to serve the web UI's embedded assets; this fork ships no UI, so the asset table is empty and every
// lookup misses. Same declarations as tools/ui/ui.h.in with N_ASSETS = 0 and LLAMA_UI_HAS_ASSETS undefined.
#pragma once

#include <array>
#include <string>

struct llama_ui_asset {
    std::string           name;
    const unsigned char * data;
    std::size_t           size;
    std::string           etag;
    std::string           type;
};

const llama_ui_asset * llama_ui_find_asset(const std::string & name);
bool llama_ui_use_gzip();
const std::array<llama_ui_asset, 0> & llama_ui_get_assets();
