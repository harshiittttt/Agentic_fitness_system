// A fake of expo-speech-recognition that behaves like the REAL package where it matters:
//  * events are delivered only to listeners attached through the module OBJECT (module.addListener). The package's top-level export
//    `addSpeechRecognitionListener` is the same function UNBOUND, so calling it through the package attaches to nothing (the 0.3.0-0.3.3 bug).
//  * start() / stop() / abort(); permission and availability checks.
// Tests drive it with __emit(...) and inspect __starts.
const React = require('react');

const listeners = { result: new Set(), end: new Set(), error: new Set(), start: new Set() };
const state = { starts: [], stops: 0, aborts: 0, running: false, permission: true, available: true };

const ExpoSpeechRecognitionModule = {
  addListener(name, fn) {
    if (this !== ExpoSpeechRecognitionModule) return { remove() {} };          // unbound call: silently attaches to nothing, like the real thing
    listeners[name].add(fn);
    return { remove: () => listeners[name].delete(fn) };
  },
  async requestPermissionsAsync() { return { granted: state.permission, status: state.permission ? 'granted' : 'denied' }; },
  isRecognitionAvailable() { return state.available; },
  supportsOnDeviceRecognition() { return false; },
  start(opts) { state.starts.push(opts); state.running = true; },
  stop() { state.stops++; },
  abort() { state.aborts++; state.running = false; },
};

module.exports = {
  ExpoSpeechRecognitionModule,
  addSpeechRecognitionListener: ExpoSpeechRecognitionModule.addListener,      // UNBOUND, exactly as the package exports it
  useSpeechRecognitionEvent(name, fn) {
    const ref = React.useRef(fn); ref.current = fn;
    React.useEffect(() => { const h = (e) => ref.current(e); listeners[name].add(h); return () => listeners[name].delete(h); }, [name]);
  },
  __emit(name, payload) { for (const f of Array.from(listeners[name])) f(payload || {}); if (name === 'end' || name === 'error') state.running = false; },
  __state: state,
  __reset() { Object.values(listeners).forEach((s) => s.clear()); state.starts = []; state.stops = 0; state.aborts = 0; state.running = false; state.permission = true; state.available = true; },
  __listenerCount(name) { return listeners[name].size; },
};
