#include <iostream>
#include <fstream>
#include <iomanip>
#include <array>
#include <cerrno>
#include <cmath>
#include <csignal>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <vector>

#include "daemon_log.h"
#include "memory_telemetry.h"
#include "sysfs_util.h"
#include "telemetry_config.h"
#include "vrm_backend.h"

// Installer metadata, deliberately emitted even under -O2. Ordinary string
// literals (such as the snapshot path) may be split into immediate stores.
// Inspecting this marker never executes an older daemon or touches hardware.
// install.sh greps for the memory_snapshot_v1 prefix; keep it first.
static const char BC250_FEATURES[] __attribute__((used)) =
    "BC250_TELEMETRY_FEATURES=memory_snapshot_v1,vrm_hwmon_v1,memory_hwmon_v1,config_v1";

using namespace std;
using daemon_log::log;
using telemetry_config::Config;
using telemetry_config::MemorySource;
using telemetry_config::VrmSource;
namespace L = daemon_log;

static constexpr const char *DEFAULT_CONFIG_PATH = "/etc/bc250-telemetry.conf";

// Hardware/driver re-detection and NVMe polling period. Re-detection reads
// only sysfs (no I2C) unless a raw PMBus probe is actually due.
static constexpr double REFRESH_PERIOD_S = 10.0;

// bc250-memory.service output; only the test suite points this elsewhere.
static string g_memory_snapshot = "/run/bc250-memory/telemetry";

static volatile sig_atomic_t g_running = 1;
static volatile sig_atomic_t g_reload = 0;

static void handle_shutdown_signal(int) { g_running = 0; }
static void handle_reload_signal(int) { g_reload = 1; }

// ------------------------------------------------------------ VRM source ---

class VrmManager {
public:
    void configure(const Config &cfg, int bus_override) {
        pm_.close_bus();
        hw_ = vrm::HwmonVrm();
        cfg_ = cfg;
        bus_override_ = bus_override;
        kind_ = Kind::None;
        probe_rounds_ = 0;
        gave_up_ = false;
        next_probe_at_ = 0;
        failures_ = 0;
        next_read_at_ = 0;
        status_.clear();
    }

