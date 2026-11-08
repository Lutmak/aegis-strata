// UnbiasedStrata: a control token's text inside a message is text.
//
// The chat template renders one string with the messages' content interpolated, and that string is tokenized with
// special tokens parsed, because the template's own markers (<|im_start|>, <|im_end|>, [gMASK]<sop>, ...) have to
// become their ids. By then nothing tells the template's <|im_end|> from one a user typed, or from one inside a file
// an agent read: a pasted chat log ends a turn, "<|image_pad|>" in a document becomes a picture the engine finds no
// rows for (USTRAT-14). Upstream Strata's Python server had this covered (#537, #554: mark_think_literals in
// serve/frontend.py); this is that scheme for the C++ front end.
//
// Before the template renders, every literal (the texts of the vocabulary's control tokens and of its marker-like
// added tokens: <think>, <tool_call>, ...) inside a message or a tool definition is swapped for a mark, a private-use
// character the template passes through untouched. After rendering, the marks are swapped back, and the byte spans
// they covered come out as "plain" spans: the caller tokenizes those as text and everything else as it always did.
// Only the control tokens the template writes are control tokens.
//
// One exception, upstream's as well: an assistant message whose content opens with a whole <think>...</think> block
// (clients that send the reasoning inline) keeps those two markers as the model's; the template reads them to strip
// the block from the history. The rest of that content is marked like any other.
//
// Header-only and free of llama.cpp (the JSON functions are templates over common_json's interface: is_string,
// get<std::string>, contains, at, items, iteration), so frontend/tests/literals_test.cpp runs it without a model.
#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

namespace strata_frontend {

struct Literals {
    // the literal texts, longest first (as the tokenizer matches them: a literal inside a longer one must not be
    // marked before it), each with its mark
    std::vector<std::pair<std::string, std::string>> tags;
    std::string think_open;   // "<think>" when both it and its close are literals, else empty
    std::string think_close;

    // marks are U+F0000 + k (Supplementary Private Use Area-A): four UTF-8 bytes, the first always 0xF3
    static constexpr uint32_t MARK0 = 0xF0000;
    static constexpr size_t   MAX_LITERALS = 0x10000;

    static std::string mark_of(size_t k) {
        const uint32_t cp = MARK0 + (uint32_t) k;
        std::string s(4, '\0');
        s[0] = (char) (0xF0 | (cp >> 18));
        s[1] = (char) (0x80 | ((cp >> 12) & 0x3F));
        s[2] = (char) (0x80 | ((cp >> 6) & 0x3F));
        s[3] = (char) (0x80 | (cp & 0x3F));
        return s;
    }

    // A token's text that is a marker: "<...>" or "[...]" with no whitespace inside, at least three characters.
    // The vocabulary's control tokens all look like this; of its other added tokens (which llama.cpp's tokenizer
    // matches whether or not specials are parsed) it keeps <think>, <tool_call>, [gMASK]-style markers and leaves
    // alone added tokens that are ordinary text (a run of spaces, an emoji).
    static bool marker_like(const std::string & t) {
        if (t.size() < 3) return false;
        const char a = t.front(), z = t.back();
        if (!((a == '<' && z == '>') || (a == '[' && z == ']'))) return false;
        for (unsigned char c : t) {
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') return false;
        }
        return true;
    }

    static Literals from_texts(std::vector<std::string> texts) {
        std::sort(texts.begin(), texts.end());
        texts.erase(std::unique(texts.begin(), texts.end()), texts.end());
        texts.erase(std::remove_if(texts.begin(), texts.end(), [](const std::string & t) { return t.empty(); }), texts.end());
        if (texts.size() > MAX_LITERALS) texts.resize(MAX_LITERALS);
        std::stable_sort(texts.begin(), texts.end(), [](const std::string & a, const std::string & b) { return a.size() > b.size(); });
        Literals L;
        for (size_t k = 0; k < texts.size(); ++k) {
            L.tags.emplace_back(texts[k], mark_of(k));
        }
        const bool has_open  = std::find(texts.begin(), texts.end(), "<think>")  != texts.end();
        const bool has_close = std::find(texts.begin(), texts.end(), "</think>") != texts.end();
        if (has_open && has_close) {
            L.think_open  = "<think>";
            L.think_close = "</think>";
        }
        return L;
    }

    bool empty() const { return tags.empty(); }

    bool has_literal(const std::string & s) const {
        for (const auto & t : tags) {
            if (s.find(t.first) != std::string::npos) return true;
        }
        return false;
    }

    template <class J>
    bool has_literal(const J & v) const {
        if (v.is_string()) return has_literal(v.template get<std::string>());
        if (v.is_array() || v.is_object()) {
            for (const auto & x : v) {
                if (has_literal(x)) return true;
            }
        }
        return false;
    }

    std::string mark(const std::string & s) const {
        std::string out = s;
        for (const auto & t : tags) {
            for (size_t p = out.find(t.first); p != std::string::npos; p = out.find(t.first, p + t.second.size())) {
                out.replace(p, t.first.size(), t.second);
            }
        }
        return out;
    }

