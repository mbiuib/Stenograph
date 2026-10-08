/**
 * Browser live capture: microphone / shared-screen audio → 16 kHz mono int16
 * → WebSocket `/ws/live`.
 *
 * An AudioWorklet resamples the device stream with linear interpolation and
 * posts 100 ms chunks back to the page; each chunk is framed as one track byte
 * (0 = mic, 1 = system) plus the little-endian PCM payload — the wire format
 * the server's `live/web.py` expects.
 */

export type LiveTrack = "mic" | "system";

export interface LiveCaptureOptions {
  tracks: LiveTrack[];
  language?: string | null;
  onLevel?: (track: LiveTrack, rms: number) => void;
  onClosed?: (reason: string) => void;
}

export interface LiveCapture {
  jobId: string;
  tracks: LiveTrack[];
  stop(): Promise<void>;
}

const TRACK_INDEX: Record<LiveTrack, number> = { mic: 0, system: 1 };

const WORKLET_SOURCE = `
class StenographCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.pos = 0;
    this.last = 0;
    this.target = 1600; // 100 ms at 16 kHz
    this.chunk = new Float32Array(this.target);
    this.filled = 0;
    this.sumSq = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) {
      let pos = this.pos;
      while (pos < channel.length) {
        const i0 = Math.floor(pos);
        const frac = pos - i0;
        const a = i0 === 0 ? this.last : channel[i0 - 1];
        const b = channel[i0];
        const value = a + (b - a) * frac;
        this.chunk[this.filled] = value;
        this.sumSq += value * value;
        this.filled += 1;
        if (this.filled === this.target) {
          const out = new Int16Array(this.target);
          for (let i = 0; i < this.target; i += 1) {
            const v = Math.max(-1, Math.min(1, this.chunk[i]));
            out[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
          }
          const rms = Math.sqrt(this.sumSq / this.target);
          this.port.postMessage({ samples: out, rms: rms }, [out.buffer]);
          this.filled = 0;
          this.sumSq = 0;
        }
        pos += this.ratio;
      }
      this.last = channel[channel.length - 1];
      this.pos = pos - channel.length;
    }
    return true;
  }
}

registerProcessor("stenograph-capture", StenographCapture);
`;

interface ReadyMessage {
  type: "ready";
  job_id: string;
  tracks: string[];
}

function describeMediaError(err: unknown): string {
  const name = err instanceof DOMException ? err.name : "";
  switch (name) {
    case "NotAllowedError":
      return "доступ запрещён или запрос отменён";
    case "NotFoundError":
      return "устройство не найдено";
    case "NotReadableError":
      return "устройство занято другим приложением";
    default:
      return err instanceof Error ? err.message : String(err);
  }
}