    void refresh(double now) {
        if (cfg_.vrm_source == VrmSource::Off) {
            pm_.close_bus();
            kind_ = Kind::Off;
            set_status(L::Info, "VRM telemetry disabled (vrm_source = off)");
            return;
        }

        // 1. Kernel driver: preferred in auto, required in hwmon mode.
        const string dir = sysfs::find_hwmon("bc250_vrm");
        if (cfg_.vrm_source != VrmSource::Pmbus && !dir.empty()) {
            if (kind_ != Kind::Hwmon || hw_.dir() != dir) {
                pm_.close_bus();
                if (hw_.bind(dir)) {
                    kind_ = Kind::Hwmon;
                    failures_ = 0;
                    next_read_at_ = 0;
                    set_status(L::Info, "VRM source: kernel bc250_vrm driver (" + dir +
                                        "); no direct I2C access");
                } else {
                    kind_ = Kind::None;
                    set_status(L::Warning, "bc250_vrm device " + dir +
                                           " lacks the expected sensor channels; VRM telemetry unavailable");
                }
            }
            return;
        }
        if (kind_ == Kind::Hwmon) {
            kind_ = Kind::None;
            hw_ = vrm::HwmonVrm();
        }
        if (cfg_.vrm_source == VrmSource::Hwmon) {
            set_status(L::Info, "waiting for the bc250_vrm kernel driver (vrm_source = hwmon)");
            return;
        }

        // 2. Raw PMBus — only while no kernel driver owns the PMIC address.
        vector<int> buses;
        if (bus_override_ >= 0) {
            buses.push_back(bus_override_);
        } else {
            for (const auto &a : vrm::pmbus_adapters()) buses.push_back(a.bus);
        }
        for (int bus : buses) {
            const string driver = vrm::bound_driver(bus);
            if (driver.empty()) continue;
            pm_.close_bus();
            kind_ = Kind::None;
            set_status(L::Warning, "I2C device " + to_string(bus) + "-0060 is owned by kernel driver '" +
                                   driver + "' without a usable bc250_vrm hwmon device; "
                                   "raw PMBus access disabled to avoid colliding with it");
            return;
        }
        if (kind_ == Kind::Pmbus && pm_.is_open()) return;
        if (gave_up_ || now < next_probe_at_) return;
        if (buses.empty()) {
            set_status(L::Info, "no AMD SMBus (PIIX4) adapter found; VRM telemetry unavailable "
                                "(set i2c_bus in " + string(DEFAULT_CONFIG_PATH) + " to force one)");
            next_probe_at_ = now + 60;
            return;
        }

        switch (pm_.discover(buses)) {
        case vrm::Pmbus::Probe::Found:
            kind_ = Kind::Pmbus;
            probe_rounds_ = 0;
            failures_ = 0;
            set_status(L::Info, "VRM source: raw PMBus on /dev/i2c-" + to_string(pm_.bus()) +
                                (bus_override_ >= 0 ? " (configured bus)" : " (auto-detected)"));
            break;
        case vrm::Pmbus::Probe::NoAccess:
            set_status(L::Warning, "cannot open /dev/i2c-* for the SMBus adapters "
                                   "(is the i2c-dev module loaded?); retrying");
            next_probe_at_ = now + 30;
            break;
        case vrm::Pmbus::Probe::NotFound: {
            // Every probe that misses shows up in dmesg as "SMBus Timeout" /
            // "Failed!" from i2c-piix4, so back off hard and eventually stop:
            // a board without the SMBus hardware mod will never answer.
            static constexpr int BACKOFF_S[] = {30, 60, 120, 300, 600};
            static constexpr int MAX_ROUNDS = sizeof(BACKOFF_S) / sizeof(BACKOFF_S[0]) + 1;
            ++probe_rounds_;
            if (probe_rounds_ >= MAX_ROUNDS) {
                gave_up_ = true;
                set_status(L::Notice, "no VRM PMIC answered at 0x60 after " + to_string(MAX_ROUNDS) +
                                      " attempts; stopped probing to keep the SMBus quiet. VRM telemetry "
                                      "needs the SMBus hardware mod (see hardware.md). "
                                      "Run 'systemctl reload apu-telemetry' to retry");
            } else {
                const int delay = BACKOFF_S[probe_rounds_ - 1];
                next_probe_at_ = now + delay;
                set_status(L::Info, "no VRM PMIC answered at 0x60 (attempt " + to_string(probe_rounds_) +
                                    "); next attempt in " + to_string(delay) + " s");
            }
            break;
        }
        }
    }

    void read(vrm::Rail &cpu, vrm::Rail &gpu, double now) {
        cpu = vrm::Rail();
        gpu = vrm::Rail();
        if (now < next_read_at_) return;
        if (kind_ == Kind::Hwmon) {
            if (hw_.read(cpu, gpu, now) == 0) {
                on_failure(now, strerror(hw_.last_errno()));
            } else {
                on_success();
            }
        } else if (kind_ == Kind::Pmbus) {
            cpu = pm_.read(0);
            gpu = pm_.read(1);
            if (cpu.valid || gpu.valid) {
                on_success();
            } else if (on_failure(now, "no valid PMBus response")) {
                // Bus/PMIC went away: drop it and rediscover with back-off
                // rather than retrying the same dead bus every cycle.
                pm_.close_bus();
                kind_ = Kind::None;
                next_read_at_ = 0;
                next_probe_at_ = now + 30;
            }
        }
    }

    const char *source_name() const {
        switch (kind_) {
        case Kind::Hwmon: return "hwmon";
        case Kind::Pmbus: return "pmbus";
        case Kind::Off: return "off";
        default: return "none";
        }
    }

private:
    enum class Kind { None, Hwmon, Pmbus, Off };
    static constexpr int FAIL_LIMIT = 5;          // consecutive fully failed cycles
    static constexpr double FAILED_POLL_S = 10.0; // first poll period while failing...
    static constexpr double FAILED_POLL_MAX_S = 300.0; // ...doubling up to this

