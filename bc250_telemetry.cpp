#include <iostream>
#include <fstream>
#include <iomanip>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/i2c.h>
#include <linux/i2c-dev.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cerrno>
#include <cmath>
#include <csignal>
#include <dirent.h>
#include <string>
#include <sys/stat.h>
#include <cstdarg>

#define PMBUS_ADDR 0x60
#define PMBUS_PAGE       0x00
#define PMBUS_READ_VIN   0x88
#define PMBUS_READ_VOUT  0x8B
#define PMBUS_READ_IOUT  0x8C
#define PMBUS_READ_TEMP1 0x8D
#define PMBUS_STATUS_IOUT 0x7B
#define PMBUS_STATUS_TEMP 0x7D

using namespace std;

// Main loop interval.
static constexpr useconds_t LOOP_INTERVAL_US = 700000; // 700 ms
// NVMe heats up and cools down slowly, so we poll it less often than the other sensors:
// NVME_POLL_CYCLES cycles of LOOP_INTERVAL_US ≈ 9.8 seconds.
static constexpr int NVME_POLL_CYCLES = 14;

// Divisors for converting raw 16-bit PMBus words into physical units.
// The chip's format (linear11 vs. direct) isn't documented anywhere — see
// "Open questions" in CLAUDE.md. The coefficients below were found empirically,
// by comparing readings against a multimeter on real hardware.
static constexpr float VIN_DIVISOR  = 100.0f;   // raw / 100  -> volts (10 mV step)
static constexpr float VOUT_DIVISOR = 1000.0f;  // raw / 1000 -> volts (1 mV step)
static constexpr float IOUT_DIVISOR = 10.0f;    // raw / 10   -> amps (100 mA step)
static constexpr uint16_t TEMP_MASK = 0x07FF;   // low 11 bits of the READ_TEMP1 register are the value;
                                                 // the upper bits aren't used in this PMIC's readings
// Guards against I2C bus glitches: >250 A is obviously bus noise, not a real current reading.
static constexpr int32_t IOUT_GLITCH_THRESHOLD_RAW = 2500;

// External sensor files under /run (tmpfs): vanish on reboot, cheap to rewrite.
// CoolerControl wants a sysfs integer (millidegrees C). MangoHud has no file
// sensor — it cats a human-readable string via custom_text + exec.
// External sensor files under /run (tmpfs): PMBus-only metrics that have no
// hwmon driver. Die temps, clocks, fans, NVMe, NCT — use hwmon / MangoHud built-ins.
static constexpr const char *SENSOR_DIR = "/run/bc250";

// Fault/warning bits within STATUS_IOUT (0x7B) and STATUS_TEMPERATURE (0x7D) —
// latched by the PMIC itself when a rail crosses protection thresholds set by
// the board vendor, independent of any threshold guessed in software.
static constexpr uint8_t STATUS_IOUT_OC_FAULT   = 0x80;
static constexpr uint8_t STATUS_IOUT_OC_WARNING = 0x20;
static constexpr uint8_t STATUS_TEMP_OT_FAULT   = 0x80;
static constexpr uint8_t STATUS_TEMP_OT_WARNING = 0x40;

static volatile sig_atomic_t g_running = 1;

void handle_shutdown_signal(int) {
    g_running = 0;
}

// --- I2C functions ---
int32_t i2c_smbus_access(int file, char read_write, uint8_t command, int size, union i2c_smbus_data *data) {
    struct i2c_smbus_ioctl_data args;
    args.read_write = read_write;
    args.command = command;
    args.size = size;
    args.data = data;
    return ioctl(file, I2C_SMBUS, &args);
}

int32_t i2c_smbus_write_byte_data(int file, uint8_t command, uint8_t value) {
    union i2c_smbus_data data;
    data.byte = value;
    return i2c_smbus_access(file, I2C_SMBUS_WRITE, command, I2C_SMBUS_BYTE_DATA, &data);
}

int32_t i2c_smbus_read_word_data(int file, uint8_t command) {
    union i2c_smbus_data data;
    if (i2c_smbus_access(file, I2C_SMBUS_READ, command, I2C_SMBUS_WORD_DATA, &data)) return -1;
    return 0x0FFFF & data.word;
}

