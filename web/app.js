const state = {};
const SMOOTHING = 0.08;
const SMOOTHING_CRISP = 0.2; // for temps/fans — smooth but still responsive, no lag
let latestData = null;

// ==========================================
// CPU OPTIMIZATION: only touch the DOM when the text has ACTUALLY changed
// ==========================================
function setText(id, text) {
    const el = document.getElementById(id);
    if (el && el.textContent !== text) {
        el.textContent = text;
    }
}

function setBar(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    const max = parseFloat(el.dataset.max);
    let percent = value / max;
    if (percent > 1) percent = 1;
    if (percent < 0) percent = 0;
    const width = (percent * 100).toFixed(1) + '%';
    if (el.style.width !== width) {
        el.style.width = width;
    }
}

// ==========================================
// TEMPERATURE COLOR SEMANTICS (good → warning → serious → critical)
// Thresholds are approximate — just for UI signaling.
// ==========================================
const TEMP_THRESH = {
    dieTemp: [70, 85, 95],   // CPU/GPU die
    vrmTemp: [60, 75, 90],   // VRM mosfets
    nvmeTemp: [55, 65, 75],  // NVMe
    boardTemp: [45, 55, 65], // Board/ambient (nct_t14/t15 thermistors)
};

function statusFor(value, [warnAt, seriousAt, criticalAt]) {
    if (value >= criticalAt) return 'status-critical';
    if (value >= seriousAt) return 'status-serious';
    if (value >= warnAt) return 'status-warning';
    return 'status-good';
}

function setTempStatus(id, tempC, thresholds) {
    const el = document.getElementById(id);
    if (!el) return;
    const status = statusFor(tempC, thresholds);
    if (el.dataset.status === status) return;
    el.classList.remove('status-good', 'status-warning', 'status-serious', 'status-critical');
    el.classList.add(status);
    el.dataset.status = status;
}

function setGauge(id, value, max) {
    const gauge = document.getElementById(id);
    if (!gauge) return;
    const dashArray = 125.66;
    let percent = value / max;
    if (percent > 1) percent = 1;
    if (percent < 0) percent = 0;

    // Round to 2 decimal places so the browser doesn't re-render for imperceptible changes
    const offset = (dashArray - (percent * dashArray)).toFixed(2);
    if (gauge.style.strokeDashoffset !== offset) {
        gauge.style.strokeDashoffset = offset;
    }
}

// ==========================================
// SMOOTHING MATH
// ==========================================
function smooth(key, targetValue, factor = SMOOTHING) {
    if (state[key] === undefined) {
        state[key] = targetValue;
    } else {
        let diff = targetValue - state[key];
        // Once we're basically at the target (diff < 0.05), snap to it directly —
        // stops the math and lets the CPU idle.
        if (Math.abs(diff) < 0.05) {
            state[key] = targetValue;
        } else {
            state[key] = state[key] + diff * factor;
        }
    }
    return state[key];
}

// ==========================================
// NETWORK FETCH LOOP
// ==========================================
async function fetchTelemetry() {
    try {
        const response = await fetch('/api/telemetry');
        const data = await response.json();
        if (!data.error) {
            latestData = data;
        }
    } catch (error) {
        console.error("Failed to fetch telemetry:", error);
    }
}