export async function startLiveCapture(options: LiveCaptureOptions): Promise<LiveCapture> {
  if (!options.tracks.length) throw new Error("Выберите хотя бы один источник");
  if (!window.isSecureContext) {
    throw new Error("браузер разрешает запись только на HTTPS или на localhost");
  }

  const streamByTrack = new Map<LiveTrack, MediaStream>();
  const stopStreams = () => {
    streamByTrack.forEach((stream) => stream.getTracks().forEach((track) => track.stop()));
  };

  try {
    if (options.tracks.includes("mic")) {
      try {
        const mic = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
        });
        streamByTrack.set("mic", mic);
      } catch (err) {
        throw new Error(`не удалось получить доступ к микрофону (${describeMediaError(err)})`);
      }
    }
    if (options.tracks.includes("system")) {
      if (typeof navigator.mediaDevices?.getDisplayMedia !== "function") {
        throw new Error(
          "этот браузер не умеет захват звука системы — на телефоне можно записать только микрофон",
        );
      }
      let display: MediaStream;
      try {
        display = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
      } catch (err) {
        throw new Error(`не удалось получить звук системы (${describeMediaError(err)})`);
      }
      if (display.getAudioTracks().length === 0) {
        display.getTracks().forEach((track) => track.stop());
        throw new Error("звук не выбран — в окне выбора отметьте «Поделиться звуком» и попробуйте снова");
      }
      streamByTrack.set("system", display);
    }
  } catch (err) {
    stopStreams();
    throw err;
  }

  const context = new AudioContext();
  let moduleUrl: string | null = null;
  try {
    moduleUrl = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "text/javascript" }));
    await context.audioWorklet.addModule(moduleUrl);
  } catch (err) {
    stopStreams();
    void context.close().catch(() => {});
    throw new Error(`не удалось инициализировать обработку звука (${describeMediaError(err)})`);
  } finally {
    if (moduleUrl) URL.revokeObjectURL(moduleUrl);
  }

  const socket = new WebSocket(
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`,
  );
  socket.binaryType = "arraybuffer";

  let ready: ReadyMessage;
  try {
    ready = await new Promise<ReadyMessage>((resolve, reject) => {
      let settled = false;
      socket.onerror = () => {
        if (!settled) {
          settled = true;
          reject(new Error("соединение с сервером не установлено"));
        }
      };
      socket.onclose = () => {
        if (!settled) {
          settled = true;
          reject(new Error("сервер закрыл соединение"));
        }
      };
      socket.onopen = () => {
        socket.send(
          JSON.stringify({
            type: "start",
            tracks: options.tracks,
            language: options.language || undefined,
          }),
        );
      };
      socket.onmessage = (event) => {
        if (settled || typeof event.data !== "string") return;
        let message: { type?: string; message?: string; job_id?: string; tracks?: string[] };
        try {
          message = JSON.parse(event.data);
        } catch {
          return;
        }
        if (message.type === "ready" && message.job_id) {
          settled = true;
          resolve(message as ReadyMessage);
        } else if (message.type === "error") {
          settled = true;
          reject(new Error(message.message || "сервер отклонил сессию"));
        }
      };
    });
  } catch (err) {
    stopStreams();
    void context.close().catch(() => {});
    socket.close();
    throw err;
  }

  const nodes: AudioWorkletNode[] = [];
  const sourcesNodes: MediaStreamAudioSourceNode[] = [];
  const mutes: GainNode[] = [];
  let stopped = false;
  let notified = false;

  const cleanup = () => {
    nodes.forEach((node) => {
      node.port.onmessage = null;
      node.disconnect();
    });
    sourcesNodes.forEach((source) => source.disconnect());
    mutes.forEach((gain) => gain.disconnect());
    stopStreams();
    void context.close().catch(() => {});
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;
    if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
      socket.close();
    }
  };

  const notifyClosed = (reason: string) => {
    if (notified || stopped) return;
    notified = true;
    cleanup();
    options.onClosed?.(reason);
  };

  streamByTrack.forEach((stream, track) => {
    const source = context.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(context, "stenograph-capture");
    const mute = context.createGain();
    mute.gain.value = 0; // keep the graph running without playing audio back
    source.connect(node).connect(mute).connect(context.destination);
    node.port.onmessage = (event: MessageEvent) => {
      const { samples, rms } = event.data as { samples: Int16Array; rms: number };
      options.onLevel?.(track, rms);
      if (socket.readyState !== WebSocket.OPEN || socket.bufferedAmount > 1_500_000) return;
      const frame = new Uint8Array(1 + samples.byteLength);
      frame[0] = TRACK_INDEX[track];
      frame.set(new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength), 1);
      socket.send(frame);
    };
    sourcesNodes.push(source);
    nodes.push(node);
    mutes.push(mute);
  });

  socket.onmessage = (event) => {
    if (typeof event.data !== "string") return;
    try {
      const message = JSON.parse(event.data) as { type?: string; message?: string };
      if (message.type === "error") notifyClosed(message.message || "сервер завершил сессию");
    } catch {
      /* ignore malformed frames */
    }
  };
  socket.onerror = () => {
    /* onclose follows */
  };
  socket.onclose = (event) =>
    notifyClosed(event.wasClean ? "запись остановлена" : "соединение с сервером потеряно");

  const stop = async () => {
    if (stopped) return;
    stopped = true;
    try {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "stop" }));
    } catch {
      /* ignore */
    }
    await new Promise<void>((resolve) => {
      const timer = window.setTimeout(() => {
        cleanup();
        resolve();
      }, 10000);
      socket.onclose = () => {
        window.clearTimeout(timer);
        cleanup();
        resolve();
      };
      socket.onmessage = null;
    });
  };

  return { jobId: ready.job_id, tracks: options.tracks, stop };
}
