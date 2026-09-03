.pragma library

// Formatting and sorting helpers for the Vitals panel. Pure functions only.

function pad2(n) { return (n < 10 ? "0" : "") + n }

// 1234567 -> "1.2 MiB". Binary units, one decimal under 10, none above.
function bytes(n, unitSuffix) {
  n = Number(n)
  if (!isFinite(n) || n < 0) return "—"
  var units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
  var i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  var s = i === 0 ? String(Math.round(n)) : (n < 10 ? n.toFixed(1) : (n < 100 ? n.toFixed(1) : String(Math.round(n))))
  return s + " " + units[i] + (unitSuffix || "")
}

// Compact form for tight table cells: "1.0G", "689M", "12K".
function bytesShort(n) {
  n = Number(n)
  if (!isFinite(n) || n < 0) return "—"
  var units = ["B", "K", "M", "G", "T"]
  var i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  if (i === 0) return String(Math.round(n)) + units[i]
  return (n < 10 ? n.toFixed(1) : String(Math.round(n))) + units[i]
}

function rate(bps) {
  if (bps === null || bps === undefined) return "—"
  return bytes(bps, "/s")
}

function percent(v, decimals) {
  if (v === null || v === undefined || !isFinite(v)) return "—"
  return (decimals ? Number(v).toFixed(decimals) : String(Math.round(v))) + "%"
}

function temp(c) {
  if (c === null || c === undefined || !isFinite(c)) return "—"
  return Math.round(c) + "°C"
}

function mhz(v) {
  if (v === null || v === undefined || !isFinite(v)) return "—"
  if (v <= 0) return "idle"
  return v >= 1000 ? (v / 1000).toFixed(2) + " GHz" : Math.round(v) + " MHz"
}

function rpm(v) {
  if (v === null || v === undefined || !isFinite(v)) return "—"
  if (v <= 0) return "idle"
  return count(Math.round(v)) + " rpm"
}

function watts(w) {
  if (w === null || w === undefined || !isFinite(w)) return "—"
  return (w < 10 ? w.toFixed(1) : String(Math.round(w))) + " W"
}

function uptime(seconds) {
  seconds = Math.max(0, Math.floor(Number(seconds) || 0))
  var d = Math.floor(seconds / 86400)
  var h = Math.floor((seconds % 86400) / 3600)
  var m = Math.floor((seconds % 3600) / 60)
  if (d > 0) return d + "d " + h + "h"
  if (h > 0) return h + "h " + pad2(m) + "m"
  return m + "m"
}

function loadAvg(load) {
  if (!load || load.length < 3) return "—"
  return load.map(function(x) { return Number(x).toFixed(2) }).join("  ")
}

function count(n) {
  n = Number(n) || 0
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",")
}

// A friendly label for a mount point: "/" -> "root", "/home" -> "home".
function mountLabel(mount) {
  if (mount === "/") return "root"
  var parts = String(mount).split("/").filter(function(p) { return p !== "" })
  return parts.length ? parts[parts.length - 1] : mount
}

function fsLabel(disk) {
  var s = disk.fstype || ""
  var src = String(disk.source || "")
  if (src.indexOf("/dev/mapper/") === 0) s += " · encrypted"
  else if (src.indexOf("/dev/") === 0) s += " · " + src.substring(5)
  else if (src) s += " · " + src
  return s
}

// Sort comparator for the process table. Numeric columns default to
// descending (biggest first); text columns ascending.
function sortProcesses(list, key, reverse) {
  var out = list.slice()
  var numeric = key === "cpu" || key === "mem" || key === "pid" || key === "threads"
  out.sort(function(a, b) {
    var av = a[key], bv = b[key]
    var c
    if (numeric) c = (Number(bv) || 0) - (Number(av) || 0)
    else {
      av = String(av || "").toLowerCase(); bv = String(bv || "").toLowerCase()
      c = av < bv ? -1 : (av > bv ? 1 : 0)
    }
    if (c === 0) c = (a.pid || 0) - (b.pid || 0)
    return reverse ? -c : c
  })
  return out
}

function filterProcesses(list, query) {
  query = String(query || "").trim().toLowerCase()
  if (!query) return list
  return list.filter(function(p) {
    return String(p.name || "").toLowerCase().indexOf(query) >= 0
      || String(p.cmd || "").toLowerCase().indexOf(query) >= 0
      || String(p.user || "").toLowerCase().indexOf(query) >= 0
      || String(p.pid) === query
  })
}

// Push onto a fixed-length history buffer (returns a new array so bindings fire).
function pushHistory(history, value, max) {
  var next = (history || []).slice()
  next.push(value === null || value === undefined ? 0 : Number(value))
  while (next.length > max) next.shift()
  return next
}

function maxOf(arr, floor) {
  var m = floor || 0
  for (var i = 0; i < (arr || []).length; i++) if (arr[i] > m) m = arr[i]
  return m
}

function gpuVendorLabel(v) {
  if (v === "intel") return "Intel"
  if (v === "amd") return "AMD"
  if (v === "nvidia") return "NVIDIA"
  return ""
}