// ==========================================
// RENDER LOOP
// ==========================================
function updateUI() {
    if (!latestData) return;
    const data = latestData;

    setText('total-power', smooth('total_power', data.hardware.total_power).toFixed(1));
    const totalPowerStat = document.getElementById('total-power-stat');
    if (totalPowerStat) {
        // total_power_valid == false: the CPU or GPU I2C reading is currently
        // invalid — the sum only reflects the working rail, not a true zero.
        const uncertain = data.hardware.total_power_valid === false;
        totalPowerStat.classList.toggle('uncertain', uncertain);
        totalPowerStat.title = uncertain ? 'CPU or GPU VRM I2C reading is currently invalid — this total is partial, not a true 0' : '';
    }
    if (data.software.gpu_ppt_w >= 0) {
        const pptVal = smooth('sys_ppt', data.software.gpu_ppt_w);
        setText('sys-ppt', pptVal.toFixed(1));
        const pptStat = document.getElementById('sys-ppt-stat');
        if (pptStat) {
            pptStat.classList.toggle('ppt-critical', pptVal >= 180);
            pptStat.classList.toggle('ppt-warning', pptVal >= 150 && pptVal < 180);
        }
    }

    if (data.hardware.cpu.valid) {
        setText('cpu-vin', smooth('cpu_vin', data.hardware.cpu.vin).toFixed(2));
        setText('cpu-vout', smooth('cpu_vout', data.hardware.cpu.vout).toFixed(3));
        setText('cpu-iout', smooth('cpu_iout', data.hardware.cpu.iout).toFixed(1));
        setText('cpu-pout', smooth('cpu_pout', data.hardware.cpu.pout).toFixed(1));
        setText('cpu-temp', smooth('cpu_vrm_temp', data.hardware.cpu.temp, SMOOTHING_CRISP).toFixed(0));

        setBar('cpu-vin-bar', data.hardware.cpu.vin);
        setBar('cpu-vout-bar', data.hardware.cpu.vout);
        setBar('cpu-iout-bar', data.hardware.cpu.iout);
        setBar('cpu-pout-bar', data.hardware.cpu.pout);
        setBar('cpu-temp-bar', data.hardware.cpu.temp);
        setTempStatus('cpu-temp-figure', data.hardware.cpu.temp, TEMP_THRESH.vrmTemp);
        setTempStatus('cpu-temp-bar', data.hardware.cpu.temp, TEMP_THRESH.vrmTemp);
    }

    if (data.hardware.gpu.valid) {
        setText('gpu-vin', smooth('gpu_vin', data.hardware.gpu.vin).toFixed(2));
        setText('gpu-vout', smooth('gpu_vout', data.hardware.gpu.vout).toFixed(3));
        setText('gpu-iout', smooth('gpu_iout', data.hardware.gpu.iout).toFixed(1));
        setText('gpu-pout', smooth('gpu_pout', data.hardware.gpu.pout).toFixed(1));
        setText('gpu-temp', smooth('gpu_vrm_temp', data.hardware.gpu.temp, SMOOTHING_CRISP).toFixed(0));

        setBar('gpu-vin-bar', data.hardware.gpu.vin);
        setBar('gpu-vout-bar', data.hardware.gpu.vout);
        setBar('gpu-iout-bar', data.hardware.gpu.iout);
        setBar('gpu-pout-bar', data.hardware.gpu.pout);
        setBar('gpu-temp-bar', data.hardware.gpu.temp);
        setTempStatus('gpu-temp-figure', data.hardware.gpu.temp, TEMP_THRESH.vrmTemp);
        setTempStatus('gpu-temp-bar', data.hardware.gpu.temp, TEMP_THRESH.vrmTemp);
    }

    if (data.software.cpu_temp_c >= 0) {
        const val = smooth('sys_cpu_temp', data.software.cpu_temp_c, SMOOTHING_CRISP);
        setText('sys-cpu-temp', val.toFixed(1) + '°C');
        setTempStatus('sys-cpu-temp', val, TEMP_THRESH.dieTemp);
    }
    if (data.software.gpu_temp_c >= 0) {
        const val = smooth('sys_gpu_temp', data.software.gpu_temp_c, SMOOTHING_CRISP);
        setText('sys-gpu-temp', val.toFixed(1) + '°C');
        setTempStatus('sys-gpu-temp', val, TEMP_THRESH.dieTemp);
    }
    if (data.software.nvme_temp_c >= 0) {
        const val = smooth('nvme_temp_c', data.software.nvme_temp_c, SMOOTHING_CRISP);
        setText('nvme-temp', val.toFixed(1));
        setBar('nvme-temp-bar', val);
        setTempStatus('nvme-temp-figure', val, TEMP_THRESH.nvmeTemp);
        setTempStatus('nvme-temp-bar', val, TEMP_THRESH.nvmeTemp);
    }
    if (data.software.nct_t14_c >= 0) {
        const val = smooth('nct_t14', data.software.nct_t14_c, SMOOTHING_CRISP);
        setText('therm-14', val.toFixed(1));
        setBar('therm-14-bar', val);
        setTempStatus('therm-14-figure', val, TEMP_THRESH.boardTemp);
        setTempStatus('therm-14-bar', val, TEMP_THRESH.boardTemp);
    }
    if (data.software.nct_t15_c >= 0) {
        const val = smooth('nct_t15', data.software.nct_t15_c, SMOOTHING_CRISP);
        setText('therm-15', val.toFixed(1));
        setBar('therm-15-bar', val);
        setTempStatus('therm-15-figure', val, TEMP_THRESH.boardTemp);
        setTempStatus('therm-15-bar', val, TEMP_THRESH.boardTemp);
    }

    if (data.software.cpu_freq_mhz >= 0) {
        let val = smooth('cpu_freq', data.software.cpu_freq_mhz);
        setText('cpu-freq', val.toFixed(0));
        setGauge('cpu-freq-gauge', val, 4500);
    }

    if (data.software.gpu_sclk_mhz >= 0) {
        let val = smooth('gpu_freq', data.software.gpu_sclk_mhz);
        setText('gpu-clock', val.toFixed(0));
        setGauge('gpu-freq-gauge', val, 2500);
    }

    if (data.cooling && data.cooling.fan_rpm >= 0) {
        let val = smooth('fan_rpm', data.cooling.fan_rpm, SMOOTHING_CRISP);
        setText('fan-rpm', val.toFixed(0));
        let pwmVal = smooth('fan_pwm', data.cooling.fan_pwm_pct, SMOOTHING_CRISP);
        setText('fan-pwm', pwmVal.toFixed(0));
        setBar('fan-pwm-bar', pwmVal);
        setGauge('fan-rpm-gauge', val, 4000);
    }
}

setInterval(fetchTelemetry, 700);
fetchTelemetry();

// Using requestAnimationFrame instead of setInterval to sync with the display
// This lets the browser pause rendering when the tab isn't visible
function renderLoop() {
    updateUI();
    // Schedule the next frame after a 50ms delay
    setTimeout(() => {
        requestAnimationFrame(renderLoop);
    }, 50);
}
requestAnimationFrame(renderLoop);
