const state = {};
const SMOOTHING = 0.08;
const SMOOTHING_CRISP = 0.2; // for temps/fans — smooth, but without noticeable lag
let latest = null; // last known-valid frame — same as v1: never show a fake 0
let frameReady = false; // true exactly once per new telemetry frame (~700ms) — gates the chart history push

// The daemon publishes a frame roughly every 700 ms, so a window in seconds
// converts to a point count. The buffer always holds the longest window on
// offer; a shorter selection draws its tail, so switching down and back up
// loses nothing.
const POLL_MS = 700;
const WINDOWS = [
    { secs: 30,  label: '30S' },
    { secs: 60,  label: '1M'  },
    { secs: 120, label: '2M'  },
];
const DEFAULT_WINDOW = 30;
const pointsFor = (secs) => Math.round((secs * 1000) / POLL_MS);
const HISTORY_MAX = pointsFor(WINDOWS[WINDOWS.length - 1].secs);
const history = { cpuTemp: [], cpuFreq: [], gpuTemp: [], gpuClock: [] };

let windowSecs = DEFAULT_WINDOW;
try {
    const saved = parseInt(localStorage.getItem('bc250.chartWindow'), 10);
    if (WINDOWS.some((w) => w.secs === saved)) windowSecs = saved;
} catch (e) { /* private window or blocked storage: keep the default */ }

function pushHistory(key, value) {
    const arr = history[key];
    arr.push(value);
    if (arr.length > HISTORY_MAX) arr.shift();
}

// The tail of a series for the selected window. Having fewer points than the
// window asks for is normal right after load — spark() draws what it is given.
const inWindow = (key) => history[key].slice(-pointsFor(windowSecs));

function drawCharts() {
    // minRange: keeps a few tenths of a degree / a couple hundred MHz of noise
    // from filling the whole chart height — the real range has to be sizeable
    // enough to actually earn that space.
    setPath('v2-cpu-chart-area', spark(inWindow('cpuTemp'), 200, 100, true, 6));
    setPath('v2-cpu-chart-temp', spark(inWindow('cpuTemp'), 200, 100, false, 6));
    setPath('v2-cpu-chart-freq', spark(inWindow('cpuFreq'), 200, 100, false, 200));
    setPath('v2-gpu-chart-area', spark(inWindow('gpuTemp'), 200, 100, true, 6));
    setPath('v2-gpu-chart-temp', spark(inWindow('gpuTemp'), 200, 100, false, 6));
    setPath('v2-gpu-chart-freq', spark(inWindow('gpuClock'), 200, 100, false, 200));
}

// Both cards carry the same buttons and drive one shared setting, so the CPU
// and GPU charts always cover the same span and stay comparable.
function setWindow(secs) {
    windowSecs = secs;
    try { localStorage.setItem('bc250.chartWindow', String(secs)); } catch (e) { /* ignore */ }
    document.querySelectorAll('.win-btn').forEach((btn) => {
        const on = Number(btn.dataset.secs) === secs;
        btn.classList.toggle('is-on', on);
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    drawCharts();   // repaint now instead of waiting for the next telemetry frame
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.win-btn').forEach((btn) => {
        btn.addEventListener('click', () => setWindow(Number(btn.dataset.secs)));
    });
    setWindow(windowSecs);
});