    void set_status(L::Level level, const string &message) {
        if (message == status_) return;
        status_ = message;
        log(level, "%s", message.c_str());
    }

    // Returns true when the failure limit was just reached.
    bool on_failure(double now, const char *why) {
        ++failures_;
        // Every failed transfer is an "SMBus Timeout" line in dmesg, so a
        // dead PMIC gets polled less and less often.
        if (failures_ >= FAIL_LIMIT)
            next_read_at_ = now + min(FAILED_POLL_MAX_S, FAILED_POLL_S * (1 << min(failures_ - FAIL_LIMIT, 5)));
        if (failures_ == FAIL_LIMIT) {
            log(L::Warning, "VRM reads failing (%s); backing off (%.0f s, up to %.0f s) until they recover",
                why, FAILED_POLL_S, FAILED_POLL_MAX_S);
            return true;
        }
        return false;
    }

    void on_success() {
        if (failures_ >= FAIL_LIMIT) log(L::Notice, "VRM readings recovered");
        failures_ = 0;
        next_read_at_ = 0;
    }

    Config cfg_;
    int bus_override_ = -1;
    Kind kind_ = Kind::None;
    vrm::HwmonVrm hw_;
    vrm::Pmbus pm_;
    int probe_rounds_ = 0;
    bool gave_up_ = false;
    double next_probe_at_ = 0;
    int failures_ = 0;
    double next_read_at_ = 0;
    string status_;
};

// --------------------------------------------------------- GDDR6 source ---

class MemoryManager {
public:
    void configure(const Config &cfg) {
        cfg_ = cfg;
        kind_ = Kind::None;
        dir_.clear();
        status_.clear();
        have_sample_ = false;
        error_.clear();
        next_read_at_ = 0;
        failures_ = 0;
        conflict_warned_ = false;
    }

    void refresh(double /*now*/) {
        if (cfg_.memory_source == MemorySource::Off) {
            kind_ = Kind::Off;
            set_status(L::Info, "GDDR6 telemetry disabled (memory_source = off)");
            return;
        }
        const string dir = sysfs::find_hwmon("bc250_memory");
        // The kernel driver and bc250-memory.service both drive SMU message
        // queue 3; running them together wedges the SMU (fans ramp up).
        if (!dir.empty() && !conflict_warned_ && collector_active()) {
            conflict_warned_ = true;
            log(L::Warning, "both the bc250_memory kernel driver and bc250-memory.service are active; "
                            "they collide on SMU queue 3. Disable one of them, e.g. "
                            "'sudo systemctl disable --now bc250-memory.service'");
        }
        if (cfg_.memory_source != MemorySource::Collector && !dir.empty()) {
            if (kind_ != Kind::Hwmon || dir_ != dir) bind(dir);
            return;
        }
        if (cfg_.memory_source == MemorySource::Hwmon) {
            kind_ = Kind::None;
            set_status(L::Info, "waiting for the bc250_memory kernel driver (memory_source = hwmon)");
            return;
        }
        kind_ = Kind::Collector;
        set_status(L::Info, "GDDR6 source: bc250-memory.service snapshot (if installed)");
    }

    string json(double now) {
        switch (kind_) {
        case Kind::Collector: return memory_telemetry::read_json(g_memory_snapshot);
        case Kind::Hwmon: return hwmon_json(now);
        default: return memory_telemetry::unavailable("unavailable");
        }
    }

    const char *source_name() const {
        switch (kind_) {
        case Kind::Hwmon: return "hwmon";
        case Kind::Collector: return "collector";
        case Kind::Off: return "off";
        default: return "none";
        }
    }

private:
    enum class Kind { None, Hwmon, Collector, Off };
    // Keep showing the previous sample through a short read hiccup, but
    // never longer than the dashboards' own 15 s staleness limit.
    static constexpr double HOLD_S = 10.0;

