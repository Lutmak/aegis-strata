// aegis-strata: the server context with a Strata engine behind it.
//
// This is the fork's replacement for llama.cpp's tools/server/server-context.cpp. Everything around it is llama.cpp's
// own, compiled unmodified from the submodule: the HTTP layer (server-http), the task queue and response reader
// (server-queue), the task and result types with their OpenAI/Anthropic/Responses JSON (server-task), request parsing,
// tokenization helpers and SSE formatting (server-common), the request schema (server-schema). What this file adds is
// the part that llama.cpp does with llama_context, llama_batch and common_sampler and that the engine does inside
// its own process: run one request at a time through `strata`, turn its `T <id>` lines into partial
// results, and its `DONE` line into the final one.
#pragma once

#include "server-http.h"

#include <memory>
#include <string>
#include <vector>

struct common_params;
struct strata_context_impl;

struct strata_context {
    std::unique_ptr<strata_context_impl> impl;

    strata_context();
    ~strata_context();

    // Registers the HTTP routes on `http`. Handlers run only once http.is_ready is set, after load().
    void init_routes(server_http_context & http);

    // fork+exec the engine. Call before any thread exists (before http.start()).
    bool start_engine(const common_params & params, const std::string & engine_exe, const std::vector<std::string> & engine_args, std::string & err);

    // Loads the vocabulary and chat template from the GGUF (vocab only, no tensors) and waits for the engine's READY.
    bool load(common_params & params, std::string & err);
    // --picture-cache: how many pictures' encoded rows are kept for the turns that resend them (0: none); before load()
    void set_picture_cache(size_t entries, size_t bytes);

    // --engine-timeout: seconds of silence from the engine during a request before the server gives up on it and
    // ends, so a service manager restarts both (0: wait for ever). Call before load().
    void set_engine_timeout(int seconds);

    // --no-sandbox / --sandbox-allow-read / --sandbox-allow-write; call before start_engine()
    void set_sandbox(bool enabled, std::vector<std::string> allow_read, std::vector<std::string> allow_write);

    // Blocks the calling thread running the task loop until request_stop() or terminate().
    void start_loop();

    // Ask the task loop to return, and nothing more. Safe to call from a signal handler: it touches one queue
    // flag and no slot, no model and no HTTP state.
    //
    // This exists because terminate() is NOT safe there. It tears down the result queue, and llama.cpp's
    // server-queue calls std::terminate() on a reader parked in recv_with_timeout ("we cannot return here since
    // the caller is HTTP code") -- so a SIGTERM arriving while a request was generating aborted the process with
    // exit 134 and a core dump instead of stopping cleanly. The in-flight request is now completed on the main
    // thread by fail_inflight() after start_loop() returns, with no reader left parked by the time the result
    // queue goes away.
    void request_stop();

    // Complete a request still in flight with `why` (503), so its HTTP handler wakes and returns. Call on the
    // main thread after start_loop() has returned, i.e. once the task loop is no longer touching the slot, and
    // before terminate(). Does nothing when no request is in flight. Returns true if one was completed.
    bool fail_inflight(const std::string & why);

    void terminate();

    // Non-blocking: true once the engine process has exited; `why` names the signal or exit code.
    bool engine_exited(std::string & why);
    bool engine_alive() const;

    // True when the engine let us down during a request -- it closed its output, or it went silent past
    // --engine-timeout. NOT set by a normal shutdown, which quits the engine on purpose: read it after
    // start_loop() to tell "we were asked to stop" from "the engine failed and we should be restarted".
    bool engine_failed() const;

    // the context the engine allocated (READY line)
    long long n_ctx() const;
};