// Catmull-Rom -> cubic Bezier: a single pass over the points gives a smooth
// curve with no canvas/rAF involved.
// minRange is a floor on the Y auto-scale range: without it, tiny sensor
// noise (say ±0.3°C) would get stretched across the full chart height and
// read as sharp spikes.
// Tangents are further damped by TENSION, and control points are hard-clamped
// to [0,h] — thanks to the Bezier convex-hull property, the curve then simply
// cannot escape the container bounds, even on sharp zigzags.
const SPARK_TENSION = 0.6;
function spark(values, w, h, close, minRange = 0) {
    if (values.length < 2) return '';
    let min = Math.min(...values), max = Math.max(...values);
    if (max - min < minRange) {
        const mid = (max + min) / 2;
        min = mid - minRange / 2;
        max = mid + minRange / 2;
    }
    if (max - min < 1e-6) { min -= 1; max += 1; }
    const stepX = w / (values.length - 1);
    const clampY = (y) => Math.max(0, Math.min(h, y));
    const p = values.map((v, i) => [
        i * stepX,
        h - ((v - min) / (max - min)) * (h - 14) - 7 // margin absorbs Bezier overshoot
    ]);
    const n = (i) => p[Math.max(0, Math.min(p.length - 1, i))];
    const f = (v) => v.toFixed(2);
    let d = `M ${f(p[0][0])} ${f(clampY(p[0][1]))}`;
    for (let i = 0; i < p.length - 1; i++) {
        const p0 = n(i - 1), p1 = p[i], p2 = p[i + 1], p3 = n(i + 2);
        d += ` C ${f(p1[0] + (p2[0] - p0[0]) / 6 * SPARK_TENSION)} ${f(clampY(p1[1] + (p2[1] - p0[1]) / 6 * SPARK_TENSION))}`
          +  ` ${f(p2[0] - (p3[0] - p1[0]) / 6 * SPARK_TENSION)} ${f(clampY(p2[1] - (p3[1] - p1[1]) / 6 * SPARK_TENSION))}`
          +  ` ${f(p2[0])} ${f(clampY(p2[1]))}`;
    }
    return close ? d + ` L ${w} ${h} L 0 ${h} Z` : d;
}

const TEMP_THRESH = {
    die: [70, 85, 95],    // CPU/GPU die
    vrm: [60, 75, 90],    // VRM mosfets
    nvme: [55, 65, 75],   // NVMe
    board: [45, 55, 65],  // Board/ambient (nct_t14/t15 thermistors)
};

function statusFor(value, [warnAt, seriousAt, criticalAt]) {
    if (value >= criticalAt) return 'status-critical';
    if (value >= seriousAt) return 'status-serious';
    if (value >= warnAt) return 'status-warning';
    return 'status-good';
}

function setText(id, text) {
    const el = document.getElementById(id);
    if (el && el.textContent !== text) el.textContent = text;
}

function setStatusClass(id, value, thresholds) {
    const el = document.getElementById(id);
    if (!el) return;
    const status = statusFor(value, thresholds);
    if (el.dataset.status === status) return;
    el.classList.remove('status-good', 'status-warning', 'status-serious', 'status-critical');
    el.classList.add(status);
    el.dataset.status = status;
}

// stockAt/maxAt are real reference points for this SKU (6 cores / 24 CU is
// the commonly cited factory baseline per bc250-toolkit and
// bc250-cu-live-manager; 8 cores / 40 CU is the architectural ceiling — same
// silicon in every BC-250). A permanent partial unlock above stock (say
// 38/40) isn't broken or incomplete, it's just this die's real ceiling — so
// it gets its own steady color rather than being lumped in with either end.
function setCoreBadgeClass(id, count, stockAt, maxAt) {
    const el = document.getElementById(id);
    if (!el) return;
    const cls = count >= maxAt ? 'cores-maxed' : count > stockAt ? 'cores-partial' : 'cores-stock';
    if (el.dataset.coreClass === cls) return;
    el.classList.remove('cores-maxed', 'cores-partial', 'cores-stock');
    el.classList.add(cls);
    el.dataset.coreClass = cls;
}

// Chip-reported STATUS_TEMPERATURE/STATUS_IOUT fault/warning bits — only
// shown when the PMIC itself has latched the bit, never a software-guessed threshold.
function setFaultBadge(id, warning, fault, kind) {
    const el = document.getElementById(id);
    if (!el) return;
    const badgeState = fault ? 'fault' : (warning ? 'warning' : 'none');
    if (el.dataset.faultState === badgeState) return;
    el.classList.remove('active-warning', 'active-fault');
    if (badgeState === 'fault') {
        el.classList.add('active-fault');
        el.textContent = 'FAULT';
        el.title = `PMIC ${kind} FAULT bit is set`;
    } else if (badgeState === 'warning') {
        el.classList.add('active-warning');
        el.textContent = 'WARN';
        el.title = `PMIC ${kind} WARNING bit is set`;
    } else {
        el.textContent = '';
        el.title = '';
    }
    el.dataset.faultState = badgeState;
}