    void bind(const string &dir) {
        const auto labels = sysfs::hwmon_labels(dir);
        for (int i = 0; i < 8; ++i) {
            const auto it = labels.find("VRAM Chip " + to_string(i));
            // bc250_memory 1.0.0: temp1 hotspot, temp2 average, temp3..10 chips.
            chip_paths_[i] = it != labels.end() ? it->second : dir + "/temp" + to_string(i + 3) + "_input";
        }
        kind_ = Kind::Hwmon;
        dir_ = dir;
        have_sample_ = false;
        next_read_at_ = 0;
        failures_ = 0;
        set_status(L::Info, "GDDR6 source: kernel bc250_memory driver (" + dir + "), every " +
                            to_string(cfg_.memory_poll_interval_ms) + " ms");
    }

    // Each poll costs eight SMU round-trips inside the driver (it caches for
    // 1 s), so the memory poll runs on its own, slower clock.
    string hwmon_json(double now) {
        if (now >= next_read_at_) {
            std::array<int, 8> chips{};
            bool ok = true;
            int err = 0;
            for (int i = 0; i < 8 && ok; ++i) {
                long mc;
                // Stop at the first failure: every further read would just
                // wait out another SMU timeout inside the driver.
                if (!sysfs::read_long(chip_paths_[i], mc)) { ok = false; err = errno; break; }
                chips[i] = static_cast<int>(lround(mc / 1000.0));
            }
            const double interval = cfg_.memory_poll_interval_ms / 1000.0;
            if (ok) {
                if (failures_ >= 3) log(L::Notice, "GDDR6 readings recovered");
                failures_ = 0;
                chips_ = chips;
                sampled_at_ = now;
                have_sample_ = true;
                next_read_at_ = now + interval;
            } else {
                ++failures_;
                error_ = string("bc250_memory read failed: ") + strerror(err);
                if (failures_ == 3) log(L::Warning, "%s; backing off", error_.c_str());
                next_read_at_ = now + min(60.0, interval * (1 << min(failures_, 5)));
            }
        }
        if (have_sample_ && now - sampled_at_ <= HOLD_S)
            return memory_telemetry::from_chips(chips_, lround((now - sampled_at_) * 1000));
        if (!error_.empty()) return memory_telemetry::unavailable("error", error_);
        return memory_telemetry::unavailable("starting");
    }

    static bool collector_active() {
        return memory_telemetry::read_json(g_memory_snapshot).rfind("{\"valid\":true", 0) == 0;
    }

    void set_status(L::Level level, const string &message) {
        if (message == status_) return;
        status_ = message;
        log(level, "%s", message.c_str());
    }

    Config cfg_;
    Kind kind_ = Kind::None;
    string dir_;
    array<string, 8> chip_paths_;
    array<int, 8> chips_{};
    bool have_sample_ = false;
    double sampled_at_ = 0;
    string error_;
    double next_read_at_ = 0;
    int failures_ = 0;
    bool conflict_warned_ = false;
    string status_;
};

// ------------------------------------------------- plain hwmon devices ---

// Finds a hwmon directory by name and keeps it fresh: hwmonN numbers are
// reassigned when a module reloads, so a cached path is re-validated on every
// refresh instead of being trusted forever.
struct HwmonDevice {
    const char *name;
    string dir;
    void refresh() {
        if (!dir.empty() && sysfs::read_text(dir + "/name") == name) return;
        const string found = sysfs::find_hwmon(name);
        if (found != dir && !found.empty()) log(L::Info, "found %s: %s", name, found.c_str());
        dir = found;
    }
    long read(const char *attr) const {
        long v;
        if (dir.empty() || !sysfs::read_long(dir + "/" + attr, v)) return -1;
        return v;
    }
};

// ----------------------------------------------------- /run/bc250 files ---

// External sensor files under /run (tmpfs): vanish on reboot, cheap to rewrite.
// CoolerControl wants a sysfs integer (millidegrees C). MangoHud has no file
// sensor — it cats a human-readable string via custom_text + exec.
static string g_sensor_dir = "/run/bc250";