int32_t i2c_smbus_read_byte_data(int file, uint8_t command) {
    union i2c_smbus_data data;
    if (i2c_smbus_access(file, I2C_SMBUS_READ, command, I2C_SMBUS_BYTE_DATA, &data)) return -1;
    return 0xFF & data.byte;
}

struct Telemetry {
    float vin, vout, iout, temp, pout;
    bool valid;
    bool iout_warning, iout_fault;
    bool temp_warning, temp_fault;
};

Telemetry read_telemetry(int fd, uint8_t page) {
    Telemetry t = {0, 0, 0, 0, 0, false, false, false, false, false};
    if (fd < 0) return t; // I2C isn't available right now (not connected yet, or dropped) — don't crash
    i2c_smbus_write_byte_data(fd, PMBUS_PAGE, page);
    usleep(5000);
    int32_t vin_raw = i2c_smbus_read_word_data(fd, PMBUS_READ_VIN);
    int32_t vout_raw = i2c_smbus_read_word_data(fd, PMBUS_READ_VOUT);
    int32_t iout_raw = i2c_smbus_read_word_data(fd, PMBUS_READ_IOUT);
    int32_t temp_raw = i2c_smbus_read_word_data(fd, PMBUS_READ_TEMP1);
    if (vin_raw < 0 || vout_raw < 0 || iout_raw < 0 || temp_raw < 0) return t;
    if ((vin_raw == 0 || vin_raw == 0xFFFF) && (vout_raw == 0 || vout_raw == 0xFFFF)) return t;
    if (iout_raw > IOUT_GLITCH_THRESHOLD_RAW) return t;

    t.valid = true;
    t.vin = vin_raw / VIN_DIVISOR;
    t.vout = vout_raw / VOUT_DIVISOR;
    t.iout = iout_raw / IOUT_DIVISOR;
    t.temp = (float)(temp_raw & TEMP_MASK);
    t.pout = t.vout * t.iout;

    // Hardware fault/warning bits, latched by the PMIC's own protection
    // comparators — a missing read (-1) just leaves the flags false.
    int32_t status_iout_raw = i2c_smbus_read_byte_data(fd, PMBUS_STATUS_IOUT);
    int32_t status_temp_raw = i2c_smbus_read_byte_data(fd, PMBUS_STATUS_TEMP);
    t.iout_warning = (status_iout_raw >= 0) && (status_iout_raw & STATUS_IOUT_OC_WARNING);
    t.iout_fault   = (status_iout_raw >= 0) && (status_iout_raw & STATUS_IOUT_OC_FAULT);
    t.temp_warning = (status_temp_raw >= 0) && (status_temp_raw & STATUS_TEMP_OT_WARNING);
    t.temp_fault   = (status_temp_raw >= 0) && (status_temp_raw & STATUS_TEMP_OT_FAULT);

    return t;
}

// Tries to open the given I2C device and check that the BC-250 PMIC actually
// answers on it (PMBUS_ADDR is a hardware address and is stable across
// distros — unlike the /dev/i2c-N bus number).
bool try_open_pmbus(const string &path, int &out_fd) {
    int fd = open(path.c_str(), O_RDWR);
    if (fd < 0) return false;

    if (ioctl(fd, I2C_SLAVE, PMBUS_ADDR) < 0) {
        close(fd);
        return false;
    }

    i2c_smbus_write_byte_data(fd, PMBUS_PAGE, 0);
    usleep(5000);
    int32_t probe = i2c_smbus_read_word_data(fd, PMBUS_READ_VOUT);
    if (probe <= 0 || probe == 0xFFFF) {
        close(fd);
        return false;
    }

    out_fd = fd;
    return true;
}