function setDisplay(id, show) {
    const el = document.getElementById(id);
    if (!el) return;
    const wanted = show ? '' : 'none';
    if (el.style.display !== wanted) el.style.display = wanted;
}

function setPath(id, d) {
    const el = document.getElementById(id);
    if (el && el.getAttribute('d') !== d) el.setAttribute('d', d);
}

function setBar(id, value, max) {
    const el = document.getElementById(id);
    if (!el) return;
    const m = max !== undefined ? max : parseFloat(el.dataset.max);
    let percent = value / m;
    if (percent > 1) percent = 1;
    if (percent < 0) percent = 0;
    const width = (percent * 100).toFixed(1) + '%';
    if (el.style.width !== width) el.style.width = width;
}

function smooth(key, targetValue, factor = SMOOTHING) {
    if (state[key] === undefined) {
        state[key] = targetValue;
    } else {
        const diff = targetValue - state[key];
        state[key] = Math.abs(diff) < 0.05 ? targetValue : state[key] + diff * factor;
    }
    return state[key];
}

// Touch only animation-duration, and only on a meaningful change — rewriting the
// whole `animation` shorthand restarts the keyframes and reads as a visible stutter.
const durations = {};
function setDuration(id, seconds) {
    const el = document.getElementById(id);
    if (!el) return;
    if (!seconds) {
        if (durations[id] !== 0) {
            el.style.animationPlayState = 'paused';
            durations[id] = 0;
        }
        return;
    }
    const prev = durations[id];
    if (prev === 0) el.style.animationPlayState = 'running';
    if (prev && Math.abs(seconds - prev) / prev < 0.08) return;
    el.style.animationDuration = seconds.toFixed(2) + 's';
    durations[id] = seconds;
}

function setGlow(id, watts, max) {
    const el = document.getElementById(id);
    if (!el) return;
    el.setAttribute('opacity', (0.15 + Math.min(1, watts / max) * 0.75).toFixed(3));
}

// Renders the SVG board's live state: heat glow (driven by SMU PPT), VRM
// glow intensity, fan speed, and the "current flow" animation on the traces.
function paintBoard(ppt, cpuPout, gpuPout, fanRpm, totalPower) {
    // Same 150/180W thresholds as the .ppt-item warning/critical classes on the SMU PPT card.
    const heat = Math.min(1, Math.max(0, ppt / 200));
    const heatEl = document.getElementById('bc-heat');
    if (heatEl) {
        heatEl.setAttribute('opacity', (0.18 + heat * 0.78).toFixed(3));
        heatEl.setAttribute('rx', String(96 + heat * 58));
        heatEl.setAttribute('ry', String(76 + heat * 44));
        const gradId = ppt >= 180 ? 'bc-heat-grad-crit' : ppt >= 150 ? 'bc-heat-grad-warn' : 'bc-heat-grad';
        const wantFill = `url(#${gradId})`;
        if (heatEl.getAttribute('fill') !== wantFill) heatEl.setAttribute('fill', wantFill);
    }

    setDuration('bc-fan1-blades', fanRpm > 40 ? Math.max(0.3, 2200 / fanRpm) : 0);

    const speed = Math.max(0.7, 4.2 - totalPower / 45);
    ['bc-power-flow', 'bc-vrm-flow-cpu', 'bc-vrm-flow-gpu'].forEach(id => setDuration(id, speed));

    setGlow('bc-cpu-vrm-glow', cpuPout, 30);
    setGlow('bc-gpu-vrm-glow', gpuPout, 130);
}

