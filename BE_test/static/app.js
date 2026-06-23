/* global window, document */
(() => {
  const btnConnect = document.getElementById("btnConnect");
  const btnToggle = document.getElementById("btnToggle");
  const statusEl = document.getElementById("status");
  const logEl = document.getElementById("log");

  let ws = null;
  let audioContext = null;
  let micStream = null;
  let sourceNode = null;
  let processorNode = null;
  let recording = false;

  let ttsExpected = 0;
  let ttsChunks = [];
  let ttsBytes = 0;

  function log(...args) {
    const line = args.map(String).join(" ");
    logEl.textContent += (logEl.textContent ? "\n" : "") + line;
    logEl.scrollTop = logEl.scrollHeight;
    console.log(...args);
  }

  function setStatus(text) {
    statusEl.textContent = text;
  }

  function wsUrl() {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${window.location.host}/ws/chat`;
  }

  function downsampleTo16k(float32, inSampleRate) {
    if (inSampleRate === 16000) return float32;
    const ratio = inSampleRate / 16000;
    const newLength = Math.round(float32.length / ratio);
    const result = new Float32Array(newLength);
    let offsetResult = 0;
    let offsetBuffer = 0;
    while (offsetResult < result.length) {
      const nextOffsetBuffer = Math.round((offsetResult + 1) * ratio);
      let accum = 0;
      let count = 0;
      for (
        let i = offsetBuffer;
        i < nextOffsetBuffer && i < float32.length;
        i++
      ) {
        accum += float32[i];
        count++;
      }
      result[offsetResult] = count ? accum / count : 0;
      offsetResult++;
      offsetBuffer = nextOffsetBuffer;
    }
    return result;
  }

  function floatTo16BitPCM(float32) {
    const out = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
      let s = Math.max(-1, Math.min(1, float32[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }

  async function connect() {
    if (
      ws &&
      (ws.readyState === WebSocket.OPEN ||
        ws.readyState === WebSocket.CONNECTING)
    )
      return;

    ws = new WebSocket(wsUrl());
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      setStatus("connected");
      btnToggle.disabled = false;
      log("WS connected");
    };

    ws.onclose = () => {
      setStatus("disconnected");
      btnToggle.disabled = true;
      btnToggle.textContent = "Bắt đầu nói";
      recording = false;
      log("WS disconnected");
    };

    ws.onerror = (e) => log("WS error", e?.message || "");

    ws.onmessage = async (ev) => {
      if (typeof ev.data === "string") {
        let msg = null;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          msg = { event: "text", raw: ev.data };
        }

        const event = (msg.event || "").toLowerCase();
        if (event === "processing") log("Server:", "processing...");
        else if (event === "transcript") log("You:", msg.text || "");
        else if (event === "assistant_text") log("Assistant:", msg.text || "");
        else if (event === "error") log("Error:", msg.message || "");
        else if (event === "tts_start") {
          ttsExpected = Number(msg.size || 0);
          ttsChunks = [];
          ttsBytes = 0;
          log("TTS:", "start", `(${ttsExpected} bytes)`);
        } else if (event === "tts_end") {
          log("TTS:", "end", `(${ttsBytes} bytes received)`);
          tryPlayTts();
        } else {
          log("Event:", event || "(unknown)", JSON.stringify(msg));
        }
        return;
      }

      // binary chunk of TTS audio
      const buf = ev.data;
      if (buf && buf.byteLength) {
        ttsChunks.push(new Uint8Array(buf));
        ttsBytes += buf.byteLength;
      }
    };
  }

  function concatUint8(chunks) {
    const total = chunks.reduce((s, c) => s + c.length, 0);
    const out = new Uint8Array(total);
    let off = 0;
    for (const c of chunks) {
      out.set(c, off);
      off += c.length;
    }
    return out;
  }

  function tryPlayTts() {
    if (!ttsChunks.length) return;
    const wav = concatUint8(ttsChunks);
    const blob = new Blob([wav], { type: "audio/wav" });
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.onended = () => URL.revokeObjectURL(url);
    audio.play().catch((e) => log("Audio play failed:", e?.message || e));
  }

  async function startRecording() {
    if (!ws || ws.readyState !== WebSocket.OPEN)
      throw new Error("WebSocket is not connected");
    if (recording) return;

    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });

    audioContext = new (window.AudioContext || window.webkitAudioContext)();
    sourceNode = audioContext.createMediaStreamSource(micStream);
    processorNode = audioContext.createScriptProcessor(4096, 1, 1);

    processorNode.onaudioprocess = (e) => {
      if (!recording) return;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;

      const input = e.inputBuffer.getChannelData(0);
      const down = downsampleTo16k(input, audioContext.sampleRate);
      const pcm16 = floatTo16BitPCM(down);
      ws.send(pcm16.buffer);
    };

    sourceNode.connect(processorNode);
    processorNode.connect(audioContext.destination); // keep processor alive

    // clear any previous buffer on server
    ws.send(JSON.stringify({ event: "reset" }));

    recording = true;
    btnToggle.textContent = "Dừng và gửi";
    setStatus("recording");
    log("Recording started");
  }

  async function stopRecording() {
    if (!recording) return;
    recording = false;
    btnToggle.textContent = "Bắt đầu nói";
    setStatus("connected");

    try {
      if (ws && ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ event: "stop_recording" }));
    } catch {}

    try {
      if (processorNode) processorNode.disconnect();
      if (sourceNode) sourceNode.disconnect();
    } catch {}

    try {
      if (micStream) micStream.getTracks().forEach((t) => t.stop());
    } catch {}

    try {
      if (audioContext) await audioContext.close();
    } catch {}

    processorNode = null;
    sourceNode = null;
    micStream = null;
    audioContext = null;
    log("Recording stopped; sent to server");
  }

  btnConnect.addEventListener("click", () =>
    connect().catch((e) => log("Connect failed:", e?.message || e)),
  );
  btnToggle.addEventListener("click", () => {
    if (!recording)
      startRecording().catch((e) => log("Start failed:", e?.message || e));
    else stopRecording().catch((e) => log("Stop failed:", e?.message || e));
  });

  setStatus("disconnected");
})();
