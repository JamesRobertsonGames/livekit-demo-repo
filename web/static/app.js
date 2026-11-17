// Page logic: one button that starts or stops a voice session, and a blob that shows it.
//
// A session is either live (LiveKit, imported only when one starts) or, with ?preview in the
// URL, a scripted stand-in that never touches the network or the microphone.

import { Blob } from "./blob.js";

const AGENT_STATE = "lk.agent.state";
const AGENT_JOIN_TIMEOUT_MS = 20_000;
const LABELS = { start: "Start talking to agent", stop: "Stop talking to agent" };
const STATUS = {
  connecting: "Connecting…",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
};

const button = document.getElementById("toggle");
const statusEl = document.getElementById("status");
const preview = new URLSearchParams(location.search).has("preview");
const idleNote = preview ? "Preview: simulated audio, nothing connects to LiveKit" : "";

let session = null;

/** Loudness of one audio track, 0 to 1, read on demand from an AnalyserNode. */
class Meter {
  constructor(audio, mediaStreamTrack) {
    this.source = audio.createMediaStreamSource(new MediaStream([mediaStreamTrack]));
    this.analyser = audio.createAnalyser();
    this.analyser.fftSize = 1024;
    this.source.connect(this.analyser);
    this.samples = new Float32Array(this.analyser.fftSize);
  }

  level() {
    this.analyser.getFloatTimeDomainData(this.samples);
    let sum = 0;
    for (const s of this.samples) sum += s * s;
    const db = 20 * Math.log10(Math.sqrt(sum / this.samples.length) + 1e-9);
    // About -58 dBFS (room noise) maps to 0 and -18 dBFS (close speech) to 1.
    return Math.min(1, Math.max(0, (db + 58) / 40));
  }

  dispose() {
    this.source.disconnect();
  }
}

class LiveSession {
  constructor(events) {
    this.events = events;
    this.mode = "connecting";
    this.closed = false;
    // Created inside the click handler so the browser lets it run.
    this.audio = new AudioContext();
  }

  async start() {
    const { Room, RoomEvent, Track, createLocalAudioTrack } = await import("livekit-client");
    if (this.closed) return;

    // The microphone comes first, so a blocked mic never opens a room or starts the agent.
    this.mic = await createLocalAudioTrack({
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    });
    if (this.closed) return this.dispose();
    this.userMeter = new Meter(this.audio, this.mic.mediaStreamTrack);

    const res = await fetch("/api/token", { method: "POST" }).catch(() => {
      throw new Error("Couldn't reach the local server. Is `uv run web/server.py` running?");
    });
    if (!res.ok) throw new Error(`The local token server answered ${res.status}.`);
    const { serverUrl, participantToken } = await res.json();
    if (this.closed) return;

    const room = new Room();
    this.room = room;
    room
      .on(RoomEvent.ParticipantConnected, (p) => p.isAgent && this.onAgent(p))
      .on(RoomEvent.ParticipantDisconnected, (p) => {
        if (p === this.agent) this.events.ended("The agent ended the call.");
      })
      .on(RoomEvent.ParticipantAttributesChanged, (_changed, p) => {
        if (p === this.agent) this.readAgentState();
      })
      .on(RoomEvent.TrackSubscribed, (track, _pub, p) => {
        if (p.isAgent && track.kind === Track.Kind.Audio) this.onAgentAudio(track);
      })
      .on(RoomEvent.TrackUnsubscribed, (track) => {
        track.detach().forEach((el) => el.remove());
        if (track === this.agentTrack) {
          this.agentMeter?.dispose();
          this.agentMeter = this.agentTrack = null;
        }
      })
      // Before connect() resolves, a failure also rejects it, which reports the reason.
      .on(RoomEvent.Disconnected, () => this.connected && this.events.ended("Disconnected."));

    await room.connect(serverUrl, participantToken);
    if (this.closed) return this.dispose();
    this.connected = true;
    await room.localParticipant.publishTrack(this.mic);
    await room.startAudio();
    if (this.closed) return;

    for (const p of room.remoteParticipants.values()) if (p.isAgent) this.onAgent(p);
    this.joinTimer = setTimeout(() => {
      if (!this.agent) {
        this.events.failed("The agent didn't join. Is `uv run src/agent.py dev` running?");
      }
    }, AGENT_JOIN_TIMEOUT_MS);
  }

  onAgent(participant) {
    this.agent = participant;
    clearTimeout(this.joinTimer);
    this.readAgentState();
  }

  onAgentAudio(track) {
    this.agentTrack = track;
    document.body.append(track.attach());
    this.agentMeter?.dispose();
    this.agentMeter = new Meter(this.audio, track.mediaStreamTrack);
  }