function updateUI() {
    if (!latest) return;
    const hw = latest.hardware || {};
    const sw = latest.software || {};
    const cl = latest.cooling || {};
    const cpu = hw.cpu || {};
    const gpu = hw.gpu || {};

    const pick = (v, fallback) => (typeof v === 'number' && v >= 0 ? v : fallback);
    const p = state.prevFrame || {};

    const cpuDie = pick(sw.cpu_temp_c, p.cpuDie ?? 0);
    const gpuDie = pick(sw.gpu_temp_c, p.gpuDie ?? 0);
    const cpuFreq = pick(sw.cpu_freq_mhz, p.cpuFreq ?? 0);
    const gpuClock = pick(sw.gpu_sclk_mhz, p.gpuClock ?? 0);
    const ppt = pick(sw.gpu_ppt_w, p.ppt ?? 0);
    const nvme = pick(sw.nvme_temp_c, p.nvme ?? 0);
    const t14 = pick(sw.nct_t14_c, p.t14 ?? 0);
    const t15 = pick(sw.nct_t15_c, p.t15 ?? 0);
    const fanRpm = pick(cl.fan_rpm, p.fanRpm ?? 0);
    const fanPwm = pick(cl.fan_pwm_pct, p.fanPwm ?? 0);

    const cpuVin = cpu.valid ? cpu.vin : (p.cpuVin ?? 0);
    const cpuVout = cpu.valid ? cpu.vout : (p.cpuVout ?? 0);
    const cpuIout = cpu.valid ? cpu.iout : (p.cpuIout ?? 0);
    const cpuPout = cpu.valid ? cpu.pout : (p.cpuPout ?? 0);
    const cpuT = cpu.valid ? cpu.temp : (p.cpuT ?? 0);
    const cpuTempWarning = cpu.valid ? !!cpu.temp_warning : (p.cpuTempWarning ?? false);
    const cpuTempFault = cpu.valid ? !!cpu.temp_fault : (p.cpuTempFault ?? false);
    const cpuIoutWarning = cpu.valid ? !!cpu.iout_warning : (p.cpuIoutWarning ?? false);
    const cpuIoutFault = cpu.valid ? !!cpu.iout_fault : (p.cpuIoutFault ?? false);
    const gpuVin = gpu.valid ? gpu.vin : (p.gpuVin ?? 0);
    const gpuVout = gpu.valid ? gpu.vout : (p.gpuVout ?? 0);
    const gpuIout = gpu.valid ? gpu.iout : (p.gpuIout ?? 0);
    const gpuPout = gpu.valid ? gpu.pout : (p.gpuPout ?? 0);
    const gpuT = gpu.valid ? gpu.temp : (p.gpuT ?? 0);
    const gpuTempWarning = gpu.valid ? !!gpu.temp_warning : (p.gpuTempWarning ?? false);
    const gpuTempFault = gpu.valid ? !!gpu.temp_fault : (p.gpuTempFault ?? false);
    const gpuIoutWarning = gpu.valid ? !!gpu.iout_warning : (p.gpuIoutWarning ?? false);
    const gpuIoutFault = gpu.valid ? !!gpu.iout_fault : (p.gpuIoutFault ?? false);

    const totalValid = hw.total_power_valid !== false;
    const total = pick(hw.total_power, cpuPout + gpuPout);

    state.prevFrame = {
        cpuDie, gpuDie, cpuFreq, gpuClock, ppt, nvme, t14, t15, fanRpm, fanPwm,
        cpuVin, cpuVout, cpuIout, cpuPout, cpuT, gpuVin, gpuVout, gpuIout, gpuPout, gpuT,
        cpuTempWarning, cpuTempFault, gpuTempWarning, gpuTempFault,
        cpuIoutWarning, cpuIoutFault, gpuIoutWarning, gpuIoutFault,
    };

    // --- History for the mini-charts (once per real telemetry frame, not per rAF tick) ---
    if (frameReady) {
        frameReady = false;
        pushHistory('cpuTemp', cpuDie);
        pushHistory('cpuFreq', cpuFreq);
        pushHistory('gpuTemp', gpuDie);
        pushHistory('gpuClock', gpuClock);
        drawCharts();
    }

    // pptVal is computed once here (not inside paintBoard) — calling smooth() twice
    // per frame with the same target value would double the smoothing step.
    const pptVal = smooth('ppt', ppt);

    // --- SVG board ---
    paintBoard(pptVal, smooth('cpuPout', cpuPout), smooth('gpuPout', gpuPout), smooth('fanRpm', fanRpm, SMOOTHING_CRISP), total);

    // --- BOARD · COOLING ---
    setText('v2-ppt', pptVal.toFixed(1));
    const pptStat = document.getElementById('v2-ppt-stat');
    if (pptStat) {
        pptStat.classList.toggle('ppt-critical', pptVal >= 180);
        pptStat.classList.toggle('ppt-warning', pptVal >= 150 && pptVal < 180);
    }
    setText('v2-fan-rpm', Math.round(smooth('fanRpmTxt', fanRpm, SMOOTHING_CRISP)).toString());
    setText('v2-fan-pwm', Math.round(smooth('fanPwm', fanPwm, SMOOTHING_CRISP)).toString());

    // Second (backplate) fan — the block and its SVG model only appear if the
    // daemon actually reports cooling.vrm_fan_rpm (field isn't guaranteed, degrade quietly).
    const hasBackFan = typeof cl.vrm_fan_rpm === 'number' && cl.vrm_fan_rpm >= 0;
    setDisplay('v2-fan2-hero', hasBackFan);
    const heroEl = document.getElementById('v2-board-hero');
    if (heroEl) heroEl.classList.toggle('has-backfan', hasBackFan);
    const backFanSvg = document.getElementById('bc-backplate-fan');
    if (backFanSvg) backFanSvg.style.display = hasBackFan ? '' : 'none';
    if (hasBackFan) {
        const fan2Rpm = smooth('fan2Rpm', cl.vrm_fan_rpm, SMOOTHING_CRISP);
        const fan2Pwm = typeof cl.vrm_fan_pwm_pct === 'number' ? cl.vrm_fan_pwm_pct : 0;
        setText('v2-fan2-rpm', Math.round(fan2Rpm).toString());
        setText('v2-fan2-pwm', Math.round(smooth('fan2Pwm', fan2Pwm, SMOOTHING_CRISP)).toString());
        setDuration('bc-fan2-blades', fan2Rpm > 40 ? Math.max(0.3, 2200 / fan2Rpm) : 0);
    } else {
        setDuration('bc-fan2-blades', 0);
    }

    // Memory/Fabric — same idea, gated on df_pstate/mem_clock_mhz being present in software.
    const hasMemInfo = typeof sw.df_pstate === 'number' && sw.df_pstate >= 0
        && typeof sw.mem_clock_mhz === 'number' && sw.mem_clock_mhz >= 0;
    setDisplay('v2-memory-panel', hasMemInfo);
    // Drives the min/max-height of the mini-charts in CPU/GPU CORE (style.css) —
    // without the MEMORY panel, the BOARD·COOLING column is shorter, so the charts
    // shrink to match instead of leaving dead space in THERMALS.
    const boardGridEl = document.getElementById('v2-board-grid');
    if (boardGridEl) boardGridEl.classList.toggle('has-memory', hasMemInfo);
    if (hasMemInfo) {
        const pstate = Math.round(sw.df_pstate);
        setText('v2-mem-state', 'P' + pstate);
        setText('v2-mem-clock', Math.round(smooth('memClock', sw.mem_clock_mhz)).toString());
        setBar('v2-mem-bar', Math.max(0, 100 - pstate * 25));
    }

    const board = (t14 + t15) / 2;
    const boardVal = smooth('board', board, SMOOTHING_CRISP);
    setText('v2-board-temp', boardVal.toFixed(1));
    setBar('v2-board-temp-bar', boardVal);
    setStatusClass('v2-board-temp-figure', boardVal, TEMP_THRESH.board);
    setStatusClass('v2-board-temp-bar', boardVal, TEMP_THRESH.board);

    const nvmeVal = smooth('nvme', nvme, SMOOTHING_CRISP);
    setText('v2-nvme-temp', nvmeVal.toFixed(1));
    setBar('v2-nvme-temp-bar', nvmeVal);
    setStatusClass('v2-nvme-temp-figure', nvmeVal, TEMP_THRESH.nvme);
    setStatusClass('v2-nvme-temp-bar', nvmeVal, TEMP_THRESH.nvme);

    const t15Val = smooth('t15', t15, SMOOTHING_CRISP);
    setText('v2-vrmmos-temp', t15Val.toFixed(1));
    setBar('v2-vrmmos-temp-bar', t15Val);
    setStatusClass('v2-vrmmos-temp-figure', t15Val, TEMP_THRESH.vrm);
    setStatusClass('v2-vrmmos-temp-bar', t15Val, TEMP_THRESH.vrm);

    setText('v2-rail-vin', cpuVin.toFixed(2));
    setBar('v2-rail-vin-bar', cpuVin);
    const totalVal = smooth('total', total);
    setText('v2-power-sum', totalVal.toFixed(1));
    setBar('v2-power-sum-bar', totalVal);
    setText('v2-power-sum-lbl', totalValid ? 'VRM Sum (CPU+GPU)' : 'VRM Sum · partial');
    document.getElementById('v2-power-sum-row')?.classList.toggle('uncertain', !totalValid);

    // --- CPU CORE ---
    // Core/CU badges are populated separately by fetchTopology() (see
    // bottom of file) — that data comes from a once-per-page-load request,
    // not this per-frame telemetry update.

    setText('v2-cpu-die', cpuDie.toFixed(1));
    setStatusClass('v2-cpu-die-unit', cpuDie, TEMP_THRESH.die);
    setText('v2-cpu-freq', Math.round(smooth('cpuFreq', cpuFreq)).toString());

    setText('v2-cpu-vin', smooth('cpuVin', cpuVin).toFixed(2));
    setBar('v2-cpu-vin-bar', cpuVin);
    setText('v2-cpu-vout', smooth('cpuVout', cpuVout).toFixed(3));
    setBar('v2-cpu-vout-bar', cpuVout);
    setText('v2-cpu-iout', smooth('cpuIout', cpuIout).toFixed(1));
    setBar('v2-cpu-iout-bar', cpuIout);
    setFaultBadge('v2-cpu-iout-fault-badge', cpuIoutWarning, cpuIoutFault, 'over-current (STATUS_IOUT)');
    setText('v2-cpu-pout', smooth('cpuPoutTxt', cpuPout).toFixed(1));
    setBar('v2-cpu-pout-bar', cpuPout);
    const cpuTVal = smooth('cpuT', cpuT, SMOOTHING_CRISP);
    setText('v2-cpu-vrmtemp', cpuTVal.toFixed(0));
    setBar('v2-cpu-vrmtemp-bar', cpuTVal);
    setStatusClass('v2-cpu-vrmtemp-figure', cpuTVal, TEMP_THRESH.vrm);
    setStatusClass('v2-cpu-vrmtemp-bar', cpuTVal, TEMP_THRESH.vrm);
    setFaultBadge('v2-cpu-temp-fault-badge', cpuTempWarning, cpuTempFault, 'over-temperature (STATUS_TEMPERATURE)');

    // --- GPU CORE ---
    setText('v2-gpu-die', gpuDie.toFixed(1));
    setStatusClass('v2-gpu-die-unit', gpuDie, TEMP_THRESH.die);
    setText('v2-gpu-clock', Math.round(smooth('gpuClock', gpuClock)).toString());

    setText('v2-gpu-vin', smooth('gpuVin', gpuVin).toFixed(2));
    setBar('v2-gpu-vin-bar', gpuVin);
    setText('v2-gpu-vout', smooth('gpuVout', gpuVout).toFixed(3));
    setBar('v2-gpu-vout-bar', gpuVout);
    setText('v2-gpu-iout', smooth('gpuIout', gpuIout).toFixed(1));
    setBar('v2-gpu-iout-bar', gpuIout);
    setFaultBadge('v2-gpu-iout-fault-badge', gpuIoutWarning, gpuIoutFault, 'over-current (STATUS_IOUT)');
    setText('v2-gpu-pout', smooth('gpuPoutTxt', gpuPout).toFixed(1));
    setBar('v2-gpu-pout-bar', gpuPout);
    const gpuTVal = smooth('gpuT', gpuT, SMOOTHING_CRISP);
    setText('v2-gpu-vrmtemp', gpuTVal.toFixed(0));
    setBar('v2-gpu-vrmtemp-bar', gpuTVal);
    setStatusClass('v2-gpu-vrmtemp-figure', gpuTVal, TEMP_THRESH.vrm);
    setStatusClass('v2-gpu-vrmtemp-bar', gpuTVal, TEMP_THRESH.vrm);
    setFaultBadge('v2-gpu-temp-fault-badge', gpuTempWarning, gpuTempFault, 'over-temperature (STATUS_TEMPERATURE)');

}

