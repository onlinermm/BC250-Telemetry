#pragma once
// /etc/bc250-telemetry.conf — simple "key = value" file, '#' or ';' comments.
//
// The parser never refuses to start the daemon: an unknown key or a bad value
// is reported as a warning and the built-in default is used instead. That way
// a config written for a newer release (or a typo) degrades gracefully rather
// than putting apu-telemetry.service into a restart loop.
#include <cerrno>
#include <cstdlib>
#include <fstream>
#include <string>
#include <vector>

#include "sysfs_util.h"

namespace telemetry_config {

enum class VrmSource { Auto, Hwmon, Pmbus, Off };
enum class MemorySource { Auto, Hwmon, Collector, Off };

struct Config {
    VrmSource vrm_source = VrmSource::Auto;
    int i2c_bus = -1;                       // -1 = auto-detect (PMBus only)
    MemorySource memory_source = MemorySource::Auto;
    int poll_interval_ms = 700;             // main loop
    int memory_poll_interval_ms = 3000;     // kernel bc250_memory reads (SMU)
    bool run_files = true;                  // /run/bc250/* for CoolerControl/MangoHud
};

inline const char *to_string(VrmSource s) {
    switch (s) {
    case VrmSource::Auto: return "auto";
    case VrmSource::Hwmon: return "hwmon";
    case VrmSource::Pmbus: return "pmbus";
    case VrmSource::Off: return "off";
    }
    return "?";
}

inline const char *to_string(MemorySource s) {
    switch (s) {
    case MemorySource::Auto: return "auto";
    case MemorySource::Hwmon: return "hwmon";
    case MemorySource::Collector: return "collector";
    case MemorySource::Off: return "off";
    }
    return "?";
}

inline bool parse_vrm_source(const std::string &v, VrmSource &out) {
    if (v == "auto") out = VrmSource::Auto;
    else if (v == "hwmon") out = VrmSource::Hwmon;
    else if (v == "pmbus") out = VrmSource::Pmbus;
    else if (v == "off") out = VrmSource::Off;
    else return false;
    return true;
}

inline bool parse_memory_source(const std::string &v, MemorySource &out) {
    if (v == "auto") out = MemorySource::Auto;
    else if (v == "hwmon") out = MemorySource::Hwmon;
    else if (v == "collector") out = MemorySource::Collector;
    else if (v == "off") out = MemorySource::Off;
    else return false;
    return true;
}

inline bool parse_int(const std::string &v, long lo, long hi, int &out) {
    if (v.empty()) return false;
    char *end = nullptr;
    errno = 0;
    const long n = std::strtol(v.c_str(), &end, 10);
    if (errno != 0 || *end != '\0' || n < lo || n > hi) return false;
    out = static_cast<int>(n);
    return true;
}

inline bool parse_bool(const std::string &v, bool &out) {
    if (v == "on" || v == "yes" || v == "true" || v == "1") out = true;
    else if (v == "off" || v == "no" || v == "false" || v == "0") out = false;
    else return false;
    return true;
}

// Returns the parsed config; human-readable problems go into `warnings`.
// A missing file is not a problem — every key has a default.
inline Config load(const std::string &path, std::vector<std::string> &warnings) {
    Config c;
    std::ifstream f(path);
    if (!f) {
        if (errno != ENOENT) warnings.push_back("cannot read " + path + ", using defaults");
        return c;
    }
    std::string line;
    unsigned lineno = 0;
    while (std::getline(f, line)) {
        ++lineno;
        line = sysfs::trim(line);
        if (line.empty() || line[0] == '#' || line[0] == ';') continue;
        const auto where = path + ":" + std::to_string(lineno) + ": ";
        const auto eq = line.find('=');
        if (eq == std::string::npos) {
            warnings.push_back(where + "expected key = value, line ignored");
            continue;
        }
        const std::string key = sysfs::trim(line.substr(0, eq));
        std::string value = sysfs::trim(line.substr(eq + 1));
        // Allow trailing comments: "vrm_source = auto  # comment"
        const auto hash = value.find(" #");
        if (hash != std::string::npos) value = sysfs::trim(value.substr(0, hash));
        bool ok = true;
        if (key == "vrm_source") ok = parse_vrm_source(value, c.vrm_source);
        else if (key == "memory_source") ok = parse_memory_source(value, c.memory_source);
        else if (key == "i2c_bus") {
            if (value == "auto") c.i2c_bus = -1;
            else ok = parse_int(value, 0, 255, c.i2c_bus);
        }
        else if (key == "poll_interval_ms") ok = parse_int(value, 250, 10000, c.poll_interval_ms);
        else if (key == "memory_poll_interval_ms") ok = parse_int(value, 1000, 10000, c.memory_poll_interval_ms);
        else if (key == "run_files") ok = parse_bool(value, c.run_files);
        else {
            warnings.push_back(where + "unknown key '" + key + "' ignored");
            continue;
        }
        if (!ok) warnings.push_back(where + "invalid value '" + value + "' for " + key + ", using default");
    }
    return c;
}

} // namespace telemetry_config
