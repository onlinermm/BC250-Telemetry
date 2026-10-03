#pragma once
// Small POSIX helpers for sysfs/hwmon. Deliberately no std::filesystem: the
// daemon is built with -static on many distros/toolchains, and older GCCs
// need an extra -lstdc++fs for it.
#include <algorithm>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <map>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

namespace sysfs {

// Root of the sysfs tree. Only the test suite points this elsewhere (at a
// synthetic fixture); production always uses /sys.
inline std::string &root() {
    static std::string r = "/sys";
    return r;
}

inline std::string trim(const std::string &s) {
    const auto first = s.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    const auto last = s.find_last_not_of(" \t\r\n");
    return s.substr(first, last - first + 1);
}

// Reads a whole (small) sysfs attribute. One open/read/close per call: a
// hwmon *_input read is what triggers the driver to fetch a fresh value, so
// the file must not be kept open and re-read from a cached offset.
// On failure returns false and leaves errno set (EIO/EBUSY/ETIMEDOUT come
// straight from the kernel driver).
inline bool read_text(const std::string &path, std::string &out) {
    const int fd = open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    if (fd < 0) return false;
    char buf[256];
    ssize_t n;
    do {
        n = read(fd, buf, sizeof(buf) - 1);
    } while (n < 0 && errno == EINTR);
    const int saved = errno;
    close(fd);
    if (n < 0) { errno = saved; return false; }
    buf[n] = '\0';
    out = trim(buf);
    return true;
}

inline std::string read_text(const std::string &path) {
    std::string s;
    return read_text(path, s) ? s : std::string();
}

inline bool read_long(const std::string &path, long &out) {
    std::string s;
    if (!read_text(path, s)) return false;
    if (s.empty()) { errno = EINVAL; return false; }
    char *end = nullptr;
    errno = 0;
    const long v = std::strtol(s.c_str(), &end, 10);
    if (errno != 0 || end == s.c_str() || *end != '\0') { errno = EINVAL; return false; }
    out = v;
    return true;
}

inline bool exists(const std::string &path) {
    struct stat st{};
    return stat(path.c_str(), &st) == 0;
}

inline bool is_symlink(const std::string &path) {
    struct stat st{};
    return lstat(path.c_str(), &st) == 0 && S_ISLNK(st.st_mode);
}

inline std::vector<std::string> list_dir(const std::string &dir) {
    std::vector<std::string> names;
    DIR *d = opendir(dir.c_str());
    if (d == nullptr) return names;
    while (const dirent *ent = readdir(d)) {
        if (ent->d_name[0] == '.') continue;
        names.emplace_back(ent->d_name);
    }
    closedir(d);
    std::sort(names.begin(), names.end());
    return names;
}

// Basename of a symlink target, e.g. <dev>/driver -> ".../bc250_vrm".
inline std::string link_basename(const std::string &path) {
    char buf[512];
    const ssize_t n = readlink(path.c_str(), buf, sizeof(buf) - 1);
    if (n <= 0) return {};
    buf[n] = '\0';
    const char *slash = strrchr(buf, '/');
    return slash ? slash + 1 : buf;
}

// First /sys/class/hwmon/hwmonN whose "name" equals target, or "".
inline std::string find_hwmon(const std::string &target) {
    const std::string base = root() + "/class/hwmon";
    for (const auto &entry : list_dir(base)) {
        if (entry.rfind("hwmon", 0) != 0) continue;
        const std::string dir = base + "/" + entry;
        if (read_text(dir + "/name") == target) return dir;
    }
    return {};
}

// Maps every "<type>N_label" in a hwmon directory to its "<type>N_input"
// path, e.g. {"CPU VRM Temp" -> ".../temp1_input"}. Done once when a device
// is bound: labels are static strings, no need to re-read them every cycle.
inline std::map<std::string, std::string> hwmon_labels(const std::string &dir) {
    std::map<std::string, std::string> labels;
    for (const auto &name : list_dir(dir)) {
        const auto pos = name.rfind("_label");
        if (pos == std::string::npos || pos + 6 != name.size()) continue;
        const std::string input = dir + "/" + name.substr(0, pos) + "_input";
        if (!exists(input)) continue;
        const std::string label = read_text(dir + "/" + name);
        if (!label.empty()) labels.emplace(label, input);
    }
    return labels;
}

} // namespace sysfs