async function fetchTelemetry() {
    try {
        const response = await fetch('/api/telemetry');
        const data = await response.json();
        if (!data.error) { latest = data; frameReady = true; }
    } catch (error) {
        console.error('Failed to fetch telemetry:', error);
    }
}

setInterval(fetchTelemetry, 700);
fetchTelemetry();

// Core/CU counts, unlike temp/clock/power, only change on a reboot (CPU) or
// a live WGP toggle (GPU) — not worth polling every 700ms, so this is a
// separate, once-per-page-load fetch rather than part of the telemetry loop.
async function fetchTopology() {
    try {
        const response = await fetch('/api/topology');
        const topo = await response.json();

        const hasCoreCount = typeof topo.cpu_physical_cores === 'number' && topo.cpu_physical_cores > 0;
        setDisplay('v2-cpu-cores-badge', hasCoreCount);
        if (hasCoreCount) {
            setText('v2-cpu-cores', String(topo.cpu_physical_cores));
            setCoreBadgeClass('v2-cpu-cores-badge', topo.cpu_physical_cores, 6, 8);
        }

        const hasCuCount = typeof topo.gpu_cu_active === 'number' && topo.gpu_cu_active > 0;
        setDisplay('v2-gpu-cus-badge', hasCuCount);
        if (hasCuCount) {
            setText('v2-gpu-cus', String(topo.gpu_cu_active));
            setCoreBadgeClass('v2-gpu-cus-badge', topo.gpu_cu_active, 24, 40);
        }
    } catch (error) {
        console.error('Failed to fetch topology:', error);
    }
}
fetchTopology();