// Auto-detects the I2C bus the PMIC (0x60) is on. The bus number differs
// across Bazzite/SteamOS/CachyOS depending on kernel version and module load
// order, so it can't be hardcoded. bus_override (--bus / BC250_I2C_BUS) lets
// you skip the scan and use a specific bus instead.
int open_pmbus_device(int bus_override) {
    int fd = -1;

    if (bus_override >= 0) {
        string path = "/dev/i2c-" + to_string(bus_override);
        if (try_open_pmbus(path, fd)) {
            fprintf(stderr, "[INFO] PMBus found on %s (manually specified)\n", path.c_str());
            return fd;
        }
        fprintf(stderr, "[ERROR] On the specified bus %s, the PMBus device (address 0x%02X) isn't responding: %s\n",
                path.c_str(), PMBUS_ADDR, strerror(errno));
        return -1;
    }

    DIR *dir = opendir("/dev");
    if (dir == nullptr) {
        fprintf(stderr, "[ERROR] Failed to open /dev: %s\n", strerror(errno));
        return -1;
    }

    struct dirent *ent;
    while ((ent = readdir(dir)) != nullptr) {
        string name = ent->d_name;
        if (name.rfind("i2c-", 0) != 0) continue;

        string path = "/dev/" + name;
        if (try_open_pmbus(path, fd)) {
            fprintf(stderr, "[INFO] PMBus auto-detected on %s\n", path.c_str());
            closedir(dir);
            return fd;
        }
    }
    closedir(dir);

    fprintf(stderr,
            "[ERROR] No I2C bus responded on address 0x%02X (BC-250 PMIC). "
            "Specify the bus manually with --bus N or the BC250_I2C_BUS env var.\n",
            PMBUS_ADDR);
    return -1;
}

// Parses --bus N / --bus=N from the command-line args. Returns -1 if the
// flag wasn't given (main() then falls back to checking BC250_I2C_BUS).
int parse_bus_arg(int argc, char **argv) {
    for (int i = 1; i < argc; ++i) {
        string arg = argv[i];
        if (arg == "--bus" && i + 1 < argc) {
            return atoi(argv[++i]);
        }
        if (arg.rfind("--bus=", 0) == 0) {
            return atoi(arg.substr(6).c_str());
        }
    }
    return -1;
}

// --- Sysfs functions ---
string get_hwmon_dir(const string& target_name) {
    DIR *dir;
    struct dirent *ent;
    if ((dir = opendir("/sys/class/hwmon/")) != NULL) {
        while ((ent = readdir(dir)) != NULL) {
            string dirname = ent->d_name;
            if (dirname.find("hwmon") != string::npos) {
                string name_path = "/sys/class/hwmon/" + dirname + "/name";
                ifstream name_file(name_path);
                if (name_file.is_open()) {
                    string name;
                    name_file >> name;
                    if (name == target_name) {
                        closedir(dir);
                        return "/sys/class/hwmon/" + dirname;
                    }
                }
            }
        }
        closedir(dir);
    }
    return "";
}

// Atomic replace of a small /run file. Skipped entirely when the reading isn't
// valid so CoolerControl / MangoHud keep the last good value instead of 0 °C.
void write_run_file(const char *path, const string &contents) {
    string tmp = string(path) + ".tmp";
    ofstream file(tmp);
    if (!file.is_open()) return;
    file << contents;
    file.close();
    if (rename(tmp.c_str(), path) != 0) {
        unlink(tmp.c_str());
    }
}

void write_sensor(const char *name, bool valid, const char *fmt, ...) {
    if (!valid) return;

    char path[192];
    char buf[64];
    snprintf(path, sizeof(path), "%s/%s", SENSOR_DIR, name);

    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    write_run_file(path, buf);
}

void write_temp_pair(const char *cc_name, const char *mh_name, float temp_c, bool valid) {
    if (!valid || temp_c < 0.0f || temp_c >= 250.0f) return;

    const long rounded = lroundf(temp_c);
    write_sensor(cc_name, true, "%ld\n", rounded * 1000);
    write_sensor(mh_name, true, "%ld\xC2\xB0" "C\n", rounded);
}

long read_sysfs_long(const string& path) {
    ifstream file(path);
    if (file.is_open()) {
        long val;
        if (file >> val) return val;
    }
    return -1;
}

// Average frequency across all CPU cores, read from /proc/cpuinfo.
long read_cpu_freq() {
    ifstream file("/proc/cpuinfo");
    if (!file.is_open()) return -1;

    string line;
    double sum = 0.0;
    long count = 0;
    while (getline(file, line)) {
        if (line.rfind("cpu MHz", 0) == 0) {
            size_t pos = line.find(':');
            if (pos != string::npos) {
                try {
                    sum += std::stod(line.substr(pos + 1));
                    ++count;
                } catch (...) {
                    // malformed cpuinfo line — skip it
                }
            }
        }
    }
    if (count == 0) return -1;
    return lround(sum / count);
}