    // every string in `v`, marked; a part that carries media (image_url, input_audio, video_url) is left alone
    template <class J>
    void mark_deep(J & v) const {
        if (v.is_string()) {
            std::string s = v.template get<std::string>();
            if (has_literal(s)) v = mark(s);
            return;
        }
        if (v.is_object()) {
            if (v.contains("type") && v.at("type").is_string()) {
                const std::string t = v.at("type").template get<std::string>();
                if (t == "image_url" || t == "input_audio" || t == "video_url" || t == "file") return;
            }
            for (auto kv : v.items()) mark_deep(kv.value());
            return;
        }
        if (v.is_array()) {
            for (auto & x : v) mark_deep(x);
        }
    }

    // an assistant message's content: a leading whole <think>...</think> block keeps its two markers
    std::string mark_assistant_content(const std::string & s) const {
        if (think_open.empty()) return mark(s);
        const size_t start = s.find_first_not_of(" \t\r\n");
        if (start == std::string::npos || s.compare(start, think_open.size(), think_open) != 0) return mark(s);
        const size_t inner = start + think_open.size();
        const size_t close = s.find(think_close, inner);
        if (close == std::string::npos) return mark(s);
        return s.substr(0, inner) + mark(s.substr(inner, close - inner)) + think_close + mark(s.substr(close + think_close.size()));
    }

    // the request's messages and tools, marked in place; false when nothing in them was a literal (then the
    // request renders and tokenizes exactly as it always did)
    template <class J>
    bool mark_request(J & messages, std::type_identity_t<J> * tools) const {
        if (empty()) return false;
        const bool in_msgs  = messages.is_array() && has_literal(messages);
        const bool in_tools = tools != nullptr && has_literal(*tools);
        if (!in_msgs && !in_tools) return false;
        if (in_msgs) {
            for (auto & m : messages) {
                if (!m.is_object()) continue;
                const std::string role = m.value("role", std::string());
                for (auto kv : m.items()) {
                    if (kv.key() == "role") continue;
                    if (kv.key() == "content" && kv.value().is_string() && role == "assistant") {
                        const std::string s = kv.value().template get<std::string>();
                        if (has_literal(s)) kv.value() = mark_assistant_content(s);
                        continue;
                    }
                    mark_deep(kv.value());
                }
            }
        }
        if (in_tools) mark_deep(*tools);
        return true;
    }

    // Puts the literals back into the rendered prompt. Returns the byte spans [begin, end) of the result that are
    // plain text, in order, adjacent spans merged. A private-use character that is not one of our marks passes through.
    // A mark comes back from the template as it went in, or, where the template asked `tojson(ensure_ascii=True)` for
    // a tool definition, as the JSON escape of its UTF-16 surrogate pair (\udb80\udc00 for mark 0); both are read.
    std::vector<std::pair<size_t, size_t>> unmark(std::string & prompt) const {
        std::vector<std::pair<size_t, size_t>> spans;
        if (prompt.find('\xF3') == std::string::npos && prompt.find("\\ud") == std::string::npos && prompt.find("\\uD") == std::string::npos) {
            return spans;
        }
        std::string out;
        out.reserve(prompt.size());
        auto put = [&](size_t k, size_t consumed, size_t & i) {
            const std::string & lit = tags[k].first;
            const size_t begin = out.size();
            out += lit;
            if (!spans.empty() && spans.back().second == begin) {
                spans.back().second = out.size();
            } else {
                spans.emplace_back(begin, out.size());
            }
            i += consumed;
        };
        for (size_t i = 0; i < prompt.size();) {
            const unsigned char c = (unsigned char) prompt[i];
            if (c == 0xF3 && i + 4 <= prompt.size()) {
                const unsigned char b1 = (unsigned char) prompt[i + 1], b2 = (unsigned char) prompt[i + 2], b3 = (unsigned char) prompt[i + 3];
                if ((b1 & 0xC0) == 0x80 && (b2 & 0xC0) == 0x80 && (b3 & 0xC0) == 0x80) {
                    const uint32_t cp = ((uint32_t) (c & 0x07) << 18) | ((uint32_t) (b1 & 0x3F) << 12) | ((uint32_t) (b2 & 0x3F) << 6) | (uint32_t) (b3 & 0x3F);
                    if (cp >= MARK0 && cp - MARK0 < tags.size()) {
                        put(cp - MARK0, 4, i);
                        continue;
                    }
                }
            } else if (c == '\\' && i + 12 <= prompt.size() && prompt[i + 1] == 'u' && prompt[i + 6] == '\\' && prompt[i + 7] == 'u') {
                uint32_t hi = 0, lo = 0;
                if (hex4(prompt, i + 2, hi) && hex4(prompt, i + 8, lo) && hi >= 0xD800 && hi < 0xDC00 && lo >= 0xDC00 && lo < 0xE000) {
                    const uint32_t cp = 0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00);
                    if (cp >= MARK0 && cp - MARK0 < tags.size()) {
                        put(cp - MARK0, 12, i);
                        continue;
                    }
                }
            }
            out += (char) c;
            ++i;
        }
        prompt = std::move(out);
        return spans;
    }

    static bool hex4(const std::string & s, size_t at, uint32_t & v) {
        v = 0;
        for (size_t j = 0; j < 4; ++j) {
            const char c = s[at + j];
            uint32_t d;
            if (c >= '0' && c <= '9') d = (uint32_t) (c - '0');
            else if (c >= 'a' && c <= 'f') d = (uint32_t) (c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') d = (uint32_t) (c - 'A' + 10);
            else return false;
            v = (v << 4) | d;
        }
        return true;
    }
};

} // namespace strata_frontend
