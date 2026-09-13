#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <locale>
#include <sstream>
#include <string>
#include <sys/stat.h>
#include <time.h>

// File-only IPC. SMU access remains in bc250-memory.service.
namespace memory_telemetry {
inline double boot_seconds() {
    timespec ts{};
    if (clock_gettime(CLOCK_BOOTTIME, &ts) != 0) return -1;
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

inline std::string json_string(const std::string &text) {
    std::ostringstream out;
    out << '"';
    for (unsigned char c : text) {
        if (c == '"' || c == '\\') out << '\\' << c;
        else if (c < 0x20) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << int(c);
        else out << c;
    }
    out << '"';
    return out.str();
}

inline std::string unavailable(const std::string &status, const std::string &error = "", long age = -1) {
    return "{\"valid\":false,\"status\":" + json_string(status)
        + ",\"error\":" + (error.empty() ? "null" : json_string(error))
        + ",\"age_ms\":" + (age < 0 ? "null" : std::to_string(age))
        + ",\"chips_c\":[null,null,null,null,null,null,null,null],"
          "\"average_c\":null,\"hotspot_c\":null,\"hotspot_chip\":null,"
          "\"saturated\":false,\"saturated_chips\":[],\"raw\":[]}";
}

inline std::string read_json(const std::string &path = "/run/bc250-memory/telemetry",
                             double now = -1) {
    // Confirm a regular file before opening: a FIFO planted at this path
    // (root-only, since /run/bc250-memory is 0755 root-owned) would otherwise
    // block ifstream::read forever and wedge the daemon's main loop, which has
    // no watchdog. Any residual stat/open race is likewise root-only.
    struct stat st{};
    if (stat(path.c_str(), &st) != 0) return unavailable("unavailable");
    if (!S_ISREG(st.st_mode)) return unavailable("invalid_data");
    std::ifstream file(path);
    if (!file) return unavailable("unavailable");
    char buffer[4097];
    file.read(buffer, sizeof(buffer));
    if (file.bad() || file.gcount() == sizeof(buffer)) return unavailable("invalid_data");
    std::istringstream input(std::string(buffer, static_cast<size_t>(file.gcount())));
    input.imbue(std::locale::classic());
    std::string magic, timestamp, status, words, error, extra;
    if (!std::getline(input, magic) || magic != "BC250_MEMORY_V1"
        || !std::getline(input, timestamp) || !std::getline(input, status)
        || !std::getline(input, words) || !std::getline(input, error)
        || std::getline(input, extra)) return unavailable("invalid_data");
    std::istringstream stamp(timestamp);
    stamp.imbue(std::locale::classic());
    double sampled;
    if (now == -1) now = boot_seconds();
    if (!(stamp >> sampled) || (stamp >> extra) || !std::isfinite(sampled)
        || !std::isfinite(now) || sampled < 0 || now < sampled) return unavailable("invalid_data");
    const double elapsed = now - sampled;
    const long age = static_cast<long>(std::min(elapsed * 1000, 2147483647.0));
    if (elapsed > 15) return unavailable("stale", "memory collector has not supplied a fresh sample", age);
    if (status != "ok") {
        if (status != "starting" && status != "stopped" && status != "error" && status != "invalid_reading")
            return unavailable("invalid_data");
        if (!words.empty()) return unavailable("invalid_data");
        return unavailable(status, error, age);
    }
    std::array<uint32_t, 8> raw{};
    std::array<int, 8> chips{};
    std::istringstream values(words);
    unsigned long long value;
    int sum = 0, hotspot = 0;
    bool saturated = false;
    for (size_t i = 0; i < raw.size(); ++i) {
        if (!(values >> value) || value > UINT32_MAX || (value & 0xFF) > 80)
            return unavailable("invalid_data");
        raw[i] = static_cast<uint32_t>(value);
        chips[i] = static_cast<int>(value & 0xFF) * 2 - 40;
        sum += chips[i];
        if (chips[i] > chips[hotspot]) hotspot = static_cast<int>(i);
        saturated |= chips[i] == 120;
    }
    if ((values >> extra) || !error.empty()) return unavailable("invalid_data");
    std::ostringstream out;
    out.imbue(std::locale::classic());
    out << "{\"valid\":true,\"status\":\"ok\",\"error\":null,\"age_ms\":" << age << ",\"chips_c\":[";
    for (size_t i = 0; i < chips.size(); ++i) out << (i ? "," : "") << chips[i];
    out << "],\"average_c\":" << sum / 8.0 << ",\"hotspot_c\":" << chips[hotspot]
        << ",\"hotspot_chip\":" << hotspot << ",\"saturated\":" << (saturated ? "true" : "false")
        << ",\"saturated_chips\":[";
    bool first = true;
    for (size_t i = 0; i < chips.size(); ++i) if (chips[i] == 120) {
        out << (first ? "" : ",") << i;
        first = false;
    }
    out << "],\"raw\":[";
    for (size_t i = 0; i < raw.size(); ++i) out << (i ? "," : "") << raw[i];
    out << "]}";
    return out.str();
}
} // namespace memory_telemetry
