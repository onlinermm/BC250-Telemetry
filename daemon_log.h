#pragma once
// Journal-friendly logging. Under systemd (JOURNAL_STREAM set) each line gets
// an sd-daemon "<N>" priority prefix so `journalctl -p warning` works; on a
// terminal a readable [LEVEL] tag is used instead.
//
// Policy for the main loop: log state *changes* only (source selected, lost,
// recovered), never once per poll cycle — the daemon runs every ~700 ms.
#include <cstdarg>
#include <cstdio>
#include <cstdlib>

namespace daemon_log {

enum Level { Error = 3, Warning = 4, Notice = 5, Info = 6, Debug = 7 };

inline void vlog(Level level, const char *fmt, va_list ap) {
    static const bool journal = getenv("JOURNAL_STREAM") != nullptr;
    if (journal) {
        fprintf(stderr, "<%d>", static_cast<int>(level));
    } else {
        const char *tag = level <= Error ? "ERROR" : level == Warning ? "WARN"
                        : level == Debug ? "DEBUG" : "INFO";
        fprintf(stderr, "[%s] ", tag);
    }
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    fflush(stderr);
}

inline void log(Level level, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
inline void log(Level level, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vlog(level, fmt, ap);
    va_end(ap);
}

} // namespace daemon_log