  readAgentState() {
    const state = this.agent?.attributes[AGENT_STATE];
    const mode =
      state === "speaking" || state === "thinking"
        ? state
        : state === "listening" || state === "idle"
          ? "listening"
          : "connecting";
    if (mode !== this.mode) {
      this.mode = mode;
      this.events.changed();
    }
  }

  userLevel() {
    return this.userMeter?.level() ?? 0;
  }

  agentLevel() {
    return this.agentMeter?.level() ?? 0;
  }

  /** Safe to call more than once, and while start() is still running. */
  dispose() {
    this.closed = true;
    clearTimeout(this.joinTimer);
    this.room?.removeAllListeners();
    this.room?.disconnect();
    this.mic?.stop();
    this.userMeter?.dispose();
    this.agentMeter?.dispose();
    this.agentTrack?.detach().forEach((el) => el.remove());
    if (this.audio.state !== "closed") this.audio.close();
  }
}

/** A scripted conversation with made-up audio levels, for looking at the page offline. */
class PreviewSession {
  static SCRIPT = [
    ["connecting", 1.2],
    ["speaking", 3.6],
    ["listening", 4.2],
    ["thinking", 1.6],
    ["speaking", 5.0],
  ];

  constructor(events) {
    this.events = events;
    this.mode = "connecting";
  }

  start() {
    this.startedAt = performance.now() / 1000;
    this.timer = setInterval(() => this.tick(), 50);
  }

  tick() {
    // Play the script once, then loop from the caller's first line.
    const loopFrom = PreviewSession.SCRIPT[0][1] + PreviewSession.SCRIPT[1][1];
    const loopLength = PreviewSession.SCRIPT.slice(2).reduce((sum, [, s]) => sum + s, 0);
    let t = performance.now() / 1000 - this.startedAt;
    if (t > loopFrom) t = loopFrom + ((t - loopFrom) % loopLength);
    for (const [mode, length] of PreviewSession.SCRIPT) {
      if (t < length) {
        this.into = t / length;
        if (mode !== this.mode) {
          this.mode = mode;
          this.events.changed();
        }
        return;
      }
      t -= length;
    }
  }

  static voice(t) {
    // Syllables at about 4 Hz inside phrases a couple of seconds long.
    const syllables = Math.max(0, Math.sin(t * 26 + Math.sin(t * 3.1) * 2));
    const phrase = 0.55 + 0.45 * Math.sin(t * 1.7);
    return Math.min(1, 0.1 + 0.8 * Math.sqrt(syllables) * phrase);
  }

  userLevel() {
    // The caller talks for most of the turn, then pauses before the agent replies.
    if (this.mode !== "listening" || this.into > 0.8) return 0.03;
    return PreviewSession.voice(performance.now() / 1000);
  }

  agentLevel() {
    return this.mode === "speaking" ? PreviewSession.voice(performance.now() / 1000 + 7) : 0;
  }

  dispose() {
    clearInterval(this.timer);
  }
}

function sample(seconds) {
  if (!session) return { tone: "idle", level: 0.05 + 0.03 * Math.sin(seconds * 0.9) };
  switch (session.mode) {
    case "speaking":
      return { tone: "agent", level: session.agentLevel() };
    case "thinking":
      return { tone: "user", level: 0.14 + 0.08 * Math.sin(seconds * 6) };
    case "listening":
      return { tone: "user", level: session.userLevel() };
    default:
      return { tone: "user", level: 0.06 + 0.04 * Math.sin(seconds * 3) };
  }
}

function render(message = "", isError = false) {
  document.body.dataset.mode = session ? session.mode : "idle";
  button.textContent = session ? LABELS.stop : LABELS.start;
  statusEl.textContent = session ? STATUS[session.mode] : message || idleNote;
  statusEl.classList.toggle("error", !session && isError);
}

function describe(err) {
  switch (err?.name) {
    case "NotAllowedError":
      return "Microphone access was blocked.";
    case "NotFoundError":
      return "No microphone was found.";
    case "ConnectionError":
      return "Couldn't connect to LiveKit.";
    default:
      return err?.message || "Something went wrong.";
  }
}

function stop(message = "", isError = false) {
  const ending = session;
  session = null;
  ending?.dispose();
  render(message, isError);
}

async function start() {
  const events = {
    changed: () => session === current && render(),
    ended: (message) => session === current && stop(message),
    failed: (message) => session === current && stop(message, true),
  };
  const current = preview ? new PreviewSession(events) : new LiveSession(events);
  session = current;
  render();
  try {
    await current.start();
  } catch (err) {
    if (session !== current) return; // stopped while starting; the error is just the cancel
    console.error(err);
    stop(describe(err), true);
  }
}

button.addEventListener("click", () => (session ? stop() : start()));
new Blob(document.getElementById("blob"), sample);
render();
