#pragma once
// CPU/GPU VRM (PMIC at I2C address 0x60) data sources:
//
//  * HwmonVrm — the kernel bc250_vrm driver (Hexxeh/bc250-vrm-dkms, also
//    built into linux-cachyos-bc250). Preferred whenever it is bound: the
//    kernel then owns the device and serializes every PAGE+READ pair.
//  * Pmbus     — direct /dev/i2c-N access, for systems without that driver.
//
// The two must never run at the same time: our PAGE write could land between
// the driver's PAGE write and its READ (wrong-rail values), and probing other
// buses while the driver holds 0x60 is what produced "i2c-5..8: SMBus
// Timeout / Failed!" floods in dmesg.
#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <cstdint>
#include <ctime>
#include <fcntl.h>
#include <linux/i2c-dev.h>
#include <linux/i2c.h>
#include <string>
#include <sys/ioctl.h>
#include <unistd.h>
#include <vector>

#include "sysfs_util.h"

namespace vrm {

inline double now_s() {
    timespec ts{};
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

struct Rail {
    float vin = 0, vout = 0, iout = 0, temp = 0, pout = 0;
    bool valid = false;
    bool iout_warning = false, iout_fault = false;
    bool temp_warning = false, temp_fault = false;
};

// ---------------------------------------------------------------- hwmon ---

// Channel layout of bc250_vrm 1.0.0. Labels are matched first; the indices
// are only a fallback in case a future driver renames a label.
struct ChannelSpec { const char *label; const char *fallback; };
static constexpr ChannelSpec VIN_SPEC = {"VIN (12V Input)", "in0_input"};
static constexpr ChannelSpec VOUT_SPEC[2] = {{"CPU Voltage", "in1_input"}, {"GPU Core Voltage", "in2_input"}};
static constexpr ChannelSpec IOUT_SPEC[2] = {{"CPU Current", "curr1_input"}, {"GPU Current", "curr2_input"}};
static constexpr ChannelSpec TEMP_SPEC[2] = {{"CPU VRM Temp", "temp1_input"}, {"GPU VRM Temp", "temp2_input"}};

class HwmonVrm {
public:
    // A single failed transfer (the driver returns EIO for a NAK or for an
    // implausible current) must not blank the dashboard: reuse a channel's
    // last good value for this long before declaring the rail invalid.
    static constexpr double HOLD_S = 3.0;

    bool bind(const std::string &dir) {
        *this = HwmonVrm();
        const auto labels = sysfs::hwmon_labels(dir);
        auto resolve = [&](Channel &ch, const ChannelSpec &spec, double scale) {
            const auto it = labels.find(spec.label);
            ch.path = it != labels.end() ? it->second : dir + "/" + spec.fallback;
            ch.scale = scale;
            return sysfs::exists(ch.path);
        };
        // hwmon ABI units: in = mV, curr = mA, temp = m°C.
        bool ok = resolve(vin_, VIN_SPEC, 1e-3);
        for (int r = 0; r < 2; ++r) {
            ok &= resolve(vout_[r], VOUT_SPEC[r], 1e-3);
            ok &= resolve(iout_[r], IOUT_SPEC[r], 1e-3);
            ok &= resolve(temp_[r], TEMP_SPEC[r], 1e-3);
            // Protection limits are PMIC configuration, not live data: read
            // them once per bind (one I2C transfer each), never per cycle.
            temp_max_[r] = read_limit(temp_[r].path, "_max");
            temp_crit_[r] = read_limit(temp_[r].path, "_crit");
            curr_crit_[r] = read_limit(iout_[r].path, "_crit");
        }
        dir_ = ok ? dir : std::string();
        return ok;
    }

    const std::string &dir() const { return dir_; }
    bool bound() const { return !dir_.empty(); }

    // Seven transfers per cycle: VIN once (shared input), then VOUT/IOUT/TEMP
    // per rail. power*_input is not read — the driver implements it as two
    // more PAGE+READ pairs, and VOUT*IOUT is exactly what it computes.
    // Returns the number of channels read fresh this cycle; last_errno()
    // holds the most recent failure.
    //
    // A rail is valid when VIN, VOUT and the temperature are usable. Current
    // is deliberately NOT required: near 0 A (idle) the PMIC reports values the
    // driver rejects with EIO about a quarter of the time, while every other
    // channel on the same bus keeps answering. A failed current read is then a
    // genuine "about zero", so it must not take the temperature down with it.
    int read(Rail &cpu, Rail &gpu, double now) {
        int fresh = 0;
        streak_ = 0;
        fresh += sample(vin_, now);
        Rail *rails[2] = {&cpu, &gpu};
        for (int r = 0; r < 2; ++r) {
            fresh += sample(vout_[r], now);
            fresh += sample(iout_[r], now);
            fresh += sample(temp_[r], now);
            Rail &t = *rails[r];
            t = Rail();
            if (!usable(vin_, now) || !usable(vout_[r], now) || !usable(temp_[r], now)) continue;
            double iout = 0;
            if (iout_[r].ok_at == now) {
                iout = iout_[r].value;
            } else if (vout_[r].ok_at == now || temp_[r].ok_at == now) {
                iout = 0;                       // bus alive, only the current read failed
            } else if (usable(iout_[r], now)) {
                iout = iout_[r].value;          // bus silent: hold briefly like the others
            } else {
                continue;
            }
            t.vin = static_cast<float>(vin_.value);
            t.vout = static_cast<float>(vout_[r].value);
            t.iout = static_cast<float>(iout);
            t.temp = static_cast<float>(temp_[r].value);
            t.pout = t.vout * t.iout;
            if (t.vin <= 0 && t.vout <= 0) continue;
            t.valid = true;
            const double temp_mc = temp_[r].value * 1000.0, iout_ma = iout * 1000.0;
            t.temp_warning = temp_max_[r] > 0 && temp_mc >= temp_max_[r];
            t.temp_fault = temp_crit_[r] > 0 && temp_mc >= temp_crit_[r];
            t.iout_fault = curr_crit_[r] > 0 && iout_ma >= curr_crit_[r];
        }
        return fresh;
    }

    int last_errno() const { return last_errno_; }

private:
    struct Channel {
        std::string path;
        double scale = 1;
        double value = 0;
        double ok_at = -1;   // monotonic time of the last good read, -1 = never
    };

    // After two failures in a row the PMIC is most likely not answering at
    // all: skip the rest of this cycle instead of issuing (and timing out)
    // every remaining transfer. Skipped channels fall back to their held
    // values like any other failed read.
    int sample(Channel &ch, double now) {
        if (streak_ >= 2) return 0;
        long raw;
        if (!sysfs::read_long(ch.path, raw)) {
            last_errno_ = errno;
            ++streak_;
            return 0;
        }
        streak_ = 0;
        ch.value = raw * ch.scale;
        ch.ok_at = now;
        return 1;
    }

    static bool usable(const Channel &ch, double now) {
        return ch.ok_at >= 0 && now - ch.ok_at <= HOLD_S;
    }

    static long read_limit(const std::string &input_path, const char *suffix) {
        const auto pos = input_path.rfind("_input");
        if (pos == std::string::npos) return -1;
        long v;
        const std::string path = input_path.substr(0, pos) + suffix;
        if (!sysfs::exists(path) || !sysfs::read_long(path, v) || v <= 0) return -1;
        return v;
    }

    std::string dir_;
    Channel vin_, vout_[2], iout_[2], temp_[2];
    long temp_max_[2] = {-1, -1}, temp_crit_[2] = {-1, -1}, curr_crit_[2] = {-1, -1};
    int last_errno_ = 0;
    int streak_ = 0;
};

// ---------------------------------------------------------------- PMBus ---

static constexpr uint8_t PMBUS_ADDR = 0x60;

// I2C adapters that may carry the PMIC: the AMD FCH SMBus (i2c_piix4).
// Port 0 first — that is where the hardware mod wires the VRM and the only
// port the kernel driver itself looks at. Never AMDGPU DDC/AUX buses: those
// go to the monitor.
struct Adapter { int bus; std::string name; };

inline std::vector<Adapter> pmbus_adapters() {
    std::vector<Adapter> port0, other;
    const std::string base = sysfs::root() + "/bus/i2c/devices";
    for (const auto &entry : sysfs::list_dir(base)) {
        if (entry.rfind("i2c-", 0) != 0) continue;
        char *end = nullptr;
        const long bus = std::strtol(entry.c_str() + 4, &end, 10);
        if (end == entry.c_str() + 4 || *end != '\0' || bus < 0 || bus > 255) continue;
        const std::string name = sysfs::read_text(base + "/" + entry + "/name");
        if (name.find("PIIX4") == std::string::npos) continue;
        // Older kernels name port 0 just "SMBus PIIX4 adapter at 0b00".
        const bool is_port0 = name.find("port 0") != std::string::npos ||
                              name.find(" port ") == std::string::npos;
        (is_port0 ? port0 : other).push_back({static_cast<int>(bus), name});
    }
    auto by_bus = [](const Adapter &a, const Adapter &b) { return a.bus < b.bus; };
    std::sort(port0.begin(), port0.end(), by_bus);
    std::sort(other.begin(), other.end(), by_bus);
    port0.insert(port0.end(), other.begin(), other.end());
    return port0;
}

// Name of the kernel driver bound to <bus>-0060, or "" if none. A bound
// client means hands off: i2c-dev would refuse I2C_SLAVE with EBUSY anyway,
// and I2C_SLAVE_FORCE is never used.
inline std::string bound_driver(int bus) {
    const std::string dev = sysfs::root() + "/bus/i2c/devices/" + std::to_string(bus) + "-0060";
    if (!sysfs::is_symlink(dev + "/driver")) return {};
    std::string name = sysfs::link_basename(dev + "/driver");
    return name.empty() ? "unknown" : name;
}

class Pmbus {
public:
    enum class Probe { Found, NotFound, NoAccess };

    ~Pmbus() { close_bus(); }

    bool is_open() const { return fd_ >= 0; }
    int bus() const { return bus_; }

    void close_bus() {
        if (fd_ >= 0) close(fd_);
        fd_ = -1;
        bus_ = -1;
    }

    // Tries the given buses in order. NoAccess means no transfer was issued
    // at all (no /dev/i2c-N yet, permissions, or every candidate is owned by
    // a kernel driver), so it does not count as a failed hardware probe.
    Probe discover(const std::vector<int> &buses) {
        close_bus();
        bool touched = false;
        for (int bus : buses) {
            if (!bound_driver(bus).empty()) continue;
            const int r = try_bus(bus);
            if (r > 0) return Probe::Found;
            if (r == 0) touched = true;
        }
        return touched ? Probe::NotFound : Probe::NoAccess;
    }

    Rail read(uint8_t page) {
        Rail t;
        if (fd_ < 0) return t;
        if (write_byte(PMBUS_PAGE, page) < 0) return t;
        usleep(5000);
        // Stop at the first failed transfer; the rest would fail the same way.
        const int32_t vin_raw = read_word(PMBUS_READ_VIN);
        if (vin_raw < 0) return t;
        const int32_t vout_raw = read_word(PMBUS_READ_VOUT);
        if (vout_raw < 0) return t;
        const int32_t iout_raw = read_word(PMBUS_READ_IOUT);
        if (iout_raw < 0) return t;
        const int32_t temp_raw = read_word(PMBUS_READ_TEMP1);
        if (temp_raw < 0) return t;
        if ((vin_raw == 0 || vin_raw == 0xFFFF) && (vout_raw == 0 || vout_raw == 0xFFFF)) return t;
        // >250 A is bus noise, not a real current reading.
        if (iout_raw > IOUT_GLITCH_THRESHOLD_RAW) return t;

        // Divisors found empirically against a multimeter; they match the
        // kernel driver's scaling (VIN*10 mV, VOUT mV, IOUT*100 mA).
        t.valid = true;
        t.vin = vin_raw / 100.0f;
        t.vout = vout_raw / 1000.0f;
        t.iout = iout_raw / 10.0f;
        t.temp = static_cast<float>(temp_raw & TEMP_MASK);
        t.pout = t.vout * t.iout;

        // Fault/warning bits latched by the PMIC's own protection comparators;
        // a failed read (-1) just leaves the flags false.
        const int32_t status_iout = read_byte(PMBUS_STATUS_IOUT);
        const int32_t status_temp = read_byte(PMBUS_STATUS_TEMP);
        t.iout_warning = status_iout >= 0 && (status_iout & 0x20);
        t.iout_fault = status_iout >= 0 && (status_iout & 0x80);
        t.temp_warning = status_temp >= 0 && (status_temp & 0x40);
        t.temp_fault = status_temp >= 0 && (status_temp & 0x80);
        return t;
    }

private:
    static constexpr uint8_t PMBUS_PAGE = 0x00;
    static constexpr uint8_t PMBUS_STATUS_IOUT = 0x7B;
    static constexpr uint8_t PMBUS_STATUS_TEMP = 0x7D;
    static constexpr uint8_t PMBUS_READ_VIN = 0x88;
    static constexpr uint8_t PMBUS_READ_VOUT = 0x8B;
    static constexpr uint8_t PMBUS_READ_IOUT = 0x8C;
    static constexpr uint8_t PMBUS_READ_TEMP1 = 0x8D;
    static constexpr uint8_t PMBUS_REVISION = 0x98;
    static constexpr uint16_t TEMP_MASK = 0x07FF;
    static constexpr int32_t IOUT_GLITCH_THRESHOLD_RAW = 2500;

    // 1 = found (fd_ kept open), 0 = transfers issued but no PMIC, -1 = no transfer.
    int try_bus(int bus) {
        const std::string path = "/dev/i2c-" + std::to_string(bus);
        const int fd = open(path.c_str(), O_RDWR | O_CLOEXEC);
        if (fd < 0) return -1;
        if (ioctl(fd, I2C_SLAVE, PMBUS_ADDR) < 0) { close(fd); return -1; }
        fd_ = fd;
        // Identify with a read first (as the kernel driver does): a device
        // that is not our PMIC never sees a write from us.
        if (read_byte(PMBUS_REVISION) < 0) { close_bus(); return 0; }
        if (write_byte(PMBUS_PAGE, 0) < 0) { close_bus(); return 0; }
        usleep(5000);
        const int32_t vout = read_word(PMBUS_READ_VOUT);
        if (vout <= 0 || vout == 0xFFFF) { close_bus(); return 0; }
        bus_ = bus;
        return 1;
    }

    int32_t access(char rw, uint8_t cmd, int size, i2c_smbus_data *data) const {
        i2c_smbus_ioctl_data args{};
        args.read_write = rw;
        args.command = cmd;
        args.size = size;
        args.data = data;
        return ioctl(fd_, I2C_SMBUS, &args);
    }
    int32_t write_byte(uint8_t cmd, uint8_t value) const {
        i2c_smbus_data d{};
        d.byte = value;
        return access(I2C_SMBUS_WRITE, cmd, I2C_SMBUS_BYTE_DATA, &d);
    }
    int32_t read_byte(uint8_t cmd) const {
        i2c_smbus_data d{};
        return access(I2C_SMBUS_READ, cmd, I2C_SMBUS_BYTE_DATA, &d) ? -1 : (d.byte & 0xFF);
    }
    int32_t read_word(uint8_t cmd) const {
        i2c_smbus_data d{};
        return access(I2C_SMBUS_READ, cmd, I2C_SMBUS_WORD_DATA, &d) ? -1 : (d.word & 0xFFFF);
    }

    int fd_ = -1;
    int bus_ = -1;
};

} // namespace vrm