static const char *const SENSOR_FILES[] = {
    "cpu_vrm_temp", "cpu_vrm_c", "gpu_vrm_temp", "gpu_vrm_c", "vin", "cpu_vout", "gpu_vout",
    "cpu_iout", "gpu_iout", "cpu_pout", "gpu_pout", "total_power",
    "memory_hotspot_temp", "memory_hotspot_c", "memory_avg_temp", "memory_avg_c",
};

// Atomic replace of a small /run file. Skipped entirely when the reading isn't
// valid so CoolerControl / MangoHud keep the last good value instead of 0 °C.
static void write_run_file(const string &path, const string &contents) {
    const string tmp = path + ".tmp";
    ofstream file(tmp);
    if (!file.is_open()) return;
    file << contents;
    file.close();
    if (rename(tmp.c_str(), path.c_str()) != 0) unlink(tmp.c_str());
}

static void write_sensor(const char *name, bool valid, const char *fmt, ...) __attribute__((format(printf, 3, 4)));
static void write_sensor(const char *name, bool valid, const char *fmt, ...) {
    if (!valid) return;
    char buf[64];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    write_run_file(g_sensor_dir + "/" + name, buf);
}

static void write_temp_pair(const char *cc_name, const char *mh_name, float temp_c, bool valid) {
    if (!valid || temp_c < 0.0f || temp_c >= 250.0f) return;
    const long rounded = lroundf(temp_c);
    write_sensor(cc_name, true, "%ld\n", rounded * 1000);
    write_sensor(mh_name, true, "%ld\xC2\xB0" "C\n", rounded);
}

static void remove_sensor_files() {
    for (const char *name : SENSOR_FILES) unlink((g_sensor_dir + "/" + name).c_str());
}

// Pulls one numeric field out of the JSON string memory_telemetry just built
// for us this cycle. Not a general JSON parser: the input is our own
// fixed-shape string, so a plain key search is safe. Returns NAN for "null"
// or a missing key (i.e. memory data isn't currently valid).
static double json_number_field(const string &json, const char *key) {
    const string needle = string("\"") + key + "\":";
    size_t pos = json.find(needle);
    if (pos == string::npos) return NAN;
    pos += needle.size();
    if (json.compare(pos, 4, "null") == 0) return NAN;
    try {
        return stod(json.substr(pos));
    } catch (...) {
        return NAN;
    }
}

// Average frequency across all CPU cores, read from /proc/cpuinfo.
static long read_cpu_freq() {
    ifstream file("/proc/cpuinfo");
    if (!file.is_open()) return -1;
    string line;
    double sum = 0.0;
    long count = 0;
    while (getline(file, line)) {
        if (line.rfind("cpu MHz", 0) != 0) continue;
        const size_t pos = line.find(':');
        if (pos == string::npos) continue;
        try {
            sum += stod(line.substr(pos + 1));
            ++count;
        } catch (...) {
            // malformed cpuinfo line — skip it
        }
    }
    if (count == 0) return -1;
    return lround(sum / count);
}

static void write_rail(ofstream &file, const char *name, const vrm::Rail &r, bool last) {
    file << "    \"" << name << "\": {\n";
    file << "      \"valid\": " << (r.valid ? "true" : "false") << ",\n";
    file << "      \"vin\": " << fixed << setprecision(2) << r.vin << ",\n";
    file << "      \"vout\": " << fixed << setprecision(3) << r.vout << ",\n";
    file << "      \"iout\": " << fixed << setprecision(1) << r.iout << ",\n";
    file << "      \"pout\": " << fixed << setprecision(1) << r.pout << ",\n";
    file << "      \"temp\": " << fixed << setprecision(1) << r.temp << ",\n";
    file << "      \"iout_warning\": " << (r.iout_warning ? "true" : "false") << ",\n";
    file << "      \"iout_fault\": " << (r.iout_fault ? "true" : "false") << ",\n";
    file << "      \"temp_warning\": " << (r.temp_warning ? "true" : "false") << ",\n";
    file << "      \"temp_fault\": " << (r.temp_fault ? "true" : "false") << "\n";
    file << "    }" << (last ? "\n" : ",\n");
}

// ------------------------------------------------------------------ main ---