// Overclock config/service state — same once-per-page-load cadence as
// topology: an OC file edit or a service (de)activation isn't something
// that needs 700ms polling.
function setOcPanel(panelId, dotId, active, configPresent, valueIds) {
    setDisplay(panelId, configPresent);
    if (!configPresent) return;
    const dotEl = document.getElementById(dotId);
    if (dotEl) {
        dotEl.classList.toggle('active', active);
        dotEl.title = active ? 'Active' : 'Inactive';
    }
    const panel = document.getElementById(panelId);
    if (panel) panel.classList.toggle('oc-inactive', !active);
    valueIds.forEach(([id, value]) => setText(id, value == null ? '—' : String(value)));
}

async function fetchOverclock() {
    try {
        const response = await fetch('/api/overclock');
        const oc = await response.json();

        setOcPanel('v2-cpu-oc-panel', 'v2-cpu-oc-dot', oc.cpu?.active, oc.cpu?.config_present, [
            ['v2-cpu-oc-freq-val', oc.cpu?.target_freq_mhz],
            ['v2-cpu-oc-maxtemp', oc.cpu?.max_temp_c],
        ]);
        setOcPanel('v2-gpu-oc-panel', 'v2-gpu-oc-dot', oc.gpu?.active, oc.gpu?.config_present, [
            ['v2-gpu-oc-maxfreq', oc.gpu?.max_freq_mhz],
            ['v2-gpu-oc-minfreq', oc.gpu?.min_freq_mhz],
        ]);
    } catch (error) {
        console.error('Failed to fetch overclock state:', error);
    }
}
fetchOverclock();

function renderLoop() {
    updateUI();
    setTimeout(() => requestAnimationFrame(renderLoop), 50);
}
requestAnimationFrame(renderLoop);
