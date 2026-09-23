"use strict";

window.FitnessSpeech = (() => {
  let active = null;
  const stopTracks = (session) => session.stream?.getTracks().forEach((track) => track.stop());

  // Browser-native decoding/resampling keeps uploads to mono, 16 kHz PCM WAV.
  async function wavBlob(blob) {
    const Audio = window.AudioContext || window.webkitAudioContext;
    const audio = new Audio();
    let decoded;
    try { decoded = await audio.decodeAudioData(await blob.arrayBuffer()); }
    finally { await audio.close(); }
    if (!decoded.length || decoded.duration > 30.5) throw new Error("录音过长，请分段录制");
    const frames = Math.min(Math.ceil(decoded.duration * 16000), 30 * 16000);
    const offline = new OfflineAudioContext(1, frames, 16000);
    const source = offline.createBufferSource();
    source.buffer = decoded;
    source.connect(offline.destination);
    source.start();
    const samples = (await offline.startRendering()).getChannelData(0);
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const text = (offset, value) => [...value].forEach((char, i) => view.setUint8(offset + i, char.charCodeAt(0)));
    text(0, "RIFF"); view.setUint32(4, buffer.byteLength - 8, true); text(8, "WAVE");
    text(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, 1, true); view.setUint32(24, 16000, true); view.setUint32(28, 32000, true);
    view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, "data");
    view.setUint32(40, samples.length * 2, true);
    samples.forEach((sample, i) => {
      const value = Math.max(-1, Math.min(1, sample));
      view.setInt16(44 + i * 2, value * (value < 0 ? 32768 : 32767), true);
    });
    return new Blob([buffer], {type: "audio/wav"});
  }

  function unlock(session) {
    session.locked?.forEach(([element, disabled]) => { if (element.isConnected) element.disabled = disabled; });
    session.locked = null;
    session.busy = false;
    session.onBusy(false);
  }

  function cancel() {
    const session = active;
    if (!session?.busy) return;
    session.ticket++;
    clearTimeout(session.timer);
    clearInterval(session.ticker);
    session.abort?.abort();
    if (session.recorder?.state === "recording") session.recorder.stop();
    stopTracks(session);
    session.recorder = session.stream = null;
    unlock(session);
    session.consent.disabled = !session.enabled;
    session.status.textContent = "录音已取消";
    session.start.disabled = !session.consent.checked || !session.enabled;
    session.stop.hidden = session.discard.hidden = true;
  }

  function dispose() {
    cancel();
    active = null;
  }

  function mount(root, target, enabled, onBusy) {
    dispose();
    root.innerHTML = `<div class="speech-controls">
      <label class="speech-consent"><input type="checkbox">同意将本次录音发送至阿里云转写</label>
      <div class="speech-toolbar"><button type="button" class="icon-button" data-voice="start" aria-label="开始录音" title="开始录音" disabled>${icon("mic")}</button>
      <button type="button" data-voice="stop" hidden>${icon("square")}停止并转写</button>
      <button type="button" class="icon-button" data-voice="cancel" hidden aria-label="取消录音" title="取消录音">${icon("x")}</button>
      <span class="small muted" role="status"></span></div>
      <div class="speech-result" hidden><label>语音转写<textarea maxlength="2000"></textarea></label><button type="button" data-voice="use">${icon("check")}填入描述</button></div>
    </div>`;
    const session = {root, target, enabled, onBusy, ticket: 0, busy: false,
      start: root.querySelector('[data-voice="start"]'), stop: root.querySelector('[data-voice="stop"]'),
      discard: root.querySelector('[data-voice="cancel"]'), consent: root.querySelector('input'),
      status: root.querySelector('[role="status"]')};
    active = session;
    session.status.textContent = enabled ? "最长30秒" : "语音转写暂不可用，可直接输入文字";
    session.consent.disabled = !enabled;
    session.consent.onchange = () => { session.start.disabled = !session.consent.checked || !enabled; };
    session.discard.onclick = cancel;
    session.stop.onclick = () => session.recorder?.state === "recording" && session.recorder.stop();
    const insertText = (text) => {
      const result = text.trim();
      const combined = [target.value.trim(), result].filter(Boolean).join("\n");
      if (!result) { session.status.textContent = "没有识别到文字，请重新录音"; return false; }
      if (combined.length > target.maxLength) {
        session.status.textContent = "合并文字过长，请先精简描述或转写内容";
        return false;
      }
      target.value = combined;
      target.dispatchEvent(new Event("input", {bubbles: true}));
      root.querySelector('.speech-result').hidden = true;
      root.querySelector('.speech-result textarea').value = "";
      target.focus();
      return true;
    };
    root.querySelector('[data-voice="use"]').onclick = () => {
      if (session.busy || active !== session) return;
      insertText(root.querySelector('.speech-result textarea').value);
    };
    session.start.onclick = async () => {
      if (session.busy || !session.consent.checked || !enabled) return;
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
        session.status.textContent = "当前浏览器无法录音，请使用支持录音的 HTTPS 页面";
        return;
      }
      const ticket = ++session.ticket;
      session.busy = true;
      session.locked = [...target.closest('form').querySelectorAll('input,textarea,select,button')]
        .filter((element) => !root.contains(element)).map((element) => [element, element.disabled]);
      session.locked.forEach(([element]) => { element.disabled = true; });
      onBusy(true);
      session.start.disabled = session.consent.disabled = true;
      session.discard.hidden = false;
      session.status.textContent = "等待麦克风授权…";
      const current = () => active === session && session.ticket === ticket && root.isConnected;
      try {
        const stream = await navigator.mediaDevices.getUserMedia({audio: true});
        if (!current()) { stream.getTracks().forEach((track) => track.stop()); return; }
        session.stream = stream;
        const mimeType = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm", "audio/ogg;codecs=opus"]
          .find((type) => MediaRecorder.isTypeSupported(type));
        if (!mimeType) throw new Error("浏览器不支持当前录音格式，请更换浏览器");
        const recorder = new MediaRecorder(stream, {mimeType});
        session.recorder = recorder;
        const chunks = [];
        const finished = new Promise((resolve, reject) => {
          recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
          recorder.onerror = () => reject(new Error("录音失败，请重新录制"));
          recorder.onstop = resolve;
        });
        recorder.start();
        session.stop.hidden = false;
        let seconds = 0;
        session.status.textContent = "录音中 0 / 30 秒";
        session.ticker = setInterval(() => { session.status.textContent = `录音中 ${++seconds} / 30 秒`; }, 1000);
        session.timer = setTimeout(() => { if (recorder.state === "recording") recorder.stop(); }, 29000);
        await finished;
        clearTimeout(session.timer); clearInterval(session.ticker); stopTracks(session);
        if (!current()) return;
        session.stop.hidden = true;
        session.status.textContent = "正在转写…";
        const blob = await wavBlob(new Blob(chunks, {type: mimeType}));
        if (!current()) return;
        session.abort = new AbortController();
        const timeout = setTimeout(() => session.abort.abort(), 35000);
        let result;
        try {
          result = await api("/speech/transcribe", {method: "POST", headers: {"Content-Type": "audio/wav"}, body: blob, signal: session.abort.signal});
        } finally { clearTimeout(timeout); }
        if (!current()) return;
        root.querySelector('.speech-result textarea').value = result.text;
        if (insertText(result.text)) {
          session.status.textContent = "转写已填入描述，待核对";
        } else {
          root.querySelector('.speech-result').hidden = !result.text?.trim();
        }
      } catch (error) {
        if (current()) session.status.textContent = error.name === "NotAllowedError" ? "麦克风权限被拒绝，可在浏览器设置中开启" : error.name === "AbortError" ? "转写已超时或取消，未自动重试" : error.message;
      } finally {
        if (current()) {
          clearTimeout(session.timer); clearInterval(session.ticker); stopTracks(session);
          if (session.recorder) session.recorder.ondataavailable = session.recorder.onstop = session.recorder.onerror = null;
          session.recorder = session.stream = null;
          unlock(session);
          session.consent.disabled = false;
          session.start.disabled = !session.consent.checked;
          session.stop.hidden = session.discard.hidden = true;
        }
      }
    };
  }
  document.addEventListener("visibilitychange", () => { if (document.hidden) cancel(); });
  window.addEventListener("pagehide", dispose);
  return {mount, cancel, dispose};
})();