struct Options {
    string config_path = DEFAULT_CONFIG_PATH;
    int cli_bus = -1;
    string cli_vrm_source;
    bool check_config = false;
    bool once = false;
    string run_dir = "/run";
};

static bool parse_options(int argc, char **argv, Options &o) {
    auto value = [&](int &i, const string &arg, const char *flag, string &out) {
        const string eq = string(flag) + "=";
        if (arg == flag && i + 1 < argc) { out = argv[++i]; return true; }
        if (arg.rfind(eq, 0) == 0) { out = arg.substr(eq.size()); return true; }
        return false;
    };
    for (int i = 1; i < argc; ++i) {
        const string arg = argv[i];
        string v;
        if (value(i, arg, "--config", v)) o.config_path = v;
        else if (value(i, arg, "--bus", v)) {
            if (!telemetry_config::parse_int(v, 0, 255, o.cli_bus)) {
                fprintf(stderr, "--bus expects a bus number 0..255\n");
                return false;
            }
        }
        else if (value(i, arg, "--vrm-source", v)) o.cli_vrm_source = v;
        // Test-only: run against a synthetic sysfs tree / output directory.
        else if (value(i, arg, "--sysfs-root", v)) sysfs::root() = v;
        else if (value(i, arg, "--run-dir", v)) o.run_dir = v;
        else if (value(i, arg, "--memory-snapshot", v)) g_memory_snapshot = v;
        else if (arg == "--check-config") o.check_config = true;
        else if (arg == "--once") o.once = true;
        else {
            fprintf(stderr, "unknown option '%s'\n"
                            "usage: apu_telemetry [--config PATH] [--bus N] [--vrm-source auto|hwmon|pmbus|off]"
                            " [--check-config]\n", arg.c_str());
            return false;
        }
    }
    return true;
}

// Loads the config file and applies overrides. Precedence for the PMBus bus:
// --bus > BC250_I2C_BUS (older installs set it in the unit) > i2c_bus.
static Config load_config(const Options &o, int &bus_override, bool &clean) {
    vector<string> warnings;
    Config cfg = telemetry_config::load(o.config_path, warnings);
    if (!o.cli_vrm_source.empty() &&
        !telemetry_config::parse_vrm_source(o.cli_vrm_source, cfg.vrm_source))
        warnings.push_back("--vrm-source: invalid value '" + o.cli_vrm_source + "' ignored");
    bus_override = cfg.i2c_bus;
    if (const char *env = getenv("BC250_I2C_BUS")) {
        int bus;
        if (telemetry_config::parse_int(env, 0, 255, bus)) bus_override = bus;
        else warnings.push_back("BC250_I2C_BUS: invalid value ignored");
    }
    if (o.cli_bus >= 0) bus_override = o.cli_bus;
    for (const auto &w : warnings) log(L::Warning, "config: %s", w.c_str());
    clean = warnings.empty();
    log(L::Info, "config: vrm_source=%s i2c_bus=%s memory_source=%s poll_interval_ms=%d "
                 "memory_poll_interval_ms=%d run_files=%s",
        telemetry_config::to_string(cfg.vrm_source),
        bus_override >= 0 ? to_string(bus_override).c_str() : "auto",
        telemetry_config::to_string(cfg.memory_source), cfg.poll_interval_ms,
        cfg.memory_poll_interval_ms, cfg.run_files ? "on" : "off");
    return cfg;
}

