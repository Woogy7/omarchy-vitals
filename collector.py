#!/usr/bin/env python3
"""System snapshot collector for the Omarchy Vitals panel.

Streams one JSON object per line on stdout every --interval seconds while the
panel is open. Reads /proc and /sys directly; no third-party modules, no root.
Anything that cannot be read on this machine is simply omitted (null / []).
"""

import json
import os
import pwd
import socket
import sys
import time
import glob
import subprocess
import shutil

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
NCPU = os.cpu_count() or 1

PSEUDO_FS = {
    "tmpfs", "devtmpfs", "sysfs", "proc", "procfs", "cgroup", "cgroup2", "devpts",
    "securityfs", "pstore", "efivarfs", "bpf", "debugfs", "tracefs", "configfs",
    "fusectl", "hugetlbfs", "mqueue", "autofs", "binfmt_misc", "ramfs", "squashfs",
    "overlay", "nsfs", "rpc_pipefs", "selinuxfs", "fuse.portal", "fuse.gvfsd-fuse",
    "fuse.appimagelauncherfs",
}


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def read_int(path):
    v = read(path)
    if v is None:
        return None
    try:
        return int(v.strip())
    except ValueError:
        return None


def read_str(path):
    v = read(path)
    return v.strip() if v is not None else None


# ---------------------------------------------------------------- CPU

def cpu_times():
    out = {}
    for line in read("/proc/stat", "").splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        name = parts[0]
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        out[name] = (idle, total)
    return out


def cpu_percentages(prev, cur):
    res = {}
    for name, (idle, total) in cur.items():
        if name not in prev:
            res[name] = 0.0
            continue
        pidle, ptotal = prev[name]
        dt = total - ptotal
        di = idle - pidle
        res[name] = 0.0 if dt <= 0 else max(0.0, min(100.0, (dt - di) * 100.0 / dt))
    return res


def cpu_model():
    for line in read("/proc/cpuinfo", "").splitlines():
        if line.startswith("model name"):
            name = line.split(":", 1)[1].strip()
            for junk in ("(R)", "(TM)", "(tm)", "CPU", "Processor"):
                name = name.replace(junk, "")
            name = " ".join(name.split())
            # Drop trailing "@ 3.20GHz" style suffixes.
            if "@" in name:
                name = name.split("@")[0].strip()
            return name
    return read_str("/sys/firmware/devicetree/base/model") or "CPU"


