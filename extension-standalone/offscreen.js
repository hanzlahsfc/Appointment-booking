/* Alarm audio — service workers cannot play sound, so the alarm lives here. */
(() => {
  let ctx, osc, gain, timer;
  async function start(seconds) {
    stop();
    ctx = ctx || new AudioContext();
    osc = ctx.createOscillator();
    gain = ctx.createGain();
    osc.frequency.value = 1000;
    osc.connect(gain).connect(ctx.destination);
    gain.gain.value = 0;
    osc.start();
    let on = false;
    timer = setInterval(() => { gain.gain.value = (on = !on) ? 0.3 : 0; }, 400);
    setTimeout(stop, Math.max(5, seconds || 120) * 1000);
  }
  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
    try { osc && osc.stop(); } catch (_) {}
    osc = null;
  }
  chrome.runtime.onMessage.addListener((m) => {
    if (m.type === "alarm") start(m.seconds);
    if (m.type === "alarmStop") stop();
  });
})();