int main(int argc, char **argv) {
    Options opts;
    if (!parse_options(argc, argv, opts)) return 2;

    int bus_override = -1;
    bool config_clean = true;
    Config cfg = load_config(opts, bus_override, config_clean);
    if (opts.check_config) return config_clean ? 0 : 1;

    // No SA_RESTART: a signal should cut the inter-cycle sleep short.
    struct sigaction sa{};
    sa.sa_handler = handle_shutdown_signal;
    sigaction(SIGTERM, &sa, nullptr);
    sigaction(SIGINT, &sa, nullptr);
    sa.sa_handler = handle_reload_signal;
    sigaction(SIGHUP, &sa, nullptr);

    const string json_path = opts.run_dir + "/apu_telemetry.json";
    const string json_tmp = opts.run_dir + "/apu_telemetry.tmp";
    g_sensor_dir = opts.run_dir + "/bc250";
    if (mkdir(g_sensor_dir.c_str(), 0755) != 0 && errno != EEXIST)
        log(L::Error, "failed to create %s: %s", g_sensor_dir.c_str(), strerror(errno));

    VrmManager vrm_source;
    MemoryManager memory_source;
    vrm_source.configure(cfg, bus_override);
    memory_source.configure(cfg);
    HwmonDevice amdgpu{"amdgpu", {}}, k10temp{"k10temp", {}}, nct{"nct6686", {}}, nvme{"nvme", {}};

    log(L::Info, "APU telemetry daemon started; writing %s", json_path.c_str());

    double next_refresh = 0;
    long nvme_temp_raw = -1;
    bool open_failure_logged = false;

    while (g_running) {
        if (g_reload) {
            g_reload = 0;
            log(L::Info, "reloading configuration (SIGHUP)");
            cfg = load_config(opts, bus_override, config_clean);
            vrm_source.configure(cfg, bus_override);
            memory_source.configure(cfg);
            if (!cfg.run_files) remove_sensor_files();
            next_refresh = 0;
        }

        const double now = vrm::now_s();
        if (now >= next_refresh) {
            // Drivers may appear (DKMS module loading late, i2c-dev after
            // us) or go away (rmmod) while we run; re-detect periodically.
            next_refresh = now + REFRESH_PERIOD_S;
            vrm_source.refresh(now);
            memory_source.refresh(now);
            amdgpu.refresh();
            k10temp.refresh();
            nct.refresh();
            nvme.refresh();
            // NVMe heats up and cools down slowly; poll it on the refresh clock.
            nvme_temp_raw = nvme.read("temp1_input");
        }

        vrm::Rail cpu, gpu;
        vrm_source.read(cpu, gpu, now);
        const bool total_power_valid = cpu.valid && gpu.valid;
        const float total_power = (cpu.valid ? cpu.pout : 0.0f) + (gpu.valid ? gpu.pout : 0.0f);

        const long k10_temp = k10temp.read("temp1_input");
        const long amd_sclk = amdgpu.read("freq1_input");
        const long amd_ppt = amdgpu.read("power1_input");
        const long amd_edge = amdgpu.read("temp1_input");
        const long nct_fan = nct.read("fan2_input");
        const long nct_pwm = nct.read("pwm2"); // 0-255
        const long nct_t14 = nct.read("temp2_input");
        const long nct_t15 = nct.read("temp3_input");

        const float sys_cpu_temp = (k10_temp >= 0) ? (k10_temp / 1000.0f) : -1.0f;
        const float sys_gpu_temp = (amd_edge >= 0) ? (amd_edge / 1000.0f) : -1.0f;
        const float sys_gpu_sclk = (amd_sclk >= 0) ? (amd_sclk / 1000000.0f) : -1.0f;
        const float sys_gpu_ppt = (amd_ppt >= 0) ? (amd_ppt / 1000000.0f) : -1.0f;
        const float sys_nvme_temp = (nvme_temp_raw >= 0) ? (nvme_temp_raw / 1000.0f) : -1.0f;
        const float pwm_percent = (nct_pwm >= 0) ? (nct_pwm * 100.0f / 255.0f) : -1.0f;
        const float sys_t14 = (nct_t14 >= 0) ? (nct_t14 / 1000.0f) : -1.0f;
        const float sys_t15 = (nct_t15 >= 0) ? (nct_t15 / 1000.0f) : -1.0f;
        const long cpu_freq = read_cpu_freq();
        const string memory_json = memory_source.json(now);

        ofstream file(json_tmp);
        if (file.is_open()) {
            open_failure_logged = false;
            file << "{\n";
            file << "  \"hardware\": {\n";
            write_rail(file, "cpu", cpu, false);
            write_rail(file, "gpu", gpu, false);
            file << "    \"total_power\": " << fixed << setprecision(1) << total_power << ",\n";
            file << "    \"total_power_valid\": " << (total_power_valid ? "true" : "false") << "\n";
            file << "  },\n";
            file << "  \"software\": {\n";
            file << "    \"cpu_temp_c\": " << fixed << setprecision(1) << sys_cpu_temp << ",\n";
            file << "    \"cpu_freq_mhz\": " << cpu_freq << ",\n";
            file << "    \"gpu_temp_c\": " << fixed << setprecision(1) << sys_gpu_temp << ",\n";
            file << "    \"gpu_sclk_mhz\": " << fixed << setprecision(0) << sys_gpu_sclk << ",\n";
            file << "    \"gpu_ppt_w\": " << fixed << setprecision(1) << sys_gpu_ppt << ",\n";
            file << "    \"nvme_temp_c\": " << fixed << setprecision(1) << sys_nvme_temp << ",\n";
            file << "    \"nct_t14_c\": " << fixed << setprecision(1) << sys_t14 << ",\n";
            file << "    \"nct_t15_c\": " << fixed << setprecision(1) << sys_t15 << "\n";
            file << "  },\n";
            file << "  \"cooling\": {\n";
            file << "    \"fan_rpm\": " << nct_fan << ",\n";
            file << "    \"fan_pwm_pct\": " << fixed << setprecision(0) << pwm_percent << "\n";
            file << "  },\n";
            file << "  \"sources\": {\n";
            file << "    \"vrm\": \"" << vrm_source.source_name() << "\",\n";
            file << "    \"memory\": \"" << memory_source.source_name() << "\"\n";
            file << "  },\n";
            file << "  \"memory\": " << memory_json << "\n";
            file << "}\n";
            file.close();
            rename(json_tmp.c_str(), json_path.c_str());
        } else if (!open_failure_logged) {
            open_failure_logged = true;
            log(L::Error, "failed to open %s for writing: %s", json_tmp.c_str(), strerror(errno));
        }

        if (cfg.run_files) {
            // GDDR6 for MangoHud/CoolerControl: just the two aggregate figures
            // the dashboards also lead with, not all eight chips.
            const double mem_hotspot = json_number_field(memory_json, "hotspot_c");
            const double mem_average = json_number_field(memory_json, "average_c");
            write_temp_pair("memory_hotspot_temp", "memory_hotspot_c",
                            static_cast<float>(mem_hotspot), std::isfinite(mem_hotspot));
            write_temp_pair("memory_avg_temp", "memory_avg_c",
                            static_cast<float>(mem_average), std::isfinite(mem_average));
            write_temp_pair("cpu_vrm_temp", "cpu_vrm_c", cpu.temp, cpu.valid);
            write_temp_pair("gpu_vrm_temp", "gpu_vrm_c", gpu.temp, gpu.valid);
            write_sensor("vin", cpu.valid || gpu.valid, "%.2fV\n", cpu.valid ? cpu.vin : gpu.vin);
            write_sensor("cpu_vout", cpu.valid, "%.2fV\n", cpu.vout);
            write_sensor("gpu_vout", gpu.valid, "%.2fV\n", gpu.vout);
            write_sensor("cpu_iout", cpu.valid, "%.1fA\n", cpu.iout);
            write_sensor("gpu_iout", gpu.valid, "%.1fA\n", gpu.iout);
            write_sensor("cpu_pout", cpu.valid, "%.1fW\n", cpu.pout);
            write_sensor("gpu_pout", gpu.valid, "%.1fW\n", gpu.pout);
            write_sensor("total_power", cpu.valid || gpu.valid, "%.1fW\n", total_power);
        }

        if (opts.once) break;
        // nanosleep rather than usleep: intervals may exceed one second.
        // A signal (stop/reload) interrupts it early, which is what we want.
        timespec pause{cfg.poll_interval_ms / 1000, (cfg.poll_interval_ms % 1000) * 1000000L};
        nanosleep(&pause, nullptr);
    }

    if (!opts.once) log(L::Info, "stopping");
    return 0;
}