int main(int argc, char **argv) {
    signal(SIGTERM, handle_shutdown_signal);
    signal(SIGINT, handle_shutdown_signal);

    int bus_override = parse_bus_arg(argc, argv);
    if (bus_override < 0) {
        const char *env_bus = getenv("BC250_I2C_BUS");
        if (env_bus != nullptr) {
            bus_override = atoi(env_bus);
        }
    }

    // A missing PMIC at startup isn't fatal: hwmon telemetry (frequencies,
    // die temperatures, fans) doesn't depend on I2C and keeps working without
    // it. The daemon carries on with fd=-1 and retries the connection every
    // NVME_POLL_CYCLES cycles in the main loop below.
    int fd = open_pmbus_device(bus_override);

    string amdgpu_dir = get_hwmon_dir("amdgpu");
    string k10temp_dir = get_hwmon_dir("k10temp");
    string nct_dir = get_hwmon_dir("nct6686");
    string nvme_dir = get_hwmon_dir("nvme");

    cout << "[INFO] APU Telemetry Daemon started." << endl;
    if (!amdgpu_dir.empty()) cout << "[INFO] Found AMDGPU: " << amdgpu_dir << endl;
    if (!k10temp_dir.empty()) cout << "[INFO] Found k10temp: " << k10temp_dir << endl;
    if (!nvme_dir.empty()) cout << "[INFO] Found NVMe: " << nvme_dir << endl;
    if (mkdir(SENSOR_DIR, 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "[ERROR] Failed to create %s: %s\n", SENSOR_DIR, strerror(errno));
    }
    cout << "[INFO] Writing data to /run/apu_telemetry.json" << endl;
    cout << "[INFO] Writing PMBus-only sensors to " << SENSOR_DIR << endl;

    int loop_counter = 0;
    long nvme_temp_raw = -1;

    while (g_running) {
        // Roughly every NVME_POLL_CYCLES cycles, try to reconnect whatever's
        // still missing: the I2C bus and any hwmon directory we haven't found
        // yet. This covers a driver or controller that shows up after the
        // daemon has already started (slow init at boot, a DKMS module
        // loading late, etc.) — without it, that sensor would stay dead until
        // the service was restarted by hand.
        if (loop_counter % NVME_POLL_CYCLES == 0) {
            if (fd < 0) fd = open_pmbus_device(bus_override);
            if (amdgpu_dir.empty()) amdgpu_dir = get_hwmon_dir("amdgpu");
            if (k10temp_dir.empty()) k10temp_dir = get_hwmon_dir("k10temp");
            if (nct_dir.empty()) nct_dir = get_hwmon_dir("nct6686");
            if (nvme_dir.empty()) nvme_dir = get_hwmon_dir("nvme");
        }

        Telemetry cpu = read_telemetry(fd, 0);
        Telemetry gpu = read_telemetry(fd, 1);
        bool total_power_valid = cpu.valid && gpu.valid;
        float total_power = (cpu.valid ? cpu.pout : 0.0f) + (gpu.valid ? gpu.pout : 0.0f);

        long k10_temp = -1, amd_sclk = -1, amd_ppt = -1, amd_edge = -1;
        long nct_fan = -1, nct_pwm = -1;
        long nct_t14 = -1, nct_t15 = -1;

        if (!k10temp_dir.empty()) k10_temp = read_sysfs_long(k10temp_dir + "/temp1_input");
        if (!amdgpu_dir.empty()) {
            amd_sclk = read_sysfs_long(amdgpu_dir + "/freq1_input");
            amd_ppt = read_sysfs_long(amdgpu_dir + "/power1_input");
            amd_edge = read_sysfs_long(amdgpu_dir + "/temp1_input");
        }
        if (!nct_dir.empty()) {
            nct_fan = read_sysfs_long(nct_dir + "/fan2_input");
            nct_pwm = read_sysfs_long(nct_dir + "/pwm2"); // Value range 0-255
            nct_t14 = read_sysfs_long(nct_dir + "/temp2_input");
            nct_t15 = read_sysfs_long(nct_dir + "/temp3_input");
        }

        if (!nvme_dir.empty()) {
            if (loop_counter % NVME_POLL_CYCLES == 0) {
                nvme_temp_raw = read_sysfs_long(nvme_dir + "/temp1_input");
            }
        }

        float sys_cpu_temp = (k10_temp >= 0) ? (k10_temp / 1000.0f) : -1.0f;
        float sys_gpu_temp = (amd_edge >= 0) ? (amd_edge / 1000.0f) : -1.0f;
        float sys_gpu_sclk = (amd_sclk >= 0) ? (amd_sclk / 1000000.0f) : -1.0f;
        float sys_gpu_ppt  = (amd_ppt >= 0) ? (amd_ppt / 1000000.0f) : -1.0f;
        float sys_nvme_temp = (nvme_temp_raw >= 0) ? (nvme_temp_raw / 1000.0f) : -1.0f;
        float pwm_percent = (nct_pwm >= 0) ? (nct_pwm * 100.0f / 255.0f) : -1.0f;
        float sys_t14 = (nct_t14 >= 0) ? (nct_t14 / 1000.0f) : -1.0f;
        float sys_t15 = (nct_t15 >= 0) ? (nct_t15 / 1000.0f) : -1.0f;
        long cpu_freq = read_cpu_freq();

        ofstream file("/run/apu_telemetry.tmp");
        if (file.is_open()) {
            file << "{\n";
            file << "  \"hardware\": {\n";
            file << "    \"cpu\": {\n";
            file << "      \"valid\": " << (cpu.valid ? "true" : "false") << ",\n";
            file << "      \"vin\": " << fixed << setprecision(2) << cpu.vin << ",\n";
            file << "      \"vout\": " << fixed << setprecision(3) << cpu.vout << ",\n";
            file << "      \"iout\": " << fixed << setprecision(1) << cpu.iout << ",\n";
            file << "      \"pout\": " << fixed << setprecision(1) << cpu.pout << ",\n";
            file << "      \"temp\": " << fixed << setprecision(1) << cpu.temp << ",\n";
            file << "      \"iout_warning\": " << (cpu.iout_warning ? "true" : "false") << ",\n";
            file << "      \"iout_fault\": " << (cpu.iout_fault ? "true" : "false") << ",\n";
            file << "      \"temp_warning\": " << (cpu.temp_warning ? "true" : "false") << ",\n";
            file << "      \"temp_fault\": " << (cpu.temp_fault ? "true" : "false") << "\n";
            file << "    },\n";
            file << "    \"gpu\": {\n";
            file << "      \"valid\": " << (gpu.valid ? "true" : "false") << ",\n";
            file << "      \"vin\": " << fixed << setprecision(2) << gpu.vin << ",\n";
            file << "      \"vout\": " << fixed << setprecision(3) << gpu.vout << ",\n";
            file << "      \"iout\": " << fixed << setprecision(1) << gpu.iout << ",\n";
            file << "      \"pout\": " << fixed << setprecision(1) << gpu.pout << ",\n";
            file << "      \"temp\": " << fixed << setprecision(1) << gpu.temp << ",\n";
            file << "      \"iout_warning\": " << (gpu.iout_warning ? "true" : "false") << ",\n";
            file << "      \"iout_fault\": " << (gpu.iout_fault ? "true" : "false") << ",\n";
            file << "      \"temp_warning\": " << (gpu.temp_warning ? "true" : "false") << ",\n";
            file << "      \"temp_fault\": " << (gpu.temp_fault ? "true" : "false") << "\n";
            file << "    },\n";
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
            file << "  }\n";
            file << "}\n";
            file.close();
            rename("/run/apu_telemetry.tmp", "/run/apu_telemetry.json");
        } else {
            fprintf(stderr, "[ERROR] Failed to open /run/apu_telemetry.tmp for writing: %s\n", strerror(errno));
        }

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

        loop_counter++;
        usleep(LOOP_INTERVAL_US);
    }

    fprintf(stderr, "[INFO] Shutdown signal received, stopping...\n");
    if (fd >= 0) close(fd);
    return 0;
}