def cpu_freqs():
    freqs = []
    for i in range(NCPU):
        v = read_int(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq")
        freqs.append(v // 1000 if v else None)
    if all(f is None for f in freqs):
        freqs = []
        for line in read("/proc/cpuinfo", "").splitlines():
            if line.startswith("cpu MHz"):
                try:
                    freqs.append(int(float(line.split(":", 1)[1])))
                except ValueError:
                    pass
    return freqs


class Hwmon:
    """Cached hwmon discovery: temps (with labels), fans, power."""

    def __init__(self):
        self.devices = []
        for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            name = read_str(d + "/name") or os.path.basename(d)
            self.devices.append((name, d))

    def find(self, names):
        for name, d in self.devices:
            if name in names:
                return d
        return None

    def temps(self, d):
        out = []
        if not d:
            return out
        for f in sorted(glob.glob(d + "/temp*_input"), key=lambda p: int(os.path.basename(p)[4:-6])):
            base = f[:-6]
            label = read_str(base + "_label") or os.path.basename(base)
            v = read_int(f)
            if v is not None:
                out.append((label, v / 1000.0))
        return out

    def fans(self, d):
        out = []
        if not d:
            return out
        for f in sorted(glob.glob(d + "/fan*_input")):
            base = f[:-6]
            label = read_str(base + "_label") or os.path.basename(base)
            v = read_int(f)
            if v is not None:
                out.append({"label": label, "rpm": v})
        return out

    def power_w(self, d):
        if not d:
            return None
        for f in sorted(glob.glob(d + "/power*_input")):
            v = read_int(f)
            if v is not None:
                return v / 1_000_000.0
        return None


class Rapl:
    def __init__(self):
        self.path = None
        self.prev = None
        for cand in ("/sys/class/powercap/intel-rapl:0/energy_uj",
                     "/sys/class/powercap/intel-rapl-mmio:0/energy_uj"):
            if read_int(cand) is not None:
                self.path = cand
                break

    def watts(self, now):
        if not self.path:
            return None
        v = read_int(self.path)
        if v is None:
            return None
        w = None
        if self.prev:
            pv, pt = self.prev
            dt = now - pt
            if dt > 0 and v >= pv:
                w = (v - pv) / 1e6 / dt
        self.prev = (v, now)
        return w


# ---------------------------------------------------------------- memory

def meminfo():
    m = {}
    for line in read("/proc/meminfo", "").splitlines():
        k, _, rest = line.partition(":")
        try:
            m[k] = int(rest.split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    total = m.get("MemTotal", 0)
    avail = m.get("MemAvailable", 0)
    free = m.get("MemFree", 0)
    cached = m.get("Cached", 0) + m.get("SReclaimable", 0)
    buffers = m.get("Buffers", 0)
    return {
        "total": total,
        "used": max(0, total - avail),
        "available": avail,
        "free": free,
        "cached": cached,
        "buffers": buffers,
        "shared": m.get("Shmem", 0),
        "swap_total": m.get("SwapTotal", 0),
        "swap_used": max(0, m.get("SwapTotal", 0) - m.get("SwapFree", 0)),
        "zswap": m.get("Zswapped", 0),
    }


def swap_devices():
    devs = []
    for line in read("/proc/swaps", "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3:
            devs.append({"name": parts[0], "type": parts[1]})
    return devs


# ---------------------------------------------------------------- disks

def diskstats():
    out = {}
    for line in read("/proc/diskstats", "").splitlines():
        p = line.split()
        if len(p) < 14:
            continue
        name = p[2]
        # sectors read = field 6 (idx 5), sectors written = field 10 (idx 9)
        out[name] = (int(p[5]) * 512, int(p[9]) * 512)
    return out


def dev_for_source(source):
    """Map a mount source ("/dev/mapper/cryptroot", "/dev/nvme0n1p5") to a
    /proc/diskstats name ("dm-0", "nvme0n1p5")."""
    if not source.startswith("/dev/"):
        return None
    try:
        rdev = os.stat(source).st_rdev
    except OSError:
        return None
    major, minor = os.major(rdev), os.minor(rdev)
    dev = read_str(f"/sys/dev/block/{major}:{minor}/uevent")
    if dev:
        for line in dev.splitlines():
            if line.startswith("DEVNAME="):
                return line[8:]
    return None


def mounts():
    seen = {}
    order = []
    for line in read("/proc/self/mounts", "").splitlines():
        p = line.split()
        if len(p) < 3:
            continue
        source, target, fstype = p[0], p[1].replace("\\040", " "), p[2]
        if fstype in PSEUDO_FS or fstype.startswith("fuse.") and "sshfs" not in fstype:
            continue
        if not (source.startswith("/dev/") or ":" in source or source.startswith("//") or fstype in ("nfs", "nfs4", "cifs", "smb3")):
            continue
        if target.startswith(("/proc", "/sys", "/dev", "/run/", "/snap/", "/var/lib/docker", "/var/lib/containers")):
            continue
        try:
            st = os.statvfs(target)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total <= 0:
            continue
        free = st.f_bavail * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        # btrfs subvolumes (and bind mounts) share one filesystem: fold them
        # into the first mount so the list reads as devices, not subvolumes.
        key = (source, total, used)
        if key in seen:
            seen[key]["mounts"].append(target)
            continue
        entry = {
            "mount": target,
            "source": source,
            "fstype": fstype,
            "total": total,
            "used": used,
            "free": free,
            "mounts": [],
            "dev": dev_for_source(source),
        }
        seen[key] = entry
        order.append(entry)
    order.sort(key=lambda e: (e["mount"] != "/", e["mount"]))
    return order


# ---------------------------------------------------------------- network

def net_dev():
    out = {}
    for line in read("/proc/net/dev", "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        p = rest.split()
        if len(p) >= 9:
            out[name.strip()] = (int(p[0]), int(p[8]))
    return out


def default_iface():
    for line in read("/proc/net/route", "").splitlines()[1:]:
        p = line.split()
        if len(p) >= 2 and p[1] == "00000000":
            return p[0]
    return None


def iface_ips():
    ips = {}
    if not shutil.which("ip"):
        return ips
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=1).stdout
    except (subprocess.SubprocessError, OSError):
        return ips
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 4 and p[2] == "inet":
            ips.setdefault(p[1], p[3].split("/")[0])
    return ips


def wifi_ssids():
    ssids = {}
    if not shutil.which("iw"):
        return ssids
    for d in glob.glob("/sys/class/net/*/wireless"):
        iface = d.split("/")[-2]
        try:
            out = subprocess.run(["iw", "dev", iface, "link"], capture_output=True, text=True, timeout=1).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("SSID:"):
                ssids[iface] = line[5:].strip()
    return ssids


# ---------------------------------------------------------------- GPU

class Gpus:
    def __init__(self):
        self.cards = []
        self.rc6_prev = {}
        self.nvidia = shutil.which("nvidia-smi")
        self.names = self.pci_names()
        for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
            if "-" in os.path.basename(card):
                continue
            dev = card + "/device"
            vendor = read_str(dev + "/vendor")
            if not vendor:
                continue
            driver = os.path.basename(os.path.realpath(dev + "/driver")) if os.path.exists(dev + "/driver") else ""
            hw = glob.glob(dev + "/hwmon/hwmon*")
            self.cards.append({
                "path": card,
                "dev": dev,
                "vendor": vendor,
                "driver": driver,
                "hwmon": hw[0] if hw else None,
                "pci": os.path.basename(os.path.realpath(dev)),
            })

    def pci_names(self):
        names = {}
        if not shutil.which("lspci"):
            return names
        try:
            out = subprocess.run(["lspci", "-mm"], capture_output=True, text=True, timeout=2).stdout
        except (subprocess.SubprocessError, OSError):
            return names
        for line in out.splitlines():
            parts = []
            cur, inq = "", False
            for ch in line:
                if ch == '"':
                    inq = not inq
                elif ch == " " and not inq:
                    if cur:
                        parts.append(cur)
                    cur = ""
                else:
                    cur += ch
            if cur:
                parts.append(cur)
            if len(parts) >= 4:
                slot = parts[0]
                cls = parts[1]
                if "VGA" in cls or "3D" in cls or "Display" in cls:
                    name = parts[3]
                    for junk in ("Corporation", "Advanced Micro Devices, Inc.", "[AMD/ATI]"):
                        name = name.replace(junk, "")
                    names["0000:" + slot if len(slot) == 7 else slot] = " ".join(name.split())
        return names

    def clean_name(self, card):
        n = self.names.get(card["pci"])
        if n:
            # "Intel Arc Graphics [Arrow Lake-H]" style: prefer the bracket if
            # the outer name is generic.
            return n
        v = card["vendor"]
        return {"0x8086": "Intel GPU", "0x1002": "AMD GPU", "0x10de": "NVIDIA GPU"}.get(v, "GPU")

    def intel(self, card, now):
        gt = None
        for cand in glob.glob(card["path"] + "/gt/gt*"):
            gt = cand
            break
        busy = None
        rc6 = None
        if gt:
            rc6 = read_int(gt + "/rc6_residency_ms")
        if rc6 is None:
            rc6 = read_int(card["path"] + "/power/rc6_residency_ms")
        if rc6 is not None:
            key = card["path"]
            if key in self.rc6_prev:
                prc6, pt = self.rc6_prev[key]
                dt_ms = (now - pt) * 1000.0
                if dt_ms > 0:
                    busy = max(0.0, min(100.0, 100.0 - (rc6 - prc6) * 100.0 / dt_ms))
            self.rc6_prev[key] = (rc6, now)
        act = None
        mx = None
        if gt:
            act = read_int(gt + "/rps_act_freq_mhz")
            if not act:
                act = read_int(gt + "/rps_cur_freq_mhz")
            mx = read_int(gt + "/rps_RP0_freq_mhz") or read_int(gt + "/rps_max_freq_mhz")
        if act is None:
            act = read_int(card["path"] + "/gt_act_freq_mhz")
            mx = read_int(card["path"] + "/gt_RP0_freq_mhz") or read_int(card["path"] + "/gt_max_freq_mhz")
        # xe driver
        if act is None:
            for tile in glob.glob(card["path"] + "/device/tile*/gt*/freq0"):
                act = read_int(tile + "/act_freq")
                mx = read_int(tile + "/rp0_freq")
                break
        return busy, act, mx

    def amd(self, card):
        busy = read_int(card["dev"] + "/gpu_busy_percent")
        used = read_int(card["dev"] + "/mem_info_vram_used")
        total = read_int(card["dev"] + "/mem_info_vram_total")
        act = None
        mx = None
        sclk = read(card["dev"] + "/pp_dpm_sclk")
        if sclk:
            for line in sclk.splitlines():
                p = line.split()
                if len(p) >= 2:
                    try:
                        mhz = int(p[1].lower().replace("mhz", ""))
                    except ValueError:
                        continue
                    mx = mhz if mx is None else max(mx, mhz)
                    if line.strip().endswith("*"):
                        act = mhz
        if act is None and card["hwmon"]:
            f = read_int(card["hwmon"] + "/freq1_input")
            act = f // 1_000_000 if f else None
        return busy, act, mx, used, total

    def nvidia_query(self):
        if not self.nvidia:
            return []
        try:
            out = subprocess.run(
                [self.nvidia, "--query-gpu=name,utilization.gpu,clocks.gr,clocks.max.gr,temperature.gpu,power.draw,memory.used,memory.total",
                 "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2).stdout
        except (subprocess.SubprocessError, OSError):
            return []
        res = []
        for line in out.splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) < 8:
                continue

            def num(s):
                try:
                    return float(s)
                except ValueError:
                    return None
            res.append({
                "name": p[0], "vendor": "nvidia", "busy": num(p[1]), "freq_mhz": num(p[2]),
                "max_freq_mhz": num(p[3]), "temp": num(p[4]), "power_w": num(p[5]),
                "mem_used": (num(p[6]) or 0) * 1024 * 1024, "mem_total": (num(p[7]) or 0) * 1024 * 1024,
            })
        return res

    def snapshot(self, now, hwmon):
        out = []
        for card in self.cards:
            v = card["vendor"]
            g = {"name": self.clean_name(card), "vendor": v, "busy": None, "freq_mhz": None,
                 "max_freq_mhz": None, "temp": None, "power_w": None, "mem_used": None, "mem_total": None}
            if v == "0x8086":
                g["busy"], g["freq_mhz"], g["max_freq_mhz"] = self.intel(card, now)
                g["vendor"] = "intel"
            elif v == "0x1002":
                g["busy"], g["freq_mhz"], g["max_freq_mhz"], g["mem_used"], g["mem_total"] = self.amd(card)
                g["vendor"] = "amd"
            elif v == "0x10de":
                # nvidia via nvidia-smi below (proprietary driver exposes little in sysfs)
                if self.nvidia:
                    continue
                g["vendor"] = "nvidia"
            if card["hwmon"]:
                temps = hwmon.temps(card["hwmon"])
                if temps:
                    g["temp"] = temps[0][1]
                g["power_w"] = hwmon.power_w(card["hwmon"])
            out.append(g)
        out.extend(self.nvidia_query())
        return out


# ---------------------------------------------------------------- processes

class Procs:
    def __init__(self):
        self.prev = {}         # pid -> (ticks, start)
        self.users = {}
        self.cmdlines = {}     # pid -> (start, cmd)

    def user(self, uid):
        u = self.users.get(uid)
        if u is None:
            try:
                u = pwd.getpwuid(uid).pw_name
            except KeyError:
                u = str(uid)
            self.users[uid] = u
        return u

    def cmdline(self, pid, start):
        c = self.cmdlines.get(pid)
        if c and c[0] == start:
            return c[1]
        raw = read(f"/proc/{pid}/cmdline", "")
        cmd = raw.replace("\0", " ").strip()
        self.cmdlines[pid] = (start, cmd)
        return cmd

    def snapshot(self, dt_ticks_total, elapsed):
        procs = []
        cur = {}
        threads_total = 0
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            stat = read(f"/proc/{pid}/stat")
            if not stat:
                continue
            try:
                lp = stat.index("(")
                rp = stat.rindex(")")
                comm = stat[lp + 1:rp]
                fields = stat[rp + 2:].split()
                state = fields[0]
                ppid = int(fields[1])
                utime = int(fields[11])
                stime = int(fields[12])
                nthreads = int(fields[17])
                start = int(fields[19])
                rss = int(fields[21]) * PAGE
                st = os.stat(f"/proc/{pid}")
            except (ValueError, IndexError, OSError):
                continue
            ticks = utime + stime
            cur[pid] = (ticks, start)
            cpu = 0.0
            p = self.prev.get(pid)
            if p and p[1] == start and elapsed > 0:
                cpu = max(0.0, (ticks - p[0]) / CLK_TCK / elapsed * 100.0)
            threads_total += nthreads
            cmd = self.cmdline(pid, start)
            name = comm
            if len(comm) >= 15 and cmd:
                base = os.path.basename(cmd.split(" ", 1)[0])
                if base.startswith(comm) and len(base) <= 40:
                    name = base
            procs.append({
                "pid": pid, "ppid": ppid, "name": name, "user": self.user(st.st_uid),
                "cpu": round(cpu, 1), "mem": rss, "threads": nthreads, "state": state,
                "cmd": cmd[:200],
            })
        # Forget stale pids.
        self.prev = cur
        for pid in list(self.cmdlines):
            if pid not in cur:
                del self.cmdlines[pid]
        return procs, threads_total


# ---------------------------------------------------------------- main loop

def main():
    interval = 2.0
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--interval" and i + 1 < len(args):
            try:
                interval = max(0.5, float(args[i + 1]))
            except ValueError:
                pass

    hwmon = Hwmon()
    rapl = Rapl()
    gpus = Gpus()
    procs = Procs()
    model = cpu_model()
    host = socket.gethostname()
    coretemp_dev = hwmon.find({"coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal", "cpu-thermal"})
    # Prefer the hwmon that labels its fans (asus, thinkpad, dell...) over a
    # bare acpi_fan entry, then the one with the most fans.
    fan_dev = None
    best = (-1, -1)
    for name, d in hwmon.devices:
        fans = glob.glob(d + "/fan*_input")
        if not fans:
            continue
        labelled = 1 if glob.glob(d + "/fan*_label") else 0
        score = (labelled, len(fans))
        if score > best:
            best, fan_dev = score, d
    nvme_dev = hwmon.find({"nvme"})

    prev_cpu = cpu_times()
    prev_disk = diskstats()
    prev_net = net_dev()
    prev_t = time.monotonic()
    # Prime process deltas so the first frame has real per-process CPU.
    procs.snapshot(0, 0)
    time.sleep(0.6)

    slow_every = 5  # frames between the slow lookups (ip/iw/mounts)
    frame = 0
    ips, ssids, mount_list = {}, {}, []

    while True:
        now = time.monotonic()
        elapsed = max(1e-3, now - prev_t)
        cur_cpu = cpu_times()
        pct = cpu_percentages(prev_cpu, cur_cpu)
        cores = [pct.get(f"cpu{i}", 0.0) for i in range(NCPU)]
        dt_total = cur_cpu.get("cpu", (0, 0))[1] - prev_cpu.get("cpu", (0, 0))[1]

        temps = hwmon.temps(coretemp_dev)
        pkg_temp = None
        core_temp_by_id = {}
        for label, v in temps:
            ll = label.lower()
            if pkg_temp is None and ("package" in ll or ll.startswith("tctl") or ll.startswith("tdie") or ll == "temp1"):
                pkg_temp = v
            if ll.startswith("core"):
                try:
                    core_temp_by_id[int(ll[4:].strip())] = v
                except ValueError:
                    pass
        # Map logical cpus onto their physical core's sensor so C3 shows C3's
        # temperature, not the third sensor in sysfs order.
        core_temps = []
        if core_temp_by_id:
            for i in range(NCPU):
                cid = read_int(f"/sys/devices/system/cpu/cpu{i}/topology/core_id")
                core_temps.append(core_temp_by_id.get(cid))
            if all(t is None for t in core_temps):
                core_temps = []
        if pkg_temp is None and temps:
            pkg_temp = max(v for _, v in temps)
        if pkg_temp is None:
            for z in glob.glob("/sys/class/thermal/thermal_zone*"):
                t = read_str(z + "/type") or ""
                if "cpu" in t.lower() or "x86_pkg" in t.lower() or "soc" in t.lower():
                    v = read_int(z + "/temp")
                    if v is not None:
                        pkg_temp = v / 1000.0
                        break

        freqs = cpu_freqs()
        valid = [f for f in freqs if f]
        load = read("/proc/loadavg", "0 0 0 0/0 0").split()
        up = read("/proc/uptime", "0 0").split()[0]

        # ---- disks
        cur_disk = diskstats()
        if frame % slow_every == 0:
            mount_list = mounts()
        disks = []
        for m in mount_list:
            r = w = None
            dev = m["dev"]
            if dev and dev in cur_disk and dev in prev_disk:
                r = (cur_disk[dev][0] - prev_disk[dev][0]) / elapsed
                w = (cur_disk[dev][1] - prev_disk[dev][1]) / elapsed
            disks.append({**{k: m[k] for k in ("mount", "source", "fstype", "total", "used", "free", "mounts")},
                          "read_bps": r, "write_bps": w})

        # ---- network
        cur_net = net_dev()
        if frame % slow_every == 0:
            ips = iface_ips()
            ssids = wifi_ssids()
        ifaces = []
        for name, (rx, tx) in cur_net.items():
            if name == "lo":
                continue
            oper = read_str(f"/sys/class/net/{name}/operstate") or "unknown"
            prx, ptx = prev_net.get(name, (rx, tx))
            ifaces.append({
                "name": name,
                "up": oper == "up" or (oper == "unknown" and rx > 0),
                "ip": ips.get(name),
                "wireless": os.path.isdir(f"/sys/class/net/{name}/wireless"),
                "ssid": ssids.get(name),
                "rx_bps": max(0.0, (rx - prx) / elapsed),
                "tx_bps": max(0.0, (tx - ptx) / elapsed),
                "rx_total": rx,
                "tx_total": tx,
            })
        dflt = default_iface()
        ifaces.sort(key=lambda i: (i["name"] != dflt, not i["up"], i["name"]))

        # ---- processes
        plist, threads_total = procs.snapshot(dt_total, elapsed)
        running = 0
        try:
            running = int(load[3].split("/")[0])
        except (ValueError, IndexError):
            pass

        snap = {
            "t": time.time(),
            "host": host,
            "uptime": float(up),
            "load": [float(x) for x in load[:3]],
            "ncpu": NCPU,
            "procs": {"total": len(plist), "running": running, "threads": threads_total},
            "cpu": {
                "model": model,
                "total": pct.get("cpu", 0.0),
                "cores": cores,
                "freq_mhz": (sum(valid) / len(valid)) if valid else None,
                "core_freq": freqs,
                "temp": pkg_temp,
                "core_temps": core_temps,
                "fans": hwmon.fans(fan_dev),
                "power_w": rapl.watts(now),
            },
            "mem": {**meminfo(), "swap_devices": swap_devices()},
            "disks": disks,
            "nvme_temp": (hwmon.temps(nvme_dev)[0][1] if hwmon.temps(nvme_dev) else None),
            "net": {"default": dflt, "ifaces": ifaces},
            "gpus": gpus.snapshot(now, hwmon),
            "processes": plist,
        }
        try:
            sys.stdout.write(json.dumps(snap, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            return

        prev_cpu, prev_disk, prev_net, prev_t = cur_cpu, cur_disk, cur_net, now
        frame += 1
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
